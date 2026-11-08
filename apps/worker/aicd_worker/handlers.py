"""Маршрутизация доменных событий в оркестратор (ADR-0002 п.3, ADR-0007).

Сообщение несёт идентификаторы и тип события; состояние перечитывается из PostgreSQL. Повторная доставка
(at-least-once) отсекается `processed_events(consumer, event_id)`: отметка вставляется в отдельной короткой
транзакции, которая фиксируется только после успешной обработки (ошибка ⇒ откат ⇒ Dramatiq повторит).
Конкурентный дубль ждёт на уникальном ключе и после commit первого получает конфликт ⇒ no-op.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_collaboration import acp
from aicd_platform import consumer, context
from aicd_platform.db import DbContext, apply_context, transaction
from aicd_platform.ids import uuid7
from aicd_worker.runtime import Runtime

log = logging.getLogger("aicd.worker")

ORCHESTRATOR_CONSUMER = "orchestrator.handle_event"
EXEC_CONSUMER = "exec.execute_task"

# Типы, которые диспетчер outbox направляет в очередь оркестратора (остальные — только SSE/прочие потребители).
ORCHESTRATOR_EVENT_TYPES = frozenset(
    {"run.created", "run.state_changed", "task.state_changed", "approval.decided", "agent.message"}
)
# Запрос загрузки документации (без run): очередь docs, обработчик `aicd_worker.docs.import_documentation`.
DOCS_EVENT_TYPES = frozenset({"documentation_source.refresh_requested"})
# Потребители доставки ACP (ACP §7), обслуживаемые воркером: проекции в чат run и ленту активности.
ACP_PROJECTORS = ((acp.CONSUMER_CHAT, acp.project_chat), (acp.CONSUMER_ACTIVITY, acp.project_activity))

Outcome = Literal[
    "acp_delivered",
    "duplicate",
    "ignored",
    "on_run_created",
    "on_plan_approval_decided",
    "on_run_paused_or_cancelled",
    "on_run_resumed",
    "execute_task",
    "tick",
]


@asynccontextmanager
async def claimed(engine: AsyncEngine, consumer_name: str, event_id: uuid.UUID) -> AsyncIterator[bool]:
    """Отметка обработки в собственной транзакции; commit — по успешному выходу из блока."""
    async with engine.begin() as conn:
        await apply_context(conn, DbContext(actor_type="system"))
        yield await consumer.claim(conn, consumer_name, event_id)


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def handle_event(rt: Runtime, event: dict[str, Any]) -> Outcome:
    event_id = _uuid(event.get("event_id"))
    run_id = _uuid(event.get("run_id"))
    ws = _uuid(event.get("workspace_id"))
    if event_id is None or run_id is None or ws is None:
        return "ignored"
    context.bind(run_id=run_id, workspace_id=ws)
    async with claimed(rt.engine, ORCHESTRATOR_CONSUMER, event_id) as first:
        if not first:
            return "duplicate"
        return await _route(rt, event, event_id, run_id, ws)


async def _route(rt: Runtime, event: dict[str, Any], event_id: uuid.UUID, run_id: uuid.UUID, ws: uuid.UUID) -> Outcome:
    etype = event.get("event_type")
    payload = event.get("payload") or {}
    orch = rt.orchestrator
    if etype == "agent.message":
        # Каждая доставка идемпотентна сама (message_deliveries: блокировка строки, ack); сбой → backoff/DLQ.
        for consumer_id, handler in ACP_PROJECTORS:
            await acp.handle_outbox_event(rt.engine, event, consumer_id=consumer_id, handler=handler)
        return "acp_delivered"
    if etype == "run.created":
        await orch.on_run_created(run_id, ws)
        return "on_run_created"
    if etype == "approval.decided":
        decision = await _plan_decision(rt.engine, _uuid(event.get("aggregate_id")), run_id, ws, payload)
        if decision is not None:
            approved, plan_version = decision
            await orch.on_plan_approval_decided(run_id, ws, approved=approved, plan_version=plan_version)
            return "on_plan_approval_decided"
        approval_id = _uuid(event.get("aggregate_id"))
        if approval_id is not None:  # approvals уровня задачи (MANUAL_REVIEW / SELF_REVIEW_FALLBACK)
            await orch.on_task_approval_decided(approval_id, ws)
    if etype == "run.state_changed":
        to_status = payload.get("to_status", payload.get("to"))
        from_status = payload.get("from_status", payload.get("from"))
        if to_status in ("PAUSED", "CANCELLED"):
            await orch.on_run_paused_or_cancelled(run_id, ws, str(to_status))
            return "on_run_paused_or_cancelled"
        if from_status == "PAUSED":
            await orch.on_run_resumed(run_id, ws)
            return "on_run_resumed"
    if etype == "task.state_changed" and payload.get("to_status", payload.get("to")) == "READY":
        task_id = _uuid(payload.get("task_id")) or _uuid(event.get("aggregate_id"))
        if task_id is not None:
            await rt.dispatch_execution(execution_message(task_id, ws, event_id))
            return "execute_task"
    # tick возвращает READY-задачи, для которых освободился слот параллелизма (они уже были READY и
    # нового события READY не получат) — ставим их в exec; повторная постановка безопасна (execute_task идемпотентен).
    for ready_id in await orch.tick(run_id, ws):
        await rt.dispatch_execution(execution_message(ready_id, ws, event_id))
    return "tick"


async def _plan_decision(
    engine: AsyncEngine, approval_id: uuid.UUID | None, run_id: uuid.UUID, ws: uuid.UUID, payload: dict[str, Any]
) -> tuple[bool, int] | None:
    """(approved, plan_version) для решения по PLAN_APPROVAL; None — иное действие/статус."""
    if approval_id is None:
        return None
    async with transaction(engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT a.action_type, a.status, a.subject, r.plan_version FROM approvals a"
                        " JOIN runs r ON r.id = a.run_id WHERE a.id = :a AND a.run_id = :r"
                    ),
                    {"a": approval_id, "r": run_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None or row["action_type"] != "PLAN_APPROVAL" or row["status"] not in ("APPROVED", "REJECTED"):
        return None
    subject = row["subject"] if isinstance(row["subject"], dict) else {}
    raw = payload.get("plan_version", subject.get("plan_version", row["plan_version"]))
    try:
        plan_version = int(raw)
    except (TypeError, ValueError):
        plan_version = int(row["plan_version"])
    return row["status"] == "APPROVED", plan_version


def execution_message(task_id: uuid.UUID, ws: uuid.UUID, cause: uuid.UUID | None = None) -> dict[str, Any]:
    """Сообщение очереди exec. `event_id` уникален на постановку (дедупликация повторной доставки)."""
    return {
        "event_id": str(uuid7()),
        "task_id": str(task_id),
        "workspace_id": str(ws),
        "causation_id": str(cause) if cause else None,
    }


async def execute_task(rt: Runtime, message: dict[str, Any]) -> Outcome:
    """Исполнение задачи. Отметка фиксируется до запуска (длинная операция не держит транзакцию);
    восстановление после падения — reaper аренд и периодический tick (maintenance)."""
    event_id = _uuid(message.get("event_id"))
    task_id = _uuid(message.get("task_id"))
    ws = _uuid(message.get("workspace_id"))
    if event_id is None or task_id is None or ws is None:
        return "ignored"
    async with claimed(rt.engine, EXEC_CONSUMER, event_id) as first:
        pass
    if not first:
        return "duplicate"
    context.bind(task_id=task_id, workspace_id=ws)
    await rt.orchestrator.execute_task(task_id, ws)
    return "execute_task"
