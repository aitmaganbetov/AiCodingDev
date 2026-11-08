"""Outbox-диспетчер (P3-03): at-least-once, проекция в run_event_log без дублей, монотонный seq, backoff."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform import outbox
from aicd_platform.db import DbContext, transaction
from aicd_platform.outbox_dispatcher import backoff_seconds, dispatch_batch
from aicd_platform.sse import envelope
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.events import validate_sse

pytestmark = pytest.mark.db


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self.notified: list[uuid.UUID] = []
        self.fail = fail

    async def publish(self, event: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("redis down: password=hunter2")
        self.published.append(event)

    async def notify(self, run_id: uuid.UUID) -> None:
        self.notified.append(run_id)

    def for_run(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        return [e for e in self.published if e["run_id"] == str(run_id)]


def _seed(database: testdb.TestDatabase) -> tuple[orch.Tenant, uuid.UUID]:
    with orch.connect_admin(database) as conn:
        tenant = orch.seed_tenant(conn)
        run = orch.new_run(conn, tenant)
    return tenant, run


async def _emit(
    app_engine: AsyncEngine, tenant: orch.Tenant, run: uuid.UUID, versions: range, event_type: str = "run.state_changed"
) -> list[uuid.UUID]:
    ids = []
    async with transaction(app_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
        for v in versions:
            payload = {"from": "CREATED", "to": "DISCOVERY", "version": v, "reason": f"step {v}"}
            ids.append(
                await outbox.emit(
                    conn,
                    event_type=event_type,
                    aggregate_type="run",
                    aggregate_id=run,
                    workspace_id=tenant.workspace_id,
                    run_id=run,
                    payload=payload,
                )
            )
    return ids


async def _drain(engine: AsyncEngine, rec: Recorder, batch: int = 100) -> None:
    for _ in range(100):
        if await dispatch_batch(engine, rec.publish, rec.notify, batch) == 0:
            return


async def _log(app_engine: AsyncEngine, tenant: orch.Tenant, run: uuid.UUID) -> list[dict[str, Any]]:
    async with transaction(app_engine, DbContext(workspace_id=tenant.workspace_id)) as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT run_id, seq, event_id, event_type, schema_version, payload, occurred_at FROM run_event_log"
                        " WHERE run_id = :r ORDER BY seq"
                    ),
                    {"r": run},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def test_backoff_is_exponential_and_capped() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [1, 2, 4, 8]
    assert backoff_seconds(30) == 300


async def test_dispatch_projects_publishes_and_notifies(
    database: testdb.TestDatabase, app_engine: AsyncEngine, worker_engine: AsyncEngine
) -> None:
    tenant, run = _seed(database)
    ids = await _emit(app_engine, tenant, run, range(1, 4))
    created = await _emit(app_engine, tenant, run, range(1, 2), event_type="run.created")  # не SSE-тип
    rec = Recorder()
    await _drain(worker_engine, rec)
    assert [e["event_id"] for e in rec.for_run(run)] == [str(i) for i in ids + created]
    assert rec.for_run(run)[0]["payload"]["version"] == 1
    assert run in rec.notified
    log = await _log(app_engine, tenant, run)
    assert [r["seq"] for r in log] == [1, 2, 3]
    assert [r["event_id"] for r in log] == ids
    for row in log:
        env = envelope(row)
        validate_sse(env)
        assert env["payload"]["to_status"] == "DISCOVERY"
    with orch.connect_admin(database) as conn:
        last = conn.execute("SELECT last_event_seq FROM runs WHERE id = %s", (run,)).fetchone()
        pending = conn.execute(
            "SELECT count(*) FROM outbox_events WHERE run_id = %s AND published_at IS NULL", (run,)
        ).fetchone()
    assert last is not None and last[0] == 3
    assert pending is not None and pending[0] == 0


async def test_publish_failure_backoff_then_redelivery_without_duplicates(
    database: testdb.TestDatabase, app_engine: AsyncEngine, worker_engine: AsyncEngine
) -> None:
    tenant, run = _seed(database)
    (event_id,) = await _emit(app_engine, tenant, run, range(1, 2))
    failing = Recorder(fail=True)
    await dispatch_batch(worker_engine, failing.publish, failing.notify)
    with orch.connect_admin(database) as conn:
        row = conn.execute(
            "SELECT publish_attempts, last_error, available_at > now(), published_at FROM outbox_events WHERE id = %s",
            (event_id,),
        ).fetchone()
    assert row is not None
    attempts, last_error, delayed, published_at = row
    assert attempts == 1 and delayed and published_at is None
    assert "ConnectionError" in last_error and "hunter2" not in last_error  # ошибка редактируется
    assert len(await _log(app_engine, tenant, run)) == 1  # проекция уже зафиксирована

    # backoff ещё не истёк — событие не берётся
    ok = Recorder()
    await _drain(worker_engine, ok)
    assert ok.for_run(run) == []
    with orch.connect_admin(database) as conn:
        conn.execute("UPDATE outbox_events SET available_at = now() WHERE id = %s", (event_id,))
    await _drain(worker_engine, ok)
    assert [e["event_id"] for e in ok.for_run(run)] == [str(event_id)]

    # «падение» после publish до commit: событие снова неопубликовано ⇒ повторная публикация (at-least-once),
    # но run_event_log не дублируется и seq не растёт.
    with orch.connect_admin(database) as conn:
        conn.execute("UPDATE outbox_events SET published_at = NULL WHERE id = %s", (event_id,))
    again = Recorder()
    await _drain(worker_engine, again)
    assert [e["event_id"] for e in again.for_run(run)] == [str(event_id)]
    log = await _log(app_engine, tenant, run)
    assert [(r["seq"], r["event_id"]) for r in log] == [(1, event_id)]
    with orch.connect_admin(database) as conn:
        last = conn.execute("SELECT last_event_seq FROM runs WHERE id = %s", (run,)).fetchone()
    assert last is not None and last[0] == 1


async def test_concurrent_dispatchers_keep_per_run_seq_monotonic(
    database: testdb.TestDatabase, app_engine: AsyncEngine, worker_engine: AsyncEngine
) -> None:
    runs = [_seed(database) for _ in range(3)]
    expected: dict[uuid.UUID, list[uuid.UUID]] = {run: [] for _, run in runs}
    for v in range(1, 21):  # чередование событий разных run
        for tenant, run in runs:
            expected[run] += await _emit(app_engine, tenant, run, range(v, v + 1))
    recorders = [Recorder() for _ in range(4)]

    async def worker(rec: Recorder) -> None:
        for _ in range(200):
            if await dispatch_batch(worker_engine, rec.publish, rec.notify, batch=7) == 0:
                await asyncio.sleep(0.01)
                if await dispatch_batch(worker_engine, rec.publish, rec.notify, batch=7) == 0:
                    return

    await asyncio.gather(*(worker(r) for r in recorders))
    published = [e["event_id"] for r in recorders for e in r.published]
    for tenant, run in runs:
        log = await _log(app_engine, tenant, run)
        assert [r["seq"] for r in log] == list(range(1, 21))
        assert [r["event_id"] for r in log] == expected[run]  # порядок seq = порядок outbox в run
        assert [envelope(r)["payload"]["run_version"] for r in log] == list(range(1, 21))
        assert {str(i) for i in expected[run]} <= set(published)


async def test_task_and_terminal_events_match_sse_schema(
    database: testdb.TestDatabase, app_engine: AsyncEngine, worker_engine: AsyncEngine
) -> None:
    tenant, run = _seed(database)
    with orch.connect_admin(database) as conn:
        task = orch.new_task(conn, tenant, run)
    async with transaction(app_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
        await outbox.emit(
            conn,
            event_type="task.state_changed",
            aggregate_type="task",
            aggregate_id=task,
            workspace_id=tenant.workspace_id,
            run_id=run,
            payload={"task_id": str(task), "from": "PENDING", "to": "READY", "reason": "deps", "blocked_reason": None},
        )
        await outbox.emit(
            conn,
            event_type="run.completed",
            aggregate_type="run",
            aggregate_id=run,
            workspace_id=tenant.workspace_id,
            run_id=run,
            payload={"final_status": "CANCELLED"},
        )
        await outbox.emit(
            conn,
            event_type="cli.output",
            aggregate_type="cli_execution",
            aggregate_id=uuid.uuid4(),
            workspace_id=tenant.workspace_id,
            run_id=run,
            payload={
                "execution_id": str(uuid.uuid4()),
                "task_id": str(task),
                "provider": "codex",
                "agent_role": "developer",
                "cli_seq": 1,
                "cli_event_type": "output",
                "stream": "stdout",
                "text": "я" * 5000,
                "internal": "dropped",
            },
        )
    await _drain(worker_engine, Recorder())
    log = await _log(app_engine, tenant, run)
    assert [r["event_type"] for r in log] == ["task.state_changed", "run.completed", "cli.output"]
    for row in log:
        validate_sse(envelope(row))
    task_payload = envelope(log[0])["payload"]
    assert task_payload["agent_role"] == "developer" and task_payload["complexity"] == "L2"
    cli = envelope(log[2])["payload"]
    assert "internal" not in cli and cli["truncated"] is True
    assert len(cli["text"].encode()) <= 8192
