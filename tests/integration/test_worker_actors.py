"""Акторы воркера (P3-03): маршрутизация событий, идемпотентность (StubBroker), reaper аренд, точки входа."""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import dramatiq
import pytest
from dramatiq.brokers.stub import StubBroker
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.recovery import RecoveryReport
from aicd_platform import outbox
from aicd_platform.db import DbContext, create_engine, transaction
from aicd_platform.ids import uuid7
from aicd_worker import handlers, maintenance, runtime
from aicd_worker.broker import EXEC_QUEUE, ORCHESTRATOR_QUEUE, build_broker, register_actors
from aicd_worker.main import main, run_outbox
from aicd_worker.settings import WorkerSettings
from aicd_worker.wiring import ConfigurationError, check_exec_adapter
from tests.conftest import REDIS_URL
from tests.support import db as testdb
from tests.support import orchestration as orch

pytestmark = pytest.mark.db


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def on_run_created(self, run_id: uuid.UUID, ws: uuid.UUID) -> None:
        self.calls.append(("on_run_created", run_id))

    async def on_plan_approval_decided(
        self, run_id: uuid.UUID, ws: uuid.UUID, *, approved: bool, plan_version: int
    ) -> None:
        self.calls.append(("on_plan_approval_decided", run_id, approved, plan_version))

    async def on_run_paused_or_cancelled(self, run_id: uuid.UUID, ws: uuid.UUID, status: str) -> None:
        self.calls.append(("on_run_paused_or_cancelled", run_id, status))

    async def on_run_resumed(self, run_id: uuid.UUID, ws: uuid.UUID) -> None:
        self.calls.append(("on_run_resumed", run_id))

    async def tick(self, run_id: uuid.UUID, ws: uuid.UUID) -> list[uuid.UUID]:
        self.calls.append(("tick", run_id))
        return []

    async def execute_task(self, task_id: uuid.UUID, ws: uuid.UUID) -> None:
        self.calls.append(("execute_task", task_id))


@pytest.fixture(scope="module")
async def worker_engine(database: testdb.TestDatabase) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database.url_for("aicd_worker"), pool_size=8)
    yield engine
    await engine.dispose()


def _runtime(engine: AsyncEngine, fake: FakeOrchestrator, dispatched: list[dict[str, Any]]) -> runtime.Runtime:
    async def dispatch(message: dict[str, Any]) -> None:
        dispatched.append(message)

    return runtime.Runtime(
        engine=engine,
        orchestrator=fake,
        loop=asyncio.get_running_loop(),
        dispatch_execution=dispatch,
        stale_ready_seconds=1,
    )


def _event(
    etype: str,
    run: uuid.UUID,
    ws: uuid.UUID,
    payload: dict[str, Any] | None = None,
    aggregate_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid7()),
        "event_type": etype,
        "run_id": str(run),
        "workspace_id": str(ws),
        "aggregate_id": str(aggregate_id or run),
        "payload": payload or {},
    }


