"""Идемпотентные потребители событий (ADR-0002): `processed_events(consumer, event_id)`."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def claim(conn: AsyncConnection, consumer: str, event_id: uuid.UUID) -> bool:
    """True — событие обрабатывается впервые (отметка в той же транзакции, что и эффект обработки).

    Повторная доставка (at-least-once) вернёт False, и обработчик не должен выполнять эффект.
    """
    inserted = (
        await conn.execute(
            text(
                "INSERT INTO processed_events (consumer, event_id) VALUES (:c, :e)"
                " ON CONFLICT DO NOTHING RETURNING event_id"
            ),
            {"c": consumer, "e": event_id},
        )
    ).scalar_one_or_none()
    return inserted is not None
