"""P1-16 / AC-12: пользователь без прав не видит чужой workspace и не может в нём действовать.

Не член workspace получает 404 (не 403) — существование ресурса не раскрывается.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import UserSession, login
from tests.support.contract import validate_response

pytestmark = pytest.mark.db


@dataclass
class World:
    alice: UserSession
    mallory: UserSession
    ws: dict[str, Any]
    project: dict[str, Any]


@pytest.fixture
async def world(app: FastAPI, app_engine: AsyncEngine) -> AsyncIterator[World]:
    alice, mallory = await login(app, app_engine), await login(app, app_engine)
    ws = await alice.create_workspace()
    project = await alice.create_project(ws["id"])
    await mallory.create_workspace()  # у mallory есть свой workspace и роль owner в нём
    yield World(alice, mallory, ws, project)
    await alice.client.aclose()
    await mallory.client.aclose()


def _cases(w: World) -> list[tuple[str, str, str, dict[str, Any]]]:
    ws, p = w.ws["id"], w.project["id"]
    m = w.mallory
    return [
        ("listProjects", "GET", "/api/v1/projects", {"headers": {"X-Workspace-Id": ws}}),
        ("createProject", "POST", "/api/v1/projects", {"json": {"name": "x"}, "headers": m.headers(ws, idem=True)}),
        ("getProject", "GET", f"/api/v1/projects/{p}", {}),
        (
            "updateProject",
            "PATCH",
            f"/api/v1/projects/{p}",
            {"json": {"name": "pwned"}, "headers": m.headers(if_match='"v0"')},
        ),
        ("listRepositories", "GET", f"/api/v1/projects/{p}/repositories", {}),
        (
            "attachRepository",
            "POST",
            f"/api/v1/projects/{p}/repositories",
            {"json": {"provider": "local", "local_path": "main/x"}, "headers": m.headers(idem=True)},
        ),
        ("listMembers", "GET", f"/api/v1/workspaces/{ws}/members", {}),
        (
            "addMember",
            "POST",
            f"/api/v1/workspaces/{ws}/members",
            {"json": {"email": m.email, "role": "owner"}, "headers": m.headers(idem=True)},
        ),
        (
            "updateMember",
            "PATCH",
            f"/api/v1/workspaces/{ws}/members/{w.alice.user_id}",
            {"json": {"role": "viewer"}, "headers": m.headers(if_match='"v0"')},
        ),
        (
            "removeMember",
            "DELETE",
            f"/api/v1/workspaces/{ws}/members/{w.alice.user_id}",
            {"headers": m.headers(if_match='"v0"')},
        ),
        ("listAuditEvents", "GET", "/api/v1/audit-events", {"headers": {"X-Workspace-Id": ws}}),
    ]


async def test_foreign_workspace_is_invisible(world: World) -> None:
    for operation, method, url, kwargs in _cases(world):
        r = await world.mallory.client.request(method, url, **kwargs)
        assert r.status_code == 404, f"{operation}: {r.status_code} {r.text}"
        assert r.json()["code"] == "not_found"
        validate_response(operation, r)
    # ничего не изменилось
    r = await world.alice.client.get(f"/api/v1/projects/{world.project['id']}")
    assert r.json()["name"] == world.project["name"]
    r = await world.alice.client.get(f"/api/v1/workspaces/{world.ws['id']}/members")
    assert [m["user"]["id"] for m in r.json()["items"]] == [world.alice.user_id]


async def test_unknown_resource_and_foreign_resource_look_identical(world: World) -> None:
    fake = "0190f5a0-0000-7000-8000-" + secrets.token_hex(6)
    foreign = await world.mallory.client.get(f"/api/v1/projects/{world.project['id']}")
    missing = await world.mallory.client.get(f"/api/v1/projects/{fake}")
    assert foreign.status_code == missing.status_code == 404

    def strip(body: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in body.items() if k not in ("instance", "request_id")}

    assert strip(foreign.json()) == strip(missing.json())


async def test_workspace_header_mismatch_on_resource_is_404(world: World) -> None:
    mallory_ws = (await world.mallory.client.get("/api/v1/workspaces")).json()["items"][0]["id"]
    r = await world.alice.client.get(f"/api/v1/projects/{world.project['id']}", headers={"X-Workspace-Id": mallory_ws})
    assert r.status_code == 404


async def test_workspace_list_only_own(world: World) -> None:
    r = await world.mallory.client.get("/api/v1/workspaces")
    assert world.ws["id"] not in {w["id"] for w in r.json()["items"]}


async def test_removed_member_loses_access(app: FastAPI, app_engine: AsyncEngine, world: World) -> None:
    bob = await login(app, app_engine)
    r = await world.alice.client.post(
        f"/api/v1/workspaces/{world.ws['id']}/members",
        json={"email": bob.email, "role": "developer"},
        headers=world.alice.headers(idem=True),
    )
    assert r.status_code == 201
    assert (await bob.client.get(f"/api/v1/projects/{world.project['id']}")).status_code == 200
    r = await world.alice.client.delete(
        f"/api/v1/workspaces/{world.ws['id']}/members/{bob.user_id}",
        headers=world.alice.headers(if_match='"v0"'),
    )
    assert r.status_code == 204
    assert (await bob.client.get(f"/api/v1/projects/{world.project['id']}")).status_code == 404
    await bob.client.aclose()
