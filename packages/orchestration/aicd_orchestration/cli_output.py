"""Живой вывод CLI: нормализованные события исполнения → outbox `cli.output` → run_event_log → SSE
(API_DESIGN §13–§14, EVENT_SCHEMAS §2.2, §4.3; ТЗ §11 «live CLI output»).

Санитизация выполнена адаптером до этой точки (`aicd_cli_adapters.output`: очистка, построчная буферизация,
редактирование секретов, чанки ≤ 4 KiB, скрытые рассуждения отброшены); здесь текст редактируется ещё раз
(defence in depth) и ничего несанитизированного не пишется.

**Лимит ≤ 20 событий/с на run** (API_DESIGN §13): token bucket на run (`RunRateLimiter`, общий для исполнений
этого процесса). Пока бюджета нет, события копятся и при следующем токене уходят **одним** коалесцированным
событием: `coalesced_count` — сколько нормализованных событий оно представляет, `dropped_chunks` — сколько из них
не вошло в текст (вытеснены важным событием или не уместились в 4096 символов). Полный санитизированный вывод —
artifact `cli_output` (ссылка `artifact_id` в событии `finished`). `finished` никогда не сливается с выводом:
перед ним досылается накопленное. Лимит учитывается в процессе воркера: несколько процессов, исполняющих задачи
одного run, имеют каждый свой бюджет.

**Выдержки в ленту чата** (EVENT_SCHEMAS §4.2–4.3, `CLI_HIGHLIGHT`): значимые события — инструмент `file_patch`/
`file_write`/`command` в фазах `completed`/`denied`/`failed`, предупреждения, завершение — не чаще одного
сообщения на 10 с на исполнение (кроме `finished`): `chat_messages(message_kind='cli_excerpt')` + `chat.message`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.models import NormalizedCliEvent
from aicd_collaboration import chat
from aicd_collaboration.acp.projection import ensure_thread
from aicd_platform import outbox
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text

log = logging.getLogger(__name__)

EVENT_TYPE = "cli.output"
MAX_EVENTS_PER_SECOND = 20.0
TEXT_MAX_CHARS = 4096  # sse-event.schema.json cli_output.text maxLength
HIGHLIGHT_INTERVAL_SECONDS = 10.0
_HIGHLIGHT_TOOLS = frozenset({"file_patch", "file_write", "command"})
_HIGHLIGHT_PHASES = frozenset({"completed", "denied", "failed"})


class RunRateLimiter:
    """Token bucket на run: `rate` событий/с, запас `burst`."""

    def __init__(
        self,
        rate: float = MAX_EVENTS_PER_SECOND,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = rate
        self.burst = burst if burst is not None else rate
        self._clock = clock
        self._buckets: dict[uuid.UUID, tuple[float, float]] = {}

    def _refill(self, run_id: uuid.UUID) -> float:
        now = self._clock()
        tokens, last = self._buckets.get(run_id, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        self._buckets[run_id] = (tokens, now)
        return tokens

    def try_acquire(self, run_id: uuid.UUID) -> bool:
        tokens = self._refill(run_id)
        if tokens < 1:
            return False
        self._buckets[run_id] = (tokens - 1, self._buckets[run_id][1])
        return True

    def wait_seconds(self, run_id: uuid.UUID) -> float:
        tokens = self._refill(run_id)
        return 0.0 if tokens >= 1 else (1 - tokens) / self.rate

    def forget(self, run_id: uuid.UUID) -> None:
        self._buckets.pop(run_id, None)


RUN_LIMITER = RunRateLimiter()


@dataclass(frozen=True)
class CliOutputContext:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    execution_id: uuid.UUID
    provider: str
    agent_role: str


def _text(value: Any) -> str:
    return redact_text(str(value))[:TEXT_MAX_CHARS]


def payload_for(ctx: CliOutputContext, event: NormalizedCliEvent) -> dict[str, Any]:
    """Нормализованное событие CLI → payload SSE `cli_output` (sse-event.schema.json)."""
    p = event.payload
    payload: dict[str, Any] = {
        "execution_id": str(ctx.execution_id),
        "task_id": str(ctx.task_id),
        "provider": ctx.provider,
        "agent_role": ctx.agent_role,
        "cli_seq": event.seq,
        "cli_event_type": event.type,
    }
    if event.type == "output":
        payload.update(
            stream=p.get("stream") if p.get("stream") in ("stdout", "stderr", "agent_message") else "stdout",
            text=_text(p.get("text", "")),
            redactions=max(int(p.get("redactions") or 0), 0),
            truncated=bool(p.get("truncated")),
        )
    elif event.type == "tool_event":
        payload.update(tool_kind=p.get("tool_kind", "other"), tool_phase=p.get("phase", "completed"))
        if p.get("target"):
            payload["target"] = _text(p["target"])[:1024]
        if p.get("summary"):
            payload["text"] = _text(p["summary"])
    elif event.type == "warning":
        payload["warning_code"] = str(p.get("code", "OTHER"))[:50]
        if p.get("message"):
            payload["text"] = _text(p["message"])
    elif event.type == "artifact":
        ref = str(p.get("ref", ""))
        if ref.startswith("artifact:"):
            payload["artifact_id"] = ref.removeprefix("artifact:")
        payload["text"] = _text(p.get("artifact_type", ""))
    elif event.type == "finished":
        payload["execution_status"] = p.get("execution_status")
        exit_code, error_code = p.get("exit_code"), p.get("error_code")
        payload["text"] = _text(f"exit {exit_code}" + (f" · {error_code}" if error_code else ""))
    elif event.type == "started" and p.get("cli_version"):
        payload["text"] = _text(f"{p.get('provider', ctx.provider)} {p['cli_version']}")
    return payload


def coalesce(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Несколько payload → одно событие (см. модульную документацию). Порядок `cli_seq` — последнего."""
    if len(payloads) == 1:
        return payloads[0]
    last = payloads[-1]
    important = [p for p in payloads if p["cli_event_type"] != "output"]
    if important:
        merged = dict(important[-1])
        dropped = len(payloads) - 1
    else:
        streams = {p.get("stream") for p in payloads}
        mixed = len(streams) > 1
        merged = dict(last)
        parts: list[str] = []
        used = 0
        dropped = 0
        truncated = False
        for p in payloads:
            piece = (f"[{p.get('stream')}] " if mixed else "") + str(p.get("text", ""))
            room = TEXT_MAX_CHARS - used - (1 if parts else 0)
            if room <= 0:
                dropped += 1
                continue
            if len(piece) > room:
                piece, truncated = piece[:room], True
            parts.append(piece)
            used += len(piece) + (1 if len(parts) > 1 else 0)
            truncated = truncated or bool(p.get("truncated"))
        merged.update(
            text="\n".join(parts),
            redactions=sum(int(p.get("redactions") or 0) for p in payloads),
            truncated=truncated,
        )
        if mixed:
            merged["stream"] = last.get("stream")
    merged["cli_seq"] = last["cli_seq"]
    merged["coalesced_count"] = len(payloads)
    merged["dropped_chunks"] = dropped
    return merged


