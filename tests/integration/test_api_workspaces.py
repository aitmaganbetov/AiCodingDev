"""Workspaces, участники, RBAC на HTTP-уровне (P1-10, P1-11)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import UserSession, login
from tests.support.contract import validate_response

pytestmark = pytest.mark.db


async def test_create_and_list_workspaces(user: UserSession) -> None:
    ws = await user.create_workspace("Команда")
    assert ws["role"] == "owner"
    r = await user.client.get("/api/v1/workspaces")
    validate_response("listWorkspaces", r)
    assert [w["id"] for w in r.json()["items"]] == [ws["id"]]
    s = await user.client.get("/api/v1/auth/session")
    assert s.json()["workspaces"][0]["role"] == "owner"


async def test_duplicate_slug_conflict(user: UserSession) -> None:
    ws = await user.create_workspace()
    r = await user.client.post(
        "/api/v1/workspaces", json={"name": "x", "slug": ws["slug"]}, headers=user.headers(idem=True)
    )
    validate_response("createWorkspace", r)
    assert r.status_code == 409


async def test_workspace_pagination(user: UserSession) -> None:
    created = [await user.create_workspace() for _ in range(3)]
    r = await user.client.get("/api/v1/workspaces", params={"limit": 2})
    page1 = r.json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    r = await user.client.get("/api/v1/workspaces", params={"limit": 2, "cursor": page1["next_cursor"]})
    page2 = r.json()
    ids = [w["id"] for w in page1["items"] + page2["items"]]
    assert sorted(ids) == sorted(w["id"] for w in created) and page2["next_cursor"] is None
    bad = await user.client.get("/api/v1/workspaces", params={"cursor": page1["next_cursor"][:-3] + "AAA"})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_cursor"


async def _member_session(app: FastAPI, engine: AsyncEngine, owner: UserSession, ws_id: str, role: str) -> UserSession:
    other = await login(app, engine)
    r = await owner.client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": other.email, "role": role},
        headers=owner.headers(idem=True),
    )
    validate_response("addMember", r)
    assert r.status_code == 201, r.text
    return other


async def test_member_management_owner_only_and_etag(app: FastAPI, app_engine: AsyncEngine, user: UserSession) -> None:
    ws = await user.create_workspace()
    admin = await _member_session(app, app_engine, user, ws["id"], "admin")
    viewer = await _member_session(app, app_engine, user, ws["id"], "viewer")
    # admin не управляет членством (member:manage = owner)
    r = await admin.client.post(
        f"/api/v1/workspaces/{ws['id']}/members",
        json={"email": user.email, "role": "viewer"},
        headers=admin.headers(idem=True),
    )
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    # viewer видит участников
    r = await viewer.client.get(f"/api/v1/workspaces/{ws['id']}/members")
    validate_response("listMembers", r)
    assert {m["role"] for m in r.json()["items"]} == {"owner", "admin", "viewer"}
    # PATCH требует If-Match
    url = f"/api/v1/workspaces/{ws['id']}/members/{viewer.user_id}"
    r = await user.client.patch(url, json={"role": "developer"}, headers=user.headers())
    assert r.status_code == 428
    r = await user.client.patch(url, json={"role": "developer"}, headers=user.headers(if_match='"v7"'))
    assert r.status_code == 412
    r = await user.client.patch(url, json={"role": "developer"}, headers=user.headers(if_match='"v0"'))
    validate_response("updateMember", r)
    assert r.status_code == 200 and r.json()["role"] == "developer" and r.headers["etag"] == '"v1"'
    r = await user.client.delete(url, headers=user.headers(if_match='"v1"'))
    validate_response("removeMember", r)
    assert r.status_code == 204
    for c in (admin, viewer):
        await c.client.aclose()


async def test_last_owner_cannot_leave(user: UserSession) -> None:
    ws = await user.create_workspace()
    url = f"/api/v1/workspaces/{ws['id']}/members/{user.user_id}"
    r = await user.client.patch(url, json={"role": "admin"}, headers=user.headers(if_match='"v0"'))
    assert r.status_code == 409 and r.json()["code"] == "last_owner"
    r = await user.client.delete(url, headers=user.headers(if_match='"v0"'))
    assert r.status_code == 409


async def test_add_unknown_user_is_validation_error(user: UserSession) -> None:
    ws = await user.create_workspace()
    r = await user.client.post(
        f"/api/v1/workspaces/{ws['id']}/members",
        json={"email": "ghost@example.test", "role": "viewer"},
        headers=user.headers(idem=True),
    )
    validate_response("addMember", r)
    assert r.status_code == 422


async def test_unknown_fields_rejected(user: UserSession) -> None:
    r = await user.client.post(
        "/api/v1/workspaces", json={"name": "x", "slug": "abc-d", "owner": "me"}, headers=user.headers(idem=True)
    )
    validate_response("createWorkspace", r)
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/owner"


@pytest.mark.parametrize("slug", ["ab-", "-ab", "a", "UPPER", "a_b"])
async def test_slug_rules_match_database(user: UserSession, slug: str) -> None:
    r = await user.client.post("/api/v1/workspaces", json={"name": "x", "slug": slug}, headers=user.headers(idem=True))
    validate_response("createWorkspace", r)
    assert r.status_code == 422, r.text
