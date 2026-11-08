"""P4-13 (часть на Fake CLI): handoff developer → независимый reviewer → fix → повторное ревью (AC-03, AC-04, AC-05).

Агенты — процессы Fake CLI, получающие собранный платформой промпт и возвращающие `.aicd/result.json`;
сообщения между ними идут только через брокер ACP. Ревью первого изменения требует исправлений, ревью
исправления — одобряет. Проверяется: независимость ревьюера (другой провайдер), FIX_REQUEST засчитан в
счётчик исходной реализации, fix-задача и повторное ревью созданы и выполнены, run COMPLETED с evidence.
Прогон с реальными CLI (Codex developer → Claude reviewer) — opt-in, после P2-01.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from aicd_cli_adapters.models import ExecutionRequest
from aicd_platform.db import DbContext, transaction
from tests.support.phase3 import Slice, create_run, drive, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


def scenario(request: ExecutionRequest) -> str:
    if request.agent_role == "reviewer":
        # Первое ревью — замечания; повторное ревью (задача цикла review.rN) — одобрение.
        return "agent_review_approve" if "Проверить исправления" in request.input.objective else "agent_review_changes"
    return "agent"


async def _rows(s: Slice, sql: str, run_id: str) -> list[dict[str, Any]]:
    async with transaction(s.worker, DbContext(workspace_id=uuid.UUID(s.ws), actor_type="system")) as conn:
        return [dict(r) for r in (await conn.execute(text(sql), {"r": uuid.UUID(run_id)})).mappings().all()]


async def test_develop_review_fix_rereview_handoff(world: Slice) -> None:
    await install_both(world.worker)
    world.scenario["name"] = scenario
    run_id = await create_run(world)
    assert await drive(world, run_id) == "COMPLETED"

    tasks = {
        t["task_key"]: t
        for t in await _rows(
            world,
            "SELECT id, task_key, status, agent_role, origin_task_id, review_loop_count FROM tasks WHERE run_id = :r",
            run_id,
        )
    }
    assert {k: t["status"] for k, t in tasks.items()} == {
        "main": "SUCCEEDED",
        "review": "SUCCEEDED",
        "main.fix1": "SUCCEEDED",
        "review.r1": "SUCCEEDED",
    }
    assert tasks["main.fix1"]["origin_task_id"] == tasks["main"]["id"]
    assert tasks["main"]["review_loop_count"] == 1, "FIX_REQUEST засчитан исходной реализации"

    providers = {
        r["task_key"]: r["provider"]
        for r in await _rows(
            world,
            "SELECT t.task_key, a.provider FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
            " WHERE t.run_id = :r AND a.status = 'SUCCEEDED'",
            run_id,
        )
    }
    assert providers["main"] == providers["main.fix1"], "FIX-итерация остаётся у исполнителя (stickiness)"
    assert providers["review"] != providers["main"] and providers["review.r1"] != providers["main"], (
        "ревью независимо: провайдер ревьюера ≠ автор изменения (AC-04)"
    )

    messages = await _rows(
        world,
        "SELECT m.message_type, m.sender_role, m.recipient_role, t.task_key FROM agent_messages m"
        " LEFT JOIN tasks t ON t.id = m.task_id WHERE m.run_id = :r ORDER BY m.created_at",
        run_id,
    )
    kinds = [(m["message_type"], m["sender_role"], m["task_key"]) for m in messages]
    assert ("REVIEW_REQUEST", "developer", "main") in kinds
    assert ("FIX_REQUEST", "reviewer", "review") in kinds
    assert ("REVIEW_REQUEST", "developer", "main.fix1") in kinds
    assert ("TASK_COMPLETED", "reviewer", "review.r1") in kinds

    decisions = await _rows(
        world,
        "SELECT t.task_key, d.score_breakdown ->> 'review_independence' AS independence"
        " FROM routing_decisions d JOIN tasks t ON t.id = d.task_id WHERE t.run_id = :r AND t.agent_role = 'reviewer'",
        run_id,
    )
    assert {d["independence"] for d in decisions} == {"independent"}, "самопроверки не было (AC-05)"

    evidence = await _rows(
        world,
        "SELECT count(*) AS n FROM gate_executions g JOIN tasks t ON t.id = g.task_id"
        " WHERE g.run_id = :r AND t.task_key = 'main.fix1' AND g.status = 'PASS' AND g.evidence_id IS NOT NULL",
        run_id,
    )
    assert evidence[0]["n"] >= 1, "исправление проверено гейтами с evidence"
