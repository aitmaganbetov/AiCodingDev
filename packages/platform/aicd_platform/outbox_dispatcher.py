"""Диспетчер transactional outbox (ADR-0002, ADR-0011, EVENT_SCHEMAS §3.3): outbox → run_event_log + Dramatiq.

Один вызов `dispatch_batch` — одна транзакция под ролью `aicd_worker`:

1. события одного run обрабатывает только один диспетчер: per-run advisory lock (`pg_try_advisory_xact_lock`)
   берётся до выборки, события run без блокировки пропускаются (их заберёт владелец блокировки) — порядок
   `seq` совпадает с порядком outbox в пределах run;
2. `SELECT … FOR UPDATE SKIP LOCKED` неопубликованных событий (`available_at <= now()`);
3. для SSE-типов — проекция в `run_event_log` с `seq = runs.last_event_seq + 1` (строка runs блокируется до
   commit, поэтому порядок фиксации совпадает с порядком seq и live-читатель SSE не видит «дыр»);
   повторная доставка не создаёт дубль (проверка `event_id`);
4. `publish(event)` (Dramatiq); ошибка ⇒ `publish_attempts + 1`, экспоненциальный `available_at`,
   `last_error` (редактированный); успех ⇒ `published_at = now()`;
5. после commit — `notify(run_id)` (Redis pub/sub: только сигнал пробуждения SSE).

Гарантия — at-least-once: падение между publish и commit приводит к повторной публикации.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_platform import sse
from aicd_platform.db import DbContext, apply_context
from aicd_platform.redaction import redact_text

log = logging.getLogger("aicd.outbox")

Publish = Callable[[dict[str, Any]], Awaitable[None]]
Notify = Callable[[uuid.UUID], Awaitable[None]]

BACKOFF_BASE_SECONDS = 1
BACKOFF_MAX_SECONDS = 300
MAX_ERROR_LENGTH = 2000

_COLUMNS = (
    "id, created_at, workspace_id, run_id, aggregate_type, aggregate_id, event_type, schema_version, payload,"
    " correlation_id, causation_id, publish_attempts"
)


def backoff_seconds(attempts: int) -> int:
    """Задержка повторной публикации после `attempts` неудач: 1, 2, 4, … ≤ 300 с."""
    return int(min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS))


def _record(row: Any) -> sse.OutboxRecord:
    m = row._mapping
    return sse.OutboxRecord(
        id=m["id"],
        created_at=m["created_at"],
        workspace_id=m["workspace_id"],
        run_id=m["run_id"],
        aggregate_type=m["aggregate_type"],
        aggregate_id=m["aggregate_id"],
        event_type=m["event_type"],
        schema_version=m["schema_version"],
        payload=dict(m["payload"]),
        correlation_id=m["correlation_id"],
        causation_id=m["causation_id"],
        publish_attempts=int(m["publish_attempts"]),
    )


async def _lock_runs(conn: AsyncConnection, batch: int) -> list[uuid.UUID]:
    candidates = (
        (
            await conn.execute(
                text(
                    "SELECT DISTINCT run_id FROM (SELECT run_id FROM outbox_events WHERE published_at IS NULL"
                    " AND available_at <= now() ORDER BY available_at, id LIMIT :n) s WHERE run_id IS NOT NULL"
                ),
                {"n": batch},
            )
        )
        .scalars()
        .all()
    )
    locked: list[uuid.UUID] = []
    for run_id in sorted(candidates):
        got = (
            await conn.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended('aicd.run_events:' || CAST(:r AS text), 0))"),
                {"r": run_id},
            )
        ).scalar_one()
        if got:
            locked.append(run_id)
    return locked


async def append_run_event(conn: AsyncConnection, event: sse.OutboxRecord) -> int | None:
    """Проекция события в run_event_log; возвращает seq (существующий при повторе) или None."""
    if event.run_id is None or event.workspace_id is None or event.event_type not in sse.PROJECTED_TYPES:
        return None
    await conn.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(event.workspace_id)})
    existing = (
        await conn.execute(text("SELECT seq FROM run_event_log WHERE event_id = :id"), {"id": event.id})
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)
    projection = await sse.project(conn, event)
    if projection is None:
        log.warning("event not projected to SSE", extra={"event_type": event.event_type, "event_id": str(event.id)})
        return None
    seq = (
        await conn.execute(
            text("UPDATE runs SET last_event_seq = last_event_seq + 1 WHERE id = :r RETURNING last_event_seq"),
            {"r": event.run_id},
        )
    ).scalar_one_or_none()
    if seq is None:
        raise LookupError(f"run {event.run_id} не найден для события {event.id}")
    await conn.execute(
        text(
            "INSERT INTO run_event_log (workspace_id, run_id, seq, event_id, event_type, schema_version, task_id,"
            " payload, occurred_at) VALUES (:ws, :r, :seq, :id, :t, :sv, :task, CAST(:p AS jsonb), :at)"
        ),
        {
            "ws": event.workspace_id,
            "r": event.run_id,
            "seq": seq,
            "id": event.id,
            "t": projection.event_type,
            "sv": event.schema_version,
            "task": projection.task_id,
            "at": event.created_at,
            "p": json.dumps(projection.payload, ensure_ascii=False, default=str),
        },
    )
    return int(seq)


async def _fail(conn: AsyncConnection, event: sse.OutboxRecord, exc: BaseException) -> None:
    attempts = event.publish_attempts + 1
    message = redact_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_LENGTH]
    await conn.execute(
        text(
            "UPDATE outbox_events SET publish_attempts = :a, last_error = :e,"
            " available_at = now() + make_interval(secs => :d) WHERE id = :id AND created_at = :c"
        ),
        {"a": attempts, "e": message, "d": backoff_seconds(attempts), "id": event.id, "c": event.created_at},
    )


async def dispatch_batch(engine: AsyncEngine, publish: Publish, notify: Notify, batch: int = 100) -> int:
    """Обрабатывает до `batch` событий; возвращает число успешно опубликованных."""
    published = 0
    touched_runs: set[uuid.UUID] = set()
    async with engine.begin() as conn:
        await apply_context(conn, DbContext(actor_type="system"))
        locked = await _lock_runs(conn, batch)
        rows = (
            await conn.execute(
                text(
                    f"SELECT {_COLUMNS} FROM outbox_events WHERE published_at IS NULL AND available_at <= now()"  # noqa: S608
                    " AND (run_id IS NULL OR run_id = ANY(:locked)) ORDER BY available_at, id LIMIT :n"
                    " FOR UPDATE SKIP LOCKED"
                ),
                {"locked": locked, "n": batch},
            )
        ).all()
        for row in rows:
            event = _record(row)
            try:
                async with conn.begin_nested():
                    seq = await append_run_event(conn, event)
            except (SQLAlchemyError, LookupError, ValueError, TypeError) as exc:
                log.exception("run_event_log projection failed", extra={"event_id": str(event.id)})
                await _fail(conn, event, exc)
                continue
            if seq is not None and event.run_id is not None:
                touched_runs.add(event.run_id)
            try:
                await publish(event.message())
            except Exception as exc:  # noqa: BLE001 — любая ошибка транспорта ⇒ повтор с backoff
                log.warning("outbox publish failed", extra={"event_id": str(event.id), "error": type(exc).__name__})
                await _fail(conn, event, exc)
                continue
            await conn.execute(
                text("UPDATE outbox_events SET published_at = now() WHERE id = :id AND created_at = :c"),
                {"id": event.id, "c": event.created_at},
            )
            published += 1
    for run_id in sorted(touched_runs):
        try:
            await notify(run_id)
        except Exception:  # noqa: BLE001 — потеря сигнала компенсируется опросом SSE
            log.warning("sse notify failed", extra={"run_id": str(run_id)})
    return published
