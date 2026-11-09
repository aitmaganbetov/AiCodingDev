"""SSE-поток run и история событий (ТЗ §8.3, API_DESIGN §13, ADR-0011, EVENT_SCHEMAS §3).

Данные всегда читаются из `run_event_log` (PostgreSQL); Redis pub/sub `aicd:run:<id>` — только сигнал
пробуждения, его потеря компенсируется опросом. `id:` = `seq`; возобновление — `Last-Event-ID`
(приоритет) или `?after_seq`; heartbeat без `id:`; проверка доступа при подключении и не реже раза в 60 с.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, Path, Query, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from starlette.responses import Response, StreamingResponse

from aicd_api.deps import Access, app_state, authorize, current_principal, workspace_access
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_api.sse_hub import SignalHub, Subscription
from aicd_collaboration import notifications
from aicd_identity import api as identity
from aicd_identity.rbac import Role, has_permission
from aicd_platform import audit, metrics, sse
from aicd_platform.clock import isoformat_z, utcnow
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

log = logging.getLogger("aicd.sse")

router = APIRouter(tags=["runs"])

SseEventType = Literal[
    "run.state_changed", "task.state_changed", "agent.message", "cli.output", "directive.impact_ready",
    "directive.state_changed", "approval.requested", "approval.decided", "gate.completed", "run.completed",
    "chat.message", "heartbeat",
]  # fmt: skip
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]


@dataclass
class StreamConfig:
    """Параметры потока (API_DESIGN §13); тесты сокращают интервалы."""

    heartbeat_seconds: float = 15.0
    access_recheck_seconds: float = 60.0
    poll_seconds: float = 5.0
    max_lifetime_seconds: float = 30 * 60
    replay_batch: int = 500
    max_streams_per_user: int = 5
    max_streams_per_process: int = 250  # утверждённый SLO P9-04: p95 ≤ 2 с при ≤ 200–300 потоках на процесс
    retry_ms: int = 3000


CONFIG = StreamConfig()
_active_streams = 0


def access_channel(user_id: uuid.UUID | str) -> str:
    """Сигнал немедленной перепроверки доступа пользователя (logout, revoke, удаление участника)."""
    return f"aicd:access:{user_id}"


async def _run_access(request: Request, principal: identity.Principal, run_id: uuid.UUID) -> Access:
    """Workspace run через SECURITY DEFINER `sys_resolve_run_workspace` (только для членов) — иначе 404."""
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        ws = (await conn.execute(text("SELECT sys_resolve_run_workspace(:id)"), {"id": run_id})).scalar_one_or_none()
        role = await identity.workspace_role(conn, ws) if ws else None
    if ws is None or role is None:
        metrics.AUTHZ_DENIED.labels("tenant").inc()  # 404 на чужой/несуществующий ресурс — метрика безопасности (AC-12)
        raise ProblemError("not_found")
    header = request.headers.get("x-workspace-id")
    if header is not None and header != str(ws):
        raise ProblemError("not_found")
    return Access(principal, ws, Role(role))


def _start_seq(last_event_id: str | None, after_seq: int | None) -> int:
    if last_event_id is not None:
        value = last_event_id.strip()
        if not value.isdigit() or len(value) > 19:
            raise ProblemError(
                "validation_error", errors=[FieldError("/headers/Last-Event-ID", "pattern", "ожидается seq (число)")]
            )
        return int(value)
    return after_seq or 0


async def _check_replay_window(access: Access, request: Request, run_id: uuid.UUID, start: int) -> bool:
    """410, если события после `start` уже удалены retention; True — run уже в терминальном статусе."""
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT r.last_event_seq, (SELECT min(seq) FROM run_event_log l WHERE l.run_id = r.id) AS min_seq,"
                    " r.status IN ('COMPLETED', 'FAILED', 'CANCELLED') AS terminal FROM runs r WHERE r.id = :r"
                ),
                {"r": run_id},
            )
        ).first()
    if row is None:
        raise ProblemError("not_found")
    last, min_seq = int(row[0]), row[1]
    if start < last and (min_seq is None or int(min_seq) > start + 1):
        raise ProblemError("replay_window_exceeded", extensions={"min_available_seq": min_seq, "last_seq": last})
    return bool(row[2])


RETRY_AFTER_SECONDS = 5


def _unavailable(detail: str) -> ProblemError:
    return ProblemError("service_unavailable", detail=detail, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


def signal_hub(request: Request) -> SignalHub:
    """Один pub/sub-приёмник на процесс (приложение) — общий для всех SSE-потоков (`aicd_api.sse_hub`)."""
    hub: SignalHub | None = getattr(request.app.state, "aicd_sse_hub", None)
    if hub is None:
        hub = SignalHub(app_state(request).redis)
        request.app.state.aicd_sse_hub = hub
    return hub


async def _acquire_slot(redis: Redis, user_id: uuid.UUID) -> str:
    """Слот потока: лимит процесса ⇒ 503 + Retry-After; сбой Redis при учёте — тоже 503, не 500."""
    global _active_streams
    if _active_streams >= CONFIG.max_streams_per_process:
        metrics.SSE_REJECTED.labels("process_limit").inc()
        raise _unavailable("Превышен лимит SSE-соединений сервера")
    key = f"aicd:sse:streams:{user_id}"
    try:
        count = int(await redis.incr(key))
        await redis.expire(key, int(CONFIG.max_lifetime_seconds * 2) + 60)
    except (RedisError, OSError, IndexError) as exc:
        metrics.SSE_REJECTED.labels("redis_unavailable").inc()
        log.warning("sse slot accounting failed", extra={"error": type(exc).__name__})
        raise _unavailable("Учёт SSE-потоков временно недоступен") from None
    if count > CONFIG.max_streams_per_user:
        await redis.decr(key)
        raise ProblemError("rate_limited", detail="Не более 5 одновременных SSE-потоков", headers={"Retry-After": "5"})
    _active_streams += 1
    return key


async def _release_slot(redis: Redis, key: str) -> None:
    global _active_streams
    _active_streams = max(_active_streams - 1, 0)
    with contextlib.suppress(Exception):
        if int(await redis.decr(key)) <= 0:
            await redis.delete(key)


async def _audit_connect(request: Request, access: Access, run_id: uuid.UUID) -> None:
    """`sse.connect` агрегируется: одно событие на сессию+run в час."""
    state = app_state(request)
    try:
        first = await state.redis.set(f"aicd:sse:audit:{access.principal.session_id}:{run_id}", "1", nx=True, ex=3600)
    except Exception:  # noqa: BLE001 — недоступность Redis не блокирует аудит
        first = True
    if not first:
        return
    async with transaction(state.engine, access.db()) as conn:
        await audit.record(
            conn, action="sse.connect", resource_type="run", resource_id=run_id, actor_type="user",
            actor_id=access.principal.user_id, workspace_id=access.workspace_id,
        )  # fmt: skip


async def _still_allowed(request: Request, token: str, run_id: uuid.UUID) -> bool:
    """Сессия действительна и пользователь по-прежнему член workspace run с правом run:read."""
    state = app_state(request)
    async with transaction(state.engine, DbContext()) as conn:
        principal = await identity.resolve_session(conn, token, state.session_policy)
    if isinstance(principal, str):
        return False
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        ws = (await conn.execute(text("SELECT sys_resolve_run_workspace(:id)"), {"id": run_id})).scalar_one_or_none()
        role = await identity.workspace_role(conn, ws) if ws else None
    return role is not None and has_permission(role, "run:read")


async def _fetch(
    request: Request, access: Access, run_id: uuid.UUID, after: int, types: list[str] | None, task_id: uuid.UUID | None
) -> list[dict[str, Any]]:
    state = app_state(request)
    sql = (
        "SELECT run_id, seq, event_id, event_type, schema_version, payload, occurred_at FROM run_event_log"
        " WHERE run_id = :r AND seq > :after"
    )
    params: dict[str, Any] = {"r": run_id, "after": after, "n": CONFIG.replay_batch}
    if types:
        sql += " AND event_type = ANY(:types)"
        params["types"] = types
    if task_id:
        sql += " AND task_id = :task"
        params["task"] = task_id
    async with transaction(state.engine, access.db()) as conn:
        rows = (await conn.execute(text(sql + " ORDER BY seq LIMIT :n"), params)).mappings().all()
    return [dict(r) for r in rows]


async def _max_seq(request: Request, access: Access, run_id: uuid.UUID) -> int:
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        value = (
            await conn.execute(text("SELECT coalesce(max(seq), 0) FROM run_event_log WHERE run_id = :r"), {"r": run_id})
        ).scalar_one()
    return int(value)


async def _shielded[T](operation: Coroutine[Any, Any, T]) -> T:
    """Запрос к БД не прерывается отключением клиента посреди транзакции (соединение пула остаётся целым)."""
    with anyio.CancelScope(shield=True):
        result = await operation
    return result


async def _wait_signal(subscription: Subscription, wait_seconds: float) -> str | None:
    """Ждёт сигнал Redis не дольше `wait_seconds`; возвращает канал сигнала или None (без Redis — опрос)."""
    return await subscription.wait(wait_seconds)


async def _stream(
    request: Request,
    access: Access,
    run_id: uuid.UUID,
    start: int,
    types: list[str] | None,
    task_id: uuid.UUID | None,
    token: str,
    slot: str,
    terminal: bool,
) -> AsyncIterator[str]:
    state = app_state(request)
    cfg = CONFIG
    metrics.SSE_CONNECTIONS.labels("run").inc()
    metrics.SSE_CONNECTIONS_OPENED.labels("run").inc()
    subscription = await signal_hub(request).subscribe(
        (sse.run_channel(run_id), access_channel(access.principal.user_id))
    )
    try:
        yield f"retry: {cfg.retry_ms}\n\n"
        last = start
        opened = time.monotonic()
        last_sent = opened
        last_check = opened
        while True:
            rows = await _shielded(_fetch(request, access, run_id, last, types, task_id))
            for row in rows:
                env = sse.envelope(row)
                last = env["seq"]
                last_sent = time.monotonic()
                yield sse.frame(env)
                if env["type"] == sse.TERMINAL_TYPE:
                    return  # run завершён — сервер закрывает поток (API_DESIGN §13)
            if len(rows) == cfg.replay_batch:
                continue  # replay пачками до «хвоста»
            if terminal:
                return  # run был завершён до подключения: история отдана, live не будет
            now = time.monotonic()
            if now - opened >= cfg.max_lifetime_seconds:
                return  # клиент переподключится с Last-Event-ID
            deadline = min(
                last_sent + cfg.heartbeat_seconds,
                last_check + cfg.access_recheck_seconds,
                now + cfg.poll_seconds,
                opened + cfg.max_lifetime_seconds,
            )
            signal = await _wait_signal(subscription, deadline - now)
            now = time.monotonic()
            if signal == access_channel(access.principal.user_id) or now - last_check >= cfg.access_recheck_seconds:
                last_check = now
                if not await _shielded(_still_allowed(request, token, run_id)):
                    return  # доступ отозван: прекращаем отправку и закрываем соединение
            if now - last_sent >= cfg.heartbeat_seconds:
                last_sent = now
                yield sse.heartbeat_frame(
                    run_id, max(last, await _shielded(_max_seq(request, access, run_id))), utcnow()
                )
    finally:
        metrics.SSE_CONNECTIONS.labels("run").dec()
        with anyio.CancelScope(shield=True):
            with contextlib.suppress(Exception):
                await subscription.close()
            await _release_slot(state.redis, slot)


@router.get("/runs/{run_id}/events", **op("streamRunEvents", "run:read"))
async def stream_run_events(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    types: Annotated[list[SseEventType] | None, Query()] = None,
    task_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Response:
    access = await _run_access(request, principal, run_id)
    await authorize(request, access, "run:read")
    start = _start_seq(request.headers.get("last-event-id"), after_seq)
    terminal = await _check_replay_window(access, request, run_id, start)
    state = app_state(request)
    token = request.cookies.get(state.settings.session_cookie_name) or ""
    slot = await _acquire_slot(state.redis, principal.user_id)
    try:
        await _audit_connect(request, access, run_id)
    except BaseException:
        await _release_slot(state.redis, slot)
        raise
    filtered: list[str] | None = [str(t) for t in types if t != "heartbeat"] if types else None
    return StreamingResponse(
        _stream(request, access, run_id, start, filtered or None, task_id, token, slot, terminal),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _activity_item(row: dict[str, Any]) -> dict[str, Any]:
    env = sse.envelope(row)
    payload = env["payload"]
    execution = payload.get("execution_id") if isinstance(payload, dict) else None
    return {
        "id": env["event_id"],
        "run_id": env["run_id"],
        "task_id": str(row["task_id"]) if row.get("task_id") else None,
        "execution_id": str(execution) if execution else None,
        "event_type": env["type"],
        "payload": payload,
        "occurred_at": env["timestamp"],
        "seq": env["seq"],
    }


@router.get("/runs/{run_id}/activity", **op("getRunActivity", "run:read"))
async def get_run_activity(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    task_id: Annotated[uuid.UUID | None, Query()] = None,
    event_type: Annotated[str | None, Query(max_length=64)] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await _run_access(request, principal, run_id)
    await authorize(request, access, "run:read")
    state = app_state(request)
    filters = {"r": "activity", "run": str(run_id), "task": str(task_id) if task_id else None, "type": event_type}
    # Детерминированный порядок — по seq по возрастанию (контракт getRunActivity); курсор = seq последнего элемента.
    after = after_seq or 0
    if cursor:
        (after,) = decode_cursor(state.secret_key, cursor, filters)
        after = int(after)
    sql = (
        "SELECT run_id, seq, event_id, event_type, schema_version, task_id, payload, occurred_at FROM run_event_log"
        " WHERE run_id = :r AND seq > :after"
    )
    params: dict[str, Any] = {"r": run_id, "after": after, "n": limit + 1}
    if task_id:
        sql += " AND task_id = :task"
        params["task"] = task_id
    if event_type:
        sql += " AND event_type = :etype"
        params["etype"] = event_type
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql + " ORDER BY seq LIMIT :n"), params)).mappings().all()]
    page = rows[:limit]
    next_cursor = encode_cursor(state.secret_key, [int(page[-1]["seq"])], filters) if len(rows) > limit else None
    return json_response({"items": [_activity_item(r) for r in page], "next_cursor": next_cursor})


# --- Поток уведомлений workspace (OQ-45 (c), API_DESIGN §13.1) --------------------------------------------

WorkspaceSseEventType = Literal[
    "run.state_changed", "approval.requested", "approval.decided", "run.completed", "notification.created"
]  # fmt: skip
# Перекрытие повтора: строка run_event_log получает created_at = начало транзакции worker-outbox и становится
# видимой только после commit, поэтому событие с меньшим временем может появиться позже уже отданного.
WORKSPACE_OVERLAP = timedelta(seconds=60)
WORKSPACE_REPLAY_WINDOW = timedelta(hours=24)
_SENT_IDS_MAX = 5000


def _workspace_cursor(last_event_id: str | None) -> int | None:
    if last_event_id is None:
        return None
    value = last_event_id.strip()
    if not value.isdigit() or len(value) > 19:
        raise ProblemError(
            "validation_error", errors=[FieldError("/headers/Last-Event-ID", "pattern", "ожидается курсор (число)")]
        )
    return int(value)


async def _still_member(request: Request, token: str, workspace_id: uuid.UUID) -> bool:
    state = app_state(request)
    async with transaction(state.engine, DbContext()) as conn:
        principal = await identity.resolve_session(conn, token, state.session_policy)
    if isinstance(principal, str):
        return False
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        role = await identity.workspace_role(conn, workspace_id)
    return role is not None and has_permission(role, "run:read")


_WORKSPACE_LOG_SELECT = (
    "SELECT 'run' AS source, run_id, seq, event_id, event_type::text AS event_type,"
    " schema_version::text AS schema_version, payload, occurred_at, created_at"
    " FROM run_event_log WHERE workspace_id = :ws AND event_type = ANY(:types)"
)


async def _fetch_workspace(
    request: Request, access: Access, after: tuple[datetime, uuid.UUID | None], types: list[str], limit: int
) -> list[dict[str, Any]]:
    """Страница потока workspace после keyset `(created_at, event_id)` (event_id None — с `created_at` включительно):
    строки run_event_log выбранных типов и (тип `notification.created`) уведомления текущего пользователя
    (`notifications`, RLS: только свои) — в одном порядке, поэтому курсор `id:` общий для обоих источников."""
    state = app_state(request)
    log_types = [t for t in types if t != sse.NOTIFICATION_TYPE]
    parts = [_WORKSPACE_LOG_SELECT] if log_types else []
    if sse.NOTIFICATION_TYPE in types:
        parts.append(notifications.SSE_SELECT)
    if not parts:
        return []
    params: dict[str, Any] = {"ws": access.workspace_id, "types": log_types, "n": limit, "t": after[0]}
    if after[1] is None:
        cond = "created_at >= :t"
    else:
        cond = "(created_at, event_id) > (:t, :id)"
        params["id"] = after[1]
    sql = f"SELECT * FROM ({' UNION ALL '.join(parts)}) e WHERE {cond} ORDER BY created_at, event_id LIMIT :n"  # noqa: S608
    async with transaction(state.engine, access.db()) as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


def _workspace_envelope(row: dict[str, Any]) -> dict[str, Any]:
    """SseEnvelope строки журнала или кадр `notification.created` (SseNotificationCreated, без seq)."""
    if row["source"] != "notification":
        return sse.envelope(row)
    return {
        "event_id": str(row["event_id"]),
        "run_id": str(row["run_id"]) if row["run_id"] else None,
        "seq": None,
        "type": sse.NOTIFICATION_TYPE,
        "timestamp": isoformat_z(row["occurred_at"]),
        "schema_version": row["schema_version"] or sse.SCHEMA_VERSION,
        "payload": notifications.sse_payload(row["payload"], row["created_at"]),
    }


async def _stream_workspace(
    request: Request, access: Access, since: datetime, types: list[str], token: str, slot: str
) -> AsyncIterator[str]:
    """Повтор с перекрытием и live-опрос `run_event_log` workspace; дубли в пределах соединения отсекаются по
    event_id, между соединениями — клиентом (контракт streamWorkspaceEvents)."""
    state = app_state(request)
    cfg = CONFIG
    metrics.SSE_CONNECTIONS.labels("workspace").inc()
    metrics.SSE_CONNECTIONS_OPENED.labels("workspace").inc()
    subscription = await signal_hub(request).subscribe(
        (sse.workspace_channel(access.workspace_id), access_channel(access.principal.user_id))
    )
    sent: dict[str, datetime] = {}
    watermark = since
    try:
        yield f"retry: {cfg.retry_ms}\n\n"
        opened = time.monotonic()
        last_sent = opened
        last_check = opened
        while True:
            # Цикл: от watermark минус перекрытие до «хвоста» пачками (keyset внутри цикла).
            after: tuple[datetime, uuid.UUID | None] = (watermark - WORKSPACE_OVERLAP, None)
            while True:
                rows = await _shielded(_fetch_workspace(request, access, after, types, cfg.replay_batch))
                for row in rows:
                    event_id = str(row["event_id"])
                    created = row["created_at"]
                    after = (created, row["event_id"])
                    watermark = max(watermark, created)
                    if event_id in sent:
                        continue
                    sent[event_id] = created
                    last_sent = time.monotonic()
                    yield sse.workspace_frame(_workspace_envelope(row), sse.cursor_of(created))
                if len(rows) < cfg.replay_batch:
                    break
            # Забываем event_id старше окна перекрытия — они больше не попадут в выборку.
            horizon = watermark - WORKSPACE_OVERLAP
            if len(sent) > _SENT_IDS_MAX or any(t < horizon for t in sent.values()):
                sent = {k: t for k, t in sent.items() if t >= horizon}
            now = time.monotonic()
            if now - opened >= cfg.max_lifetime_seconds:
                return  # клиент переподключится с Last-Event-ID
            deadline = min(
                last_sent + cfg.heartbeat_seconds,
                last_check + cfg.access_recheck_seconds,
                now + cfg.poll_seconds,
                opened + cfg.max_lifetime_seconds,
            )
            signal = await _wait_signal(subscription, deadline - now)
            now = time.monotonic()
            if signal == access_channel(access.principal.user_id) or now - last_check >= cfg.access_recheck_seconds:
                last_check = now
                if not await _shielded(_still_member(request, token, access.workspace_id)):
                    return
            if now - last_sent >= cfg.heartbeat_seconds:
                last_sent = now
                yield sse.workspace_heartbeat_frame(access.workspace_id, utcnow())
    finally:
        metrics.SSE_CONNECTIONS.labels("workspace").dec()
        with anyio.CancelScope(shield=True):
            with contextlib.suppress(Exception):
                await subscription.close()
            await _release_slot(state.redis, slot)


async def _audit_workspace_connect(request: Request, access: Access) -> None:
    """`sse.connect` агрегируется: одно событие на сессию+workspace в час."""
    state = app_state(request)
    try:
        first = await state.redis.set(
            f"aicd:sse:audit:{access.principal.session_id}:ws:{access.workspace_id}", "1", nx=True, ex=3600
        )
    except Exception:  # noqa: BLE001 — недоступность Redis не блокирует аудит
        first = True
    if not first:
        return
    async with transaction(state.engine, access.db()) as conn:
        await audit.record(
            conn, action="sse.connect", resource_type="workspace", resource_id=access.workspace_id,
            actor_type="user", actor_id=access.principal.user_id, workspace_id=access.workspace_id,
        )  # fmt: skip


@router.get("/workspaces/{workspace_id}/events", tags=["workspaces"], **op("streamWorkspaceEvents", "run:read"))
async def stream_workspace_events(
    request: Request,
    workspace_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    types: Annotated[list[WorkspaceSseEventType] | None, Query()] = None,
) -> Response:
    access = await workspace_access(request, principal, workspace_id)
    await authorize(request, access, "run:read")
    cursor = _workspace_cursor(request.headers.get("last-event-id"))
    now = utcnow()
    if cursor is None:
        since = now
    else:
        try:
            since = sse.from_cursor(cursor)
        except OverflowError as exc:
            raise ProblemError(
                "validation_error", errors=[FieldError("/headers/Last-Event-ID", "range", "курсор вне диапазона")]
            ) from exc
        if since < now - WORKSPACE_REPLAY_WINDOW:
            raise ProblemError("replay_window_exceeded", extensions={"replay_window_seconds": 86400})
        since = min(since, now)
    state = app_state(request)
    token = request.cookies.get(state.settings.session_cookie_name) or ""
    slot = await _acquire_slot(state.redis, principal.user_id)
    try:
        await _audit_workspace_connect(request, access)
    except BaseException:
        await _release_slot(state.redis, slot)
        raise
    selected = [str(t) for t in types] if types else [*sse.WORKSPACE_TYPES, sse.NOTIFICATION_TYPE]
    return StreamingResponse(
        _stream_workspace(request, access, since, selected, token, slot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
