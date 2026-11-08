"""Доставка ACP потребителям (AGENT_COMMUNICATION_PROTOCOL §7): at-least-once, идемпотентный эффект.

Протокол потребителя — одна транзакция: `SELECT … FOR UPDATE` строки `message_deliveries(message_id,
consumer_id)` → если `acked_at` не NULL — no-op → эффект → `acked_at = now()`. Повторная доставка
(дубль Dramatiq-сообщения, повтор после падения) поэтому даёт ровно один эффект: строка доставки
уникальна (`PRIMARY KEY (message_id, consumer_id)`), а эффект и ack фиксируются атомарно.

Ошибка эффекта откатывается до savepoint; `delivery_count += 1`, `last_error` (редактированный),
`next_attempt_at` по расписанию 1 с, 5 с, 30 с, 2 мин, 10 мин, 30 мин, 1 ч, 1 ч; после 8 неудач —
`dead_lettered_at` и вызов `on_dead_letter` (публикация в очередь `acp.dlq`). Replay — `replay()`.

RLS: `message_deliveries` — таблица workspace, поэтому обработка идёт в контексте workspace
(из Dramatiq-сообщения outbox или по списку workspace для периодического прохода `deliver_pending`).
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_collaboration.acp.facts import StoredMessage
from aicd_collaboration.acp.sql import MESSAGE_COLUMNS, stored_message
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_platform.redaction import redact_text

log = logging.getLogger("aicd.acp.delivery")

BACKOFF_SECONDS: tuple[int, ...] = (1, 5, 30, 120, 600, 1800, 3600, 3600)
MAX_ATTEMPTS = len(BACKOFF_SECONDS)
MAX_ERROR_LENGTH = 2000
DLQ_QUEUE = "acp.dlq"


@dataclass(frozen=True)
class Delivery:
    workspace_id: uuid.UUID
    consumer_id: str
    delivery_count: int  # число попыток до текущей
    message: StoredMessage


Handler = Callable[[AsyncConnection, Delivery], Awaitable[None]]
DeadLetter = Callable[[AsyncConnection, Delivery, str], Awaitable[None]]


class DeliveryStatus(enum.StrEnum):
    ACKED = "ACKED"
    ALREADY_ACKED = "ALREADY_ACKED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DEAD_LETTERED = "DEAD_LETTERED"
    NOT_DUE = "NOT_DUE"
    NOT_FOUND = "NOT_FOUND"


def backoff_seconds(failures: int) -> int:
    """Задержка после `failures` неудач (1-based), §7."""
    return BACKOFF_SECONDS[min(max(failures, 1), MAX_ATTEMPTS) - 1]


async def _locked(conn: AsyncConnection, message_id: uuid.UUID, consumer_id: str) -> Mapping[str, Any] | None:
    row = (
        await conn.execute(
            text(
                "SELECT workspace_id, delivery_count, acked_at, dead_lettered_at, next_attempt_at"
                " FROM message_deliveries WHERE message_id = :m AND consumer_id = :c FOR UPDATE"
            ),
            {"m": message_id, "c": consumer_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def process_in(
    conn: AsyncConnection,
    *,
    message_id: uuid.UUID,
    consumer_id: str,
    handler: Handler,
    on_dead_letter: DeadLetter | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> DeliveryStatus:
    """Одна доставка в транзакции вызывающего (RLS-контекст workspace сообщения)."""
    row = await _locked(conn, message_id, consumer_id)
    if row is None:
        return DeliveryStatus.NOT_FOUND
    if row["acked_at"] is not None:
        return DeliveryStatus.ALREADY_ACKED
    if row["dead_lettered_at"] is not None:
        return DeliveryStatus.DEAD_LETTERED
    moment = now or utcnow()
    if not force and row["next_attempt_at"] is not None and row["next_attempt_at"] > moment:
        return DeliveryStatus.NOT_DUE
    msg_row = (
        await conn.execute(text(f"SELECT {MESSAGE_COLUMNS} FROM agent_messages WHERE id = :m"), {"m": message_id})  # noqa: S608
    ).first()
    if msg_row is None:  # pragma: no cover — FK гарантирует наличие
        return DeliveryStatus.NOT_FOUND
    delivery = Delivery(row["workspace_id"], consumer_id, int(row["delivery_count"]), stored_message(msg_row))
    attempts = delivery.delivery_count + 1
    try:
        async with conn.begin_nested():
            await handler(conn, delivery)
    except Exception as exc:  # noqa: BLE001 — любая ошибка эффекта ⇒ повтор с backoff / DLQ
        error = redact_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_LENGTH]
        dead = attempts >= MAX_ATTEMPTS
        log.warning(
            "acp delivery failed",
            extra={"message_id": str(message_id), "consumer": consumer_id, "attempt": attempts, "dead": dead},
        )
        await conn.execute(
            text(
                "UPDATE message_deliveries SET delivery_count = :n, last_error = :e,"
                " first_delivered_at = coalesce(first_delivered_at, :now), last_delivered_at = :now,"
                " next_attempt_at = CASE WHEN :dead THEN NULL ELSE :next END,"
                " dead_lettered_at = CASE WHEN :dead THEN :now ELSE NULL END"
                " WHERE message_id = :m AND consumer_id = :c"
            ),
            {
                "n": attempts,
                "e": error,
                "now": moment,
                "dead": dead,
                "next": moment + timedelta(seconds=backoff_seconds(attempts)),
                "m": message_id,
                "c": consumer_id,
            },
        )
        if dead:
            if on_dead_letter is not None:
                await on_dead_letter(conn, delivery, error)
            return DeliveryStatus.DEAD_LETTERED
        return DeliveryStatus.RETRY_SCHEDULED
    await conn.execute(
        text(
            "UPDATE message_deliveries SET delivery_count = :n, acked_at = :now, next_attempt_at = NULL,"
            " first_delivered_at = coalesce(first_delivered_at, :now), last_delivered_at = :now"
            " WHERE message_id = :m AND consumer_id = :c"
        ),
        {"n": attempts, "now": moment, "m": message_id, "c": consumer_id},
    )
    return DeliveryStatus.ACKED


async def process_delivery(
    engine: AsyncEngine,
    *,
    workspace_id: uuid.UUID,
    message_id: uuid.UUID,
    consumer_id: str,
    handler: Handler,
    on_dead_letter: DeadLetter | None = None,
    force: bool = False,
) -> DeliveryStatus:
    """Доставка одного сообщения потребителю в отдельной транзакции (вызов из Dramatiq-актора)."""
    async with transaction(engine, DbContext(workspace_id=workspace_id, actor_type="system")) as conn:
        return await process_in(
            conn,
            message_id=message_id,
            consumer_id=consumer_id,
            handler=handler,
            on_dead_letter=on_dead_letter,
            force=force,
        )


async def handle_outbox_event(
    engine: AsyncEngine,
    event: Mapping[str, Any],
    *,
    consumer_id: str,
    handler: Handler,
    on_dead_letter: DeadLetter | None = None,
) -> DeliveryStatus | None:
    """Тело Dramatiq-сообщения outbox (`OutboxRecord.message()`, тип `agent.message`) → доставка потребителю."""
    if event.get("event_type") != "agent.message" or not event.get("workspace_id"):
        return None
    payload = event.get("payload") or {}
    consumers = payload.get("consumers")
    if isinstance(consumers, list) and consumer_id not in consumers:
        return None
    return await process_delivery(
        engine,
        workspace_id=uuid.UUID(str(event["workspace_id"])),
        message_id=uuid.UUID(str(payload.get("message_id") or event["aggregate_id"])),
        consumer_id=consumer_id,
        handler=handler,
        on_dead_letter=on_dead_letter,
    )


async def deliver_pending(
    engine: AsyncEngine,
    *,
    workspace_id: uuid.UUID,
    consumer_id: str,
    handler: Handler,
    batch: int = 100,
    on_dead_letter: DeadLetter | None = None,
) -> dict[DeliveryStatus, int]:
    """Периодический проход: ожидающие доставки потребителя в workspace (новые и с наступившим backoff)."""
    async with transaction(engine, DbContext(workspace_id=workspace_id, actor_type="system")) as conn:
        ids = (
            (
                await conn.execute(
                    text(
                        "SELECT d.message_id FROM message_deliveries d JOIN agent_messages m ON m.id = d.message_id"
                        " WHERE d.consumer_id = :c AND d.acked_at IS NULL AND d.dead_lettered_at IS NULL"
                        " AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= now())"
                        " ORDER BY m.created_at, m.id LIMIT :n"
                    ),
                    {"c": consumer_id, "n": batch},
                )
            )
            .scalars()
            .all()
        )
    counts: dict[DeliveryStatus, int] = {}
    for message_id in ids:
        status = await process_delivery(
            engine,
            workspace_id=workspace_id,
            message_id=message_id,
            consumer_id=consumer_id,
            handler=handler,
            on_dead_letter=on_dead_letter,
        )
        counts[status] = counts.get(status, 0) + 1
    return counts


async def ack(conn: AsyncConnection, *, message_id: uuid.UUID, consumer_id: str) -> bool:
    """Явное подтверждение (эффект выполнен вне `process_*`, например включение в Context Pack).

    True — подтверждено сейчас; False — уже подтверждено, в DLQ или строки нет (повтор — no-op).
    """
    done = (
        await conn.execute(
            text(
                "UPDATE message_deliveries SET acked_at = now(), delivery_count = greatest(delivery_count, 1),"
                " first_delivered_at = coalesce(first_delivered_at, now()), last_delivered_at = now(),"
                " next_attempt_at = NULL"
                " WHERE message_id = :m AND consumer_id = :c AND acked_at IS NULL AND dead_lettered_at IS NULL"
                " RETURNING message_id"
            ),
            {"m": message_id, "c": consumer_id},
        )
    ).first()
    return done is not None


async def replay(conn: AsyncConnection, *, message_id: uuid.UUID, consumer_id: str) -> bool:
    """Admin replay из DLQ (§7): строка снова ожидает доставки; безопасно благодаря идемпотентности."""
    done = (
        await conn.execute(
            text(
                "UPDATE message_deliveries SET dead_lettered_at = NULL, next_attempt_at = NULL, delivery_count = 0"
                " WHERE message_id = :m AND consumer_id = :c AND dead_lettered_at IS NOT NULL RETURNING message_id"
            ),
            {"m": message_id, "c": consumer_id},
        )
    ).first()
    return done is not None


def dlq_publisher(publish: Callable[[dict[str, Any]], Awaitable[None]]) -> DeadLetter:
    """`on_dead_letter`, публикующий запись DLQ (очередь `acp.dlq`) через транспорт воркера."""

    async def _dead(conn: AsyncConnection, delivery: Delivery, error: str) -> None:
        await publish(
            {
                "queue": DLQ_QUEUE,
                "workspace_id": str(delivery.workspace_id),
                "message_id": str(delivery.message.id),
                "consumer_id": delivery.consumer_id,
                "run_id": str(delivery.message.run_id),
                "error": error,
            }
        )

    return _dead
