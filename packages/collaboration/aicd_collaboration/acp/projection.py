"""Проекции принятых ACP-сообщений (EVENT_SCHEMAS §4.3, ACP §7): Live Agent Chat и лента активности.

Обработчики имеют сигнатуру `delivery.Handler` и вызываются внутри транзакции доставки: эффект и ack
фиксируются атомарно, поэтому дубль доставки даёт одно сообщение чата / одно событие активности.
Дополнительно эффекты защищены проверкой существования по `acp_message_id` / `payload.message_id`.

Чат: тред `task` (если у сообщения есть задача), иначе `all_agents`; сообщения к/от `user` — в тред
`role` роли-собеседника. `chat_messages.message_kind = 'acp_message'`; событие outbox `chat.message`
в форме SSE `chat_message` (scope `RUN|ROLE|TASK`, kind `AGENT_MESSAGE`).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_collaboration.acp import injection, render
from aicd_collaboration.acp.broker import CONSUMER_ACP_HANDLER, insert_activity
from aicd_collaboration.acp.delivery import Delivery
from aicd_collaboration.acp.facts import StoredMessage
from aicd_collaboration.acp.model import AGENT, ORCHESTRATOR, USER
from aicd_collaboration.acp.pipeline import iter_refs
from aicd_platform import outbox
from aicd_platform.clock import isoformat_z
from aicd_platform.ids import uuid7

ACTIVITY_ACCEPTED = "acp.message_accepted"
# chat_threads.scope (БД) → scope SSE chat_message.
SSE_SCOPE = {"all_agents": "RUN", "role": "ROLE", "task": "TASK"}
INJECTION_WARNING = "⚠ Возможная prompt-injection в сообщении агента (данные не исполняются). "
LOOP_WARNING = "⚠ Лимит review/fix циклов исчерпан — новая попытка не создаётся. "


async def message_flags(conn: AsyncConnection, msg: StoredMessage) -> dict[str, Any]:
    """Флаги, не хранящиеся в agent_messages: подозрение на injection (детерминированный скан payload) и
    `loop_limit_exceeded` (FIX_REQUEST без доставки `orchestrator.acp_handler`, §6)."""
    hits = injection.scan(msg.payload)
    exceeded = False
    if msg.message_type == "FIX_REQUEST":
        exceeded = not (
            await conn.execute(
                text("SELECT EXISTS (SELECT 1 FROM message_deliveries WHERE message_id = :m AND consumer_id = :c)"),
                {"m": msg.id, "c": CONSUMER_ACP_HANDLER},
            )
        ).scalar()
    return {"suspected_injection": bool(hits), "injection_hits": hits, "loop_limit_exceeded": exceeded}


def _thread_target(msg: StoredMessage) -> tuple[str, str | None, uuid.UUID | None]:
    """(scope, target_role, target_task_id) треда для сообщения."""
    if USER in (msg.sender_role, msg.recipient_role):
        counterpart = msg.recipient_role if msg.sender_role == USER else msg.sender_role
        if counterpart in AGENT:
            return "role", counterpart, None
        return "all_agents", None, None
    if msg.task_id is not None:
        return "task", None, msg.task_id
    return "all_agents", None, None


async def ensure_thread(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    scope: str,
    target_role: str | None,
    target_task_id: uuid.UUID | None,
) -> uuid.UUID:
    await conn.execute(
        text(
            "INSERT INTO chat_threads (id, workspace_id, run_id, scope, target_role, target_task_id)"
            " VALUES (:id, :ws, :run, :scope, :role, :task) ON CONFLICT ON CONSTRAINT uq_chat_threads_scope DO NOTHING"
        ),
        {"id": uuid7(), "ws": workspace_id, "run": run_id, "scope": scope, "role": target_role, "task": target_task_id},
    )
    thread_id: uuid.UUID = (
        await conn.execute(
            text(
                "SELECT id FROM chat_threads WHERE run_id = :run AND scope = :scope"
                " AND target_role IS NOT DISTINCT FROM CAST(:role AS agent_role_t) AND target_provider IS NULL"
                " AND target_task_id IS NOT DISTINCT FROM CAST(:task AS uuid)"
            ),
            {"run": run_id, "scope": scope, "role": target_role, "task": target_task_id},
        )
    ).scalar_one()
    return thread_id


async def project_chat(conn: AsyncConnection, delivery: Delivery) -> None:
    """Потребитель `chat.projector`: одно сообщение чата на принятое ACP-сообщение."""
    msg = delivery.message
    exists = (
        await conn.execute(
            text("SELECT 1 FROM chat_messages WHERE acp_message_id = :m AND message_kind = 'acp_message' LIMIT 1"),
            {"m": msg.id},
        )
    ).first()
    if exists is not None:
        return
    flags = await message_flags(conn, msg)
    scope, target_role, target_task = _thread_target(msg)
    thread_id = await ensure_thread(
        conn,
        workspace_id=delivery.workspace_id,
        run_id=msg.run_id,
        scope=scope,
        target_role=target_role,
        target_task_id=target_task,
    )
    prefix = (INJECTION_WARNING if flags["suspected_injection"] else "") + (
        LOOP_WARNING if flags["loop_limit_exceeded"] else ""
    )
    head = f"{msg.message_type} {msg.sender_role} → {msg.recipient_role}: "
    body = render.summary(msg.message_type, msg.payload, render.BODY_MAX_CHARS - len(prefix) - len(head))
    body = prefix + head + body
    provider: str | None = None
    if msg.sender_role in AGENT:
        sender_type, sender_role, sender_id = "agent", msg.sender_role, None
        provider = await _provider(conn, msg.id)
    elif msg.sender_role == USER:
        sender_type, sender_role, sender_id = "user", None, msg.sender_user_id
    else:
        sender_type, sender_role, sender_id = "system", None, None
    mentions = [msg.recipient_role] if msg.recipient_role in AGENT else []
    chat_id = uuid7()
    created_at = (
        await conn.execute(
            text(
                "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, sender_id, sender_role,"
                " sender_provider, body, message_kind, acp_message_id, mentions) VALUES (:id, :ws, :run, :th, :st,"
                " :sid, :srole, :prov, :body, 'acp_message', :acp, CAST(:mentions AS agent_role_t[]))"
                " RETURNING created_at"
            ),
            {
                "id": chat_id,
                "ws": delivery.workspace_id,
                "run": msg.run_id,
                "th": thread_id,
                "st": sender_type,
                "sid": sender_id,
                "srole": sender_role,
                "prov": provider,
                "body": body,
                "acp": msg.id,
                "mentions": mentions,
            },
        )
    ).scalar_one()
    sse: dict[str, Any] = {
        "chat_message_id": str(chat_id),
        "thread_id": str(thread_id),
        "scope": SSE_SCOPE[scope],
        "message_kind": "AGENT_MESSAGE",
        "sender_type": sender_type,
        "sender_id": str(sender_id) if sender_id else None,
        "sender_role": msg.sender_role if msg.sender_role != USER else None,
        "provider": provider,
        "task_id": str(msg.task_id) if msg.task_id else None,
        "target_role": msg.recipient_role if msg.recipient_role in AGENT else None,
        "mentions": mentions,
        "body": body,
        "acp_type": msg.message_type,
        "source_event_id": None,
        "links": render.links(list(dict.fromkeys(iter_refs(msg.payload)))),
        "created_at": isoformat_z(created_at),
    }
    if msg.sender_role == ORCHESTRATOR and msg.message_type == "BLOCKED" and msg.payload.get("approval_id"):
        sse["approval_id"] = msg.payload["approval_id"]
    await outbox.emit(
        conn,
        event_type="chat.message",
        aggregate_type="chat_message",
        aggregate_id=chat_id,
        workspace_id=delivery.workspace_id,
        run_id=msg.run_id,
        correlation_id=msg.correlation_id,
        payload=sse,
    )


async def _provider(conn: AsyncConnection, message_id: uuid.UUID) -> str | None:
    value = (
        await conn.execute(text("SELECT sender_provider FROM agent_messages WHERE id = :m"), {"m": message_id})
    ).scalar()
    return str(value) if value is not None else None


async def project_activity(conn: AsyncConnection, delivery: Delivery) -> None:
    """Потребитель `activity.projector`: событие `acp.message_accepted` в agent_activity_events."""
    msg = delivery.message
    exists = (
        await conn.execute(
            text(
                "SELECT 1 FROM agent_activity_events WHERE run_id = :r AND event_type = :t"
                " AND payload ->> 'message_id' = :m LIMIT 1"
            ),
            {"r": msg.run_id, "t": ACTIVITY_ACCEPTED, "m": str(msg.id)},
        )
    ).first()
    if exists is not None:
        return
    flags = await message_flags(conn, msg)
    await insert_activity(
        conn,
        workspace_id=delivery.workspace_id,
        run_id=msg.run_id,
        task_id=msg.task_id,
        execution_id=None,
        event_type=ACTIVITY_ACCEPTED,
        correlation_id=msg.correlation_id,
        occurred_at=msg.created_at,
        payload={
            "message_id": str(msg.id),
            "message_type": msg.message_type,
            "from_role": msg.sender_role,
            "to_role": msg.recipient_role,
            "task_attempt_id": str(msg.task_attempt_id) if msg.task_attempt_id else None,
            "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
            "summary": render.summary(msg.message_type, msg.payload),
            **flags,
        },
    )


HANDLERS = {
    "chat.projector": project_chat,
    "activity.projector": project_activity,
}
