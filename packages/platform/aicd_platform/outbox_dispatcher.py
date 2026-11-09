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
5. после commit — `notify(run_id)` (Redis pub/sub: только сигнал пробуждения SSE), `notify_workspace` для
   workspace с новыми строками журнала или событиями `sse.WORKSPACE_SIGNAL_TYPES` (`notification.created`) и
   доменные метрики опубликованных событий (`metrics.observe_outbox_event`, P8-06).

Трассировка (P8-06): публикация каждого события — спан `outbox.publish`, дочерний к `traceparent` события
(спан, записавший событие); в Dramatiq-сообщение уходит `traceparent` спана публикации.

Гарантия — at-least-once: падение между publish и commit приводит к повторной публикации.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_platform import metrics, sse
from aicd_platform.db import DbContext, apply_context
from aicd_platform.redaction import redact_text
from aicd_platform.telemetry import current_traceparent, span

log = logging.getLogger("aicd.outbox")

Publish = Callable[[dict[str, Any]], Awaitable[None]]
Notify = Callable[[uuid.UUID], Awaitable[None]]

BACKOFF_BASE_SECONDS = 1
BACKOFF_MAX_SECONDS = 300
MAX_ERROR_LENGTH = 2000

_COLUMNS = (
    "id, created_at, workspace_id, run_id, aggregate_type, aggregate_id, event_type, schema_version, payload,"
    " correlation_id, causation_id, publish_attempts, traceparent"
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
        traceparent=m["traceparent"],
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


async def dispatch_batch(
    engine: AsyncEngine, publish: Publish, notify: Notify, batch: int = 100, *, notify_workspace: Notify | None = None
) -> int:
    """Обрабатывает до `batch` событий; возвращает число успешно опубликованных.

    `notify_workspace` — сигнал потоку уведомлений workspace (`GET /workspaces/{id}/events`) для каждого workspace,
    в журнал которого попали события (OQ-45 (c))."""
    published = failures = 0
    delivered: list[sse.OutboxRecord] = []
    touched_runs: set[uuid.UUID] = set()
    touched_workspaces: set[uuid.UUID] = set()
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
            attributes = {
                "aicd.event_id": event.id,
                "aicd.event_type": event.event_type,
                "aicd.workspace_id": event.workspace_id,
                "aicd.run_id": event.run_id,
                "messaging.system": "dramatiq",
            }
            with span("outbox.publish", traceparent=event.traceparent, attributes=attributes) as current:
                try:
                    async with conn.begin_nested():
                        seq = await append_run_event(conn, event)
                except (SQLAlchemyError, LookupError, ValueError, TypeError) as exc:
                    log.exception("run_event_log projection failed", extra={"event_id": str(event.id)})
                    current.set_attribute("aicd.outcome", "projection_failed")
                    failures += 1
                    await _fail(conn, event, exc)
                    continue
                if seq is not None and event.run_id is not None:
                    touched_runs.add(event.run_id)
                    if event.workspace_id is not None:
                        touched_workspaces.add(event.workspace_id)
                try:
                    await publish(replace(event, traceparent=current_traceparent() or event.traceparent).message())
                except Exception as exc:  # noqa: BLE001 — любая ошибка транспорта ⇒ повтор с backoff
                    log.warning("outbox publish failed", extra={"event_id": str(event.id), "error": type(exc).__name__})
                    current.set_attribute("aicd.outcome", "publish_failed")
                    failures += 1
                    await _fail(conn, event, exc)
                    continue
                await conn.execute(
                    text("UPDATE outbox_events SET published_at = now() WHERE id = :id AND created_at = :c"),
                    {"id": event.id, "c": event.created_at},
                )
                current.set_attribute("aicd.outcome", "published")
                delivered.append(event)
                if event.event_type in sse.WORKSPACE_SIGNAL_TYPES and event.workspace_id is not None:
                    touched_workspaces.add(event.workspace_id)  # уведомление пользователя: сигнал потоку workspace
                published += 1
    _observe(delivered, failures)
    for run_id in sorted(touched_runs):
        try:
            await notify(run_id)
        except Exception:  # noqa: BLE001 — потеря сигнала компенсируется опросом SSE
            log.warning("sse notify failed", extra={"run_id": str(run_id)})
    if notify_workspace is not None:
        for workspace_id in sorted(touched_workspaces):
            try:
                await notify_workspace(workspace_id)
            except Exception:  # noqa: BLE001 — потеря сигнала компенсируется опросом SSE
                log.warning("sse workspace notify failed", extra={"workspace_id": str(workspace_id)})
    return published


def _observe(delivered: list[sse.OutboxRecord], failures: int) -> None:
    """Метрики после commit: опубликованные события, queue lag, ошибки публикации."""
    if failures:
        metrics.OUTBOX_FAILURES.inc(failures)
    now = datetime.now(UTC)
    for event in delivered:
        metrics.OUTBOX_LAG.observe(max((now - event.created_at).total_seconds(), 0.0))
        try:
            metrics.observe_outbox_event(event.event_type, event.payload)
        except Exception:  # noqa: BLE001 — сбой метрики не должен ломать доставку
            log.warning("outbox metric failed", extra={"event_type": event.event_type})


async def sample_gauges(engine: AsyncEngine) -> None:
    """Периодические gauge-метрики worker-outbox (системный контекст, глобальные/служебные таблицы):
    backlog outbox, истёкшие аренды (stuck leases), размер БД. Доступность провайдеров — модуль cli_adapters."""
    started = time.monotonic()
    async with engine.begin() as conn:
        await apply_context(conn, DbContext(actor_type="system"))
        pending, oldest = (
            await conn.execute(
                text(
                    "SELECT count(*), COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0)"
                    " FROM outbox_events WHERE published_at IS NULL"
                )
            )
        ).one()
        metrics.OUTBOX_PENDING.set(int(pending))
        metrics.OUTBOX_OLDEST_AGE.set(float(oldest))
        expired: dict[str, int] = dict(
            (
                await conn.execute(
                    text(
                        "SELECT resource_type, count(*) FROM worker_leases"
                        " WHERE released_at IS NULL AND lease_until < now() GROUP BY resource_type"
                    )
                )
            )
            .tuples()
            .all()
        )
        for resource_type in LEASE_RESOURCE_TYPES:
            metrics.LEASES_EXPIRED.labels(resource_type).set(int(expired.get(resource_type, 0)))
        size = (await conn.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
        metrics.DATABASE_SIZE.set(int(size))
    log.debug("gauges sampled", extra={"duration_ms": round((time.monotonic() - started) * 1000, 1)})


LEASE_RESOURCE_TYPES = (
    "task",
    "run",
    "cli_execution",
    "indexing_job",
    "external_action",
    "outbox_partition",
    "recovery",
)
