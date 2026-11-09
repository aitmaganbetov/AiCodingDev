"""Персональные уведомления пользователя в workspace (UI redesign B8, G-15; API_DESIGN §13.2).

* `GET /notifications` — новые сверху (keyset `(created_at, id)` desc), фильтры `unread_only`, `kind`;
* `GET /notifications/unread-count` — счётчик непрочитанных (бейдж колокольчика);
* `POST /notifications/mark-read` — отметка прочтения: `notification_ids` (≤ 100), `up_to` (все до уведомления
  включительно — «прочитать всё, что я видел») или `all: true`; ответ — число отмеченных и новый счётчик.

Tenant — заголовок `X-Workspace-Id` (не член ⇒ 404, AC-12). Видны только собственные уведомления: RLS
`notifications` для `aicd_app` — `workspace_id = app_current_workspace() AND user_id = app_current_user()`, поэтому
чужие id в `notification_ids` игнорируются, а чужой `up_to` неотличим от несуществующего (404). Создаёт уведомления
только воркер (`notifications.project`); live-доставка — кадры `notification.created` потока
`GET /workspaces/{id}/events`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import app_state, authorize, current_principal, parse_workspace_header, workspace_access
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_collaboration import notifications as store
from aicd_identity import api as identity
from aicd_platform import audit
from aicd_platform.db import transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(tags=["notifications"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
NotificationKind = Literal[
    "approval.requested", "approval.decided", "run.completed", "run.failed", "run.blocked",
    "directive.awaiting_confirmation", "directive.acknowledged", "directive.completed", "directive.failed",
    "pull_request.merged", "pull_request.failed", "finding.critical",
]  # fmt: skip
MAX_KIND_FILTERS = 12


class NotificationMarkReadRequest(BaseModel):
    """Ровно одно из: `notification_ids`, `up_to`, `all` (контракт NotificationMarkReadRequest)."""

    model_config = ConfigDict(extra="forbid")

    notification_ids: Annotated[list[uuid.UUID], Field(min_length=1, max_length=store.MARK_MAX_IDS)] | None = None
    up_to: uuid.UUID | None = None
    all: Literal[True] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = [n for n in ("notification_ids", "up_to", "all") if getattr(self, n) is not None]
        if len(given) != 1:
            raise ValueError("нужно ровно одно из полей: notification_ids, up_to, all")
        return self


async def _access(request: Request, principal: identity.Principal, permission: str) -> Any:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, permission)
    return access


def _keyset(state_key: bytes, cursor: str, filters: dict[str, Any]) -> tuple[datetime, uuid.UUID]:
    key = decode_cursor(state_key, cursor, filters)
    try:
        return datetime.fromisoformat(str(key[0])), uuid.UUID(str(key[1]))
    except (IndexError, TypeError, ValueError) as exc:
        raise ProblemError("invalid_cursor") from exc


@router.get("/notifications", **op("listNotifications", "notification:read"))
async def list_notifications(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    unread_only: bool = False,
    kind: Annotated[list[NotificationKind] | None, Query()] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await _access(request, principal, "notification:read")
    state = app_state(request)
    kinds = sorted({str(k) for k in kind or []})
    if len(kinds) > MAX_KIND_FILTERS:
        raise ProblemError("validation_error", detail=f"Не больше {MAX_KIND_FILTERS} значений фильтра")
    filters = {"r": "notifications", "ws": str(access.workspace_id), "u": unread_only, "k": kinds}
    before = _keyset(state.secret_key, cursor, filters) if cursor else None
    async with transaction(state.engine, access.db()) as conn:
        rows = await store.list_for_user(conn, unread_only=unread_only, kinds=kinds, before=before, limit=limit + 1)
        unread, _latest = await store.unread_count(conn)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [store.view(r) for r in page], "next_cursor": next_cursor, "unread_count": unread})


@router.get("/notifications/unread-count", **op("getNotificationUnreadCount", "notification:read"))
async def get_unread_count(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    access = await _access(request, principal, "notification:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        unread, latest = await store.unread_count(conn)
    return json_response({"unread_count": unread, "latest_unread_at": latest})


@router.post("/notifications/mark-read", **op("markNotificationsRead", "notification:update"))
async def mark_notifications_read(
    request: Request,
    body: NotificationMarkReadRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await _access(request, principal, "notification:update")

    async def command(conn: AsyncConnection) -> CommandResult:
        if body.notification_ids is not None:
            mode = "ids"
            marked = await store.mark_read(conn, ids=sorted(set(body.notification_ids)))
        elif body.up_to is not None:
            mode = "up_to"
            anchor = await store.locate(conn, body.up_to)
            if anchor is None:  # чужое/несуществующее уведомление неотличимы (RLS)
                raise ProblemError("not_found")
            marked = await store.mark_read(conn, up_to=anchor)
        else:
            mode = "all"
            marked = await store.mark_read(conn)
        unread, _latest = await store.unread_count(conn)
        await audit.record(
            conn, action="notification.marked_read", resource_type="workspace", resource_id=access.workspace_id,
            actor_type="user", actor_id=principal.user_id, workspace_id=access.workspace_id,
            metadata={"mode": mode, "marked": marked},
        )  # fmt: skip
        return CommandResult(200, {"marked": marked, "unread_count": unread})

    return await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)
