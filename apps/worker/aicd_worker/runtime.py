"""Среда исполнения акторов: event loop с движком БД и оркестратором.

Dramatiq вызывает акторы в своих потоках; асинхронная работа (SQLAlchemy async, оркестратор) выполняется
в одном event loop процесса, к которому привязан пул соединений. Актор — тонкая синхронная обёртка,
передающая корутину в этот loop и ожидающая результат.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.models import ProbeResult
from aicd_code_intelligence.docs.importer import DocumentationImporter
from aicd_orchestration.github_flow import GitHubWorkflow
from aicd_orchestration.recovery import Reconciler
from aicd_platform import metrics
from aicd_platform.telemetry import span


class OrchestratorPort(Protocol):
    """Подмножество `aicd_orchestration.engine.Orchestrator`, которое вызывают акторы."""

    async def on_run_created(self, run_id: uuid.UUID, ws: uuid.UUID) -> None: ...

    async def on_plan_approval_decided(
        self, run_id: uuid.UUID, ws: uuid.UUID, *, approved: bool, plan_version: int
    ) -> None: ...

    async def on_task_approval_decided(self, approval_id: uuid.UUID, ws: uuid.UUID) -> None: ...

    async def on_run_paused_or_cancelled(self, run_id: uuid.UUID, ws: uuid.UUID, status: str) -> None: ...

    async def on_run_resumed(self, run_id: uuid.UUID, ws: uuid.UUID) -> None: ...

    async def tick(self, run_id: uuid.UUID, ws: uuid.UUID) -> list[uuid.UUID]: ...

    async def execute_task(self, task_id: uuid.UUID, ws: uuid.UUID) -> None: ...

    async def on_docs_available(self, ws: uuid.UUID, source_id: uuid.UUID) -> list[uuid.UUID]: ...


@dataclass
class Runtime:
    engine: AsyncEngine
    orchestrator: OrchestratorPort
    loop: asyncio.AbstractEventLoop
    # Постановка исполнения задачи в очередь exec (в тестах — прямой вызов).
    dispatch_execution: Callable[[dict[str, Any]], Awaitable[None]]
    stale_ready_seconds: int = 30
    # Recovery reconciler хоста исполнения (только у воркера exec: процессы и worktrees — его).
    reconciler: Reconciler | None = None
    # Импортёр документации (очередь docs; в тестах — с тестовым SsrfFetcher).
    docs_importer: DocumentationImporter | None = None
    # Шаг поручения пользователя (workspace_id, directive_id): impact analysis / apply (только у orchestrator).
    process_directive: Callable[[uuid.UUID, uuid.UUID], Awaitable[None]] | None = None
    # Проверка установки CLI провайдера на этом хосте (только у exec): код провайдера → ProbeResult.
    provider_prober: Callable[[str], Awaitable[ProbeResult]] | None = None
    # Хост исполнения (worker_hosts.id) — установки CLI записываются на него (только у exec).
    worker_host_id: uuid.UUID | None = None
    # Исполнитель действий GitHub (очередь github, только у orchestrator и только при настроенном GitHub App).
    github: GitHubWorkflow | None = None


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


async def traced[T](actor: str, message: Mapping[str, Any], work: Coroutine[Any, Any, T]) -> T:
    """Обработка сообщения Dramatiq в спане, продолжающем трассу отправителя (`message["traceparent"]`, P8-06),
    с метриками числа/длительности обработки по актору и исходу."""
    started = time.monotonic()
    attributes = {
        "aicd.event_id": message.get("event_id"),
        "aicd.event_type": message.get("event_type"),
        "aicd.workspace_id": message.get("workspace_id"),
        "aicd.run_id": message.get("run_id"),
        "aicd.task_id": message.get("task_id"),
        "messaging.system": "dramatiq",
        "messaging.destination.name": actor,
    }
    traceparent = message.get("traceparent")
    outcome = "error"
    try:
        with span(
            f"worker {actor}",
            traceparent=traceparent if isinstance(traceparent, str) else None,
            attributes=attributes,
            kind=SpanKind.CONSUMER,
        ) as current:
            result = await work
            outcome = metrics.label(result) if isinstance(result, str) else "ok"
            current.set_attribute("aicd.outcome", outcome)
            return result
    finally:
        metrics.WORKER_MESSAGES.labels(actor, outcome).inc()
        metrics.WORKER_MESSAGE_DURATION.labels(actor).observe(time.monotonic() - started)
