"""Аутентификация: вход, выход, текущая сессия (ADR-0009, API_DESIGN §3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from aicd_api.deps import app_state, check_origin, client_ip, current_principal, rate_limit_anonymous
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_api.schemas import LoginRequest
from aicd_identity import api as identity
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_cookies(request: Request, response: Response, *, token: str, csrf: str, max_age: int) -> None:
    s = app_state(request).settings
    response.set_cookie(
        s.session_cookie_name,
        token,
        max_age=max_age,
        path="/api",
        secure=s.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        s.csrf_cookie_name,
        csrf,
        max_age=max_age,
        path="/",
        secure=s.cookie_secure,
        httponly=False,
        samesite="strict",
    )


def _clear_cookies(request: Request, response: Response) -> None:
    s = app_state(request).settings
    response.delete_cookie(s.session_cookie_name, path="/api", secure=s.cookie_secure, httponly=True, samesite="strict")
    response.delete_cookie(s.csrf_cookie_name, path="/", secure=s.cookie_secure, samesite="strict")


async def _session_info(request: Request, principal: identity.Principal, csrf: str) -> dict[str, object]:
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        memberships = await identity.list_memberships(conn)
    return {
        "user": {"id": principal.user_id, "email": principal.email, "display_name": principal.display_name},
        "csrf_token": csrf,
        "expires_at": principal.expires_at,
        "idle_expires_at": principal.idle_expires_at,
        "workspaces": [
            {"workspace_id": m["workspace_id"], "slug": m["slug"], "name": m["name"], "role": m["role"]}
            for m in memberships
        ],
    }


@router.post("/login", **op("login", "public"))
async def login(request: Request, body: LoginRequest) -> Response:
    state = app_state(request)
    check_origin(request)  # login-CSRF: сессии ещё нет, защищаемся проверкой Origin/Sec-Fetch-Site
    await rate_limit_anonymous(request, "login", state.settings.rate_limit_login_per_ip_per_minute)
    async with transaction(state.engine, DbContext(actor_type="system")) as conn:
        issued = await identity.authenticate(
            conn,
            email=str(body.email),
            password=body.password,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            policy=state.session_policy,
        )
    if issued is None:
        raise ProblemError("unauthenticated")
    principal = identity.Principal(
        user_id=issued.user_id,
        session_id=issued.session_id,
        email=issued.email,
        display_name=issued.display_name,
        csrf_token_hash="",
        expires_at=issued.expires_at,
        idle_expires_at=issued.idle_expires_at,
    )
    response = json_response(await _session_info(request, principal, issued.csrf_token))
    max_age = int((issued.expires_at - utcnow()).total_seconds())
    _set_cookies(request, response, token=issued.token, csrf=issued.csrf_token, max_age=max_age)
    return response


@router.post("/logout", status_code=204, **op("logout", "authenticated"))
async def logout(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        await identity.logout(conn, principal)
    response = Response(status_code=204)
    _clear_cookies(request, response)
    return response


@router.get("/session", **op("getSession", "authenticated"))
async def get_session(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    """Восстановление SPA после перезагрузки.

    CSRF-токен в БД хранится только хэшем: если cookie `aicd_csrf` соответствует сессии,
    возвращается он (не ломая другие вкладки), иначе выдаётся новый.
    """
    state = app_state(request)
    csrf = request.cookies.get(state.settings.csrf_cookie_name)
    if not identity.csrf_valid(principal, csrf):
        async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
            csrf = await identity.rotate_csrf(conn, principal.session_id)
    if csrf is None:  # pragma: no cover
        raise RuntimeError("csrf token missing")
    response = json_response(await _session_info(request, principal, csrf))
    s = state.settings
    response.set_cookie(s.csrf_cookie_name, csrf, path="/", secure=s.cookie_secure, httponly=False, samesite="strict")
    return response
