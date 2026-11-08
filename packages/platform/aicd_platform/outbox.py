"""Transactional outbox (ADR-0002): событие пишется в той же транзакции, что и изменение состояния.

Доставку (outbox → Dramatiq/Redis + run_event_log для SSE) выполняет worker-outbox.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform import context
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_value
from aicd_platform.telemetry import current_traceparent

MAX_PAYLOAD_BYTES = 65536


async def emit(
    conn: AsyncConnection,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
    workspace_id: uuid.UUID | None,
    run_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
    schema_version: str = "1.0",
) -> uuid.UUID:
    body = json.dumps(redact_value(payload), ensure_ascii=False, default=str)
    if len(body.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload события превышает 64 KiB — крупные данные передаются ссылками на artifacts")
    if correlation_id is None:
        corr = context.snapshot().get("run_id")
        correlation_id = uuid.UUID(corr) if corr else run_id
    event_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO outbox_events (id, workspace_id, run_id, aggregate_type, aggregate_id, event_type,"
            " schema_version, payload, correlation_id, causation_id, traceparent)"
            " VALUES (:id, :ws, :run, :atype, :aid, :etype, :sv, CAST(:payload AS jsonb), :corr, :cause, :tp)"
        ),
        {
            "id": event_id,
            "ws": workspace_id,
            "run": run_id,
            "atype": aggregate_type,
            "aid": aggregate_id,
            "etype": event_type,
            "sv": schema_version,
            "payload": body,
            "corr": correlation_id,
            "cause": causation_id,
            # W3C-контекст текущего спана: worker-outbox продолжает трассу при публикации (P8-06).
            "tp": current_traceparent(),
        },
    )
    return event_id
