"""Сборка оркестратора для воркеров: адаптер исполнения по `AICD_EXEC_ADAPTER`, порты approvals/verification."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import ExecutionGateway, worktree_resolver
from aicd_cli_adapters.subprocess_adapter import SubprocessCLIAdapter
from aicd_orchestration.cli_executor import AdapterFactory, CliTaskExecutor
from aicd_orchestration.engine import ApprovalsPort, Orchestrator, VerifierPort
from aicd_orchestration.worktrees import WorktreeManager
from aicd_platform.artifacts import ArtifactStore, FileSystemArtifactStore
from aicd_security.local_runner import LocalProcessRunner
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


def build_executor(
    settings: WorkerSettings, engine: AsyncEngine, store: ArtifactStore | None = None
) -> CliTaskExecutor:
    gateway = ExecutionGateway(engine=engine, store=store or FileSystemArtifactStore(settings.artifact_fs_root))
    return CliTaskExecutor(
        engine=engine,
        gateway=gateway,
        worktrees=WorktreeManager(settings.worktree_root),
        repository_roots=settings.repository_roots,
        adapter_factory=fake_adapter_factory(settings),
    )


def default_ports(engine: AsyncEngine) -> tuple[ApprovalsPort, VerifierPort]:
    """Реализации портов approvals/verification подключаются вместе с соответствующими модулями Phase 3."""
    del engine
    raise ConfigurationError("порты approvals/verification ещё не подключены к воркеру (см. aicd_worker.wiring)")


def build_orchestrator(
    settings: WorkerSettings,
    engine: AsyncEngine,
    *,
    approvals: ApprovalsPort | None = None,
    verifier: VerifierPort | None = None,
) -> Orchestrator:
    if approvals is None or verifier is None:
        approvals, verifier = default_ports(engine)
    return Orchestrator(
        engine=engine, approvals=approvals, verifier=verifier, executor=build_executor(settings, engine)
    )
