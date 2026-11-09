"""SC-IAM-08 / ST-IAM-08: step-up re-auth (SECURITY_ARCHITECTURE §3.2, OQ-45 (f)).

Операции критического набора — политика workspace (`settings`), политика провайдера, управление участниками,
утверждение approvals из набора four-eyes — требуют ввода пароля не старше 10 минут; иначе 403
`step_up_required`. `POST /auth/reauthenticate` подтверждает пароль (rate limit, аудит, учёт в счётчике
блокировки) и ротирует сессию (SC-IAM-04): старый токен перестаёт действовать.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.approvals import Approvals
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, transaction
from tests.conftest import PASSWORD, UserSession, login, make_client, register_user
from tests.support import db as testdb
from tests.support.contract import validate_response
from tests.support.orch_api import HookRecorder, Team, key, make_team, orch_app, running_run
from tests.support.orchestration import connect_admin

pytestmark = [pytest.mark.db, pytest.mark.security]


async def _age_session(engine: AsyncEngine, user: UserSession, minutes: int = 11) -> None:
    """Последний ввод пароля — `minutes` минут назад (created_at сдвигается вместе: CHECK reauth ≥ created)."""
    async with transaction(engine, DbContext(actor_type="system")) as conn:
        await conn.execute(
            text(
                "UPDATE user_sessions SET created_at = now() - make_interval(mins => :m),"
                " reauthenticated_at = now() - make_interval(mins => :m)"
                " WHERE user_id = :u AND revoked_at IS NULL"
            ),
            {"m": minutes, "u": uuid.UUID(user.user_id)},
        )


async def _patch_policy(user: UserSession, ws: dict[str, object], version: int) -> httpx.Response:
    return await user.client.patch(
        f"/api/v1/workspaces/{ws['id']}",
        json={"settings": {"allow_single_member_self_approval": True}},
        headers=user.headers(if_match=f'"v{version}"'),
    )


def _audit_actions(database: testdb.TestDatabase, user_id: str) -> list[str]:
    with connect_admin(database) as conn:
        rows = conn.execute(
            "SELECT action FROM audit_events WHERE actor_id = %s AND action LIKE 'auth.%%' ORDER BY id",
            (uuid.UUID(user_id),),
        ).fetchall()
    return [r[0] for r in rows]


async def test_fresh_login_allows_policy_change(user: UserSession) -> None:
    ws = await user.create_workspace()
    r = await _patch_policy(user, ws, 0)
    assert r.status_code == 200, r.text


async def test_stale_session_requires_step_up_then_reauth_rotates_session(
    app: FastAPI, app_engine: AsyncEngine, user: UserSession, database: testdb.TestDatabase
) -> None:
    ws = await user.create_workspace()
    await _age_session(app_engine, user)

    r = await _patch_policy(user, ws, 0)
    validate_response("updateWorkspace", r)
    body = r.json()
    assert r.status_code == 403 and body["code"] == "step_up_required"
    assert body["max_age_seconds"] == 600
    # Управление участниками и политика провайдера — тот же барьер.
    other = f"other-{uuid.uuid4().hex[:8]}@example.test"
    await register_user(app_engine, other)
    m = await user.client.post(
        f"/api/v1/workspaces/{ws['id']}/members",
        json={"email": other, "role": "viewer"},
        headers=user.headers(idem=True),
    )
    assert m.status_code == 403 and m.json()["code"] == "step_up_required"
    providers = await user.client.get("/api/v1/providers", headers={"X-Workspace-Id": str(ws["id"])})
    assert providers.status_code == 200
    for p in providers.json()["items"][:1]:
        version = p["workspace_settings"]["settings_version"]
        pr = await user.client.patch(
            f"/api/v1/providers/{p['provider_id']}/settings",
            json={"enabled": True},
            headers=user.headers(str(ws["id"]), if_match=f'"v{version}"'),
        )
        assert pr.status_code == 403 and pr.json()["code"] == "step_up_required"
    # Чтение не требует step-up.
    assert (await user.client.get(f"/api/v1/workspaces/{ws['id']}")).status_code == 200

    # Неверный пароль: 403 (не 401 — сессия жива), аудит, счётчик неудач.
    bad = await user.client.post(
        "/api/v1/auth/reauthenticate", json={"password": "wrong-password-1"}, headers=user.headers()
    )
    validate_response("reauthenticate", bad)
    assert bad.status_code == 403 and bad.json()["code"] == "reauthentication_failed"
    assert "wrong-password-1" not in bad.text
    assert (await user.client.get("/api/v1/auth/session")).status_code == 200
    async with transaction(app_engine, DbContext(actor_type="system")) as conn:
        failed = (
            await conn.execute(
                text("SELECT failed_attempts FROM user_credentials WHERE user_id = :u"), {"u": uuid.UUID(user.user_id)}
            )
        ).scalar_one()
    assert failed == 1

    # CSRF обязателен и для re-auth.
    no_csrf = await user.client.post("/api/v1/auth/reauthenticate", json={"password": PASSWORD})
    assert no_csrf.status_code == 403 and no_csrf.json()["code"] == "csrf_failed"

    old_token = user.client.cookies.get("aicd_session")
    ok = await user.client.post("/api/v1/auth/reauthenticate", json={"password": PASSWORD}, headers=user.headers())
    validate_response("reauthenticate", ok)
    assert ok.status_code == 200, ok.text
    info = ok.json()
    assert info["reauthenticated_at"] and info["csrf_token"] != user.csrf
    new_token = ok.cookies.get("aicd_session")
    assert new_token and new_token != old_token, "ротация сессии после step-up (SC-IAM-04)"
    user.csrf = info["csrf_token"]

    async with make_client(app) as stale:
        stale.cookies.set("aicd_session", old_token or "", domain="testserver", path="/api")
        assert (await stale.get("/api/v1/auth/session")).status_code == 401, "старый токен отозван"

    r = await _patch_policy(user, ws, 0)
    assert r.status_code == 200, r.text
    actions = _audit_actions(database, user.user_id)
    assert {"auth.step_up_required", "auth.reauth_failed", "auth.reauthenticated"} <= set(actions)
    async with transaction(app_engine, DbContext(actor_type="system")) as conn:
        reason = (
            await conn.execute(
                text("SELECT revoke_reason FROM user_sessions WHERE user_id = :u AND revoked_at IS NOT NULL"),
                {"u": uuid.UUID(user.user_id)},
            )
        ).scalar_one()
    assert reason == "rotated"


async def test_reauthenticate_is_rate_limited_per_user(app: FastAPI, app_engine: AsyncEngine) -> None:
    user = await login(app, app_engine, ip="203.0.113.77")
    try:
        codes = []
        for _ in range(6):
            r = await user.client.post(
                "/api/v1/auth/reauthenticate", json={"password": "wrong-password-x"}, headers=user.headers()
            )
            codes.append(r.status_code)
        assert codes[:5] == [403] * 5 and codes[5] == 429, codes
    finally:
        await user.client.aclose()


async def test_reauthenticate_requires_session(app: FastAPI) -> None:
    async with make_client(app) as anon:
        r = await anon.post("/api/v1/auth/reauthenticate", json={"password": PASSWORD})
    assert r.status_code == 401


# --- Approvals критического набора ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
async def oapp(settings: Settings) -> AsyncIterator[FastAPI]:
    async for application in orch_app(settings, HookRecorder()):
        yield application


async def _approval(engine: AsyncEngine, team: Team, run: uuid.UUID, action: str) -> uuid.UUID:
    async with transaction(engine, DbContext(workspace_id=team.tenant.workspace_id, actor_type="system")) as conn:
        return await Approvals().request(
            conn, workspace_id=team.tenant.workspace_id, project_id=team.tenant.project_id, run_id=run, task_id=None,
            action_type=action, risk_level="high", subject={"run_id": str(run), "nonce": uuid.uuid4().hex},
            requested_by_type="user", requested_by=uuid.UUID(team.users["developer"].user_id),
        )  # fmt: skip


async def test_critical_approval_requires_step_up_non_critical_does_not(
    oapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection
) -> None:
    team = await make_team(oapp, app_engine, admin, ("developer", "admin"))
    run = running_run(admin, team.tenant)
    deployer = team.users["owner"]
    await _age_session(app_engine, deployer)
    critical = await _approval(app_engine, team, run, "DESTRUCTIVE_MIGRATION")
    plain = await _approval(app_engine, team, run, "PLAN_APPROVAL")

    async def decide(approval_id: uuid.UUID, decision: str = "APPROVED") -> httpx.Response:
        body: dict[str, str] = {"decision": decision}
        if decision == "REJECTED":
            body["reason"] = "нет"
        r = await deployer.client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json=body,
            headers=deployer.headers(idem_key=key(), if_match='"v0"'),
        )
        validate_response("decideApproval", r)
        return r

    r = await decide(critical)
    assert r.status_code == 403 and r.json()["code"] == "step_up_required"
    r = await decide(plain)
    assert r.status_code == 200, r.text
    # Директива APPROVE — тот же путь (API_DESIGN §5.4).
    d = await deployer.client.post(
        f"/api/v1/runs/{run}/directives",
        json={"directive_type": "APPROVE", "target": {"type": "approval", "approval_id": str(critical)},
              "message": "ok", "expected_run_version": admin.execute(
                  "SELECT version FROM runs WHERE id = %s", (run,)).fetchone()[0]},  # type: ignore[index]
        headers=deployer.headers(idem=True),
    )  # fmt: skip
    assert d.status_code == 403 and d.json()["code"] == "step_up_required", d.text
    ok = await deployer.client.post(
        "/api/v1/auth/reauthenticate", json={"password": PASSWORD}, headers=deployer.headers()
    )
    assert ok.status_code == 200
    deployer.csrf = ok.json()["csrf_token"]
    r = await decide(critical)
    assert r.status_code == 200, r.text
