"""Публичный фасад модуля identity."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_identity import passwords, repository, tokens
from aicd_identity.rbac import RANK, Role, effective_role
from aicd_platform import audit
from aicd_platform.clock import utcnow
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7


@dataclass(frozen=True)
class SessionPolicy:
    absolute_ttl: timedelta
    idle_ttl: timedelta
    max_failures: int
    lockout_seconds: int


@dataclass(frozen=True)
class IssuedSession:
    session_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    token: str
    csrf_token: str
    expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    session_id: uuid.UUID
    email: str
    display_name: str
    csrf_token_hash: str
    expires_at: datetime
    idle_expires_at: datetime
    # Последний ввод пароля в этой сессии (вход или step-up, SECURITY_ARCHITECTURE §3.2).
    reauthenticated_at: datetime | None = None


# Step-up re-auth (SC-IAM-08): операции критического набора требуют ввода пароля не старше 10 минут.
STEP_UP_MAX_AGE: Final = timedelta(minutes=10)


def step_up_fresh(principal: Principal, *, now: datetime | None = None, max_age: timedelta = STEP_UP_MAX_AGE) -> bool:
    """True, если пароль в этой сессии вводился не раньше `max_age` назад."""
    if principal.reauthenticated_at is None:
        return False
    return (now or utcnow()) - principal.reauthenticated_at <= max_age


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def create_user(conn: AsyncConnection, *, email: str, display_name: str, password: str) -> uuid.UUID:
    """Создаёт локального пользователя (CLI bootstrap, администрирование)."""
    email = normalize_email(email)
    passwords.validate_password_policy(password, email=email)
    user_id = uuid7()
    try:
        await repository.insert_user(conn, user_id, email, display_name)
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != "23505":  # не уникальность — не 409
            raise
        raise ProblemError("conflict", detail="Пользователь с таким email уже существует") from exc
    await repository.upsert_credentials(conn, user_id, passwords.hash_password(password))
    return user_id


async def authenticate(
    conn: AsyncConnection,
    *,
    email: str,
    password: str,
    ip: str | None,
    user_agent: str | None,
    policy: SessionPolicy,
) -> IssuedSession | None:
    """Проверяет пароль и создаёт новую сессию (ротация идентификатора при каждом входе).

    Неудача ⇒ `None`: вызывающий коммитит транзакцию (счётчик неудач, блокировка и аудит
    должны сохраниться) и отвечает 401 `unauthenticated` без уточнения причины (API_DESIGN §3).
    """
    email = normalize_email(email)
    user = await repository.get_user_by_email(conn, email)
    now = utcnow()
    locked = bool(user and user["locked_until"] and user["locked_until"] > now)
    ok = passwords.verify_password(user["password_hash"] if user else None, password)
    if user is None or not ok or locked or user["status"] != "ACTIVE":
        if user is not None and not locked and user["password_hash"] is not None:
            await repository.record_login_failure(conn, user["id"], policy.max_failures, policy.lockout_seconds)
        await audit.record(
            conn,
            action="auth.login_failed",
            resource_type="user_session",
            resource_id=None,
            actor_type="system",
            actor_id=None,
            workspace_id=None,
            outcome="denied",
            metadata={"reason": "locked" if locked else "invalid_credentials"},
        )
        return None
    new_hash = passwords.hash_password(password) if passwords.needs_rehash(user["password_hash"]) else None
    await repository.record_login_success(conn, user["id"], new_hash)

    token, csrf = tokens.new_token(), tokens.new_token()
    session_id = uuid7()
    expires_at = now + policy.absolute_ttl
    await repository.insert_session(
        conn,
        session_id=session_id,
        user_id=user["id"],
        token_hash=tokens.token_hash(token),
        csrf_hash=tokens.token_hash(csrf),
        ip=ip,
        user_agent=user_agent,
        expires_at=expires_at,
    )
    await audit.record(
        conn,
        action="auth.login",
        resource_type="user_session",
        resource_id=session_id,
        actor_type="user",
        actor_id=user["id"],
        workspace_id=None,
    )
    return IssuedSession(
        session_id=session_id,
        user_id=user["id"],
        email=user["email"],
        display_name=user["display_name"],
        token=token,
        csrf_token=csrf,
        expires_at=expires_at,
        idle_expires_at=min(expires_at, now + policy.idle_ttl),
    )


async def resolve_session(conn: AsyncConnection, token: str, policy: SessionPolicy) -> Principal | str:
    """Возвращает Principal или код отказа (`unauthenticated` / `session_expired`).

    Код возвращается, а не бросается, чтобы отзыв истёкшей сессии зафиксировался
    в той же транзакции (вызывающий коммитит и затем отвечает 401).
    """
    row = await repository.get_session_by_token_hash(conn, tokens.token_hash(token))
    if row is None or row["revoked_at"] is not None or row["user_status"] != "ACTIVE":
        return "unauthenticated"
    now = utcnow()
    if row["expires_at"] <= now or row["last_seen_at"] + policy.idle_ttl <= now:
        await repository.revoke_session(conn, row["id"], "expired")
        return "session_expired"
    # Обновляем last_seen не чаще раза в минуту, чтобы не писать на каждый запрос.
    if now - row["last_seen_at"] > timedelta(seconds=60):
        await repository.touch_session(conn, row["id"])
        last_seen = now
    else:
        last_seen = row["last_seen_at"]
    return Principal(
        user_id=row["user_id"],
        session_id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        csrf_token_hash=row["csrf_token_hash"],
        expires_at=row["expires_at"],
        idle_expires_at=min(row["expires_at"], last_seen + policy.idle_ttl),
        reauthenticated_at=row["reauthenticated_at"],
    )


async def reauthenticate(
    conn: AsyncConnection,
    principal: Principal,
    *,
    password: str,
    ip: str | None,
    user_agent: str | None,
    policy: SessionPolicy,
) -> IssuedSession | None:
    """Step-up re-auth (SC-IAM-08, SECURITY_ARCHITECTURE §3.2): повторный ввод пароля текущего пользователя.

    Успех ⇒ ротация сессии (SC-IAM-04): новая сессия с `reauthenticated_at = now()` и прежним абсолютным
    сроком, старая — `revoked_at`, причина `rotated`. Неудача ⇒ `None`; неудача учитывается счётчиком
    блокировки входа (подбор пароля через живую сессию ограничен тем же лимитом), вызывающий коммитит
    транзакцию. Пароль не логируется и не попадает в аудит.
    """
    creds = await repository.get_credentials(conn, principal.user_id)
    now = utcnow()
    locked = bool(creds and creds["locked_until"] and creds["locked_until"] > now)
    ok = passwords.verify_password(creds["password_hash"] if creds else None, password)
    if creds is None or not ok or locked or creds["status"] != "ACTIVE":
        if creds is not None and not locked and creds["password_hash"] is not None:
            await repository.record_login_failure(conn, principal.user_id, policy.max_failures, policy.lockout_seconds)
        await audit.record(
            conn,
            action="auth.reauth_failed",
            resource_type="user_session",
            resource_id=principal.session_id,
            actor_type="user",
            actor_id=principal.user_id,
            workspace_id=None,
            outcome="denied",
            metadata={"reason": "locked" if locked else "invalid_credentials"},
        )
        return None
    await repository.record_login_success(conn, principal.user_id, None)
    token, csrf = tokens.new_token(), tokens.new_token()
    session_id = uuid7()
    await repository.insert_session(
        conn,
        session_id=session_id,
        user_id=principal.user_id,
        token_hash=tokens.token_hash(token),
        csrf_hash=tokens.token_hash(csrf),
        ip=ip,
        user_agent=user_agent,
        expires_at=principal.expires_at,  # ротация не продлевает абсолютный срок сессии
    )
    await repository.revoke_session(conn, principal.session_id, "rotated")
    await audit.record(
        conn,
        action="auth.reauthenticated",
        resource_type="user_session",
        resource_id=session_id,
        actor_type="user",
        actor_id=principal.user_id,
        workspace_id=None,
        metadata={"rotated_from": str(principal.session_id)},
    )
    return IssuedSession(
        session_id=session_id,
        user_id=principal.user_id,
        email=principal.email,
        display_name=principal.display_name,
        token=token,
        csrf_token=csrf,
        expires_at=principal.expires_at,
        idle_expires_at=min(principal.expires_at, now + policy.idle_ttl),
    )


async def rotate_csrf(conn: AsyncConnection, session_id: uuid.UUID) -> str:
    """Выдаёт новый CSRF-токен для сессии (GET /auth/session восстанавливает SPA после перезагрузки)."""
    csrf = tokens.new_token()
    await conn.execute(
        text("UPDATE user_sessions SET csrf_token_hash = :h WHERE id = :id AND revoked_at IS NULL"),
        {"h": tokens.token_hash(csrf), "id": session_id},
    )
    return csrf


async def logout(conn: AsyncConnection, principal: Principal) -> None:
    await repository.revoke_session(conn, principal.session_id, "logout")
    await audit.record(
        conn,
        action="auth.logout",
        resource_type="user_session",
        resource_id=principal.session_id,
        actor_type="user",
        actor_id=principal.user_id,
        workspace_id=None,
    )


def csrf_valid(principal: Principal, header_value: str | None) -> bool:
    return bool(header_value) and tokens.matches(header_value or "", principal.csrf_token_hash)


async def list_memberships(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Workspaces текущего пользователя (через SECURITY DEFINER функцию, контекст app.user_id)."""
    return await repository.list_user_workspaces(conn)


