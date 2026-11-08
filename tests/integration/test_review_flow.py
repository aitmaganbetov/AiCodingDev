"""P4-10: review → fix → re-review как новые задачи плана (origin_task_id), счётчик — у исходной реализации."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration import review_flow
from aicd_platform.db import DbContext, transaction
from tests.support import db as testdb
from tests.support import orchestration as orch

pytestmark = pytest.mark.db


def _edge(conn: psycopg.Connection, tenant: orch.Tenant, run: uuid.UUID, child: uuid.UUID, parent: uuid.UUID) -> None:
    conn.execute(
        "INSERT INTO task_dependencies (workspace_id, run_id, task_id, depends_on_task_id) VALUES (%s, %s, %s, %s)",
        (tenant.workspace_id, run, child, parent),
    )


def _scene(database: testdb.TestDatabase) -> tuple[orch.Tenant, uuid.UUID, dict[str, uuid.UUID]]:
    with orch.connect_admin(database) as conn:
        tenant = orch.seed_tenant(conn)
        run = orch.new_run(conn, tenant)
        orch.drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
        tasks = {key: orch.new_task(conn, tenant, run, key=key) for key in ("main", "review", "docs")}
        conn.execute(
            "UPDATE tasks SET agent_role = 'reviewer', version = version + 1 WHERE id = %s", (tasks["review"],)
        )
        conn.execute(
            "UPDATE tasks SET agent_role = 'documentation', version = version + 1 WHERE id = %s", (tasks["docs"],)
        )
        _edge(conn, tenant, run, tasks["review"], tasks["main"])
        _edge(conn, tenant, run, tasks["docs"], tasks["review"])
    return tenant, run, tasks


async def _open(worker: AsyncEngine, tenant: orch.Tenant, review: uuid.UUID) -> review_flow.FixLoop | None:
    async with transaction(worker, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
        return await review_flow.open_fix_loop(conn, review_task_id=review, fix_request_id=None)


def _set_loops(database: testdb.TestDatabase, task: uuid.UUID, n: int) -> None:
    with orch.connect_admin(database) as conn:
        orch.update(conn, "tasks", task, review_loop_count=n)


async def test_fix_loop_creates_tasks_rewires_dependents_and_is_idempotent(
    database: testdb.TestDatabase, worker_engine: AsyncEngine
) -> None:
    tenant, run, t = _scene(database)
    assert await _open(worker_engine, tenant, t["review"]) is None, "без принятого FIX_REQUEST цикл не открывается"
    _set_loops(database, t["main"], 1)  # брокер принял FIX_REQUEST: счётчик исходной реализации = 1
    loop = await _open(worker_engine, tenant, t["review"])
    assert loop is not None and loop.iteration == 1
    assert await _open(worker_engine, tenant, t["review"]) == loop, "повторная обработка вердикта — те же задачи"
    with orch.connect_admin(database) as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT task_key, agent_role, origin_task_id, status FROM tasks WHERE run_id = %s", (run,)
            ).fetchall()
        }
        deps = set(
            conn.execute(
                "SELECT c.task_key, p.task_key FROM task_dependencies d JOIN tasks c ON c.id = d.task_id"
                " JOIN tasks p ON p.id = d.depends_on_task_id WHERE d.run_id = %s",
                (run,),
            ).fetchall()
        )
    assert rows["main.fix1"] == ("developer", t["main"], "PENDING")
    assert rows["review.r1"] == ("reviewer", t["review"], "PENDING")
    assert {("main.fix1", "review"), ("review.r1", "main.fix1"), ("docs", "review.r1")} <= deps
    # Второй цикл: проверяемая реализация — fix-задача, но счётчик и ключи — от исходной цепочки.
    _set_loops(database, t["main"], 2)
    second = await _open(worker_engine, tenant, loop.rereview_task_id)
    assert second is not None and second.iteration == 2
    with orch.connect_admin(database) as conn:
        keys = {r[0] for r in conn.execute("SELECT task_key FROM tasks WHERE run_id = %s", (run,)).fetchall()}
    assert {"main.fix2", "review.r2"} <= keys


async def test_no_fix_loop_beyond_limit(database: testdb.TestDatabase, worker_engine: AsyncEngine) -> None:
    tenant, _, t = _scene(database)
    _set_loops(database, t["main"], 3)
    assert (await _open(worker_engine, tenant, t["review"])) is not None  # 3-й цикл ещё в пределах лимита 3
    tenant2, _, t2 = _scene(database)
    with orch.connect_admin(database) as conn:
        orch.update(conn, "tasks", t2["main"], review_loop_count=3, max_review_loops=2)
    assert await _open(worker_engine, tenant2, t2["review"]) is None, "сверх лимита fix-задачи не создаются"
