"""P3-10: API approvals — RBAC по action_type, four-eyes, If-Match, idempotency, истечение, хук PLAN_APPROVAL."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.approvals import Approvals
from aicd_orchestration.directives import Directives
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, transaction
from tests.support import db as testdb
from tests.support.contract import validate_response
from tests.support.orch_api import HookRecorder, Team, key, make_team, orch_app, outsider, running_run
from tests.support.orchestration import connect_admin

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
def recorder() -> HookRecorder:
    return HookRecorder()


@pytest.fixture(scope="module")
async def oapp(settings: Settings, recorder: HookRecorder) -> AsyncIterator[FastAPI]:
    async for application in orch_app(settings, recorder):
        yield application


async def _request(engine: AsyncEngine, team: Team, run: uuid.UUID, action: str, *, requester: str | None,
                   risk: str = "medium", subject: dict[str, object] | None = None) -> uuid.UUID:  # fmt: skip
    async with transaction(engine, DbContext(workspace_id=team.tenant.workspace_id, actor_type="system")) as conn:
        return await Approvals().request(
            conn, workspace_id=team.tenant.workspace_id, project_id=team.tenant.project_id, run_id=run, task_id=None,
            action_type=action, risk_level=risk, subject=subject or {"run_id": str(run), "nonce": uuid.uuid4().hex},
            requested_by_type="user" if requester else "system",
            requested_by=uuid.UUID(team.users[requester].user_id) if requester else None,
        )  # fmt: skip


async def _decide(team: Team, who: str, approval_id: uuid.UUID, decision: str = "APPROVED", *,
                  reason: str | None = None, if_match: str | None = '"v0"', idem: str | None = None) -> httpx.Response:  # fmt: skip
    user = team.users[who]
    body: dict[str, object] = {"decision": decision}
    if reason is not None:
        body["reason"] = reason
    headers = user.headers(idem_key=idem or key(), if_match=if_match)
    r = await user.client.post(f"/api/v1/approvals/{approval_id}/decision", json=body, headers=headers)
    validate_response("decideApproval", r)
    return r


async def test_plan_approval_flow_etag_idempotency_and_hook(
    oapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection, recorder: HookRecorder
) -> None:
    team = await make_team(oapp, app_engine, admin, ("developer", "reviewer", "viewer"))
    run = running_run(admin, team.tenant)
    approval_id = await _request(app_engine, team, run, "PLAN_APPROVAL", requester="owner",
                                 subject={"run_id": str(run), "plan_version": 1})  # fmt: skip
    again = await _request(app_engine, team, run, "PLAN_APPROVAL", requester="owner",
                           subject={"run_id": str(run), "plan_version": 1})  # fmt: skip
    assert again == approval_id, "повторный запрос того же предмета не создаёт дубль"

    dev = team.users["developer"]
    r = await dev.client.get("/api/v1/approvals", headers={"X-Workspace-Id": team.ws})
    validate_response("listApprovals", r)
    [item] = r.json()["items"]
    assert item["id"] == str(approval_id) and item["can_decide"] is True and item["status"] == "PENDING"
    assert item["summary"].startswith("Утверждение плана") and item["requested_by"]["actor_type"] == "user"
    for who, decidable in (("reviewer", False), ("viewer", False), ("developer", True)):
        u = team.users[who]
        r = await u.client.get("/api/v1/approvals", params={"decidable_by_me": "true"},
                               headers={"X-Workspace-Id": team.ws})  # fmt: skip
        validate_response("listApprovals", r)
        assert bool(r.json()["items"]) is decidable, who
        r = await u.client.get(f"/api/v1/approvals/{approval_id}")
        validate_response("getApproval", r)
        assert r.json()["can_decide"] is decidable and r.headers["etag"] == '"v0"'

    assert (await _decide(team, "viewer", approval_id)).status_code == 403
    r = await _decide(team, "reviewer", approval_id)
    assert r.status_code == 403 and r.json()["code"] == "forbidden", "PLAN_APPROVAL требует developer+"
    r = await _decide(team, "developer", approval_id, if_match=None)
    assert r.status_code == 428
    r = await _decide(team, "developer", approval_id, if_match='"v7"')
    assert r.status_code == 412 and r.json()["code"] == "precondition_failed"
    r = await _decide(team, "developer", approval_id, "REJECTED")
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/reason"

    idem = key()
    recorder.reset()
    r = await _decide(team, "developer", approval_id, idem=idem)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED" and r.headers["etag"] == '"v1"'
    assert r.json()["decided_by"]["id"] == dev.user_id
    replay = await _decide(team, "developer", approval_id, idem=idem)
    assert replay.status_code == 200 and replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == r.json()
    assert [a["id"] for a in recorder.approvals] == [approval_id], "хук вызван один раз, без replay"
    assert (
        recorder.approvals[0]["action_type"] == "PLAN_APPROVAL"
        and recorder.approvals[0]["subject"]["plan_version"] == 1
    )

    r = await _decide(team, "owner", approval_id, if_match='"v1"')
    assert r.status_code == 409 and r.json()["code"] == "invalid_state_transition"
    assert r.json()["current_status"] == "APPROVED"
    audit = admin.execute(
        "SELECT metadata FROM audit_events WHERE action = 'approval.decide' AND resource_id = %s", (approval_id,)
    ).fetchall()
    assert len(audit) == 1 and audit[0][0]["decision"] == "APPROVED"
    outbox = admin.execute(
        "SELECT payload FROM outbox_events WHERE event_type = 'approval.decided' AND aggregate_id = %s", (approval_id,)
    ).fetchall()
    assert len(outbox) == 1


@pytest.mark.parametrize(
    ("action", "allowed"),
    [
        ("MANUAL_REVIEW", {"reviewer", "developer", "admin", "owner"}),
        ("DOCS_DOWNLOAD", {"developer", "admin", "owner"}),
        ("MERGE", {"admin", "owner"}),
        ("CRITICAL_DIRECTIVE", {"admin", "owner"}),
        ("PRODUCTION_DEPLOY", {"owner"}),
        ("SECRET_CHANGE", {"owner"}),
    ],
)
async def test_role_matrix_by_action_type(
    oapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection, action: str, allowed: set[str]
) -> None:
    team = await make_team(oapp, app_engine, admin, ("admin", "developer", "reviewer", "viewer"))
    run = running_run(admin, team.tenant)
    for who in ("viewer", "reviewer", "developer", "admin", "owner"):
        approval_id = await _request(app_engine, team, run, action, requester=None, risk="high")
        r = await _decide(team, who, approval_id)
        assert (r.status_code == 200) is (who in allowed), (action, who, r.text)
        if r.status_code == 403:
            assert r.json()["code"] == "forbidden"
            denied = admin.execute(
                "SELECT count(*) FROM audit_events WHERE action = 'authz.denied' AND actor_id = %s",
                (uuid.UUID(team.users[who].user_id),),
            ).fetchone()
            assert denied is not None and denied[0] >= 1


async def test_four_eyes_and_single_member_self_approval(
    oapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection
) -> None:
    team = await make_team(oapp, app_engine, admin, ("admin",))
    run = running_run(admin, team.tenant)
    merge = await _request(app_engine, team, run, "MERGE", requester="admin", risk="high")
    r = await _decide(team, "admin", merge)
    assert r.status_code == 403 and r.json()["code"] == "four_eyes_violation"
    r = await team.users["admin"].client.get(f"/api/v1/approvals/{merge}")
    assert r.json()["can_decide"] is False
    assert (await _decide(team, "owner", merge)).status_code == 200

    solo = await make_team(oapp, app_engine, admin)
    solo_run = running_run(admin, solo.tenant)
    m2 = await _request(app_engine, solo, solo_run, "MERGE", requester="owner", risk="high")
    assert (await _decide(solo, "owner", m2)).json()["code"] == "four_eyes_violation", "без настройки — запрещено"
    admin.execute(
        "UPDATE workspaces SET settings = settings || '{\"allow_single_member_self_approval\": true}',"
        " version = version + 1 WHERE id = %s",
        (solo.tenant.workspace_id,),
    )
    r = await _decide(solo, "owner", m2)
    assert r.status_code == 200
    meta = admin.execute(
        "SELECT metadata FROM audit_events WHERE action = 'approval.decide' AND resource_id = %s", (m2,)
    ).fetchone()
    assert meta is not None and meta[0]["self_approved"] is True
    deploy = await _request(app_engine, solo, solo_run, "PRODUCTION_DEPLOY", requester="owner", risk="critical")
    r = await _decide(solo, "owner", deploy, reason="ship")
    assert r.status_code == 403 and r.json()["code"] == "four_eyes_violation", "PRODUCTION_DEPLOY — никогда"


async def test_expiry_and_tenant_isolation(oapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection) -> None:
    team = await make_team(oapp, app_engine, admin, ("developer",))
    run = running_run(admin, team.tenant)
    approval_id = await _request(app_engine, team, run, "PLAN_APPROVAL", requester="owner")
    admin.execute(
        "UPDATE approvals SET requested_at = now() - interval '4 days', expires_at = now() - interval '1 day',"
        " version = version + 1 WHERE id = %s",
        (approval_id,),
    )
    r = await _decide(team, "developer", approval_id, if_match='"v1"')
    assert r.status_code == 409 and r.json()["current_status"] == "EXPIRED"
    ctx = DbContext(workspace_id=team.tenant.workspace_id, actor_type="system")
    async with transaction(app_engine, ctx) as conn:
        expired = await Directives().expire_approvals(conn)
    assert [e["id"] for e in expired] == [approval_id]
    r = await team.users["developer"].client.get(f"/api/v1/approvals/{approval_id}")
    validate_response("getApproval", r)
    assert r.json()["status"] == "EXPIRED" and r.json()["can_decide"] is False
    r = await team.users["developer"].client.get(
        "/api/v1/approvals", params={"status": ["EXPIRED", "PENDING"]}, headers={"X-Workspace-Id": team.ws}
    )
    validate_response("listApprovals", r)
    assert [i["status"] for i in r.json()["items"]] == ["EXPIRED"]

    stranger = await outsider(oapp, app_engine)
    r = await stranger.client.get(f"/api/v1/approvals/{approval_id}")
    validate_response("getApproval", r)
    assert r.status_code == 404
    r = await stranger.client.post(
        f"/api/v1/approvals/{approval_id}/decision", json={"decision": "APPROVED"},
        headers=stranger.headers(idem=True, if_match='"v2"'),
    )  # fmt: skip
    assert r.status_code == 404
    r = await team.users["developer"].client.get(f"/api/v1/approvals/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_pagination(oapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection) -> None:
    team = await make_team(oapp, app_engine, admin)
    run = running_run(admin, team.tenant)
    ids = {await _request(app_engine, team, run, "MANUAL_REVIEW", requester=None) for _ in range(3)}
    owner = team.users["owner"]
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = await owner.client.get("/api/v1/approvals", params=params, headers={"X-Workspace-Id": team.ws})
        validate_response("listApprovals", r)
        seen += [i["id"] for i in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        if not cursor:
            break
    assert set(seen) == {str(i) for i in ids} and len(seen) == 3
    r = await owner.client.get("/api/v1/approvals", params={"cursor": "garbage"}, headers={"X-Workspace-Id": team.ws})
    assert r.status_code == 422
