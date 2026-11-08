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
