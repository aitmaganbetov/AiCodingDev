"""B4 Worktrees (`GET /projects/{id}/worktrees`) и B6 глобальный поиск (`GET /search`): контракт, фильтры, пагинация,
отсутствие путей хоста, редактирование секретов в snippet, минимальная длина запроса, rate limit, изоляция workspace
(чужие данные не находятся и не просачиваются), Schemathesis-фазз (нет 5xx).
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.main import create_app
from aicd_api.routers.search import TYPE_PERMISSION, make_snippet
from aicd_identity.rbac import PERMISSIONS
from aicd_orchestration.approvals import Approvals
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from tests.conftest import UserSession, login
from tests.integration.test_oq45_api import _fuzz, _member
from tests.support import db as testdb
from tests.support import quality as qf
from tests.support.contract import validate_response
from tests.support.orch_api import add_task, running_run
from tests.support.orchestration import Tenant, connect_admin, update

pytestmark = pytest.mark.db


@dataclass
class World:
    user: UserSession
    ws: str
    tenant: Tenant
    run: uuid.UUID
    task: uuid.UUID
    finding: uuid.UUID
    approval: uuid.UUID
    source: uuid.UUID
    chat_message: str
    marker: str


async def _world(user: UserSession, database: testdb.TestDatabase, app_engine: AsyncEngine) -> World:
    marker = "zq" + secrets.token_hex(3)  # уникальное слово: результаты не смешиваются с другими тестами
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"], name=f"Payments {marker} gateway")
    with connect_admin(database) as admin:
        repo = uuid7()
        admin.execute(
            "INSERT INTO repositories (id, workspace_id, project_id, provider, local_root_ref)"
            " VALUES (%s, %s, %s, 'local', %s)",
            (repo, ws["id"], project["id"], f"main/{repo.hex}"),
        )
        tenant = Tenant(uuid.UUID(ws["id"]), uuid.UUID(user.user_id), uuid.UUID(project["id"]), repo)
        run = running_run(admin, tenant)
        update(
            admin, "runs", run, objective=f"Implement refund flow {marker} for payments\nwith password=hunter2secret"
        )
        task = add_task(admin, tenant, run, "refund")
        update(admin, "tasks", task, title=f"Refund API {marker}")
        finding = qf.finding(admin, tenant, run, rule=f"sql-injection-{marker}", cwe="CWE-89")
        source = uuid7()
        admin.execute(
            "INSERT INTO documentation_sources (id, workspace_id, source_type, uri_or_file_ref, created_by)"
            " VALUES (%s, %s, 'url', %s, %s)",
            (source, ws["id"], f"https://docs.example.test/{marker}/payments", user.user_id),
        )
    async with transaction(app_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
        approval = await Approvals().request(
            conn, workspace_id=tenant.workspace_id, project_id=tenant.project_id, run_id=run, task_id=None,
            action_type="MERGE", risk_level="high", subject={"run_id": str(run)},
            requested_by_type="system", requested_by=None,
        )  # fmt: skip
    r = await user.client.post(
        f"/api/v1/runs/{run}/chat/messages",
        json={"body": f"Проверьте {marker} возврат: token=ghp_{'b' * 30}"},
        headers=user.headers(idem=True),
    )
    assert r.status_code in (200, 201, 202), r.text
    chat_id = r.json().get("id") or r.json().get("message_id")
    return World(user, ws["id"], tenant, run, task, finding, approval, source, str(chat_id), marker)


@pytest.fixture
async def world(user: UserSession, database: testdb.TestDatabase, app_engine: AsyncEngine) -> World:
    return await _world(user, database, app_engine)


async def _search(user: UserSession, ws: str, **params: Any) -> Any:
    return await user.client.get("/api/v1/search", params=params, headers={"X-Workspace-Id": ws})


# --- Глобальный поиск (B6) -------------------------------------------------------------------------------


async def test_search_finds_every_entity_type_with_redacted_snippets(world: World) -> None:
    r = await _search(world.user, world.ws, q=world.marker, limit=50)
    validate_response("searchWorkspace", r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == world.marker and body["skipped_types"] == [] and body["truncated"] is False
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in body["items"]:
        by_type.setdefault(item["type"], []).append(item)
    assert set(by_type) == {"project", "run", "task", "finding", "documentation_source", "chat_message"}
    assert by_type["run"][0]["id"] == str(world.run) and by_type["run"][0]["api_path"] == f"/api/v1/runs/{world.run}"
    assert by_type["task"][0]["id"] == str(world.task) and by_type["task"][0]["run_id"] == str(world.run)
    assert by_type["finding"][0]["id"] == str(world.finding)
    assert by_type["finding"][0]["project_id"] == str(world.tenant.project_id)
    assert by_type["documentation_source"][0]["id"] == str(world.source)
    assert by_type["chat_message"][0]["run_id"] == str(world.run)
    raw = r.text
    assert "hunter2secret" not in raw and "ghp_" not in raw, "секреты в snippet редактируются"
    scores = [i["score"] for i in body["items"]]
    assert scores == sorted(scores, reverse=True)

    # Фильтр типов, approvals по action_type, UUID — точное совпадение id
    r = await _search(world.user, world.ws, q="merge", types=["approval"])
    validate_response("searchWorkspace", r)
    assert [i["id"] for i in r.json()["items"]] == [str(world.approval)] and r.json()["types"] == ["approval"]
    r = await _search(world.user, world.ws, q=str(world.task))
    assert [(i["type"], i["id"]) for i in r.json()["items"]] == [("task", str(world.task))]
    assert r.json()["items"][0]["score"] == 1.0
    # Находка — по правилу и CWE
    r = await _search(world.user, world.ws, q="CWE-89", types=["finding"])
    assert str(world.finding) in [i["id"] for i in r.json()["items"]]
    # limit и truncated
    r = await _search(world.user, world.ws, q=world.marker, limit=2)
    validate_response("searchWorkspace", r)
    assert len(r.json()["items"]) == 2 and r.json()["truncated"] is True
    # Wildcard LIKE — литерал
    r = await _search(world.user, world.ws, q="%%", types=["run", "task", "project"])
    assert r.status_code == 200 and r.json()["items"] == []


async def test_search_validation(world: World) -> None:
    for params in (
        {"q": "a"},
        {"q": "  a  "},
        {"q": "ab\x01"},
        {"q": "ok", "types": ["nope"]},
        {"q": "ok", "limit": 51},
    ):
        r = await _search(world.user, world.ws, **params)
        validate_response("searchWorkspace", r)
        assert r.status_code == 422, (params, r.text)
    r = await world.user.client.get("/api/v1/search", params={"q": "abc"})
    validate_response("searchWorkspace", r)
    assert r.status_code == 422  # нет X-Workspace-Id


async def test_search_never_leaks_other_workspace(
    world: World, app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase
) -> None:
    mallory = await login(app, app_engine)
    try:
        own = await mallory.create_workspace()
        await mallory.create_project(own["id"], name=f"own {world.marker} project")
        r = await _search(mallory, own["id"], q=world.marker, limit=50)
        validate_response("searchWorkspace", r)
        assert [i["type"] for i in r.json()["items"]] == ["project"], "только своё"
        r = await _search(mallory, own["id"], q=str(world.run))
        assert r.json()["items"] == []
        r = await _search(mallory, world.ws, q=world.marker)
        validate_response("searchWorkspace", r)
        assert r.status_code == 404 and r.json()["code"] == "not_found"
        # Участник-viewer видит данные workspace
        viewer = await login(app, app_engine)
        try:
            await _member(world.user, world.ws, viewer, "viewer")
            r = await _search(viewer, world.ws, q=world.marker, limit=50)
            assert r.status_code == 200 and len(r.json()["items"]) >= 6
        finally:
            await viewer.client.aclose()
    finally:
        await mallory.client.aclose()


def test_search_type_permissions_are_known_and_snippet_redacts() -> None:
    assert set(TYPE_PERMISSION.values()) <= set(PERMISSIONS)
    snippet = make_snippet("x" * 300 + " найти здесь secret=abc123 " + "y" * 300, "найти")
    assert snippet is not None and snippet.startswith("…") and snippet.endswith("…")
    assert "найти" in snippet and "abc123" not in snippet and len(snippet) <= 162


@pytest.fixture(scope="module")
async def limited_app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings.model_copy(update={"rate_limit_search_per_minute": 3}))
    async with application.router.lifespan_context(application):
        yield application


async def test_search_is_rate_limited_per_user(limited_app: FastAPI, app_engine: AsyncEngine) -> None:
    user = await login(limited_app, app_engine, ip="198.19.7.1")
    try:
        ws = await user.create_workspace()
        codes = [(await _search(user, ws["id"], q="abc")).status_code for _ in range(3)]
        blocked = await _search(user, ws["id"], q="abc")
        assert codes == [200, 200, 200]
        validate_response("searchWorkspace", blocked)
        assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
        # Обычное чтение не затронуто (отдельный бакет)
        assert (await user.client.get("/api/v1/projects", headers={"X-Workspace-Id": ws["id"]})).status_code == 200
    finally:
        await user.client.aclose()


# --- Worktrees (B4) --------------------------------------------------------------------------------------


def _worktree(
    admin: psycopg.Connection, tenant: Tenant, run: uuid.UUID, task: uuid.UUID | None, branch: str, status: str
) -> uuid.UUID:
    wid = uuid7()
    admin.execute(
        "INSERT INTO worktrees (id, workspace_id, repository_id, run_id, task_id, branch_name, base_commit_sha,"
        " head_commit_sha, path_ref, status, removed_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'REMOVED' THEN now() END)",
        (wid, tenant.workspace_id, tenant.repository_id, run, task, branch, "a" * 40,
         "b" * 40 if status != "ACTIVE" else None, f"{tenant.workspace_id}/{wid.hex}", status, status),
    )  # fmt: skip
    return wid


async def test_worktrees_listing_filters_and_hides_host_paths(
    world: World, database: testdb.TestDatabase, app: FastAPI, app_engine: AsyncEngine
) -> None:
    with connect_admin(database) as admin:
        active = _worktree(admin, world.tenant, world.run, world.task, f"aicd/{world.marker}/a", "ACTIVE")
        removed = _worktree(admin, world.tenant, world.run, None, f"aicd/{world.marker}/b", "REMOVED")
        orphan = _worktree(admin, world.tenant, world.run, None, f"aicd/{world.marker}/c", "ORPHANED")
        # Worktree другого проекта того же workspace — не попадает в список.
        other_project, other_repo = uuid7(), uuid7()
        admin.execute(
            "INSERT INTO projects (id, workspace_id, name, created_by) VALUES (%s, %s, %s, %s)",
            (other_project, world.ws, f"other-{world.marker}", world.user.user_id),
        )
        admin.execute(
            "INSERT INTO repositories (id, workspace_id, project_id, provider, local_root_ref)"
            " VALUES (%s, %s, %s, 'local', %s)",
            (other_repo, world.ws, other_project, f"main/{other_repo.hex}"),
        )
        _worktree(
            admin, Tenant(world.tenant.workspace_id, world.tenant.owner_id, other_project, other_repo), world.run,
            None, f"aicd/{world.marker}/z", "ACTIVE",
        )  # fmt: skip
    base = f"/api/v1/projects/{world.tenant.project_id}/worktrees"
    r = await world.user.client.get(base)
    validate_response("listProjectWorktrees", r)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [str(orphan), str(removed), str(active)]
    first = next(i for i in items if i["id"] == str(active))
    assert first["task_id"] == str(world.task) and first["run_id"] == str(world.run)
    assert first["branch_name"] == f"aicd/{world.marker}/a" and first["head_commit_sha"] is None
    assert next(i for i in items if i["id"] == str(removed))["removed_at"] is not None
    assert "path_ref" not in r.text and str(world.tenant.workspace_id) + "/" not in r.text
    assert all(set(i) == {"id", "repository_id", "run_id", "task_id", "branch_name", "base_commit_sha",
                          "head_commit_sha", "status", "created_at", "updated_at", "removed_at"} for i in items)  # fmt: skip

    r = await world.user.client.get(base, params={"status": ["ACTIVE", "ORPHANED"]})
    validate_response("listProjectWorktrees", r)
    assert {i["id"] for i in r.json()["items"]} == {str(active), str(orphan)}
    r = await world.user.client.get(base, params={"repository_id": str(uuid7())})
    assert r.status_code == 200 and r.json()["items"] == []
    r = await world.user.client.get(base, params={"run_id": str(world.run), "limit": 2})
    validate_response("listProjectWorktrees", r)
    page2 = await world.user.client.get(
        base, params={"run_id": str(world.run), "limit": 2, "cursor": r.json()["next_cursor"]}
    )
    validate_response("listProjectWorktrees", page2)
    assert [i["id"] for i in r.json()["items"] + page2.json()["items"]] == [str(orphan), str(removed), str(active)]
    bad = await world.user.client.get(base, params={"cursor": r.json()["next_cursor"], "status": ["LOCKED"]})
    assert bad.status_code == 422
    r = await world.user.client.get(base, params={"status": ["GONE"]})
    validate_response("listProjectWorktrees", r)
    assert r.status_code == 422

    stranger = await login(app, app_engine)
    try:
        await stranger.create_workspace()
        r = await stranger.client.get(base)
        validate_response("listProjectWorktrees", r)
        assert r.status_code == 404 and r.json()["code"] == "not_found"
    finally:
        await stranger.client.aclose()


async def test_search_and_worktrees_fuzz_no_5xx(world: World) -> None:
    await _fuzz(world.user, "listProjectWorktrees", {"project_id": str(world.tenant.project_id)}, None)
    await _fuzz(world.user, "searchWorkspace", {}, world.ws)
