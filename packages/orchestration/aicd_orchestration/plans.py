"""Установка и утверждение плана (DATA_FLOW §4.1, DATABASE_DESIGN §18.2, P3-05).

Под блокировкой строки run: новая plan_version, вставка задач/рёбер, проверка DAG в приложении
(понятная ошибка) + отложенный constraint trigger БД (постоянная гарантия ацикличности).
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_orchestration import store
from aicd_orchestration.dag import PlannedTask, critical_path_lengths, validate_plan
from aicd_orchestration.planner import TaskSpec
from aicd_platform.ids import uuid7


async def install_plan(conn: AsyncConnection, run_id: uuid.UUID, tasks: list[TaskSpec]) -> tuple[int, dict[str, uuid.UUID]]:
    run = await store.load_run(conn, run_id, lock=True)
    if run is None:
        raise LookupError(str(run_id))
    validate_plan([PlannedTask(t.key, t.role, t.complexity, t.depends_on) for t in tasks])
    plan_version = run.plan_version + 1
    ids: dict[str, uuid.UUID] = {}
    for t in tasks:
        ids[t.key] = uuid7()
        await conn.execute(
            text(
                "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title, instructions,"
                " complexity, risk_level, acceptance_criteria)"
                " VALUES (:id, :ws, :run, :pv, :key, :role, :title, :instr, :cx, :risk, CAST(:ac AS jsonb))"
            ),
            {"id": ids[t.key], "ws": run.workspace_id, "run": run_id, "pv": plan_version, "key": t.key,
             "role": t.role, "title": t.title, "instr": t.instructions, "cx": t.complexity, "risk": t.risk_level,
             "ac": json.dumps(list(t.acceptance_criteria), ensure_ascii=False)},
        )
    for t in tasks:
        for dep in t.depends_on:
            await conn.execute(
                text(
                    "INSERT INTO task_dependencies (workspace_id, run_id, task_id, depends_on_task_id)"
                    " VALUES (:ws, :run, :t, :d)"
                ),
                {"ws": run.workspace_id, "run": run_id, "t": ids[t.key], "d": ids[dep]},
            )
    await conn.execute(
        text("UPDATE runs SET plan_version = :pv, version = version + 1 WHERE id = :id"), {"pv": plan_version, "id": run_id}
    )
    return plan_version, ids


async def mark_plan_approved(conn: AsyncConnection, run_id: uuid.UUID, plan_version: int) -> None:
    await conn.execute(
        text(
            "UPDATE runs SET approved_plan_version = :pv, version = version + 1 WHERE id = :id AND plan_version = :pv"
        ),
        {"pv": plan_version, "id": run_id},
    )


def priorities(tasks: list[TaskSpec]) -> dict[str, int]:
    return critical_path_lengths([PlannedTask(t.key, t.role, t.complexity, t.depends_on) for t in tasks])