async def test_routing(database: testdb.TestDatabase, worker_engine: AsyncEngine) -> None:
    with orch.connect_admin(database) as conn:
        tenant = orch.seed_tenant(conn)
        run = orch.new_run(conn, tenant)
        orch.drive_run(conn, run, "DISCOVERY", "PLANNED", "AWAITING_APPROVAL")
        approval = uuid7()
        conn.execute(
            "INSERT INTO approvals (id, workspace_id, project_id, run_id, action_type, risk_level, required_role,"
            " requested_by_type, subject_hash, subject) VALUES (%s, %s, %s, %s, 'PLAN_APPROVAL', 'high', 'developer',"
            " 'system', %s, '{\"plan_version\": 1}')",
            (approval, tenant.workspace_id, tenant.project_id, run, "a" * 64),
        )
        orch.update(
            conn, "approvals", approval, status="APPROVED", decided_by=tenant.owner_id, decided_at=datetime.now(UTC)
        )
    ws = tenant.workspace_id
    fake = FakeOrchestrator()
    dispatched: list[dict[str, Any]] = []
    rt = _runtime(worker_engine, fake, dispatched)
    task = uuid7()
    cases = [
        (_event("run.created", run, ws), "on_run_created"),
        (_event("approval.decided", run, ws, {"status": "APPROVED"}, approval), "on_plan_approval_decided"),
        (_event("run.state_changed", run, ws, {"from": "RUNNING", "to": "PAUSED"}), "on_run_paused_or_cancelled"),
        (_event("run.state_changed", run, ws, {"from": "RUNNING", "to": "CANCELLED"}), "on_run_paused_or_cancelled"),
        (_event("run.state_changed", run, ws, {"from": "PAUSED", "to": "RUNNING"}), "on_run_resumed"),
        (
            _event("task.state_changed", run, ws, {"task_id": str(task), "from": "PENDING", "to": "READY"}),
            "execute_task",
        ),
        (_event("task.state_changed", run, ws, {"task_id": str(task), "from": "RUNNING", "to": "SUCCEEDED"}), "tick"),
        (_event("gate.completed", run, ws), "tick"),
    ]
    for event, expected in cases:
        assert await handlers.handle_event(rt, event) == expected
    assert ("on_plan_approval_decided", run, True, 1) in fake.calls
    assert ("on_run_paused_or_cancelled", run, "CANCELLED") in fake.calls
    assert [m["task_id"] for m in dispatched] == [str(task)]
    assert await handlers.handle_event(rt, {"event_id": str(uuid7())}) == "ignored"
    # повтор того же event_id — no-op
    assert await handlers.handle_event(rt, cases[0][0]) == "duplicate"


async def test_handler_failure_rolls_back_claim(worker_engine: AsyncEngine) -> None:
    class Boom(FakeOrchestrator):
        async def tick(self, run_id: uuid.UUID, ws: uuid.UUID) -> list[uuid.UUID]:
            raise RuntimeError("transient")

    rt = _runtime(worker_engine, Boom(), [])
    event = _event("gate.completed", uuid7(), uuid7())
    with pytest.raises(RuntimeError):
        await handlers.handle_event(rt, event)
    ok = FakeOrchestrator()
    rt.orchestrator = ok
    assert await handlers.handle_event(rt, event) == "tick"  # повтор Dramatiq обработает событие


async def test_stub_broker_duplicate_delivery_processed_once(worker_engine: AsyncEngine) -> None:
    broker = build_broker(None, stub=True)
    assert isinstance(broker, StubBroker)
    actors = register_actors(broker)
    fake = FakeOrchestrator()
    rt = runtime.Runtime(
        engine=worker_engine,
        orchestrator=fake,
        loop=asyncio.get_running_loop(),
        dispatch_execution=actors.dispatch_execution,
    )
    runtime.install(rt)
    run, ws, task = uuid7(), uuid7(), uuid7()
    created = _event("run.created", run, ws)
    ready = _event("task.state_changed", run, ws, {"task_id": str(task), "to": "READY"})
    for event in (created, created, ready, ready):  # at-least-once: дубли доставки
        await actors.publish(event)
    await actors.publish({**_event("cli.output", run, ws)})  # не для оркестратора — не публикуется
    worker = dramatiq.Worker(broker, worker_threads=2, worker_timeout=50)
    worker.start()
    try:
        await asyncio.to_thread(functools.partial(broker.join, ORCHESTRATOR_QUEUE, fail_fast=True))
        await asyncio.to_thread(functools.partial(broker.join, EXEC_QUEUE, fail_fast=True))
        exec_message = handlers.execution_message(task, ws)
        await actors.dispatch_execution(exec_message)
        await actors.dispatch_execution(exec_message)
        await asyncio.to_thread(functools.partial(broker.join, EXEC_QUEUE, fail_fast=True))
    finally:
        await asyncio.to_thread(worker.stop)
        runtime.install(None)
    assert fake.calls.count(("on_run_created", run)) == 1
    # READY отправлено в exec один раз (дубль события отсечён); exec_message — один раз из двух доставок.
    assert fake.calls.count(("execute_task", task)) == 2
    assert all(c[0] != "tick" for c in fake.calls)


