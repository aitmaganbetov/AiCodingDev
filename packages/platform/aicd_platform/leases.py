"""Аренды ресурсов с fencing token (ADR-0007, DATA_FLOW §5, P3-04).

* захват: свободная/просроченная аренда получает новый монотонный токен из `worker_lease_fencing_seq`;
* heartbeat продлевает аренду только при совпадении токена;
* любая запись результата обязана сверять токен (иначе — «зомби»-воркер после истечения аренды).
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform.ids import uuid7

DEFAULT_TTL_SECONDS = 60


def worker_identity(kind: str) -> str:
    return f"{socket.gethostname()}:{kind}:{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Lease:
    resource_type: str
    resource_id: uuid.UUID
    worker_id: str
    fencing_token: int
    lease_until: datetime


async def acquire(
    conn: AsyncConnection,
    *,
    resource_type: str,
    resource_id: uuid.UUID,
    worker_id: str,
    workspace_id: uuid.UUID | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Lease | None:
    row = (
        await conn.execute(
            text(
                "INSERT INTO worker_leases (id, resource_type, resource_id, workspace_id, worker_id, fencing_token,"
                " lease_until) VALUES (:id, :rt, :rid, :ws, :wid, nextval('worker_lease_fencing_seq'),"
                " now() + make_interval(secs => :ttl))"
                " ON CONFLICT ON CONSTRAINT uq_worker_leases_resource DO UPDATE SET"
                " worker_id = EXCLUDED.worker_id, fencing_token = nextval('worker_lease_fencing_seq'),"
                " acquired_at = now(), heartbeat_at = now(), lease_until = EXCLUDED.lease_until, released_at = NULL,"
                " workspace_id = EXCLUDED.workspace_id"
                " WHERE worker_leases.released_at IS NOT NULL OR worker_leases.lease_until < now()"
                " RETURNING fencing_token, lease_until"
            ),
            {"id": uuid7(), "rt": resource_type, "rid": resource_id, "ws": workspace_id, "wid": worker_id,
             "ttl": ttl_seconds},
        )
    ).first()
    if row is None:
        return None
    return Lease(resource_type, resource_id, worker_id, int(row[0]), row[1])


async def heartbeat(conn: AsyncConnection, lease: Lease, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    result = await conn.execute(
        text(
            "UPDATE worker_leases SET heartbeat_at = now(), lease_until = now() + make_interval(secs => :ttl)"
            " WHERE resource_type = :rt AND resource_id = :rid AND fencing_token = :tok AND released_at IS NULL"
            " AND lease_until >= now()"
        ),
        {"ttl": ttl_seconds, "rt": lease.resource_type, "rid": lease.resource_id, "tok": lease.fencing_token},
    )
    return result.rowcount == 1


async def release(conn: AsyncConnection, lease: Lease) -> bool:
    result = await conn.execute(
        text(
            "UPDATE worker_leases SET released_at = now() WHERE resource_type = :rt AND resource_id = :rid"
            " AND fencing_token = :tok AND released_at IS NULL"
        ),
        {"rt": lease.resource_type, "rid": lease.resource_id, "tok": lease.fencing_token},
    )
    return result.rowcount == 1


async def is_current(conn: AsyncConnection, resource_type: str, resource_id: uuid.UUID, token: int) -> bool:
    value = (
        await conn.execute(
            text(
                "SELECT 1 FROM worker_leases WHERE resource_type = :rt AND resource_id = :rid AND fencing_token = :tok"
                " AND released_at IS NULL AND lease_until >= now()"
            ),
            {"rt": resource_type, "rid": resource_id, "tok": token},
        )
    ).scalar_one_or_none()
    return value is not None


async def expired(conn: AsyncConnection, resource_type: str, limit: int = 100) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    """Истёкшие неосвобождённые аренды (для reaper): (resource_id, workspace_id)."""
    rows = (
        await conn.execute(
            text(
                "SELECT resource_id, workspace_id FROM worker_leases WHERE resource_type = :rt"
                " AND released_at IS NULL AND lease_until < now() ORDER BY lease_until LIMIT :lim"
            ),
            {"rt": resource_type, "lim": limit},
        )
    ).all()
    return [(r[0], r[1]) for r in rows]
