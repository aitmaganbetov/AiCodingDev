"""Таблица `github_verification_requests` (миграция 0014, OQ-47 (2)): запросы проверки GitHub App на воркере.

API создаёт строку PENDING (`insert`), воркер записывает результат один раз (`succeed` / `fail`; терминальные
состояния неизменяемы — триггер `trg_gvr_transition`). Контекст workspace (RLS) устанавливает вызывающий.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_COLS = (
    "id, workspace_id, kind, status, project_id, params, requested_by, result_id, error_code, error_detail,"
    " created_at, updated_at, completed_at"
)


def view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Представление GitHubVerification (контракт)."""
    error = None
    if row["error_code"] is not None:
        error = {"code": row["error_code"], "detail": row["error_detail"]}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "project_id": row["project_id"],
        "result_id": row["result_id"],
        "error": error,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


async def get(conn: AsyncConnection, verification_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any] | None:
    sql = f"SELECT {_COLS} FROM github_verification_requests WHERE id = :id" + (" FOR UPDATE" if lock else "")  # noqa: S608
    row = (await conn.execute(text(sql), {"id": verification_id})).mappings().first()
    return dict(row) if row else None


async def insert(
    conn: AsyncConnection,
    *,
    verification_id: uuid.UUID,
    workspace_id: uuid.UUID,
    kind: str,
    params: dict[str, Any],
    requested_by: uuid.UUID,
    project_id: uuid.UUID | None,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO github_verification_requests (id, workspace_id, kind, project_id, params, requested_by)"
            " VALUES (:id, :ws, :kind, :p, CAST(:params AS jsonb), :uid)"
        ),
        {
            "id": verification_id,
            "ws": workspace_id,
            "kind": kind,
            "p": project_id,
            "params": json.dumps(params, sort_keys=True),
            "uid": requested_by,
        },
    )


async def succeed(conn: AsyncConnection, verification_id: uuid.UUID, result_id: uuid.UUID) -> None:
    await conn.execute(
        text(
            "UPDATE github_verification_requests SET status = 'SUCCEEDED', result_id = :r, completed_at = now()"
            " WHERE id = :id"
        ),
        {"r": result_id, "id": verification_id},
    )


async def fail(conn: AsyncConnection, verification_id: uuid.UUID, code: str, detail: str) -> None:
    await conn.execute(
        text(
            "UPDATE github_verification_requests SET status = 'FAILED', error_code = :c, error_detail = :d,"
            " completed_at = now() WHERE id = :id"
        ),
        {"c": code, "d": detail[:500], "id": verification_id},
    )
