"""Idempotency-Key для POST-команд (API_DESIGN §8, DATABASE_DESIGN §5.7).

1. Короткая транзакция резервирует ключ (`IN_PROGRESS`) или читает существующую запись.
2. Тот же ключ с другим запросом ⇒ 422; выполняется (lease 60 с) ⇒ 409; завершён ⇒ повтор ответа.
3. Бизнес-транзакция выполняет команду и в той же транзакции сохраняет ответ (`COMPLETED`).
4. Детерминированные бизнес-ошибки (409 conflict, 422) сохраняются; прочие сбои освобождают ключ.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import app_state
from aicd_api.responses import json_response, problem_body
from aicd_api.serialization import dumps, to_jsonable
from aicd_platform import context
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7

KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,255}$")
LEASE_SECONDS = 60
# Бизнес-ошибки, ответ на которые детерминирован и сохраняется для повторов.
_STORED_PROBLEMS = frozenset({"conflict", "validation_error", "invalid_state_transition", "last_owner"})


@dataclass
class CommandResult:
    status_code: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)


def _request_hash(request: Request, body: Any) -> str:
    canonical = json.dumps(to_jsonable(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    material = "\n".join(
        [request.method, request.scope["route"].path, request.url.path, canonical, request.headers.get("if-match", "")]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def require_key(request: Request) -> str:
    key = request.headers.get("idempotency-key")
    if key is None:
        raise ProblemError("idempotency_key_missing")
    if not KEY_RE.fullmatch(key):
        raise ProblemError("validation_error", detail="Idempotency-Key: 8–255 символов [A-Za-z0-9._:-]")
    return key


def _replay(row: Any) -> Response:
    stored = row["response"] or {}
    headers = dict(stored.get("headers", {}))
    headers["Idempotency-Replayed"] = "true"
    status = int(row["status_code"])
    media = "application/problem+json" if status >= 400 else "application/json"
    if status == 204:
        return Response(status_code=204, headers=headers)
    return Response(dumps(stored.get("body")), status_code=status, media_type=media, headers=headers)


async def run_idempotent(
    request: Request,
    ctx: DbContext,
    body: Any,
    command: Callable[[AsyncConnection], Awaitable[CommandResult]],
) -> Response:
    state = app_state(request)
    key = require_key(request)
    req_hash = _request_hash(request, body)
    if ctx.user_id is None:
        raise RuntimeError("idempotent commands require an authenticated user")

    async with transaction(state.engine, ctx) as conn:
        params = {
            "id": uuid7(),
            "ws": ctx.workspace_id,
            "uid": ctx.user_id,
            "key": key,
            "m": request.method,
            "p": request.url.path[:2048],
            "h": req_hash,
        }
        inserted = (
            await conn.execute(
                text(
                    "INSERT INTO idempotency_keys (id, workspace_id, user_id, key, method, path, request_hash)"
                    " VALUES (:id, :ws, :uid, :key, :m, :p, :h)"
                    " ON CONFLICT ON CONSTRAINT uq_idempotency_keys_scope DO NOTHING RETURNING id"
                ),
                params,
            )
        ).scalar_one_or_none()
        if inserted is None:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, request_hash, status, status_code, response,"
                            " created_at > now() - make_interval(secs => :lease) AS lease_alive,"
                            " expires_at < now() AS expired"
                            " FROM idempotency_keys WHERE user_id = :uid AND key = :key"
                            " AND workspace_id IS NOT DISTINCT FROM :ws FOR UPDATE"
                        ),
                        {"uid": ctx.user_id, "key": key, "ws": ctx.workspace_id, "lease": LEASE_SECONDS},
                    )
                )
                .mappings()
                .one()
            )
            if row["expired"]:
                await conn.execute(
                    text(
                        "UPDATE idempotency_keys SET request_hash = :h, method = :m, path = :p, status = 'IN_PROGRESS',"
                        " status_code = NULL, response = NULL, created_at = now(),"
                        " expires_at = now() + interval '24 hours' WHERE id = :id"
                    ),
                    {"h": req_hash, "m": request.method, "p": params["p"], "id": row["id"]},
                )
                inserted = row["id"]
            elif row["request_hash"] != req_hash:
                raise ProblemError("idempotency_key_reuse")
            elif row["status"] == "COMPLETED":
                return _replay(row)
            elif row["lease_alive"]:
                raise ProblemError("idempotency_key_in_flight", headers={"Retry-After": "1"})
            else:
                # Предыдущая попытка аварийно прервалась (lease истёк) — перехватываем ключ.
                await conn.execute(
                    text("UPDATE idempotency_keys SET created_at = now() WHERE id = :id"), {"id": row["id"]}
                )
                inserted = row["id"]
    record_id: uuid.UUID = inserted

    try:
        async with transaction(state.engine, ctx) as conn:
            result = await command(conn)
            await _complete(conn, record_id, result.status_code, result.body, result.headers)
    except ProblemError as err:
        if err.code in _STORED_PROBLEMS:
            corr = context.snapshot()
            problem = problem_body(err, instance=request.url.path, request_id=corr.get("request_id"))
            async with transaction(state.engine, ctx) as conn:
                await _complete(conn, record_id, err.status, problem, err.headers)
        else:
            await _release(state.engine, ctx, record_id)
        raise
    except BaseException:
        await _release(state.engine, ctx, record_id)
        raise
    if result.status_code == 204:
        return Response(status_code=204, headers=result.headers)
    return json_response(result.body, result.status_code, result.headers)


async def _complete(
    conn: AsyncConnection, record_id: uuid.UUID, status: int, body: Any, headers: dict[str, str]
) -> None:
    stored = {"body": to_jsonable(body), "headers": {k: v for k, v in headers.items() if k in ("ETag", "Location")}}
    await conn.execute(
        text(
            "UPDATE idempotency_keys SET status = 'COMPLETED', status_code = :s, response = CAST(:r AS jsonb)"
            " WHERE id = :id"
        ),
        {"s": status, "r": json.dumps(stored, ensure_ascii=False), "id": record_id},
    )


async def _release(engine: Any, ctx: DbContext, record_id: uuid.UUID) -> None:
    async with transaction(engine, ctx) as conn:
        await conn.execute(
            text("DELETE FROM idempotency_keys WHERE id = :id AND status = 'IN_PROGRESS'"), {"id": record_id}
        )
