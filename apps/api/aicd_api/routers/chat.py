"""Live Agent Chat (ТЗ §8.2, §11; API_DESIGN §12; EVENT_SCHEMAS §4; P4-12): лента run и комментарии пользователя.

Лента — проекция `chat_messages` (туда же проецируются ACP-сообщения, поручения, approvals и выдержки CLI) с
присоединёнными `agent_messages`/`user_directives`/`approvals`. Потоки `stream` — фильтры одной ленты.
`POST .../chat/messages` — комментарий: агентам не доставляется (воздействие — только `POST .../directives`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import _remember_rate, app_state, authorize, current_principal
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_api.routers.directives import AgentRole, run_access
from aicd_collaboration import chat as chat_service
from aicd_identity import api as identity
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_platform.redaction import redact_text

router = APIRouter(prefix="/runs/{run_id}/chat", tags=["chat"])

ChatStream = Literal["all_agents", "codex", "claude", "role", "task", "approvals"]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
CHAT_MESSAGES_PER_MINUTE = 30  # API_DESIGN §10: директивы и chat messages — user + run
_AGENT_ROLES = frozenset(get_args(AgentRole))

# chat_messages.message_kind (БД) → (ChatItem.item_type, ChatItem.message_kind).
_KIND_VIEW: dict[str, tuple[str, str]] = {
    "text": ("chat_message", "text"),
    "directive": ("directive", "directive"),
    "acp_message": ("agent_message", "agent_relay"),
    "cli_excerpt": ("cli_excerpt", "agent_relay"),
    "approval": ("approval", "system_notice"),
    "activity": ("system_event", "system_notice"),
    "system_notice": ("system_event", "system_notice"),
}
# ChatItem.item_type → message_kind БД (фильтр item_type).
_ITEM_KINDS: dict[str, list[str]] = {}
for _db_kind, (_item_type, _) in _KIND_VIEW.items():
    _ITEM_KINDS.setdefault(_item_type, []).append(_db_kind)


class ChatMessageCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, Field(min_length=1, max_length=8000)]
    thread_id: uuid.UUID | None = None
    mentions: Annotated[list[AgentRole], Field(max_length=12)] = []

    @field_validator("body")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:  # PostgreSQL text не хранит NUL
            raise ValueError("NUL-символ недопустим")
        return value

    @field_validator("mentions")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("упоминания должны быть уникальны")
        return value


_FEED_SELECT = (
    "SELECT m.id, m.thread_id, m.sender_type, m.sender_id, u.display_name AS sender_name,"
    " m.sender_role::text AS sender_role, m.sender_provider::text AS sender_provider, m.body, m.message_kind,"
    " m.source_event_id, CAST(m.mentions AS text[]) AS mentions, m.created_at,"
    " th.target_role::text AS thread_role, th.target_task_id AS thread_task,"
    " am.message_type::text AS acp_type, am.recipient_role AS acp_recipient, am.task_id AS acp_task,"
    " d.id AS directive_id, d.status AS directive_status, d.target_role::text AS directive_role,"
    " d.target_task_id AS directive_task, d.approval_id, ap.status AS approval_status"
    " FROM chat_messages m JOIN chat_threads th ON th.id = m.thread_id"
    " LEFT JOIN users u ON u.id = m.sender_id"
    " LEFT JOIN agent_messages am ON am.id = m.acp_message_id"
    " LEFT JOIN user_directives d ON d.chat_message_id = m.id"
    " LEFT JOIN approvals ap ON ap.id = d.approval_id"
)


def _item(r: dict[str, Any]) -> dict[str, Any]:
    item_type, kind = _KIND_VIEW.get(r["message_kind"], ("system_event", "system_notice"))
    acp_recipient = r["acp_recipient"] if r["acp_recipient"] in _AGENT_ROLES else None
    user = {"id": r["sender_id"], "display_name": r["sender_name"] or ""} if r["sender_id"] else None
    return {
        "id": r["id"],
        "item_type": item_type,
        "thread_id": r["thread_id"],
        "task_id": r["acp_task"] or r["directive_task"] or r["thread_task"],
        "sender_type": r["sender_type"],
        "sender": {"user": user, "role": r["sender_role"], "provider": r["sender_provider"]},
        "recipient_role": acp_recipient or r["directive_role"] or r["thread_role"],
        "message_kind": kind,
        # Тело сохраняется уже редактированным; повторная редакция — защита на чтении (контракт: «санитизированный»).
        "body": redact_text(r["body"]),
        "acp_type": r["acp_type"],
        "directive_id": r["directive_id"],
        "directive_status": r["directive_status"],
        "approval_id": r["approval_id"],
        "approval_status": r["approval_status"],
        "mentions": list(r["mentions"] or []),
        "refs": [],
        "source_event_id": r["source_event_id"],
        "seq": None,
        "created_at": r["created_at"],
    }


async def _threads(conn: AsyncConnection, run_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT th.id, th.run_id, th.scope, th.target_role::text AS target_role, th.target_task_id,"
                    " th.created_by, u.display_name, th.created_at FROM chat_threads th"
                    " LEFT JOIN users u ON u.id = th.created_by"
                    " WHERE th.run_id = :run AND th.scope = ANY(:scopes) ORDER BY th.created_at, th.id"
                ),
                {"run": run_id, "scopes": list(chat_service.API_SCOPE)},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": r["id"],
            "run_id": r["run_id"],
            "scope": chat_service.API_SCOPE[r["scope"]],
            "target_role": r["target_role"],
            "task_id": r["target_task_id"],
            "created_by": {"id": r["created_by"], "display_name": r["display_name"] or ""} if r["created_by"] else None,
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@router.get("", **op("getRunChat", "chat:read"))
async def get_run_chat(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    stream: ChatStream = "all_agents",
    role: AgentRole | None = None,
    task_id: uuid.UUID | None = None,
    thread_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    item_type: Annotated[list[str] | None, Query()] = None,
    order: Literal["asc", "desc"] = "desc",
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await run_access(request, principal, run_id)
    await authorize(request, access, "chat:read")
    errors: list[FieldError] = []
    if stream == "role" and role is None:
        errors.append(FieldError("/role", "required", "обязателен при stream=role"))
    if stream == "task" and task_id is None:
        errors.append(FieldError("/task_id", "required", "обязателен при stream=task"))
    if q is not None and "\x00" in q:
        errors.append(FieldError("/q", "invalid", "NUL-символ недопустим"))
    unknown = sorted(set(item_type or []) - set(_ITEM_KINDS))
    if unknown:
        errors.append(FieldError("/item_type", "enum", f"допустимо: {', '.join(sorted(_ITEM_KINDS))}"))
    if errors:
        raise ProblemError("validation_error", errors=errors)
    state = app_state(request)
    filters = {
        "r": "chat",
        "run": str(run_id),
        "s": stream,
        "role": role,
        "task": str(task_id),
        "th": str(thread_id),
        "q": q,
        "it": sorted(item_type or []),
        "o": order,
    }
    clauses = ["m.run_id = :run"]
    params: dict[str, Any] = {"run": run_id, "lim": limit + 1}
    if stream in ("codex", "claude"):
        clauses.append("COALESCE(m.sender_provider::text, am.sender_provider::text) = :prov")
        params["prov"] = stream
    elif stream == "role":
        clauses.append(
            "(m.sender_role::text = :role OR th.target_role::text = :role OR :role = ANY(CAST(m.mentions AS text[]))"
            " OR am.sender_role = :role OR am.recipient_role = :role OR d.target_role::text = :role)"
        )
        params["role"] = role
    elif stream == "task":
        clauses.append("(th.target_task_id = :task OR am.task_id = :task OR d.target_task_id = :task)")
        params["task"] = task_id
    elif stream == "approvals":
        clauses.append(
            "(m.message_kind = 'approval' OR d.directive_type IN ('APPROVE', 'REJECT')"
            " OR d.status = 'AWAITING_CONFIRMATION')"
        )
    if thread_id is not None:
        clauses.append("m.thread_id = :th")
        params["th"] = thread_id
    if q:
        clauses.append("m.search_vector @@ plainto_tsquery('simple', :q)")
        params["q"] = q
    if item_type:
        clauses.append("m.message_kind = ANY(:kinds)")
        params["kinds"] = [k for it in item_type for k in _ITEM_KINDS[it]]
    direction = "DESC" if order == "desc" else "ASC"
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        clauses.append(f"(m.created_at, m.id) {'<' if order == 'desc' else '>'} (:ct, :cid)")
        params.update(ct=datetime.fromisoformat(t), cid=uuid.UUID(i))
    sql = (
        _FEED_SELECT
        + " WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY m.created_at {direction}, m.id {direction} LIMIT :lim"
    )
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
        threads = await _threads(conn, run_id)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [_item(r) for r in page], "next_cursor": next_cursor, "threads": threads})


@router.post("/messages", status_code=201, **op("postChatMessage", "chat:write"))
async def post_chat_message(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    body: ChatMessageCreateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await run_access(request, principal, run_id)
    await authorize(request, access, "chat:write")
    state = app_state(request)
    _remember_rate(
        request,
        await state.limiter.hit(f"chat:user:{principal.user_id}:run:{run_id}", CHAT_MESSAGES_PER_MINUTE),
    )

    async def command(conn: AsyncConnection) -> CommandResult:
        message = await chat_service.post_user_message(
            conn,
            workspace_id=access.workspace_id,
            run_id=run_id,
            user_id=principal.user_id,
            body=body.body,
            thread_id=body.thread_id,
            mentions=body.mentions,
        )
        location = f"/api/v1/runs/{run_id}/chat?thread_id={message['thread_id']}"
        return CommandResult(201, message, {"Location": location})

    return await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)
