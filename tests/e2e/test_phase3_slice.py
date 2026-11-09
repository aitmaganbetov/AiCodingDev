"""P3-17: сквозной срез Phase 3 на Fake CLI — API → run → план/DAG → CLI в worktree → gates → итог.

Покрывает AC-01 (запуск DEVELOP), AC-02 (DAG без циклов), AC-07 (сбой CLI и бюджет повторов),
AC-11 (exit 0 ≠ verified; провал gate блокирует COMPLETED), AC-13 (план требует утверждения),
AC-14 (состав evidence), AC-16 (нет авторизованного CLI ⇒ BLOCKED).
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.approvals import Approvals
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from tests.support.contract import validate_response
from tests.support.phase3 import Slice, create_run, drive, install_both, install_cli, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


async def test_develop_run_completes_with_verified_evidence(world: Slice) -> None:
    await install_both(world.worker)
    run_id = await create_run(world)
    assert await drive(world, run_id) == "COMPLETED"
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    validate_response("getRun", r)
    assert r.json()["status"] == "COMPLETED" and r.json()["task_counts"] == {"SUCCEEDED": 2}
    g = await world.user.client.get(f"/api/v1/runs/{run_id}/tasks")
    validate_response("getRunTasks", g)
    graph = g.json()
    assert {t["agent_role"] for t in graph["tasks"]} == {"developer", "reviewer"} and len(graph["edges"]) == 1
    async with transaction(
        world.worker, DbContext(workspace_id=uuid7().__class__(world.ws), actor_type="system")
    ) as conn:
        gates = (
            (
                await conn.execute(
                    text(
                        "SELECT g.status, e.commit_sha, e.command, e.exit_code, e.started_at, e.finished_at, e.environment,"
                        " e.verification_status, a.sha256 FROM gate_executions g JOIN evidence e ON e.id = g.evidence_id"
                        " JOIN artifacts a ON a.id = e.artifact_id WHERE g.run_id = :r"
                    ),
                    {"r": uuid7().__class__(run_id)},
                )
            )
            .mappings()
            .all()
        )
        changed = (
            await conn.execute(
                text("SELECT count(*) FROM worktrees WHERE run_id = :r AND head_commit_sha IS NOT NULL"),
                {"r": uuid7().__class__(run_id)},
            )
        ).scalar_one()
    assert gates and all(g["status"] in ("PASS", "NOT_APPLICABLE") for g in gates)
    passed = [g for g in gates if g["status"] == "PASS"]
    assert passed, "COMPLETED невозможен без хотя бы одного PASS"
    for ev in passed:  # AC-14
        assert ev["commit_sha"] and ev["command"] and ev["exit_code"] == 0 and ev["started_at"] and ev["finished_at"]
        assert ev["verification_status"] == "VERIFIED" and len(ev["sha256"]) == 64 and ev["environment"]
    assert changed == 2
    async with transaction(
        world.worker, DbContext(workspace_id=uuid7().__class__(world.ws), actor_type="system")
    ) as conn:
        compliance = (
            await conn.execute(
                text("SELECT count(*) FROM compliance_results WHERE run_id = :r AND superseded_at IS NULL"),
                {"r": uuid7().__class__(run_id)},
            )
        ).scalar_one()
    assert compliance > 0, "compliance.evaluate(run) выполнен до guard завершения и записал вердикты контролей"


async def test_secret_in_change_fails_gate_and_run_never_completes(world: Slice) -> None:
    await install_cli(world.worker)
    # Fake CLI записывает текст задачи в файл: синтетический ключ AWS должен провалить SCOPE gate.
    run_id = await create_run(world, objective="AKIAABCDEFGHIJKLMNOP")
    assert await drive(world, run_id) == "FAILED"
    async with transaction(
        world.worker, DbContext(workspace_id=uuid7().__class__(world.ws), actor_type="system")
    ) as conn:
        attempts = (
            (
                await conn.execute(
                    text(
                        "SELECT a.failure_category FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
                        " WHERE t.run_id = :r AND t.task_key = 'main' ORDER BY a.attempt_no"
                    ),
                    {"r": uuid7().__class__(run_id)},
                )
            )
            .scalars()
            .all()
        )
    assert attempts == ["GATE_FAILED"] * 3, "бюджет 3 попытки, exit 0 не засчитан как успех (AC-11)"


async def test_cli_failure_exhausts_retry_budget(world: Slice) -> None:
    await install_cli(world.worker)
    world.scenario["name"] = "nonzero_exit"
    run_id = await create_run(world)
    assert await drive(world, run_id) == "FAILED"
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    assert r.json()["status"] == "FAILED"
    tasks = (await world.user.client.get(f"/api/v1/runs/{run_id}/tasks")).json()["tasks"]
    main = next(t for t in tasks if t["agent_role"] == "developer")
    assert main["status"] == "FAILED" and main["attempt_count"] == 3
    attempts = await world.user.client.get(f"/api/v1/tasks/{main['id']}/attempts")
    validate_response("listTaskAttempts", attempts)
    assert [a["failure_category"] for a in attempts.json()["items"]] == ["PROCESS_FAILED"] * 3
    assert all(a["executions"] and a["executions"][0]["status"] == "COLLECTED" for a in attempts.json()["items"])


@pytest.mark.parametrize(("auth", "reason"), [(None, "PROVIDER_UNAVAILABLE"), ("AUTH_REQUIRED", "AUTH_REQUIRED")])
async def test_no_usable_cli_blocks_without_workaround(world: Slice, auth: str | None, reason: str) -> None:
    async with transaction(world.worker, DbContext(actor_type="system")) as conn:
        await conn.execute(text("UPDATE cli_installations SET enabled = false WHERE enabled"))
    if auth:
        await install_cli(world.worker, auth_status=auth)
    run_id = await create_run(world)
    assert await drive(world, run_id) == "BLOCKED"
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    assert r.json()["blocked_reason"] == reason  # причина видна (AC-16)


async def test_plan_approval_required_and_decided_by_member(
    app: FastAPI, app_engine: AsyncEngine, world: Slice
) -> None:
    await install_both(world.worker)
    run_id = await create_run(world, approval_policy="always")
    orch = world.orchestrator()
    ws, rid = uuid7().__class__(world.ws), uuid7().__class__(run_id)
    await orch.on_run_created(rid, ws)
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    assert r.json()["status"] == "AWAITING_APPROVAL" and r.json()["pending_approvals"] == 1
    assert await orch.drive(rid, ws) == "AWAITING_APPROVAL", "без решения задачи не исполняются (AC-13)"
    approvals = Approvals()
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        approval_id, version, pv = (
            await conn.execute(
                text(
                    "SELECT a.id, a.version, r.plan_version FROM approvals a JOIN runs r ON r.id = a.run_id"
                    " WHERE a.run_id = :r"
                ),
                {"r": rid},
            )
        ).one()
    async with transaction(
        app_engine, DbContext(user_id=uuid7().__class__(world.user.user_id), workspace_id=ws)
    ) as conn:
        await approvals.decide(
            conn,
            approval_id,
            actor_id=uuid7().__class__(world.user.user_id),
            actor_role="owner",
            decision="APPROVED",
            reason=None,
            expected_version=version,
        )
    await orch.on_plan_approval_decided(rid, ws, approved=True, plan_version=pv)
    assert await orch.drive(rid, ws) == "COMPLETED"


def test_git_available() -> None:
    assert subprocess.run(["git", "--version"], check=False, capture_output=True).returncode == 0
