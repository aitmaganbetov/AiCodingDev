"""Запись audit_events в той же транзакции, что и изменение (FR-AUD-01, API_DESIGN §17)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform import context
from aicd_platform.db import ActorType
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_value


async def record(
    conn: AsyncConnection,
    *,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_id: uuid.UUID | None,
    workspace_id: uuid.UUID | None,
    outcome: str = "success",
    metadata: dict[str, Any] | None = None,
) -> uuid.UUID:
    # Контракт AuditEvent требует resource_id для событий workspace (GET /audit-events).
    if workspace_id is not None and resource_id is None:
        raise ValueError("событие workspace должно ссылаться на ресурс")
    meta: dict[str, Any] = {}
    request_id = context.snapshot().get("request_id")
    if metadata:
        meta.update(metadata)
    event_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO audit_events (id, workspace_id, actor_type, actor_id, action, resource_type,"
            " resource_id, outcome, request_id, metadata) VALUES (:id, :ws, :actor_type, :actor_id, :action,"
            " :rtype, :rid, :outcome, :req, CAST(:meta AS jsonb))"
        ),
        {
            "id": event_id,
            "ws": workspace_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "rtype": resource_type,
            "rid": resource_id,
            "outcome": outcome,
            "req": _as_uuid(request_id),
            "meta": json.dumps(redact_value(meta), ensure_ascii=False, default=str),
        },
    )
    return event_id


def _as_uuid(value: str | None) -> uuid.UUID | None:
    """request_id = W3C trace-id (32 hex) — совместим с uuid по формату."""
    if not value:
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None
