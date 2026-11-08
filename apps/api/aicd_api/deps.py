"""Общие зависимости запросов: сессия, CSRF, rate limit, tenant, RBAC (порядок — API_DESIGN §6)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_api.ratelimit import RateDecision
from aicd_api.state import AppState
from aicd_identity import api as identity
from aicd_identity.rbac import Role, has_permission
from aicd_platform import audit, context
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def app_state(request: Request) -> AppState:
    state: AppState = request.app.state.aicd
    return state


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _remember_rate(request: Request, decision: RateDecision) -> None:
    request.state.rate = decision
    if not decision.allowed:
        raise ProblemError(
            "rate_limited",
            extensions={"retry_after_seconds": decision.reset_seconds},
            headers={"Retry-After": str(decision.reset_seconds)},
        )


async def rate_limit_anonymous(request: Request, bucket: str, limit: int) -> None:
    state = app_state(request)
    _remember_rate(request, await state.limiter.hit(f"{bucket}:ip:{client_ip(request)}", limit))


def check_origin(request: Request) -> None:
    """Defence in depth против CSRF: чужой Origin / cross-site fetch ⇒ 403 (API_DESIGN §3)."""
    allowed = app_state(request).settings.allowed_origins
    origin = request.headers.get("origin")
    if origin is not None:
        own = f"{request.url.scheme}://{request.url.netloc}"
        if origin != own and origin not in allowed:
            raise ProblemError("csrf_failed")
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise ProblemError("csrf_failed")


async def current_principal(request: Request) -> identity.Principal:
    """401 → CSRF 403 → rate limit 429 (первые шаги порядка проверок API_DESIGN §6)."""
    state = app_state(request)
    token = request.cookies.get(state.settings.session_cookie_name)
    if not token:
        raise ProblemError("unauthenticated")
    async with transaction(state.engine, DbContext()) as conn:
        resolved = await identity.resolve_session(conn, token, state.session_policy)
    if isinstance(resolved, str):
        raise ProblemError(resolved)
    principal = resolved
    context.bind(user_id=principal.user_id)
    request.state.principal = principal
    if request.method in UNSAFE_METHODS:
        check_origin(request)
        if not identity.csrf_valid(principal, request.headers.get("x-csrf-token")):
            raise ProblemError("csrf_failed")
    s = state.settings
    if request.method in UNSAFE_METHODS:
        decision = await state.limiter.hit(f"mut:user:{principal.user_id}", s.rate_limit_mutation_per_minute)
    else:
        decision = await state.limiter.hit(f"read:user:{principal.user_id}", s.rate_limit_read_per_minute)
    _remember_rate(request, decision)
    return principal


@dataclass(frozen=True)
class Access:
    principal: identity.Principal
    workspace_id: uuid.UUID
    role: Role

    def db(self) -> DbContext:
        return DbContext(user_id=self.principal.user_id, workspace_id=self.workspace_id)


def parse_workspace_header(request: Request) -> uuid.UUID:
    raw = request.headers.get("x-workspace-id")
    if raw is None:
        raise ProblemError(
            "validation_error",
            detail="Требуется заголовок X-Workspace-Id",
        )
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ProblemError("validation_error", detail="X-Workspace-Id должен быть UUID") from exc


async def workspace_access(request: Request, principal: identity.Principal, workspace_id: uuid.UUID) -> Access:
    """Не член workspace ⇒ 404 (защита от перечисления, AC-12)."""
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        role = await identity.workspace_role(conn, workspace_id)
    if role is None:
        raise ProblemError("not_found")
    context.bind(workspace_id=workspace_id)
    return Access(principal, workspace_id, role)


async def project_access(request: Request, principal: identity.Principal, project_id: uuid.UUID) -> Access:
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        found = await identity.project_access(conn, project_id)
    if found is None:
        raise ProblemError("not_found")
    header = request.headers.get("x-workspace-id")
    if header is not None and header != str(found[0]):
        raise ProblemError("not_found")
    context.bind(workspace_id=found[0])
    return Access(principal, found[0], found[1])


async def authorize(request: Request, access: Access, permission: str) -> None:
    """Проверка права; отказ на небезопасном методе аудируется (`authz.denied`, API_DESIGN §17)."""
    if has_permission(access.role, permission):
        return
    if request.method in UNSAFE_METHODS:
        state = app_state(request)
        async with transaction(state.engine, access.db()) as conn:
            await _audit_denied(conn, access, permission, request)
    raise ProblemError("forbidden")


async def _audit_denied(conn: AsyncConnection, access: Access, permission: str, request: Request) -> None:
    await audit.record(
        conn,
        action="authz.denied",
        resource_type="workspace",
        resource_id=access.workspace_id,
        actor_type="user",
        actor_id=access.principal.user_id,
        workspace_id=access.workspace_id,
        outcome="denied",
        metadata={
            "permission": permission,
            "role": access.role.value,
            "method": request.method,
            "route": getattr(request.scope.get("route"), "path", None),
        },
    )
