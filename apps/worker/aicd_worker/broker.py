"""Dramatiq: брокер и акторы (ADR-0002 п.3). Redis — только транспорт; состояние — в PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from aicd_worker import docs, github, handlers, notifications, runtime

ORCHESTRATOR_QUEUE = "orchestrator"
EXEC_QUEUE = "exec"
DOCS_QUEUE = "docs"  # загрузка документации (обслуживает воркер orchestrator вместе со своей очередью)
GITHUB_QUEUE = "github"  # PR workflow, merge после approval, вебхуки (воркер orchestrator, P8-03…P8-05)
NOTIFICATIONS_QUEUE = "notifications"  # персональные уведомления (воркер orchestrator, UI redesign B8)


def build_broker(redis_url: str | None, *, stub: bool = False) -> dramatiq.Broker:
    """Redis-брокер из настроек; `stub=True` — StubBroker для тестов (без Redis)."""
    if stub:
        broker: dramatiq.Broker = StubBroker()
    else:
        broker = RedisBroker(url=redis_url, namespace="aicd-dramatiq")  # type: ignore[no-untyped-call]
    return broker


@dataclass(frozen=True)
class Actors:
    broker: dramatiq.Broker
    handle_event: dramatiq.Actor[[dict[str, Any]], None]
    execute_task: dramatiq.Actor[[dict[str, Any]], None]
    import_documentation: dramatiq.Actor[[dict[str, Any]], None]
    probe_provider: dramatiq.Actor[[dict[str, Any]], None]
    github_event: dramatiq.Actor[[dict[str, Any]], None] | None = None
    notify: dramatiq.Actor[[dict[str, Any]], None] | None = None

    async def publish(self, event: dict[str, Any]) -> None:
        """Публикация outbox-события (для `dispatch_batch`): события run нужных типов — в очередь оркестратора,
        запросы загрузки документации (без run) — в очередь docs, перепроверка провайдера — в очередь exec."""
        if self.github_event is not None and github.is_github_event(event):
            await asyncio.to_thread(self.github_event.send, event)  # approval.decided(MERGE) — и в оркестратор
        if self.notify is not None and notifications.is_notification_source(event):
            await asyncio.to_thread(self.notify.send, event)  # уведомления — в дополнение к основной маршрутизации
        if event.get("event_type") in handlers.DOCS_EVENT_TYPES:
            await asyncio.to_thread(self.import_documentation.send, event)
        elif event.get("event_type") in handlers.PROBE_EVENT_TYPES:
            await asyncio.to_thread(self.probe_provider.send, event)
        elif event.get("run_id") and event.get("event_type") in handlers.ORCHESTRATOR_EVENT_TYPES:
            await asyncio.to_thread(self.handle_event.send, event)

    async def dispatch_execution(self, message: dict[str, Any]) -> None:
        await asyncio.to_thread(self.execute_task.send, message)


def _handle_event(event: dict[str, Any]) -> None:
    runtime.submit(lambda rt: runtime.traced(handlers.ORCHESTRATOR_CONSUMER, event, handlers.handle_event(rt, event)))


def _execute_task(message: dict[str, Any]) -> None:
    runtime.submit(lambda rt: runtime.traced(handlers.EXEC_CONSUMER, message, handlers.execute_task(rt, message)))


def _import_documentation(event: dict[str, Any]) -> None:
    runtime.submit(lambda rt: runtime.traced(docs.DOCS_CONSUMER, event, docs.import_documentation(rt, event)))


def _probe_provider(event: dict[str, Any]) -> None:
    runtime.submit(lambda rt: runtime.traced(handlers.PROBE_CONSUMER, event, handlers.probe_provider(rt, event)))


def _github_event(event: dict[str, Any]) -> None:
    runtime.submit(lambda rt: runtime.traced(github.GITHUB_CONSUMER, event, github.handle_event(rt, event)))


def _notify(event: dict[str, Any]) -> None:
    runtime.submit(
        lambda rt: runtime.traced(notifications.NOTIFICATIONS_CONSUMER, event, notifications.handle_event(rt, event))
    )


def register_actors(broker: dramatiq.Broker, *, exec_time_limit_seconds: int = 5 * 3600) -> Actors:
    handle_event: dramatiq.Actor[[dict[str, Any]], None] = dramatiq.actor(
        actor_name=handlers.ORCHESTRATOR_CONSUMER,
        queue_name=ORCHESTRATOR_QUEUE,
        broker=broker,
        max_retries=10,
        min_backoff=500,
        max_backoff=60_000,
        time_limit=10 * 60_000,
    )(_handle_event)
    execute_task: dramatiq.Actor[[dict[str, Any]], None] = dramatiq.actor(
        actor_name=handlers.EXEC_CONSUMER,
        queue_name=EXEC_QUEUE,
        broker=broker,
        max_retries=3,
        min_backoff=1_000,
        max_backoff=60_000,
        time_limit=exec_time_limit_seconds * 1000,
    )(_execute_task)
    import_documentation: dramatiq.Actor[[dict[str, Any]], None] = dramatiq.actor(
        actor_name=docs.DOCS_CONSUMER,
        queue_name=DOCS_QUEUE,
        broker=broker,
        max_retries=5,
        min_backoff=5_000,
        max_backoff=300_000,
        time_limit=15 * 60_000,
    )(_import_documentation)
    probe_provider: dramatiq.Actor[[dict[str, Any]], None] = dramatiq.actor(
        actor_name=handlers.PROBE_CONSUMER,
        queue_name=EXEC_QUEUE,
        broker=broker,
        max_retries=3,
        min_backoff=5_000,
        max_backoff=60_000,
        time_limit=5 * 60_000,
    )(_probe_provider)
    github_event: dramatiq.Actor[[dict[str, Any]], None] = dramatiq.actor(
        actor_name=github.GITHUB_CONSUMER,
        queue_name=GITHUB_QUEUE,
        broker=broker,
        max_retries=8,
        min_backoff=5_000,
        max_backoff=600_000,
        time_limit=15 * 60_000,
    )(_github_event)
    notify: dramatiq.Actor[[dict[str, Any]], None] = dramatiq.actor(
        actor_name=notifications.NOTIFICATIONS_CONSUMER,
        queue_name=NOTIFICATIONS_QUEUE,
        broker=broker,
        max_retries=8,
        min_backoff=1_000,
        max_backoff=120_000,
        time_limit=2 * 60_000,
    )(_notify)
    return Actors(broker, handle_event, execute_task, import_documentation, probe_provider, github_event, notify)
