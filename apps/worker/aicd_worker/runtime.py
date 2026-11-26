"""Среда исполнения акторов: event loop с движком БД и оркестратором.

Dramatiq вызывает акторы в своих потоках; асинхронная работа (SQLAlchemy async, оркестратор) выполняется
в одном event loop процесса, к которому привязан пул соединений. Актор — тонкая синхронная обёртка,
передающая корутину в этот loop и ожидающая результат.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.login import LoginEvent
from aicd_cli_adapters.models import ProbeResult
from aicd_code_intelligence.docs.importer import DocumentationImporter
from aicd_orchestration.github_flow import GitHubWorkflow
from aicd_orchestration.github_repositories import GitHubRepositorySync
from aicd_orchestration.recovery import Reconciler
from aicd_platform import metrics
from aicd_platform.telemetry import span

#: Запуск интерактивного входа CLI: `(provider, cancelled, code_input) -> поток событий входа`.
#: `code_input` — неблокирующее чтение кода авторизации, введённого оператором в UI (может быть None).
CodeInput = Callable[[], Awaitable[str | None]]
ProviderLoginPort = Callable[[str, Callable[[], Awaitable[bool]], CodeInput | None], AsyncIterator[LoginEvent]]
#: Подписка на коды авторизации сессии входа (pub/sub Redis, `aicd_platform.login_input.inbox`).
CodeInbox = Callable[[uuid.UUID], AbstractAsyncContextManager[CodeInput]]


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
    # Интерактивный вход CLI по подписке (POST /providers/{id}/login, только у exec): запускает официальную
    # команду входа в PTY и отдаёт события (строки вывода, device code, итог). Второй аргумент — предикат
    # отмены: воркер опрашивает им состояние сессии в БД.
    provider_login: ProviderLoginPort | None = None
    # Канал кода авторизации из UI в процесс входа (только у exec). Код нигде не сохраняется:
    # pub/sub Redis, подписка живёт ровно столько, сколько идёт сессия входа (`login_input`).
    login_code_inbox: CodeInbox | None = None
    # Лимиты подписки CLI (только у exec): чтение у самого CLI по запросу оператора и запись наблюдения
    # (Redis, `aicd_cli_adapters.limits`). Наблюдения приходят и попутно — из вывода исполнений задач.
    limits_reader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None
    limits_sink: Callable[[uuid.UUID, dict[str, Any]], Awaitable[None]] | None = None
    # Хост исполнения (worker_hosts.id) — установки CLI записываются на него (только у exec).
    worker_host_id: uuid.UUID | None = None
    # Исполнитель действий GitHub (очередь github, только у orchestrator и только при настроенном GitHub App).
    github: GitHubWorkflow | None = None
    # Чтение состава установки GitHub App для формы привязки (там же). Список кэшируется в Redis,
    # в БД не хранится: это проекция внешнего состояния GitHub (`aicd_integrations_github.repo_cache`).
    github_repositories: GitHubRepositorySync | None = None


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
