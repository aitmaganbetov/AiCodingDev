"""Обвязка сквозных тестов Phase 3 (E2E и chaos): workspace + проект + локальный репозиторий, Fake CLI,
оркестратор с настоящими gateway/worktree/verification на роли БД aicd_worker.

`build_orchestrator` используется и в тестах, и в дочернем процессе-воркере chaos-сценариев
(`tests/support/chaos_worker.py`), чтобы «убиваемый» воркер собирался так же, как в тесте.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor, ScenarioSpec
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
from tests.conftest import UserSession, init_git_repo
from tests.support import db as testdb
from tests.support.contract import validate_response


def build_executor(
    worker: AsyncEngine, tmp: Path, repository_roots: dict[str, Path], scenario: ScenarioSpec
) -> CliTaskExecutor:
    """Исполнитель задач с Fake CLI: каталог вывода агента (`.aicd/`, `output/`) — вне worktree."""
    store = FileSystemArtifactStore(tmp / "artifacts")
    gateway = ExecutionGateway(engine=worker, store=store)

    def factory(*, provider: str, installation_id: Any, gateway: ExecutionGateway, worktree_root: Path) -> Any:
        return FakeCLIExecutor(
            provider="claude" if provider == "claude" else "codex",
            scenario=scenario,
            runner=LocalProcessRunner(),
            artifact_sink=gateway.store_artifact,
            worktree_path=worktree_resolver(worktree_root),
            env_source={"PATH": os.environ["PATH"]},
            agent_io=True,
        )

    return CliTaskExecutor(
        engine=worker,
        gateway=gateway,
        worktrees=WorktreeManager(tmp / "wt"),
        repository_roots=repository_roots,
        adapter_factory=factory,
    )


def build_orchestrator(
    worker: AsyncEngine, tmp: Path, repository_roots: dict[str, Path], scenario: ScenarioSpec
) -> Orchestrator:
    executor = build_executor(worker, tmp, repository_roots, scenario)
    store = FileSystemArtifactStore(tmp / "artifacts")
    verifier = VerificationEngine(
        engine=worker,
        store=store,
        runner=LocalProcessRunner(),
        staging_root=tmp / "staging",
        local_env={"PATH": os.environ["PATH"]},
    )
    return Orchestrator(engine=worker, approvals=Approvals(), verifier=verifier, executor=executor, artifacts=store)


@dataclass
class Slice:
    user: UserSession
    ws: str
    project: str
    worker: AsyncEngine
    tmp: Path
    settings: Settings
    scenario: dict[str, ScenarioSpec]

    def orchestrator(self) -> Orchestrator:
        return build_orchestrator(self.worker, self.tmp, self.settings.repository_roots, self.scenario["name"])


# Возможности, которые probe Fake CLI сообщает о себе (CLI_ADAPTER_CONTRACT §13.1) — проходят фильтр H5 Router.
FAKE_CAPABILITIES = {
    "noninteractive_mode": True,
    "machine_readable_output": "jsonl_stream",
    "sandbox_modes": ["workspace_write"],
    "resume_session": False,
    "token_usage_exposed": True,
    "auth_status_command": True,
    "cancel_signal_safe": True,
    "tool_events_exposed": True,
}


async def install_cli(
    worker: AsyncEngine, *, auth_status: str = "AUTHENTICATED", provider: str = "codex", isolate: bool = True
) -> None:
    async with transaction(worker, DbContext(actor_type="system")) as conn:
        host = (
            await conn.execute(
                text("INSERT INTO worker_hosts (id, hostname, kind) VALUES (:id, :h, 'exec') RETURNING id"),
                {"id": uuid7(), "h": f"e2e-{secrets.token_hex(4)}"},
            )
        ).scalar_one()
        if isolate:  # изоляция от других тестов: только установки этого теста
            await conn.execute(text("UPDATE cli_installations SET enabled = false WHERE enabled"))
        await conn.execute(
            text(
                "INSERT INTO cli_installations (id, provider_id, host_id, binary_path, auth_status, max_concurrency,"
                " compatibility, capabilities)"
                " SELECT :id, id, :h, '/usr/bin/python3', :auth, 2, 'supported', CAST(:caps AS jsonb)"
                " FROM cli_providers WHERE code = :provider"
            ),
            {
                "id": uuid7(),
                "h": host,
                "auth": auth_status,
                "caps": json.dumps(FAKE_CAPABILITIES),
                "provider": provider,
            },
        )


@pytest.fixture
async def world(
    app: FastAPI,
    app_engine: AsyncEngine,
    user: UserSession,
    settings: Settings,
    database: testdb.TestDatabase,
    repo_root: Path,
    tmp_path: Path,
) -> AsyncIterator[Slice]:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo = repo_root / f"e2e-{secrets.token_hex(4)}"
    init_git_repo(repo)
    r = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"provider": "local", "local_path": str(repo)},
        headers=user.headers(idem=True),
    )
    assert r.status_code == 201, r.text
    worker = create_engine(database.url_for("aicd_worker"))
    yield Slice(user, ws["id"], project["id"], worker, tmp_path, settings, {"name": "agent"})
    await worker.dispose()


async def create_run(s: Slice, objective: str = "Добавить документ", **extra: Any) -> str:
    r = await s.user.client.post(
        "/api/v1/runs",
        json={"project_id": s.project, "mode": "DEVELOP", "objective": objective, **extra},
        headers=s.user.headers(idem=True),
    )
    validate_response("createRun", r)
    assert r.status_code == 201, r.text
    return str(r.json()["run_id"])


async def drive(s: Slice, run_id: str) -> str:
    orch = s.orchestrator()
    ws = uuid7().__class__(s.ws)
    rid = uuid7().__class__(run_id)
    await orch.on_run_created(rid, ws)
    return await orch.drive(rid, ws, fast_forward_backoff=True)


async def install_both(worker: AsyncEngine) -> None:
    """Codex и Claude: независимое ревью L3+ возможно (ROUTING_POLICY §5)."""
    await install_cli(worker, provider="codex")
    await install_cli(worker, provider="claude", isolate=False)
