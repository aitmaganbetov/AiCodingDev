"""Dramatiq: брокер и акторы (ADR-0002 п.3). Redis — только транспорт; состояние — в PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from aicd_worker import handlers, runtime

ORCHESTRATOR_QUEUE = "orchestrator"
EXEC_QUEUE = "exec"


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

    async def publish(self, event: dict[str, Any]) -> None:
        """Публикация outbox-события (для `dispatch_batch`): события run нужных типов — в очередь оркестратора."""
        if event.get("run_id") and event.get("event_type") in handlers.ORCHESTRATOR_EVENT_TYPES:
            await asyncio.to_thread(self.handle_event.send, event)

    async def dispatch_execution(self, message: dict[str, Any]) -> None:
        await asyncio.to_thread(self.execute_task.send, message)


def _handle_event(event: dict[str, Any]) -> None:
    runtime.submit(lambda rt: handlers.handle_event(rt, event))


def _execute_task(message: dict[str, Any]) -> None:
    runtime.submit(lambda rt: handlers.execute_task(rt, message))


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
    return Actors(broker, handle_event, execute_task)
