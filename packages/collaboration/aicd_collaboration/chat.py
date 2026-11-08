"""Live Agent Chat: комментарии пользователя и форма события `chat.message` (EVENT_SCHEMAS §4, API_DESIGN §12.2).

Комментарий (`POST /runs/{id}/chat/messages`) виден в ленте, но агентам НЕ доставляется и на исполнение не
влияет: воздействие на агентов — только поручения (`POST /runs/{id}/directives`). Поэтому здесь нет записи в
`agent_messages` — только `chat_messages` + outbox `chat.message` (роль БД `aicd_app` имеет INSERT на чат, 0003).

Payload `chat.message` в outbox сразу имеет форму SSE `chat_message`
(`packages/contracts/schemas/events/1.0/sse-event.schema.json`): проекция `aicd_platform.sse` лишь фильтрует поля.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_collaboration.acp.projection import ensure_thread
from aicd_platform import audit, outbox
from aicd_platform.clock import isoformat_z
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text

# chat_threads.scope (БД) → scope SSE chat_message и ChatThread.scope API (EVENT_SCHEMAS §4.1: RUN/ROLE/TASK).
# Треды 'provider'/'approvals' в схеме БД есть, но не создаются: потоки Codex/Claude/Approvals — фильтры ленты.
SSE_SCOPE = {"all_agents": "RUN", "role": "ROLE", "task": "TASK"}
API_SCOPE = {"all_agents": "run", "role": "role", "task": "task"}
BODY_MAX_CHARS = 8000  # SSE chat_message.body maxLength


def event_payload(
    *,
    chat_message_id: uuid.UUID,
    thread_id: uuid.UUID,
    scope: str,
    message_kind: str,
    sender_type: str,
    body: str,
    created_at: datetime,
    sender_id: uuid.UUID | None = None,
    sender_role: str | None = None,
    provider: str | None = None,
    task_id: uuid.UUID | None = None,
    target_role: str | None = None,
    mentions: Sequence[str] = (),
    directive_id: uuid.UUID | None = None,
    approval_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Payload outbox-события `chat.message` в форме SSE `chat_message` (additionalProperties: false)."""
    payload: dict[str, Any] = {
        "chat_message_id": str(chat_message_id),
        "thread_id": str(thread_id),
        "scope": SSE_SCOPE.get(scope, "RUN"),
        "message_kind": message_kind,
        "sender_type": sender_type,
        "sender_id": str(sender_id) if sender_id else None,
        "sender_role": sender_role,
        "provider": provider,
        "task_id": str(task_id) if task_id else None,
        "target_role": target_role,
        "mentions": list(mentions)[:12],
        "body": body[:BODY_MAX_CHARS],
        "source_event_id": None,
        "created_at": isoformat_z(created_at),
    }
    if directive_id is not None:
        payload["directive_id"] = str(directive_id)
    if approval_id is not None:
        payload["approval_id"] = str(approval_id)
    return payload


async def run_thread(conn: AsyncConnection, workspace_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    """Общий тред run (`all_agents` ≙ `RUN`): создаётся лениво, ровно один (uq_chat_threads_scope)."""
    return await ensure_thread(
        conn, workspace_id=workspace_id, run_id=run_id, scope="all_agents", target_role=None, target_task_id=None
    )


async def post_user_message(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    body: str,
    thread_id: uuid.UUID | None,
    mentions: Sequence[str],
) -> dict[str, Any]:
    """Комментарий пользователя: chat_messages(kind=text) + outbox `chat.message` (USER_TEXT) + audit, одна транзакция."""
    if thread_id is None:
        thread: dict[str, Any] = {
            "id": await run_thread(conn, workspace_id, run_id),
            "scope": "all_agents",
            "target_role": None,
            "target_task_id": None,
        }
    else:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, scope, target_role, target_task_id FROM chat_threads WHERE id = :t AND run_id = :r"
                    ),
                    {"t": thread_id, "r": run_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ProblemError(
                "validation_error", errors=[FieldError("/thread_id", "not_found", "тред не найден в этом run")]
            )
        thread = dict(row)
    message_id = uuid7()
    clean = redact_text(body)
    roles = list(dict.fromkeys(mentions))
    created_at: datetime = (
        await conn.execute(
            text(
                "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, sender_id, body,"
                " message_kind, mentions) VALUES (:id, :ws, :run, :th, 'user', :uid, :body, 'text',"
                " CAST(:mentions AS agent_role_t[])) RETURNING created_at"
            ),
            {
                "id": message_id,
                "ws": workspace_id,
                "run": run_id,
                "th": thread["id"],
                "uid": user_id,
                "body": clean,
                "mentions": roles,
            },
        )
    ).scalar_one()
    await outbox.emit(
        conn,
        event_type="chat.message",
        aggregate_type="chat_message",
        aggregate_id=message_id,
        workspace_id=workspace_id,
        run_id=run_id,
        payload=event_payload(
            chat_message_id=message_id,
            thread_id=thread["id"],
            scope=thread["scope"],
            message_kind="USER_TEXT",
            sender_type="user",
            sender_id=user_id,
            body=clean,
            created_at=created_at,
            task_id=thread["target_task_id"],
            target_role=thread["target_role"],
            mentions=roles,
        ),
    )
    # Тело сообщения в audit не пишется (API_DESIGN §17) — только ссылки.
    await audit.record(
        conn,
        action="chat.message.create",
        resource_type="chat_message",
        resource_id=message_id,
        actor_type="user",
        actor_id=user_id,
        workspace_id=workspace_id,
        metadata={"run_id": str(run_id), "thread_id": str(thread["id"]), "mentions": roles},
    )
    return {
        "id": message_id,
        "thread_id": thread["id"],
        "sender_type": "user",
        "sender_id": user_id,
        "body": clean,
        "message_kind": "text",
        "created_at": created_at,
    }
