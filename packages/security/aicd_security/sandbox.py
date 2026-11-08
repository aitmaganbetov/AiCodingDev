"""Интерфейс изолированного исполнения процессов (ADR-0010, P2-07/P2-10).

Две реализации:
* `aicd_security.docker_runner.DockerSandboxRunner` — эфемерный runner-контейнер (по умолчанию);
* `aicd_security.local_runner.LocalProcessRunner` — процесс на хосте (только политика
  LOCAL_EXECUTION_FALLBACK + approval, а также тесты/Fake-адаптер).

Платформа передаёт команду только argv-массивом (никакого shell), окружение — явным allowlist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

StreamName = Literal["stdout", "stderr"]


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class ResourceLimits:
    cpus: float = 2.0
    memory_mb: int = 4096
    pids: int = 512
    timeout_seconds: int = 1800
    idle_timeout_seconds: int | None = None
    max_output_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class ContainerSpec:
    """Описание запуска. Для LocalProcessRunner image/mounts/network игнорируются, cwd обязателен."""

    image: str
    argv: tuple[str, ...]
    workdir: str
    env: dict[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    network: str = "none"  # имя docker-сети (sandbox, без прямого выхода) или "none"
    user: str = "10001:10001"
    labels: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None  # для локального исполнения


@dataclass(frozen=True)
class OutputChunk:
    stream: StreamName
    data: bytes


class SandboxProcess(Protocol):
    @property
    def ref(self) -> str:
        """Ссылка на процесс для reconciler: `docker:<container_id>` или `pid:<pgid>`."""
        ...

    def output(self) -> AsyncIterator[OutputChunk]:
        """Поток stdout/stderr до завершения процесса."""
        ...

    async def wait(self) -> int:
        """Код завершения (отрицательный — номер сигнала)."""
        ...

    async def terminate(self, grace_seconds: float = 15.0) -> None:
        """SIGTERM всей группе процессов / контейнеру, затем SIGKILL после grace."""
        ...


class SandboxRunner(Protocol):
    async def start(self, spec: ContainerSpec) -> SandboxProcess: ...

    async def is_alive(self, ref: str) -> bool:
        """Для recovery reconciler: жив ли процесс/контейнер по ссылке."""
        ...
