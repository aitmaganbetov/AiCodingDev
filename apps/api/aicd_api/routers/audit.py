"""Журнал аудита (ТЗ §8, FR-AUD-02). Чтение журнала само аудируется (`audit.query`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from starlette.responses import Response

from aicd_api import views
from aicd_api.deps import app_state, authorize, current_principal, parse_workspace_header, workspace_access
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_identity import api as identity
from aicd_platform import audit
from aicd_platform.db import transaction
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(tags=["audit"])

_AUDIT_COLUMNS = (
    "id, workspace_id, actor_type, actor_id, action, resource_type, resource_id, outcome, metadata, created_at"
)


@router.get("/audit-events", **op("listAuditEvents", "audit:read"))
async def list_audit_events(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    actor_id: uuid.UUID | None = None,
    action: Annotated[str | None, Query(max_length=128)] = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    resource_id: uuid.UUID | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "audit:read")
    state = app_state(request)
    filters: dict[str, Any] = {
        "actor_id": actor_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "from": from_,
        "to": to,
    }
    clauses = ["workspace_id = app_current_workspace()"]
    params: dict[str, Any] = {"lim": limit + 1}
    for column, value in (
        ("actor_id", actor_id),
        ("action", action),
        ("resource_type", resource_type),
        ("resource_id", resource_id),
    ):
        if value is not None:
            clauses.append(f"{column} = :{column}")
            params[column] = value
    if from_ is not None:
        clauses.append("created_at >= :from_ts")
        params["from_ts"] = from_
    if to is not None:
        clauses.append("created_at < :to_ts")
        params["to_ts"] = to
    cursor_filters = {"r": "audit", "ws": str(access.workspace_id), **{k: str(v) for k, v in filters.items()}}
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, cursor_filters)
        clauses.append("(created_at, id) < (:ct, :cid)")
        params.update(ct=datetime.fromisoformat(t), cid=uuid.UUID(i))
    # Условия собираются только из фиксированных имён колонок; значения — bind-параметры.
    where = " AND ".join(clauses)
    sql = f"SELECT {_AUDIT_COLUMNS} FROM audit_events WHERE {where} ORDER BY created_at DESC, id DESC LIMIT :lim"  # noqa: S608
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
        await audit.record(
            conn,
            action="audit.query",
            resource_type="audit_log",
            resource_id=access.workspace_id,
            actor_type="user",
            actor_id=principal.user_id,
            workspace_id=access.workspace_id,
            metadata={"filters": {k: str(v) for k, v in filters.items() if v is not None}},
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], cursor_filters)
    return json_response({"items": [views.audit_event(r) for r in page], "next_cursor": next_cursor})
