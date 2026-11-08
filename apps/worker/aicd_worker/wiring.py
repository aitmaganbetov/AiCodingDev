"""Сборка оркестратора для воркеров: адаптер исполнения по `AICD_EXEC_ADAPTER`, порты approvals/verification."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import ExecutionGateway, worktree_resolver
from aicd_cli_adapters.subprocess_adapter import SubprocessCLIAdapter
from aicd_orchestration.approvals import Approvals
from aicd_orchestration.cli_executor import AdapterFactory, CliTaskExecutor
from aicd_orchestration.engine import ApprovalsPort, Orchestrator, VerifierPort
from aicd_orchestration.recovery import Reconciler
from aicd_orchestration.worktrees import WorktreeManager
from aicd_platform.artifacts import ArtifactStore, build_artifact_store
from aicd_security.docker_runner import DockerSandboxRunner
from aicd_security.local_runner import LocalProcessRunner
from aicd_security.sandbox import SandboxRunner
from aicd_verification.engine import VerificationEngine
from aicd_worker.settings import WorkerSettings


class ConfigurationError(RuntimeError):
    pass


def check_exec_adapter(settings: WorkerSettings) -> None:
    """Fake CLI — только для разработки/CI: в production воркер исполнения не стартует (ADR-0003)."""
    if settings.exec_adapter == "fake" and settings.environment == "production":
        raise ConfigurationError(
            "AICD_EXEC_ADAPTER=fake запрещён в production: реальные адаптеры Codex/Claude ещё не подключены"
        )


def fake_adapter_factory(settings: WorkerSettings) -> AdapterFactory:
    def factory(
        *, provider: str, installation_id: uuid.UUID, gateway: ExecutionGateway, worktree_root: Path
    ) -> SubprocessCLIAdapter:
        del installation_id
        return FakeCLIExecutor(
            provider="claude" if provider == "claude" else "codex",
            scenario=settings.fake_scenario,
            runner=LocalProcessRunner(),
            artifact_sink=gateway.store_artifact,
            worktree_path=worktree_resolver(worktree_root),
            env_source={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    return factory


def check_verification_runner(settings: WorkerSettings) -> None:
    if settings.verification_runner == "local" and settings.environment == "production":
        raise ConfigurationError("AICD_VERIFICATION_RUNNER=local запрещён в production: gates только в контейнере")


def build_sandbox_runner(settings: WorkerSettings) -> SandboxRunner:
    if settings.verification_runner == "local":
        return LocalProcessRunner()
    # Bind-монтирования gates: worktree задачи (ro) и staging входа SCOPE — других корней линтер не пропустит.
    roots = (str(settings.worktree_root), str(settings.verification_staging_root))
    return DockerSandboxRunner(settings.docker_url, allowed_bind_roots=roots)


def build_executor(
    settings: WorkerSettings, engine: AsyncEngine, store: ArtifactStore, *, worker_host_id: uuid.UUID | None = None
) -> CliTaskExecutor:
    return CliTaskExecutor(
        engine=engine,
        gateway=ExecutionGateway(engine=engine, store=store),
        worktrees=WorktreeManager(settings.worktree_root),
        repository_roots=settings.repository_roots,
        adapter_factory=fake_adapter_factory(settings),
        worker_host_id=worker_host_id,
    )


def default_ports(
    settings: WorkerSettings, engine: AsyncEngine, store: ArtifactStore
) -> tuple[ApprovalsPort, VerifierPort]:
    check_verification_runner(settings)
    verifier = VerificationEngine(
        engine=engine,
        store=store,
        runner=build_sandbox_runner(settings),
        image=settings.verification_image,
        staging_root=settings.verification_staging_root,
        local_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    return Approvals(), verifier


def build_orchestrator(
    settings: WorkerSettings,
    engine: AsyncEngine,
    *,
    approvals: ApprovalsPort | None = None,
    verifier: VerifierPort | None = None,
    store: ArtifactStore | None = None,
    worker_host_id: uuid.UUID | None = None,
) -> Orchestrator:
    store = store or build_artifact_store(settings)
    if approvals is None or verifier is None:
        approvals, verifier = default_ports(settings, engine, store)
    return Orchestrator(
        engine=engine,
        approvals=approvals,
        verifier=verifier,
        executor=build_executor(settings, engine, store, worker_host_id=worker_host_id),
        artifacts=store,
    )


def build_reconciler(
    settings: WorkerSettings, engine: AsyncEngine, worker_host_id: uuid.UUID, store: ArtifactStore | None = None
) -> Reconciler:
    """Recovery reconciler хоста исполнения: процессы CLI/контейнеры и worktrees принадлежат этому хосту.

    Probe внешних действий (GitHub и т. п.) появятся с интеграциями Phase 5; до тех пор прерванные действия
    остаются EXECUTING и попадают в отчёт (без слепого повтора, AC-09).
    """
    runners: tuple[SandboxRunner, ...] = (LocalProcessRunner(),)
    if settings.verification_runner == "docker":
        runners = (*runners, build_sandbox_runner(settings))
    return Reconciler(
        engine=engine,
        artifacts=store or build_artifact_store(settings),
        worktrees=WorktreeManager(settings.worktree_root),
        repository_roots=settings.repository_roots,
        worker_host_id=worker_host_id,
        runners=runners,
    )
