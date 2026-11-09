"""SQL-доступ модуля identity (приватный; другие модули используют aicd_identity.api)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def get_user_by_email(conn: AsyncConnection, email: str) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT u.id, u.email, u.display_name, u.status, c.password_hash, c.failed_attempts,"
                    " c.locked_until FROM users u LEFT JOIN user_credentials c ON c.user_id = u.id"
                    " WHERE u.email = :email"
                ),
                {"email": email},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def get_user(conn: AsyncConnection, user_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (await conn.execute(text("SELECT id, email, display_name, status FROM users WHERE id = :id"), {"id": user_id}))
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def insert_user(conn: AsyncConnection, user_id: uuid.UUID, email: str, display_name: str) -> None:
    await conn.execute(
        text("INSERT INTO users (id, email, display_name) VALUES (:id, :email, :name)"),
        {"id": user_id, "email": email, "name": display_name},
    )


async def upsert_credentials(conn: AsyncConnection, user_id: uuid.UUID, password_hash: str) -> None:
    await conn.execute(
        text(
            "INSERT INTO user_credentials (user_id, password_hash) VALUES (:id, :h)"
            " ON CONFLICT (user_id) DO UPDATE SET password_hash = EXCLUDED.password_hash,"
            " password_changed_at = now(), failed_attempts = 0, locked_until = NULL"
        ),
        {"id": user_id, "h": password_hash},
    )


async def record_login_failure(
    conn: AsyncConnection, user_id: uuid.UUID, max_failures: int, lockout_seconds: int
) -> None:
    await conn.execute(
        text(
            "UPDATE user_credentials SET failed_attempts = failed_attempts + 1,"
            " locked_until = CASE WHEN failed_attempts + 1 >= :max"
            "   THEN now() + make_interval(secs => :lock) ELSE locked_until END"
            " WHERE user_id = :id"
        ),
        {"id": user_id, "max": max_failures, "lock": lockout_seconds},
    )


async def record_login_success(conn: AsyncConnection, user_id: uuid.UUID, new_hash: str | None) -> None:
    await conn.execute(
        text(
            "UPDATE user_credentials SET failed_attempts = 0, locked_until = NULL,"
            " password_hash = COALESCE(:h, password_hash) WHERE user_id = :id"
        ),
        {"id": user_id, "h": new_hash},
    )
    await conn.execute(text("UPDATE users SET last_login_at = now() WHERE id = :id"), {"id": user_id})


async def insert_session(
    conn: AsyncConnection,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    token_hash: str,
    csrf_hash: str,
    ip: str | None,
    user_agent: str | None,
    expires_at: datetime,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO user_sessions (id, user_id, token_hash, csrf_token_hash, ip, user_agent, expires_at,"
            " reauthenticated_at) VALUES (:id, :uid, :th, :ch, CAST(:ip AS inet), :ua, :exp, now())"
        ),
        {
            "id": session_id,
            "uid": user_id,
            "th": token_hash,
            "ch": csrf_hash,
            "ip": ip,
            "ua": (user_agent or "")[:512] or None,
            "exp": expires_at,
        },
    )


async def get_session_by_token_hash(conn: AsyncConnection, token_hash: str) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT s.id, s.user_id, s.csrf_token_hash, s.created_at, s.last_seen_at, s.expires_at,"
                    " s.revoked_at, COALESCE(s.reauthenticated_at, s.created_at) AS reauthenticated_at,"
                    " u.email, u.display_name, u.status AS user_status"
                    " FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = :th"
                ),
                {"th": token_hash},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def get_credentials(conn: AsyncConnection, user_id: uuid.UUID) -> dict[str, Any] | None:
    """Хэш пароля и состояние блокировки пользователя (step-up re-auth, SC-IAM-08)."""
    row = (
        (
            await conn.execute(
                text(
                    "SELECT u.id, u.status, c.password_hash, c.locked_until FROM users u"
                    " LEFT JOIN user_credentials c ON c.user_id = u.id WHERE u.id = :id"
                ),
                {"id": user_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def touch_session(conn: AsyncConnection, session_id: uuid.UUID) -> None:
    await conn.execute(
        text("UPDATE user_sessions SET last_seen_at = now() WHERE id = :id AND revoked_at IS NULL"),
        {"id": session_id},
    )


async def revoke_session(conn: AsyncConnection, session_id: uuid.UUID, reason: str) -> None:
    await conn.execute(
        text("UPDATE user_sessions SET revoked_at = now(), revoke_reason = :r WHERE id = :id AND revoked_at IS NULL"),
        {"id": session_id, "r": reason},
    )


async def list_user_workspaces(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = (await conn.execute(text("SELECT * FROM sys_list_user_workspaces()"))).mappings().all()
    return [dict(r) for r in rows]


async def membership_role(conn: AsyncConnection, workspace_id: uuid.UUID) -> str | None:
    value = (await conn.execute(text("SELECT sys_membership_role(:ws)"), {"ws": workspace_id})).scalar_one_or_none()
    return str(value) if value is not None else None


async def project_access(conn: AsyncConnection, project_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    row = (
        (
            await conn.execute(
                text("SELECT sys_resolve_project_workspace(:p) AS ws, sys_project_role(:p) AS role"),
                {"p": project_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["ws"] is None or row["role"] is None:
        return None
    return row["ws"], str(row["role"])


# --- Членство (RLS: контекст workspace должен быть установлен) -----------------------

_MEMBER_SELECT = (
    "SELECT m.user_id, u.display_name, u.email, m.role, m.joined_at, m.version"
    " FROM workspace_members m JOIN users u ON u.id = m.user_id"
)


async def list_members(
    conn: AsyncConnection, after: tuple[datetime, uuid.UUID] | None, limit: int
) -> list[dict[str, Any]]:
    where = " WHERE (m.joined_at, m.user_id) > (:t, :id)" if after else ""
    rows = (
        (
            await conn.execute(
                text(_MEMBER_SELECT + where + " ORDER BY m.joined_at, m.user_id LIMIT :lim"),
                {"t": after[0] if after else None, "id": after[1] if after else None, "lim": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def get_member(conn: AsyncConnection, user_id: uuid.UUID) -> dict[str, Any] | None:
    row = (await conn.execute(text(_MEMBER_SELECT + " WHERE m.user_id = :id"), {"id": user_id})).mappings().first()
    return dict(row) if row else None


async def insert_member(
    conn: AsyncConnection, workspace_id: uuid.UUID, user_id: uuid.UUID, role: str, invited_by: uuid.UUID | None
) -> None:
    await conn.execute(
        text("INSERT INTO workspace_members (workspace_id, user_id, role, invited_by) VALUES (:ws, :uid, :role, :by)"),
        {"ws": workspace_id, "uid": user_id, "role": role, "by": invited_by},
    )


async def update_member_role(conn: AsyncConnection, user_id: uuid.UUID, role: str, expected_version: int) -> bool:
    result = await conn.execute(
        text(
            "UPDATE workspace_members SET role = :role, version = version + 1"
            " WHERE user_id = :id AND workspace_id = app_current_workspace() AND version = :v"
        ),
        {"role": role, "id": user_id, "v": expected_version},
    )
    return result.rowcount == 1


async def delete_member(conn: AsyncConnection, user_id: uuid.UUID, expected_version: int) -> bool:
    result = await conn.execute(
        text(
            "DELETE FROM workspace_members WHERE user_id = :id AND workspace_id = app_current_workspace()"
            " AND version = :v"
        ),
        {"id": user_id, "v": expected_version},
    )
    return result.rowcount == 1
