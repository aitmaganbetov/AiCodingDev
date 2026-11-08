"""P3-17: сквозной срез Phase 3 на Fake CLI — API → run → план/DAG → CLI в worktree → gates → итог.

Покрывает AC-01 (запуск DEVELOP), AC-02 (DAG без циклов), AC-07 (сбой CLI и бюджет повторов),
AC-11 (exit 0 ≠ verified; провал gate блокирует COMPLETED), AC-13 (план требует утверждения),
AC-14 (состав evidence), AC-16 (нет авторизованного CLI ⇒ BLOCKED).
"""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import ExecutionGateway, worktree_resolver
from aicd_orchestration.approvals import Approvals
from aicd_orchestration.cli_executor import CliTaskExecutor
from aicd_orchestration.engine import Orchestrator
from aicd_orchestration.worktrees import WorktreeManager
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, create_engine, transaction
from aicd_platform.ids import uuid7
from aicd_security.local_runner import LocalProcessRunner
from aicd_verification.engine import VerificationEngine
from tests.conftest import UserSession, init_git_repo, login
from tests.support import db as testdb
from tests.support.contract import validate_response

pytestmark = pytest.mark.db


@dataclass
class Slice:
    user: UserSession
    ws: str
    project: str
    worker: AsyncEngine
    tmp: Path
    settings: Settings
    scenario: dict[str, str]

    def orchestrator(self) -> Orchestrator:
        store = FileSystemArtifactStore(self.tmp / "artifacts")
        gateway = ExecutionGateway(engine=self.worker, store=store)
        scenario = self.scenario

        def factory(*, provider: str, installation_id: Any, gateway: ExecutionGateway, worktree_root: Path) -> Any:
            return FakeCLIExecutor(provider=provider, scenario=scenario["name"], runner=LocalProcessRunner(),  # type: ignore[arg-type]
                                   artifact_sink=gateway.store_artifact, worktree_path=worktree_resolver(worktree_root),
                                   env_source={"PATH": os.environ["PATH"]})

        executor = CliTaskExecutor(engine=self.worker, gateway=gateway, worktrees=WorktreeManager(self.tmp / "wt"),
                                   repository_roots=self.settings.repository_roots, adapter_factory=factory)
        verifier = VerificationEngine(engine=self.worker, store=store, runner=LocalProcessRunner(),
                                      staging_root=self.tmp / "staging", local_env={"PATH": os.environ["PATH"]})
        return Orchestrator(engine=self.worker, approvals=Approvals(), verifier=verifier, executor=executor)


async def _install_cli(worker: AsyncEngine, *, auth_status: str = "AUTHENTICATED") -> None:
    async with transaction(worker, DbContext(actor_type="system")) as conn:
        host = (await conn.execute(text(
            "INSERT INTO worker_hosts (id, hostname, kind) VALUES (:id, :h, 'exec') RETURNING id"),
            {"id": uuid7(), "h": f"e2e-{secrets.token_hex(4)}"})).scalar_one()
        await conn.execute(text(
            "UPDATE cli_installations SET enabled = false WHERE enabled"))  # изоляция от других тестов
        await conn.execute(text(
            "INSERT INTO cli_installations (id, provider_id, host_id, binary_path, auth_status, max_concurrency)"
            " SELECT :id, id, :h, '/usr/bin/python3', :auth, 2 FROM cli_providers WHERE code = 'codex'"),
            {"id": uuid7(), "h": host, "auth": auth_status})


@pytest.fixture
async def world(app: FastAPI, app_engine: AsyncEngine, user: UserSession, settings: Settings,
                database: testdb.TestDatabase, repo_root: Path, tmp_path: Path) -> AsyncIterator[Slice]:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo = repo_root / f"e2e-{secrets.token_hex(4)}"
    init_git_repo(repo)
    r = await user.client.post(f"/api/v1/projects/{project['id']}/repositories",
                               json={"provider": "local", "local_path": str(repo)}, headers=user.headers(idem=True))
    assert r.status_code == 201, r.text
    worker = create_engine(database.url_for("aicd_worker"))
    yield Slice(user, ws["id"], project["id"], worker, tmp_path, settings, {"name": "success_diff"})
    await worker.dispose()


async def _create_run(s: Slice, objective: str = "Добавить документ", **extra: Any) -> str:
    r = await s.user.client.post("/api/v1/runs", json={"project_id": s.project, "mode": "DEVELOP",
                                                       "objective": objective, **extra},
                                 headers=s.user.headers(idem=True))
    validate_response("createRun", r)
    assert r.status_code == 201, r.text
    return str(r.json()["run_id"])


async def _drive(s: Slice, run_id: str) -> str:
    orch = s.orchestrator()
    ws = uuid7().__class__(s.ws)
    rid = uuid7().__class__(run_id)
    await orch.on_run_created(rid, ws)
    return await orch.drive(rid, ws, fast_forward_backoff=True)