async def test_sweep_ticks_due_retries_redispatches_stale_ready_and_schedules_recovery(
    database: testdb.TestDatabase, worker_engine: AsyncEngine
) -> None:
    with orch.connect_admin(database) as conn:
        tenant = orch.seed_tenant(conn)
        run = orch.new_run(conn, tenant)
        orch.drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
        ready = orch.new_task(conn, tenant, run, "ready")
        orch.update(conn, "tasks", ready, status="READY")
        waiting = orch.new_task(conn, tenant, run, "waiting")
        orch.lease_task(conn, waiting, token=1)
        orch.update(conn, "tasks", waiting, status="RETRY_WAIT", next_attempt_at=datetime.now(UTC))
    reaped_run = uuid7()

    class FakeReconciler:
        runners = ()
        calls = 0

        async def reconcile(self) -> RecoveryReport:
            self.calls += 1
            return RecoveryReport(reaped_tasks=[uuid7(), uuid7()], runs_to_tick={(reaped_run, tenant.workspace_id)})

    fake = FakeOrchestrator()
    dispatched: list[dict[str, Any]] = []
    rt = _runtime(worker_engine, fake, dispatched)
    rt.stale_ready_seconds = 0
    reconciler = FakeReconciler()
    rt.reconciler = reconciler  # type: ignore[assignment]
    scheduler = maintenance.Scheduler(reap_interval=3600, reconcile_interval=3600)
    first = await maintenance.sweep(rt, scheduler)
    assert first.reaped == 2 and first.reconciled
    assert ("tick", run) in fake.calls and ("tick", reaped_run) in fake.calls  # затронутые recovery run продвигаются
    assert str(ready) in {m["task_id"] for m in dispatched}
    second = await maintenance.sweep(rt, scheduler)  # интервалы не истекли
    assert second.reaped == 0 and not second.reconciled
    assert reconciler.calls == 1


async def test_outbox_loop_publishes_and_stops(
    database: testdb.TestDatabase, app_engine: AsyncEngine, worker_engine: AsyncEngine
) -> None:
    from redis.asyncio import Redis

    with orch.connect_admin(database) as conn:
        tenant = orch.seed_tenant(conn)
        run = orch.new_run(conn, tenant)
    async with transaction(app_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
        await outbox.emit(
            conn,
            event_type="run.created",
            aggregate_type="run",
            aggregate_id=run,
            workspace_id=tenant.workspace_id,
            run_id=run,
            payload={"mode": "DEVELOP"},
        )
    broker = build_broker(None, stub=True)
    assert isinstance(broker, StubBroker)
    actors = register_actors(broker)
    settings = WorkerSettings(environment="test", redis_url=REDIS_URL, outbox_poll_seconds=0.05)
    stop = asyncio.Event()
    redis = Redis.from_url(REDIS_URL)
    loop_task = asyncio.create_task(run_outbox(settings, worker_engine, stop, actors=actors, redis=redis))
    try:
        for _ in range(100):
            queue = broker.queues.get(ORCHESTRATOR_QUEUE)
            if queue is not None and any(str(run) in m.decode() for m in list(queue.queue)):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("событие не опубликовано")
    finally:
        stop.set()
        await asyncio.wait_for(loop_task, 5)
        await redis.aclose()


def test_exec_worker_refuses_fake_adapter_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError):
        check_exec_adapter(WorkerSettings(environment="production"))
    check_exec_adapter(WorkerSettings(environment="development"))
    monkeypatch.setenv("AICD_ENVIRONMENT", "production")
    assert main(["exec"]) == 2