async def workspace_role(conn: AsyncConnection, workspace_id: uuid.UUID) -> Role | None:
    role = await repository.membership_role(conn, workspace_id)
    return Role(role) if role else None


async def project_access(conn: AsyncConnection, project_id: uuid.UUID) -> tuple[uuid.UUID, Role] | None:
    """(workspace_id, эффективная роль) или None, если пользователь не член workspace проекта."""
    access = await repository.project_access(conn, project_id)
    if access is None:
        return None
    return access[0], effective_role(access[1], None)


# --- Членство (контекст workspace уже установлен и право member:* проверено) -----------


async def list_members(
    conn: AsyncConnection, after: tuple[datetime, uuid.UUID] | None, limit: int
) -> list[dict[str, Any]]:
    return await repository.list_members(conn, after, limit)


async def add_member(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, actor_id: uuid.UUID, email: str, role: Role
) -> dict[str, Any]:
    user = await repository.get_user_by_email(conn, normalize_email(email))
    if user is None or user["status"] != "ACTIVE":
        # Приглашения по email вне v1; отсутствие пользователя — ошибка валидации поля.
        raise ProblemError("validation_error", detail="Пользователь с таким email не найден")
    if await repository.get_member(conn, user["id"]) is not None:
        raise ProblemError("conflict", detail="Пользователь уже является участником")
    await repository.insert_member(conn, workspace_id, user["id"], role.value, actor_id)
    await audit.record(
        conn,
        action="member.add",
        resource_type="workspace_member",
        resource_id=user["id"],
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"role": role.value},
    )
    member = await repository.get_member(conn, user["id"])
    if member is None:  # pragma: no cover — нарушение инварианта транзакции
        raise RuntimeError("member row vanished inside transaction")
    return member


