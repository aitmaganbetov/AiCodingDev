"""Настройки воркеров (`AICD_*`, поверх общих настроек платформы)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from aicd_platform.config import Settings


class WorkerSettings(Settings):
    # Адаптер исполнения задач. Реальные Codex/Claude адаптеры появятся вместе с доступными CLI;
    # `fake` (эмулятор CLI_ADAPTER_CONTRACT §13.1) запрещён в production.
    exec_adapter: Literal["fake"] = "fake"
    fake_scenario: str = "success_diff"
    worktree_root: Path = Path("/var/lib/aicd/worktrees")
    # Verification Engine (P3-14): gates в изолированном контейнере; `local` (процессы хоста) — только dev/test.
    verification_runner: Literal["docker", "local"] = "docker"
    verification_image: str = "python:3.12-slim-bookworm"
    verification_staging_root: Path = Path("/var/lib/aicd/verification")
    docker_url: str = "unix:///var/run/docker.sock"

    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_poll_seconds: float = Field(default=0.5, gt=0, le=10)
    tick_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    stale_ready_seconds: int = Field(default=30, ge=1)
    worker_threads: int = Field(default=4, ge=1, le=64)
    exec_time_limit_seconds: int = Field(default=5 * 3600, ge=60)

    # Индексация кода (P5-02/P5-06): `aicd-worker index` и индекс перед исполнением задачи.
    index_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    index_workspace_batch: int = Field(default=10, ge=1, le=1000)
    index_jobs_per_workspace: int = Field(default=5, ge=1, le=100)
    index_stale_seconds: int = Field(default=1800, ge=60)  # RUNNING дольше — аренда потеряна, задание повторяется
    index_export_cache_root: Path = Path("/var/lib/aicd/index-export")
    index_export_keep: int = Field(default=8, ge=1, le=1000)
    index_prune_interval_seconds: float = Field(default=3600.0, gt=0)
    ensure_index_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
