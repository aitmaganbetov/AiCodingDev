"""Доступ к PostgreSQL с RLS-контекстом на транзакцию (ADR-0008).

Каждая транзакция приложения выполняется с `SET LOCAL app.workspace_id / app.user_id /
app.actor_type` через `set_config(..., is_local => true)`. Без контекста RLS-политики
возвращают ноль строк (fail-closed).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

ActorType = Literal["user", "agent", "system"]


@dataclass(frozen=True)
class DbContext:
    user_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    actor_type: ActorType = "user"


def create_engine(url: str, *, pool_size: int = 10) -> AsyncEngine:
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC -c statement_timeout=15000"},
    )


async def apply_context(conn: AsyncConnection, ctx: DbContext) -> None:
    await conn.execute(
        text(
            "SELECT set_config('app.user_id', :uid, true),"
            " set_config('app.workspace_id', :ws, true),"
            " set_config('app.actor_type', :actor, true)"
        ),
        {
            "uid": str(ctx.user_id) if ctx.user_id else "",
            "ws": str(ctx.workspace_id) if ctx.workspace_id else "",
            "actor": ctx.actor_type,
        },
    )


@asynccontextmanager
async def transaction(engine: AsyncEngine, ctx: DbContext) -> AsyncIterator[AsyncConnection]:
    """Транзакция с RLS-контекстом; commit при успехе, rollback при исключении."""
    async with engine.begin() as conn:
        await apply_context(conn, ctx)
        yield conn