async def test_develop_run_completes_with_verified_evidence(world: Slice) -> None:
    await _install_cli(world.worker)
    run_id = await _create_run(world)
    assert await _drive(world, run_id) == "COMPLETED"
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    validate_response("getRun", r)
    assert r.json()["status"] == "COMPLETED" and r.json()["task_counts"] == {"SUCCEEDED": 2}
    g = await world.user.client.get(f"/api/v1/runs/{run_id}/tasks")
    validate_response("getRunTasks", g)
    graph = g.json()
    assert {t["agent_role"] for t in graph["tasks"]} == {"developer", "reviewer"} and len(graph["edges"]) == 1
    async with transaction(world.worker, DbContext(workspace_id=uuid7().__class__(world.ws), actor_type="system")) as conn:
        gates = (await conn.execute(text(
            "SELECT g.status, e.commit_sha, e.command, e.exit_code, e.started_at, e.finished_at, e.environment,"
            " e.verification_status, a.sha256 FROM gate_executions g JOIN evidence e ON e.id = g.evidence_id"
            " JOIN artifacts a ON a.id = e.artifact_id WHERE g.run_id = :r"), {"r": uuid7().__class__(run_id)})).mappings().all()
        changed = (await conn.execute(text(
            "SELECT count(*) FROM worktrees WHERE run_id = :r AND head_commit_sha IS NOT NULL"),
            {"r": uuid7().__class__(run_id)})).scalar_one()
    assert gates and all(g["status"] in ("PASS", "NOT_APPLICABLE") for g in gates)
    passed = [g for g in gates if g["status"] == "PASS"]
    assert passed, "COMPLETED невозможен без хотя бы одного PASS"
    for ev in passed:  # AC-14
        assert ev["commit_sha"] and ev["command"] and ev["exit_code"] == 0 and ev["started_at"] and ev["finished_at"]
        assert ev["verification_status"] == "VERIFIED" and len(ev["sha256"]) == 64 and ev["environment"]
    assert changed == 2


async def test_secret_in_change_fails_gate_and_run_never_completes(world: Slice) -> None:
    await _install_cli(world.worker)
    # Fake CLI записывает текст задачи в файл: синтетический ключ AWS должен провалить SCOPE gate.
    run_id = await _create_run(world, objective="AKIAABCDEFGHIJKLMNOP")
    assert await _drive(world, run_id) == "FAILED"
    async with transaction(world.worker, DbContext(workspace_id=uuid7().__class__(world.ws), actor_type="system")) as conn:
        attempts = (await conn.execute(text(
            "SELECT a.failure_category FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
            " WHERE t.run_id = :r AND t.task_key = 'main' ORDER BY a.attempt_no"), {"r": uuid7().__class__(run_id)})).scalars().all()
    assert attempts == ["GATE_FAILED"] * 3, "бюджет 3 попытки, exit 0 не засчитан как успех (AC-11)"


async def test_cli_failure_exhausts_retry_budget(world: Slice) -> None:
    await _install_cli(world.worker)
    world.scenario["name"] = "nonzero_exit"
    run_id = await _create_run(world)
    assert await _drive(world, run_id) == "FAILED"
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
        await _install_cli(world.worker, auth_status=auth)
    run_id = await _create_run(world)
    assert await _drive(world, run_id) == "BLOCKED"
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    assert r.json()["blocked_reason"] == reason  # причина видна (AC-16)


async def test_plan_approval_required_and_decided_by_member(app: FastAPI, app_engine: AsyncEngine,
                                                            world: Slice) -> None:
    await _install_cli(world.worker)
    run_id = await _create_run(world, approval_policy="always")
    orch = world.orchestrator()
    ws, rid = uuid7().__class__(world.ws), uuid7().__class__(run_id)
    await orch.on_run_created(rid, ws)
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    assert r.json()["status"] == "AWAITING_APPROVAL" and r.json()["pending_approvals"] == 1
    assert await orch.drive(rid, ws) == "AWAITING_APPROVAL", "без решения задачи не исполняются (AC-13)"
    approvals = Approvals()
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        approval_id, version, pv = (await conn.execute(text(
            "SELECT a.id, a.version, r.plan_version FROM approvals a JOIN runs r ON r.id = a.run_id"
            " WHERE a.run_id = :r"), {"r": rid})).one()
    async with transaction(app_engine, DbContext(user_id=uuid7().__class__(world.user.user_id), workspace_id=ws)) as conn:
        await approvals.decide(conn, approval_id, actor_id=uuid7().__class__(world.user.user_id), actor_role="owner",
                               decision="APPROVED", reason=None, expected_version=version)
    await orch.on_plan_approval_decided(rid, ws, approved=True, plan_version=pv)
    assert await orch.drive(rid, ws) == "COMPLETED"


def test_git_available() -> None:
    assert subprocess.run(["git", "--version"], check=False, capture_output=True).returncode == 0
