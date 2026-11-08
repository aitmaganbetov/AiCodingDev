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
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from aicd_platform.config import Settings

ActorType = Literal["user", "agent", "system"]


@dataclass(frozen=True)
class DbContext:
    user_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    actor_type: ActorType = "user"


@dataclass(frozen=True)
class TcpKeepalive:
    """Обнаружение «мёртвого» соединения с PostgreSQL на уровне TCP (P9-03, NFR-REL-01).

    Без keepalive соединение, у которого сеть молча теряет пакеты (без RST: сбой маршрута, failover за NAT/LB),
    висит до таймаута ядра (~2 ч): запрос ждёт ответа, `statement_timeout` сервера не помогает — сервер запрос
    не получил. `keepalives_*` — проба простаивающего соединения; `tcp_user_timeout` — предел для неподтверждённых
    отправленных данных (запрос ушёл, ответа нет). Значения — `Settings.db_keepalives_*`, `db_tcp_user_timeout_ms`.
    """

    idle_seconds: int = 30
    interval_seconds: int = 10
    count: int = 3
    tcp_user_timeout_ms: int = 60_000

    @classmethod
    def from_settings(cls, settings: Settings) -> TcpKeepalive:
        return cls(
            idle_seconds=settings.db_keepalives_idle_seconds,
            interval_seconds=settings.db_keepalives_interval_seconds,
            count=settings.db_keepalives_count,
            tcp_user_timeout_ms=settings.db_tcp_user_timeout_ms,
        )

    def libpq_params(self) -> dict[str, int]:
        params = {
            "keepalives": 1,
            "keepalives_idle": self.idle_seconds,
            "keepalives_interval": self.interval_seconds,
            "keepalives_count": self.count,
        }
        if self.tcp_user_timeout_ms:
            params["tcp_user_timeout"] = self.tcp_user_timeout_ms
        return params


def connect_args(keepalive: TcpKeepalive | None = None) -> dict[str, Any]:
    """Параметры libpq для каждого соединения пула: UTC, statement_timeout и TCP keepalive."""
    return {"options": "-c timezone=UTC -c statement_timeout=15000", **(keepalive or TcpKeepalive()).libpq_params()}


def create_engine(url: str, *, pool_size: int = 10, keepalive: TcpKeepalive | None = None) -> AsyncEngine:
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args=connect_args(keepalive),
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
