"""P4-07/P4-08 на сквозном срезе: Router в engine, независимое ревью и fallback-политики (ROUTING_POLICY §4–§5).

* два провайдера: ревью L3+ получает провайдера ≠ автора изменения, маркировка `independent`;
* один провайдер, профиль strict: ревью → BLOCKED(MANUAL_REVIEW) + approval MANUAL_REVIEW; одобрение
  человеком завершает ревью (`human_review`), run COMPLETED;
* профиль basic: SELF_REVIEW_FALLBACK только после явного одобрения; самопроверка — `self_review`, не independent;
* каждое решение Router записано в routing_decisions с разбором оценок.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration import router
from aicd_orchestration.approvals import Approvals
from aicd_platform.db import DbContext, transaction
from tests.support.phase3 import Slice, create_run, drive, install_both, install_cli, install_probed, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


async def _rows(s: Slice, sql: str, run_id: str) -> list[dict[str, Any]]:
    async with transaction(s.worker, DbContext(workspace_id=uuid.UUID(s.ws), actor_type="system")) as conn:
        return [dict(r) for r in (await conn.execute(text(sql), {"r": uuid.UUID(run_id)})).mappings().all()]


async def _decide(s: Slice, app_engine: AsyncEngine, run_id: str, action: str, decision: str) -> uuid.UUID:
    ws = uuid.UUID(s.ws)
    async with transaction(s.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        approval_id, version = (
            await conn.execute(
                text("SELECT id, version FROM approvals WHERE run_id = :r AND action_type = :a AND status = 'PENDING'"),
                {"r": uuid.UUID(run_id), "a": action},
            )
        ).one()
    user = uuid.UUID(s.user.user_id)
    async with transaction(app_engine, DbContext(user_id=user, workspace_id=ws)) as conn:
        await Approvals().decide(conn, approval_id, actor_id=user, actor_role="owner", decision=decision, reason=None,
                                 expected_version=version)  # fmt: skip
    return uuid.UUID(str(approval_id))


async def test_review_goes_to_the_other_provider_and_decisions_are_recorded(world: Slice) -> None:
    await install_both(world.worker)
    run_id = await create_run(world)
    assert await drive(world, run_id) == "COMPLETED"
    attempts = await _rows(
        world,
        "SELECT t.task_key, a.provider FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
        " WHERE t.run_id = :r AND a.status = 'SUCCEEDED'",
        run_id,
    )
    by_key = {a["task_key"]: a["provider"] for a in attempts}
    assert by_key["review"] != by_key["main"], "ревью L3 выполняет не автор изменения"
    decisions = await _rows(
        world,
        "SELECT t.task_key, d.outcome, d.selected_provider, d.complexity_level, d.score_breakdown"
        " FROM routing_decisions d JOIN tasks t ON t.id = d.task_id WHERE t.run_id = :r ORDER BY d.created_at",
        run_id,
    )
    assert [d["task_key"] for d in decisions] == ["main", "review"]
    review = decisions[1]
    assert review["outcome"] == "SELECTED" and review["selected_provider"] == by_key["review"]
    assert review["score_breakdown"]["review_independence"] == "independent"
    assert any(e["reason"] == "independence_conflict" for e in review["score_breakdown"]["excluded"])
    assert {"weights", "candidates", "quality_inputs"} <= set(decisions[0]["score_breakdown"])


async def test_single_provider_strict_requires_manual_review(world: Slice, app_engine: AsyncEngine) -> None:
    await install_cli(world.worker)
    run_id = await create_run(world)  # профиль strict по умолчанию → MANUAL_REVIEW
    assert await drive(world, run_id) == "BLOCKED"
    tasks = await _rows(world, "SELECT task_key, status, blocked_reason FROM tasks WHERE run_id = :r", run_id)
    assert {t["task_key"]: (t["status"], t["blocked_reason"]) for t in tasks}["review"] == ("BLOCKED", "MANUAL_REVIEW")
    blocked = await _rows(
        world,
        "SELECT d.outcome, d.score_breakdown FROM routing_decisions d JOIN tasks t ON t.id = d.task_id"
        " WHERE t.run_id = :r AND t.task_key = 'review'",
        run_id,
    )
    assert (
        blocked[-1]["outcome"] == "BLOCKED" and blocked[-1]["score_breakdown"]["review_independence"] == "human_review"
    )
    approval = await _decide(world, app_engine, run_id, "MANUAL_REVIEW", "APPROVED")
    orch = world.orchestrator()
    await orch.on_task_approval_decided(approval, uuid.UUID(world.ws))
    # Задача ревью разблокирована решением человека ⇒ run возобновляется сам и завершается.
    assert await drive(world, run_id) == "COMPLETED"
    review_attempts = await _rows(
        world,
        "SELECT count(*) AS n FROM task_attempts a JOIN tasks t ON t.id = a.task_id WHERE t.run_id = :r"
        " AND t.task_key = 'review'",
        run_id,
    )
    assert review_attempts[0]["n"] == 0, "ревью выполнил человек; самопроверка провайдером-автором не запускалась"


async def test_basic_profile_self_review_only_after_explicit_approval(world: Slice, app_engine: AsyncEngine) -> None:
    await install_cli(world.worker)
    run_id = await create_run(world, quality_profile="basic")  # basic → SELF_REVIEW_FALLBACK
    assert await drive(world, run_id) == "BLOCKED"
    tasks = await _rows(world, "SELECT task_key, status, blocked_reason FROM tasks WHERE run_id = :r", run_id)
    assert {t["task_key"]: (t["status"], t["blocked_reason"]) for t in tasks}["review"] == (
        "BLOCKED",
        "APPROVAL_REQUIRED",
    ), "самопроверка без одобрения не запускается"
    approval = await _decide(world, app_engine, run_id, "SELF_REVIEW_FALLBACK", "APPROVED")
    await world.orchestrator().on_task_approval_decided(approval, uuid.UUID(world.ws))
    assert await drive(world, run_id) == "COMPLETED"
    review = await _rows(
        world,
        "SELECT a.provider, d.outcome, d.score_breakdown FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
        " JOIN routing_decisions d ON d.task_id = t.id WHERE t.run_id = :r AND t.task_key = 'review'"
        " ORDER BY d.created_at DESC LIMIT 1",
        run_id,
    )
    assert review[0]["outcome"] == "FALLBACK"
    assert review[0]["score_breakdown"]["review_independence"] == "self_review", "самопроверка ≠ independent (ТЗ §4)"


async def test_untested_fake_cli_routes_by_profile_policy(world: Slice) -> None:
    """CLI_ADAPTER_CONTRACT §5.3: установки после штатного probe — `untested` (матрица пуста); basic/strict их
    допускают с предупреждением в routing_decisions, critical — исключает (`untested_version`)."""
    ours = {
        str(await install_probed(world.worker, provider="codex")),
        str(await install_probed(world.worker, provider="claude", isolate=False)),
    }
    run_id = await create_run(world)  # strict
    snapshot = await _rows(world, "SELECT policy_snapshot FROM runs WHERE id = :r", run_id)
    assert snapshot[0]["policy_snapshot"]["routing"]["allow_untested_cli_versions"] is True
    assert await drive(world, run_id) == "COMPLETED"
    decisions = await _rows(
        world,
        "SELECT d.outcome, d.reason, d.score_breakdown FROM routing_decisions d JOIN tasks t ON t.id = d.task_id"
        " WHERE t.run_id = :r",
        run_id,
    )
    assert decisions and all(d["outcome"] == "SELECTED" for d in decisions)
    for d in decisions:
        assert d["score_breakdown"]["warnings"] == [router.UNTESTED_WARNING]
        assert router.UNTESTED_WARNING in d["reason"]
        assert {c["compatibility"] for c in d["score_breakdown"]["candidates"]} == {"untested"}
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    assert set(r.json()["routing"]) == {"strategy", "allowed_providers"}  # контракт RunRouting

    critical = await create_run(world, quality_profile="critical")
    snapshot = await _rows(world, "SELECT policy_snapshot FROM runs WHERE id = :r", critical)
    assert snapshot[0]["policy_snapshot"]["routing"]["allow_untested_cli_versions"] is False
    assert await drive(world, critical) == "BLOCKED"
    blocked = await _rows(
        world,
        "SELECT d.outcome, d.score_breakdown FROM routing_decisions d JOIN tasks t ON t.id = d.task_id"
        " WHERE t.run_id = :r",
        critical,
    )
    assert blocked[0]["outcome"] == "BLOCKED"
    excluded = {e["installation_id"]: e["reason"] for e in blocked[0]["score_breakdown"]["excluded"]}
    assert {excluded[i] for i in ours} == {"untested_version"}  # прочие — выключенные установки других тестов
