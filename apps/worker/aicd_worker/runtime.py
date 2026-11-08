"""Среда исполнения акторов: event loop с движком БД и оркестратором.

Dramatiq вызывает акторы в своих потоках; асинхронная работа (SQLAlchemy async, оркестратор) выполняется
в одном event loop процесса, к которому привязан пул соединений. Актор — тонкая синхронная обёртка,
передающая корутину в этот loop и ожидающая результат.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine


class OrchestratorPort(Protocol):
    """Подмножество `aicd_orchestration.engine.Orchestrator`, которое вызывают акторы."""

    async def on_run_created(self, run_id: uuid.UUID, ws: uuid.UUID) -> None: ...

    async def on_plan_approval_decided(
        self, run_id: uuid.UUID, ws: uuid.UUID, *, approved: bool, plan_version: int
    ) -> None: ...

    async def on_run_paused_or_cancelled(self, run_id: uuid.UUID, ws: uuid.UUID, status: str) -> None: ...

    async def on_run_resumed(self, run_id: uuid.UUID, ws: uuid.UUID) -> None: ...

    async def tick(self, run_id: uuid.UUID, ws: uuid.UUID) -> list[uuid.UUID]: ...

    async def execute_task(self, task_id: uuid.UUID, ws: uuid.UUID) -> None: ...


@dataclass
class Runtime:
    engine: AsyncEngine
    orchestrator: OrchestratorPort
    loop: asyncio.AbstractEventLoop
    # Постановка исполнения задачи в очередь exec (в тестах — прямой вызов).
    dispatch_execution: Callable[[dict[str, Any]], Awaitable[None]]
    stale_ready_seconds: int = 30


_current: Runtime | None = None


def install(runtime: Runtime | None) -> None:
    global _current
    _current = runtime


def current() -> Runtime:
    if _current is None:
        raise RuntimeError("runtime воркера не инициализирован")
    return _current


def submit[T](make: Callable[[Runtime], Coroutine[Any, Any, T]]) -> T:
    """Выполняет корутину в loop runtime из потока Dramatiq и возвращает результат (исключения пробрасываются)."""
    rt = current()
    future = asyncio.run_coroutine_threadsafe(make(rt), rt.loop)
    return future.result()
