"""AC-13 (P9-01): production deploy / merge / деструктивная миграция без `APPROVED` невозможны.

Для каждого критического типа внешнего действия (ledger, ADR-0012):
* план без approval отклоняется БД (`ck_external_actions_approval_required`);
* с approval в `PENDING`, `REJECTED` или на другой предмет — `execute_once` не вызывает внешнюю систему, строка
  остаётся `PLANNED` (не переходит в `EXECUTING`); прямой `UPDATE ... SET status='EXECUTING'` в обход кода
  отклоняется триггером `trg_external_actions_approval_gate`;
* requester не может утвердить сам (four-eyes; исключение OQ-10 — single-member workspace по явной политике,
  никогда для PRODUCTION_DEPLOY — tests/integration/test_approvals_api.py::test_four_eyes_and_single_member_self_approval);
* после `APPROVED` другим участником с нужной ролью действие исполняется ровно один раз.
Слияние PR через GitHub-обвязку — tests/integration/test_github_workflow.py::test_merge_requires_approval_four_eyes_and_branch_protection.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration import external_actions as ledger
from aicd_orchestration.approvals import Approvals
from aicd_platform.db import transaction
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7
from tests.integration.test_external_actions import Calls, World, _ctx, admin, world

__all__ = ["admin", "world"]  # фикстуры pytest

pytestmark = pytest.mark.db

# action_type ledger → (approval_action_type, минимальная роль решающего, деструктивность)
CRITICAL = {
    "pr_merge": ("MERGE", "admin", False),
    "production_deploy": ("PRODUCTION_DEPLOY", "owner", False),
    "migration_apply": ("DESTRUCTIVE_MIGRATION", "owner", True),
}


def _member(admin: psycopg.Connection, w: World, role: str) -> uuid.UUID:
    user = uuid7()
    admin.execute(
        "INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'AC-13')", (user, f"ac13-{user.hex}@example.test")
    )
    admin.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, %s)",
        (w.tenant.workspace_id, user, role),
    )
    return user


async def _plan(
    engine: AsyncEngine, w: World, action: str, target: str, request: dict[str, Any], approval_id: uuid.UUID | None
) -> ledger.ExternalAction:
    async with transaction(engine, _ctx(w)) as conn:
        return await ledger.plan(
            conn,
            workspace_id=w.tenant.workspace_id,
            run_id=w.run_id,
            task_id=w.task_id,
            action_type=action,
            target=target,
            plan_version=1,
            request=request,
            is_destructive=CRITICAL[action][2],
            approval_id=approval_id,
        )


async def _status(engine: AsyncEngine, w: World, action_id: uuid.UUID) -> str:
    async with transaction(engine, _ctx(w)) as conn:
        current = await ledger.get(conn, action_id)
    assert current is not None
    return current.status


@pytest.mark.parametrize("action", sorted(CRITICAL))
async def test_ac13_critical_action_never_executes_without_approved_four_eyes_approval(
    worker_engine: AsyncEngine, world: World, admin: psycopg.Connection, action: str
) -> None:
    approval_type, decider_role, _ = CRITICAL[action]
    request = {"target": action, "ref": "a" * 40}
    with pytest.raises(IntegrityError):  # без approval действие даже не планируется
        await _plan(worker_engine, world, action, f"{action}#no-approval", request, None)

    approvals = Approvals()
    requester = _member(admin, world, "admin")
    async with transaction(worker_engine, _ctx(world)) as conn:
        approval_id = await approvals.request(
            conn,
            workspace_id=world.tenant.workspace_id,
            project_id=world.tenant.project_id,
            run_id=world.run_id,
            task_id=None,
            action_type=approval_type,
            risk_level="critical",
            subject=request,
            requested_by_type="user",
            requested_by=requester,
        )
    planned = await _plan(worker_engine, world, action, f"{action}#1", request, approval_id)
    calls = Calls()

    # PENDING: не исполняется, остаётся PLANNED.
    with pytest.raises(ledger.ApprovalRequiredError):
        await ledger.execute_once(
            worker_engine, world.tenant.workspace_id, planned.id, 1, do=calls.do_call, probe=calls.probe_call
        )
    assert calls.do == 0 and await _status(worker_engine, world, planned.id) == "PLANNED"
    # Обход кода прямым UPDATE запрещён триггером БД.
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(
            "UPDATE external_actions SET status = 'EXECUTING', executing_at = now(), fencing_token = 1 WHERE id = %s",
            (planned.id,),
        )

    # Requester не утверждает сам (four-eyes).
    async with transaction(worker_engine, _ctx(world)) as conn:
        with pytest.raises(ProblemError) as self_approval:
            await approvals.decide(
                conn,
                approval_id,
                actor_id=requester,
                actor_role="admin" if decider_role == "admin" else "owner",
                decision="APPROVED",
                reason="сам себе",
                expected_version=None,
            )
    assert self_approval.value.code in ("four_eyes_violation", "forbidden")
    assert await _status(worker_engine, world, planned.id) == "PLANNED"

    # Утверждение другим участником с нужной ролью — исполняется ровно один раз.
    decider = world.tenant.owner_id
    async with transaction(worker_engine, _ctx(world)) as conn:
        await approvals.decide(
            conn,
            approval_id,
            actor_id=decider,
            actor_role="owner",
            decision="APPROVED",
            reason="проверено",
            expected_version=None,
        )
    # Approval на другой предмет не открывает иное действие того же типа.
    other = await _plan(worker_engine, world, action, f"{action}#2", {"target": action, "ref": "b" * 40}, approval_id)
    with pytest.raises(ledger.ApprovalRequiredError):
        await ledger.execute_once(
            worker_engine, world.tenant.workspace_id, other.id, 2, do=calls.do_call, probe=calls.probe_call
        )
    assert calls.do == 0 and await _status(worker_engine, world, other.id) == "PLANNED"

    done = await ledger.execute_once(
        worker_engine, world.tenant.workspace_id, planned.id, 3, do=calls.do_call, probe=calls.probe_call
    )
    assert done.status == "CONFIRMED" and calls.do == 1
    again = await ledger.execute_once(
        worker_engine, world.tenant.workspace_id, planned.id, 4, do=calls.do_call, probe=calls.probe_call
    )
    assert again.status == "CONFIRMED" and calls.do == 1


async def test_ac13_rejected_approval_blocks_execution(
    worker_engine: AsyncEngine, world: World, admin: psycopg.Connection
) -> None:
    request = {"target": "deploy", "ref": "d" * 40}
    approvals = Approvals()
    async with transaction(worker_engine, _ctx(world)) as conn:
        approval_id = await approvals.request(
            conn,
            workspace_id=world.tenant.workspace_id,
            project_id=world.tenant.project_id,
            run_id=world.run_id,
            task_id=None,
            action_type="PRODUCTION_DEPLOY",
            risk_level="critical",
            subject=request,
            requested_by_type="system",
        )
        await approvals.decide(
            conn,
            approval_id,
            actor_id=world.tenant.owner_id,
            actor_role="owner",
            decision="REJECTED",
            reason="не сейчас",
            expected_version=None,
        )
    planned = await _plan(worker_engine, world, "production_deploy", "prod#rejected", request, approval_id)
    calls = Calls()
    with pytest.raises(ledger.ApprovalRequiredError):
        await ledger.execute_once(
            worker_engine, world.tenant.workspace_id, planned.id, 1, do=calls.do_call, probe=calls.probe_call
        )
    assert calls.do == 0 and await _status(worker_engine, world, planned.id) == "PLANNED"
