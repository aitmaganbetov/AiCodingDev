"""P3-11: ledger внешних действий (ADR-0012) — exactly-once, crash recovery через probe, approval gate (AC-09, AC-13)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration import external_actions as ledger
from aicd_orchestration.approvals import Approvals
from aicd_orchestration.external_actions import Outcome
from aicd_platform.db import DbContext, create_engine, transaction
from tests.support import db as testdb
from tests.support.orchestration import Tenant, connect_admin, drive_run, new_run, new_task, seed_tenant

pytestmark = pytest.mark.db


@dataclass(frozen=True)
class World:
    tenant: Tenant
    run_id: uuid.UUID
    task_id: uuid.UUID


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
async def worker_engine(database: testdb.TestDatabase) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database.url_for("aicd_worker"))
    yield engine
    await engine.dispose()


@pytest.fixture
def world(admin: psycopg.Connection) -> World:
    tenant = seed_tenant(admin)
    run = new_run(admin, tenant)
    drive_run(admin, run, "DISCOVERY", "PLANNED", "RUNNING")
    return World(tenant, run, new_task(admin, tenant, run))


class Calls:
    def __init__(self, ref: str = "pr#1") -> None:
        self.do = 0
        self.probe = 0
        self.ref = ref
        self.applied: str | None = None

    async def do_call(self) -> Outcome:
        self.do += 1
        self.applied = self.ref
        return Outcome(self.ref, {"number": 1})

    async def probe_call(self) -> Outcome | None:
        self.probe += 1
        return Outcome(self.applied, {"state": "open"}) if self.applied else None


def _ctx(w: World) -> DbContext:
    return DbContext(workspace_id=w.tenant.workspace_id, actor_type="system")


async def _plan(engine: AsyncEngine, w: World, action_type: str = "pr_create", target: str = "acme/app#feature",
                approval_id: uuid.UUID | None = None, request: dict[str, Any] | None = None) -> ledger.ExternalAction:  # fmt: skip
    async with transaction(engine, _ctx(w)) as conn:
        return await ledger.plan(
            conn, workspace_id=w.tenant.workspace_id, run_id=w.run_id, task_id=w.task_id, action_type=action_type,
            target=target, plan_version=1, request=request or {"head": "feature", "base": "main"},
            approval_id=approval_id,
        )  # fmt: skip


async def test_plan_is_idempotent_by_deterministic_key(worker_engine: AsyncEngine, world: World) -> None:
    first = await _plan(worker_engine, world)
    again = await _plan(worker_engine, world)
    assert first.id == again.id and first.status == "PLANNED"
    assert first.idempotency_key == ledger.idempotency_key(
        world.run_id, world.task_id, "pr_create", "acme/app#feature", 1
    )


async def test_execute_once_never_repeats_confirmed(worker_engine: AsyncEngine, world: World) -> None:
    action = await _plan(worker_engine, world)
    calls = Calls()
    done = await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 10,
                                     do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    assert done.status == "CONFIRMED" and done.external_ref == "pr#1" and calls.do == 1 and calls.probe == 0
    # «Крах после CONFIRMED»: recovery/повтор доставки с новым токеном не вызывает внешнюю систему.
    again = await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 11,
                                      do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    assert again.status == "CONFIRMED" and calls.do == 1 and calls.probe == 0
    async with transaction(worker_engine, _ctx(world)) as conn:
        with pytest.raises(ledger.AlreadyConfirmedError):
            await ledger.begin(conn, action.id, 12)


async def test_crash_during_executing_is_reconciled_by_probe(worker_engine: AsyncEngine, world: World) -> None:
    action = await _plan(worker_engine, world)
    calls = Calls("pr#7")
    async with transaction(worker_engine, _ctx(world)) as conn:
        await ledger.begin(conn, action.id, 20)
    await calls.do_call()  # внешний вызов выполнен, воркер упал до confirm
    recovered = await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 21,
                                          do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    assert recovered.status == "CONFIRMED" and recovered.external_ref == "pr#7"
    assert calls.do == 1 and calls.probe == 1, "действие не повторено — подтверждено запросом состояния"
    assert recovered.result is not None and recovered.result["reconciled"] is True
    assert recovered.fencing_token == 21


async def test_crash_before_external_call_executes_after_probe(worker_engine: AsyncEngine, world: World) -> None:
    action = await _plan(worker_engine, world)
    calls = Calls()
    async with transaction(worker_engine, _ctx(world)) as conn:
        await ledger.begin(conn, action.id, 30)
    recovered = await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 31,
                                          do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    assert recovered.status == "CONFIRMED" and calls.probe == 1 and calls.do == 1


async def test_stale_token_cannot_confirm_or_take_over(worker_engine: AsyncEngine, world: World) -> None:
    action = await _plan(worker_engine, world)
    async with transaction(worker_engine, _ctx(world)) as conn:
        await ledger.begin(conn, action.id, 40)
    async with transaction(worker_engine, _ctx(world)) as conn:
        await ledger.begin(conn, action.id, 41)  # перехват после истечения аренды
    async with transaction(worker_engine, _ctx(world)) as conn:
        with pytest.raises(ledger.StaleFencingTokenError):
            await ledger.confirm(conn, action.id, fencing_token=40, external_ref="zombie")
    async with transaction(worker_engine, _ctx(world)) as conn:
        with pytest.raises(ledger.StaleFencingTokenError):
            await ledger.begin(conn, action.id, 39)


async def test_failure_is_recorded_and_retry_probes_first(worker_engine: AsyncEngine, world: World) -> None:
    action = await _plan(worker_engine, world)

    async def boom() -> Outcome:
        raise RuntimeError("network down token=abcdefgh123")

    calls = Calls()
    with pytest.raises(RuntimeError):
        await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 50, do=boom,
                                  probe=calls.probe_call)  # fmt: skip
    async with transaction(worker_engine, _ctx(world)) as conn:
        failed = await ledger.get(conn, action.id)
    assert failed is not None and failed.status == "FAILED"
    done = await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 51,
                                     do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    assert done.status == "CONFIRMED" and calls.probe == 1 and calls.do == 1 and done.attempt_count == 2


async def test_ac13_merge_requires_approved_matching_approval(
    worker_engine: AsyncEngine, world: World, admin: psycopg.Connection
) -> None:
    request = {"pr": 12, "head_sha": "a" * 40}
    with pytest.raises(IntegrityError):  # ck_external_actions_approval_required
        await _plan(worker_engine, world, "pr_merge", "acme/app#12", request=request)
    approvals = Approvals()
    async with transaction(worker_engine, _ctx(world)) as conn:
        approval_id = await approvals.request(
            conn, workspace_id=world.tenant.workspace_id, project_id=world.tenant.project_id, run_id=world.run_id,
            task_id=None, action_type="MERGE", risk_level="high", subject=request, requested_by_type="system",
        )  # fmt: skip
    action = await _plan(worker_engine, world, "pr_merge", "acme/app#12", approval_id=approval_id, request=request)
    calls = Calls()
    with pytest.raises(ledger.ApprovalRequiredError):  # AC-13: PENDING approval
        await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 60,
                                  do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    assert calls.do == 0
    async with transaction(worker_engine, _ctx(world)) as conn:
        await approvals.decide(conn, approval_id, actor_id=world.tenant.owner_id, actor_role="owner",
                               decision="APPROVED", reason=None, expected_version=0)  # fmt: skip
    # Approval на другой предмет не открывает действие (subject_hash ≠ request_hash).
    other = await _plan(worker_engine, world, "pr_merge", "acme/app#13", approval_id=approval_id,
                        request={"pr": 13, "head_sha": "b" * 40})  # fmt: skip
    with pytest.raises(ledger.ApprovalRequiredError):
        await ledger.execute_once(worker_engine, world.tenant.workspace_id, other.id, 61,
                                  do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    done = await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 62,
                                     do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    assert done.status == "CONFIRMED" and calls.do == 1


async def test_ac13_expired_approval_blocks_execution(
    worker_engine: AsyncEngine, world: World, admin: psycopg.Connection
) -> None:
    request = {"pr": 99}
    approvals = Approvals()
    async with transaction(worker_engine, _ctx(world)) as conn:
        approval_id = await approvals.request(
            conn, workspace_id=world.tenant.workspace_id, project_id=world.tenant.project_id, run_id=world.run_id,
            task_id=None, action_type="MERGE", risk_level="high", subject=request, requested_by_type="system",
        )  # fmt: skip
        await approvals.decide(conn, approval_id, actor_id=world.tenant.owner_id, actor_role="owner",
                               decision="APPROVED", reason=None, expected_version=None)  # fmt: skip
    admin.execute(
        "UPDATE approvals SET requested_at = now() - interval '3 days', expires_at = now() - interval '1 hour',"
        " version = version + 1 WHERE id = %s",
        (approval_id,),
    )
    action = await _plan(worker_engine, world, "pr_merge", "acme/app#99", approval_id=approval_id, request=request)
    calls = Calls()
    with pytest.raises(ledger.ApprovalRequiredError):
        await ledger.execute_once(worker_engine, world.tenant.workspace_id, action.id, 70,
                                  do=calls.do_call, probe=calls.probe_call)  # fmt: skip
    assert calls.do == 0
