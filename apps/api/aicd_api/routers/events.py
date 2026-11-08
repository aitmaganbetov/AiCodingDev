"""SSE-поток run и история событий (ТЗ §8.3, API_DESIGN §13, ADR-0011, EVENT_SCHEMAS §3).

Данные всегда читаются из `run_event_log` (PostgreSQL); Redis pub/sub `aicd:run:<id>` — только сигнал
пробуждения, его потеря компенсируется опросом. `id:` = `seq`; возобновление — `Last-Event-ID`
(приоритет) или `?after_seq`; heartbeat без `id:`; проверка доступа при подключении и не реже раза в 60 с.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, Path, Query, Request
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from sqlalchemy import text
from starlette.responses import Response, StreamingResponse

from aicd_api.deps import Access, app_state, authorize, current_principal
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_identity import api as identity
from aicd_identity.rbac import Role, has_permission
from aicd_platform import audit, sse
from aicd_platform.clock import utcnow
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
    max_streams_per_process: int = 1000
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


async def _acquire_slot(redis: Redis, user_id: uuid.UUID) -> str:
    global _active_streams
    if _active_streams >= CONFIG.max_streams_per_process:
        raise ProblemError("service_unavailable", detail="Превышен лимит SSE-соединений сервера")
    key = f"aicd:sse:streams:{user_id}"
    count = int(await redis.incr(key))
    await redis.expire(key, int(CONFIG.max_lifetime_seconds * 2) + 60)
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


async def _wait_signal(pubsub: PubSub | None, wait_seconds: float) -> str | None:
    """Ждёт сигнал Redis не дольше `wait_seconds`; возвращает канал сигнала или None."""
    if wait_seconds <= 0:
        return None
    if pubsub is None:
        await asyncio.sleep(wait_seconds)
        return None
    try:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=wait_seconds)
    except Exception:  # noqa: BLE001 — Redis недоступен: деградация до опроса
        await asyncio.sleep(wait_seconds)
        return None
    if message is None:
        return None
    channel = message.get("channel")
    return channel.decode() if isinstance(channel, bytes) else str(channel)


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
    subscription = state.redis.pubsub()
    pubsub: PubSub | None = subscription
    try:
        await subscription.subscribe(sse.run_channel(run_id), access_channel(access.principal.user_id))
    except Exception:  # noqa: BLE001 — без Redis поток работает опросом
        log.warning("sse pubsub unavailable")
        pubsub = None
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
            signal = await _wait_signal(pubsub, deadline - now)
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
        with anyio.CancelScope(shield=True):
            with contextlib.suppress(Exception):
                await subscription.aclose()  # type: ignore[no-untyped-call]
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
    after_seq: Annotated[int | None, Query()] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await _run_access(request, principal, run_id)
    await authorize(request, access, "run:read")
    state = app_state(request)
    filters = {"r": "activity", "run": str(run_id), "task": str(task_id) if task_id else None, "type": event_type}
    after = max(after_seq or 0, 0)
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