def highlight_body(ctx: CliOutputContext, payload: dict[str, Any]) -> str | None:
    """Текст выдержки для ленты чата (`CLI_HIGHLIGHT`) или None, если событие не значимое."""
    who = f"{ctx.agent_role} ({ctx.provider})"
    kind = payload["cli_event_type"]
    if kind == "tool_event":
        if payload.get("tool_kind") not in _HIGHLIGHT_TOOLS or payload.get("tool_phase") not in _HIGHLIGHT_PHASES:
            return None
        target = f" {payload['target']}" if payload.get("target") else ""
        return f"{who}: {payload['tool_kind']}{target} — {payload['tool_phase']}"
    if kind == "warning":
        return f"{who}: предупреждение {payload.get('warning_code')}: {payload.get('text', '')}".rstrip(": ")
    if kind == "finished":
        return f"{who}: CLI завершён — {payload.get('execution_status')} ({payload.get('text', '')})"
    return None


@dataclass
class CliOutputPublisher:
    """EventSink для `ExecutionGateway.run(on_event=…)`; после исполнения обязательно `await close()`."""

    engine: AsyncEngine
    ctx: CliOutputContext
    limiter: RunRateLimiter = field(default_factory=lambda: RUN_LIMITER)
    highlight_interval: float = HIGHLIGHT_INTERVAL_SECONDS
    clock: Callable[[], float] = time.monotonic
    emitted: int = 0
    highlights: int = 0
    _pending: list[dict[str, Any]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _timer: asyncio.Task[None] | None = None
    _last_highlight: float | None = None
    _output_artifact: str | None = None
    _failed: bool = False

    async def __call__(self, event: NormalizedCliEvent) -> None:
        if self._failed:
            return
        try:
            payload = payload_for(self.ctx, event)
            if event.type == "artifact" and payload.get("text") == "output_excerpt":
                self._output_artifact = payload.get("artifact_id")
            if event.type == "finished":
                if self._output_artifact:
                    payload["artifact_id"] = self._output_artifact
                await self._highlight(payload, force=True)  # выдержка в чат — до завершающего события
                async with self._lock:
                    await self._drain()
                    await self._wait_token()
                    await self._publish(payload)
                return
            await self._highlight(payload, force=False)
            async with self._lock:
                self._pending.append(payload)
                if self.limiter.try_acquire(self.ctx.run_id):
                    await self._flush()
                elif self._timer is None or self._timer.done():
                    self._timer = asyncio.create_task(self._flush_later())
        except Exception:
            # Живой вывод — вспомогательный канал: сбой записи не должен ронять исполнение (полный вывод — artifact).
            self._failed = True
            log.exception("cli.output publishing failed", extra={"execution_id": str(self.ctx.execution_id)})

    async def close(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer
        if self._failed:
            return
        try:
            async with self._lock:
                await self._drain()
        except Exception:
            log.exception("cli.output final flush failed", extra={"execution_id": str(self.ctx.execution_id)})

    async def _flush_later(self) -> None:
        await asyncio.sleep(self.limiter.wait_seconds(self.ctx.run_id))
        async with self._lock:
            if self._pending:
                await self._wait_token()
                await self._flush()

    async def _wait_token(self) -> None:
        for _ in range(10_000):  # ограничение на случай неверно настроенного лимитера (rate ≤ 0)
            if self.limiter.try_acquire(self.ctx.run_id):
                return
            await asyncio.sleep(max(self.limiter.wait_seconds(self.ctx.run_id), 0.001))
        raise RuntimeError("лимит событий cli.output не выдаёт токены")

    async def _drain(self) -> None:
        if self._pending:
            await self._wait_token()
            await self._flush()

    async def _flush(self) -> None:
        batch, self._pending = self._pending, []
        if batch:
            await self._publish(coalesce(batch))

    async def _publish(self, payload: dict[str, Any]) -> None:
        async with transaction(self.engine, DbContext(workspace_id=self.ctx.workspace_id, actor_type="system")) as conn:
            await outbox.emit(
                conn,
                event_type=EVENT_TYPE,
                aggregate_type="cli_execution",
                aggregate_id=self.ctx.execution_id,
                workspace_id=self.ctx.workspace_id,
                run_id=self.ctx.run_id,
                payload=payload,
            )
        self.emitted += 1

    async def _highlight(self, payload: dict[str, Any], *, force: bool) -> None:
        body = highlight_body(self.ctx, payload)
        if body is None:
            return
        now = self.clock()
        if not force and self._last_highlight is not None and now - self._last_highlight < self.highlight_interval:
            return
        self._last_highlight = now
        try:
            await self._post_highlight(body)
        except Exception:
            log.exception("cli highlight to chat failed", extra={"execution_id": str(self.ctx.execution_id)})

    async def _post_highlight(self, body: str) -> None:
        ctx = self.ctx
        async with transaction(self.engine, DbContext(workspace_id=ctx.workspace_id, actor_type="system")) as conn:
            thread_id = await ensure_thread(
                conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id, scope="task", target_role=None,
                target_task_id=ctx.task_id,
            )  # fmt: skip
            message_id = uuid7()
            body = redact_text(body)[: chat.BODY_MAX_CHARS]
            created_at = (
                await conn.execute(
                    text(
                        "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, sender_role,"
                        " sender_provider, body, message_kind) VALUES (:id, :ws, :run, :th, 'agent', :role, :prov,"
                        " :body, 'cli_excerpt') RETURNING created_at"
                    ),
                    {
                        "id": message_id,
                        "ws": ctx.workspace_id,
                        "run": ctx.run_id,
                        "th": thread_id,
                        "role": ctx.agent_role,
                        "prov": ctx.provider,
                        "body": body,
                    },
                )
            ).scalar_one()
            await outbox.emit(
                conn,
                event_type="chat.message",
                aggregate_type="chat_message",
                aggregate_id=message_id,
                workspace_id=ctx.workspace_id,
                run_id=ctx.run_id,
                payload=chat.event_payload(
                    chat_message_id=message_id,
                    thread_id=thread_id,
                    scope="task",
                    message_kind="CLI_HIGHLIGHT",
                    sender_type="agent",
                    sender_role=ctx.agent_role,
                    provider=ctx.provider,
                    task_id=ctx.task_id,
                    body=body,
                    created_at=created_at,
                ),
            )
        self.highlights += 1
