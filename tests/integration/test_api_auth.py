"""P1-09: вход, сессии, CSRF, блокировка, выход (ADR-0009, API_DESIGN §3)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import PASSWORD, UserSession, make_client, register_user
from tests.support.contract import validate_response

pytestmark = pytest.mark.db


async def test_login_sets_hardened_cookies_and_matches_contract(
    app: FastAPI, app_engine: AsyncEngine, unique_email: str
) -> None:
    await register_user(app_engine, unique_email)
    async with make_client(app) as client:
        r = await client.post("/api/v1/auth/login", json={"email": unique_email.upper(), "password": PASSWORD})
        validate_response("login", r)
        assert r.status_code == 200
        cookies = r.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookies if c.startswith("aicd_session="))
        csrf_cookie = next(c for c in cookies if c.startswith("aicd_csrf="))
        for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/api"):
            assert attr.lower() in session_cookie.lower()
        assert "httponly" not in csrf_cookie.lower() and "secure" in csrf_cookie.lower()
        body = r.json()
        assert body["user"]["email"] == unique_email and body["workspaces"] == []
        assert csrf_cookie.split(";")[0].split("=", 1)[1] == body["csrf_token"]
        s = await client.get("/api/v1/auth/session")
        validate_response("getSession", s)
        assert s.json()["csrf_token"] == body["csrf_token"], "CSRF стабилен между вкладками"


@pytest.mark.parametrize("password", ["wrong-password-123", ""])
async def test_bad_credentials_are_indistinguishable(
    app: FastAPI, app_engine: AsyncEngine, unique_email: str, password: str
) -> None:
    await register_user(app_engine, unique_email)
    async with make_client(app) as client:
        r1 = await client.post("/api/v1/auth/login", json={"email": unique_email, "password": password or "x"})
        r2 = await client.post("/api/v1/auth/login", json={"email": "nobody@example.test", "password": "x"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["code"] == r2.json()["code"] == "unauthenticated"
    assert r1.json().get("detail") == r2.json().get("detail")
    validate_response("login", r1)


async def test_lockout_after_failures_even_with_correct_password(
    app: FastAPI, app_engine: AsyncEngine, unique_email: str
) -> None:
    await register_user(app_engine, unique_email)
    async with make_client(app) as client:
        for _ in range(5):
            assert (
                await client.post("/api/v1/auth/login", json={"email": unique_email, "password": "wrong-pass-0000"})
            ).status_code == 401
        r = await client.post("/api/v1/auth/login", json={"email": unique_email, "password": PASSWORD})
    assert r.status_code == 401
    async with app_engine.connect() as conn:
        locked = (
            await conn.execute(
                text(
                    "SELECT c.locked_until > now() FROM user_credentials c JOIN users u ON u.id = c.user_id WHERE u.email = :e"
                ),
                {"e": unique_email},
            )
        ).scalar_one()
    assert locked


async def test_unsafe_requests_require_csrf_and_same_origin(user: UserSession) -> None:
    body = {"name": "x", "slug": "csrf-check"}
    r = await user.client.post("/api/v1/workspaces", json=body, headers={"Idempotency-Key": "csrf-test-1"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    r = await user.client.post(
        "/api/v1/workspaces", json=body, headers={"X-CSRF-Token": "forged", "Idempotency-Key": "csrf-test-2"}
    )
    assert r.status_code == 403
    r = await user.client.post(
        "/api/v1/workspaces", json=body, headers={**user.headers(idem=True), "Origin": "https://evil.example"}
    )
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    r = await user.client.post(
        "/api/v1/workspaces", json=body, headers={**user.headers(idem=True), "Sec-Fetch-Site": "cross-site"}
    )
    assert r.status_code == 403


async def test_login_rejects_cross_site_origin(app: FastAPI) -> None:
    async with make_client(app) as client:
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": "a@b.test", "password": "x"},
            headers={"Origin": "https://evil.example"},
        )
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"


async def test_logout_revokes_session(user: UserSession) -> None:
    r = await user.client.post("/api/v1/auth/logout", headers=user.headers())
    validate_response("logout", r)
    assert r.status_code == 204
    r = await user.client.get("/api/v1/workspaces")
    assert r.status_code == 401
    validate_response("listWorkspaces", r)


async def test_idle_timeout_expires_session(user: UserSession, app_engine: AsyncEngine) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE user_sessions SET last_seen_at = now() - interval '2 hours'"
                " WHERE user_id = CAST(:u AS uuid) AND revoked_at IS NULL"
            ),
            {"u": user.user_id},
        )
    r = await user.client.get("/api/v1/workspaces")
    assert r.status_code == 401 and r.json()["code"] == "session_expired"
    async with app_engine.connect() as conn:
        reason = (
            await conn.execute(
                text("SELECT revoke_reason FROM user_sessions WHERE user_id = CAST(:u AS uuid)"), {"u": user.user_id}
            )
        ).scalar_one()
    assert reason == "expired", "отзыв истёкшей сессии должен фиксироваться"


async def test_no_session_is_401(app: FastAPI) -> None:
    async with make_client(app) as client:
        r = await client.get("/api/v1/projects", headers={"X-Workspace-Id": "00000000-0000-7000-8000-000000000000"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
