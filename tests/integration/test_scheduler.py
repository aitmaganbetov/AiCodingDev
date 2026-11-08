"""P3-06: планировщик — готовность по зависимостям, приоритет критического пути, лимит параллелизма."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from aicd_orchestration import engine as orch_engine
from aicd_orchestration import plans, store
from aicd_orchestration.planner import TaskSpec
from aicd_platform.db import DbContext, transaction
from tests.support.phase3 import Slice, create_run, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


def _spec(key: str, *deps: str) -> TaskSpec:
    return TaskSpec(key, "developer", f"задача {key}", "сделать", 2, "low", depends_on=deps)


# «solo» — одиночная задача; «chain» — начало цепочки chain→mid→tail (длиннее критический путь).
PLAN = [_spec("solo"), _spec("chain"), _spec("mid", "chain"), _spec("tail", "mid")]


async def _running_run(s: Slice) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    run_id, ws = uuid.UUID(await create_run(s)), uuid.UUID(s.ws)
    async with transaction(s.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        await store.transition_run(conn, run_id, "DISCOVERY", reason="test")
        version, ids = await plans.install_plan(conn, run_id, PLAN)
        await store.transition_run(conn, run_id, "PLANNED", reason="test")
        await plans.mark_plan_approved(conn, run_id, version)
        await store.transition_run(conn, run_id, "RUNNING", reason="test")
    return run_id, ws, ids


async def _run(s: Slice, ws: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    async with transaction(s.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        row = await orch_engine._run_row(conn, run_id)
    assert row is not None
    return row


async def test_ready_only_after_dependencies_and_ordered_by_critical_path(world: Slice) -> None:
    run_id, ws, ids = await _running_run(world)
    orchestrator = world.orchestrator()
    ready = await orchestrator.tick(run_id, ws)
    assert ready == [ids["chain"], ids["solo"]], "без зависимостей — оба; первым — начало длинной цепочки"
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        statuses: dict[str, str] = dict(
            (await conn.execute(text("SELECT task_key, status FROM tasks WHERE run_id = :r"), {"r": run_id})).all()
        )
    assert statuses == {"solo": "READY", "chain": "READY", "mid": "PENDING", "tail": "PENDING"}


async def test_parallelism_limit_caps_dispatch_and_execution(world: Slice) -> None:
    run_id, ws, ids = await _running_run(world)
    orchestrator = world.orchestrator()
    await orchestrator.tick(run_id, ws)
    run = await _run(world, ws, run_id)
    limited = {**run, "policy_snapshot": {**run["policy_snapshot"], "limits": {"max_parallel_tasks": 1}}}
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        assert await orch_engine._dispatchable(conn, limited) == [ids["chain"]]
        await store.transition_task(conn, ids["chain"], "LEASED", reason="test", extra={"lease_fencing_token": 1})
        assert await orch_engine._dispatchable(conn, limited) == [], "слот занят — solo ждёт в READY"
        assert await orch_engine._dispatchable(conn, run) == [ids["solo"]], "лимит по умолчанию из snapshot (4)"
