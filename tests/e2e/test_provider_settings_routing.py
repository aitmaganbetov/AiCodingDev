"""Настройки провайдера в workspace применяются Router и исполнителем (API_DESIGN §15, ROUTING_POLICY §4.1a).

Сквозной срез на Fake CLI: PATCH /providers/{id}/settings → engine:
* `enabled=false` — провайдер исключён (`workspace_disabled` в routing_decisions), задачу берёт другой;
* `allowed_roles` без роли задачи — BLOCKED(PROVIDER_UNAVAILABLE) с причиной `role_not_allowed`;
* `max_parallel_sessions` — при занятом лимите задача ждёт (WAIT: остаётся READY, без попытки), после
  освобождения сессии исполняется;
* `default_timeout_seconds` — тайм-аут исполнения CLI, если у задачи нет явного.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from tests.support import db as testdb
from tests.support.orchestration import connect_admin
from tests.support.phase3 import Slice, create_run, drive, install_both, install_cli, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


async def _patch(s: Slice, code: str, body: dict[str, Any]) -> None:
    ws = s.ws
    listed = (await s.user.client.get("/api/v1/providers", headers={"X-Workspace-Id": ws})).json()["items"]
    p = next(x for x in listed if x["code"] == code)
    version = p["workspace_settings"]["settings_version"]
    r = await s.user.client.patch(
        f"/api/v1/providers/{p['provider_id']}/settings",
        json=body,
        headers=s.user.headers(ws, if_match=f'"v{version}"'),
    )
    assert r.status_code == 200, r.text


async def _rows(s: Slice, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    async with transaction(s.worker, DbContext(workspace_id=uuid.UUID(s.ws), actor_type="system")) as conn:
        return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


async def test_disabled_provider_is_excluded_and_other_provider_runs(world: Slice) -> None:
    await install_both(world.worker)
    await _patch(world, "codex", {"enabled": False})
    run_id = await create_run(world)
    await drive(world, run_id)
    providers = await _rows(
        world,
        "SELECT DISTINCT a.provider FROM task_attempts a JOIN tasks t ON t.id = a.task_id WHERE t.run_id = :r",
        {"r": uuid.UUID(run_id)},
    )
    assert providers and {p["provider"] for p in providers} == {"claude"}
    decisions = await _rows(
        world,
        "SELECT d.outcome, d.score_breakdown FROM routing_decisions d JOIN tasks t ON t.id = d.task_id"
        " WHERE t.run_id = :r ORDER BY d.created_at",
        {"r": uuid.UUID(run_id)},
    )
    first = decisions[0]
    assert first["outcome"] == "SELECTED"
    assert {"provider": "codex", "reason": "workspace_disabled"} in [
        {"provider": e["provider"], "reason": e["reason"]} for e in first["score_breakdown"]["excluded"]
    ]


async def test_role_not_allowed_blocks_with_recorded_reason(world: Slice) -> None:
    await install_cli(world.worker)
    await _patch(world, "codex", {"allowed_roles": ["maintenance"]})
    run_id = await create_run(world)
    assert await drive(world, run_id) == "BLOCKED"
    blocked = await _rows(
        world,
        "SELECT t.status, t.blocked_reason, d.outcome, d.reason, d.score_breakdown FROM tasks t"
        " JOIN routing_decisions d ON d.task_id = t.id WHERE t.run_id = :r AND t.status = 'BLOCKED'",
        {"r": uuid.UUID(run_id)},
    )
    assert blocked and blocked[0]["blocked_reason"] == "PROVIDER_UNAVAILABLE" and blocked[0]["outcome"] == "BLOCKED"
    # Остальные установки выключены изоляцией теста (`disabled`); включённая исключена по роли.
    reasons = {e["reason"] for e in blocked[0]["score_breakdown"]["excluded"]}
    assert reasons - {"disabled"} == {"role_not_allowed"}
    assert "настройкам workspace" in blocked[0]["reason"]
    attempts = await _rows(
        world,
        "SELECT count(*) AS n FROM task_attempts a JOIN tasks t ON t.id = a.task_id WHERE t.run_id = :r",
        {"r": uuid.UUID(run_id)},
    )
    assert attempts[0]["n"] == 0


async def test_session_cap_waits_then_dispatches_and_default_timeout_applies(
    world: Slice, database: testdb.TestDatabase
) -> None:
    await install_cli(world.worker)  # max_concurrency установки = 2 — лимит workspace (1) строже
    await _patch(world, "codex", {"max_parallel_sessions": 1, "default_timeout_seconds": 600})
    # Занятая сессия codex в этом workspace: попытка RUNNING задачи другого run.
    other_run = await create_run(world, "Другой run")
    blocker_task, blocker_attempt = uuid7(), uuid7()
    with connect_admin(database) as conn:
        conn.execute(
            "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title, instructions,"
            " complexity, risk_level) VALUES (%s, %s, %s, 1, 'blocker', 'developer', 'b', 'b', 1, 'low')",
            (blocker_task, world.ws, other_run),
        )
        conn.execute(
            "INSERT INTO task_attempts (id, workspace_id, task_id, attempt_no, provider, fencing_token)"
            " VALUES (%s, %s, %s, 1, 'codex', 1)",
            (blocker_attempt, world.ws, blocker_task),
        )
    run_id = await create_run(world)
    assert await drive(world, run_id) == "RUNNING"
    waiting = await _rows(
        world,
        "SELECT t.status, (SELECT count(*) FROM task_attempts a WHERE a.task_id = t.id) AS attempts,"
        " (SELECT count(*) FROM routing_decisions d WHERE d.task_id = t.id) AS decisions"
        " FROM tasks t WHERE t.run_id = :r AND t.status = 'READY'",
        {"r": uuid.UUID(run_id)},
    )
    assert waiting and all(w["attempts"] == 0 and w["decisions"] == 0 for w in waiting), waiting

    with connect_admin(database) as conn:
        conn.execute(
            "UPDATE task_attempts SET status = 'CANCELLED', finished_at = now() WHERE id = %s", (blocker_attempt,)
        )
    # Задачи исполняются; ревью с единственным провайдером (strict) затем ждёт ручного ревью — не лимита сессий.
    assert await drive(world, run_id) in ("COMPLETED", "BLOCKED")
    blocked = await _rows(
        world, "SELECT blocked_reason FROM tasks WHERE run_id = :r AND status = 'BLOCKED'", {"r": uuid.UUID(run_id)}
    )
    assert {b["blocked_reason"] for b in blocked} <= {"MANUAL_REVIEW"}
    executions = await _rows(
        world,
        "SELECT DISTINCT e.timeout_seconds FROM cli_executions e JOIN task_attempts a ON a.id = e.task_attempt_id"
        " JOIN tasks t ON t.id = a.task_id WHERE t.run_id = :r",
        {"r": uuid.UUID(run_id)},
    )
    assert [e["timeout_seconds"] for e in executions] == [600]  # default_timeout_seconds workspace