async def _owner_count(conn: AsyncConnection) -> int:
    value = (
        await conn.execute(
            text(
                "SELECT count(*) FROM workspace_members WHERE workspace_id = app_current_workspace() AND role = 'owner'"
            )
        )
    ).scalar_one()
    return int(value)


async def change_member_role(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Role,
    expected_version: int,
) -> dict[str, Any]:
    member = await repository.get_member(conn, user_id)
    if member is None:
        raise ProblemError("not_found")
    if member["version"] != expected_version:
        raise ProblemError("precondition_failed", headers={"ETag": f'"v{member["version"]}"'})
    if member["role"] == Role.OWNER and role != Role.OWNER and await _owner_count(conn) <= 1:
        raise ProblemError("last_owner")
    if not await repository.update_member_role(conn, user_id, role.value, expected_version):
        raise ProblemError("precondition_failed")
    await audit.record(
        conn,
        action="member.role_change",
        resource_type="workspace_member",
        resource_id=user_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={
            "from": member["role"],
            "to": role.value,
            "privilege_change": RANK[Role(member["role"])] != RANK[role],
        },
    )
    updated = await repository.get_member(conn, user_id)
    if updated is None:  # pragma: no cover
        raise RuntimeError("member row vanished inside transaction")
    return updated


async def remove_member(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, actor_id: uuid.UUID, user_id: uuid.UUID, expected_version: int
) -> None:
    member = await repository.get_member(conn, user_id)
    if member is None:
        raise ProblemError("not_found")
    if member["version"] != expected_version:
        raise ProblemError("precondition_failed", headers={"ETag": f'"v{member["version"]}"'})
    if member["role"] == Role.OWNER and await _owner_count(conn) <= 1:
        raise ProblemError("last_owner")
    if not await repository.delete_member(conn, user_id, expected_version):
        raise ProblemError("precondition_failed")
    await audit.record(
        conn,
        action="member.remove",
        resource_type="workspace_member",
        resource_id=user_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"role": member["role"]},
    )
