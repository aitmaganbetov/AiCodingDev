"""Проекты, репозитории, idempotency, аудит, health (P1-11, P1-12)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import UserSession, init_git_repo, login, make_client
from tests.support.contract import validate_response

pytestmark = pytest.mark.db


async def test_project_crud_with_etag(user: UserSession) -> None:
    ws = await user.create_workspace()
    r = await user.client.post(
        "/api/v1/projects",
        json={"name": "Alpha", "documentation_mode": "OFF"},
        headers=user.headers(ws["id"], idem=True),
    )
    validate_response("createProject", r)
    project = r.json()
    assert r.status_code == 201 and project["settings"]["documentation_mode"] == "OFF"
    assert project["settings"]["quality_profile"] == "strict" and r.headers["location"].endswith(project["id"])

    r = await user.client.get("/api/v1/projects", headers={"X-Workspace-Id": ws["id"]})
    validate_response("listProjects", r)
    assert [p["id"] for p in r.json()["items"]] == [project["id"]]

    r = await user.client.get(f"/api/v1/projects/{project['id']}")
    validate_response("getProject", r)
    etag = r.headers["etag"]
    assert (
        await user.client.get(f"/api/v1/projects/{project['id']}", headers={"If-None-Match": etag})
    ).status_code == 304

    url = f"/api/v1/projects/{project['id']}"
    r = await user.client.patch(
        url, json={"name": "Beta", "quality_profile": "critical"}, headers=user.headers(if_match=etag)
    )
    validate_response("updateProject", r)
    assert r.json()["name"] == "Beta" and r.json()["settings"]["quality_profile"] == "critical"
    r2 = await user.client.patch(url, json={"name": "Gamma"}, headers=user.headers(if_match=etag))
    validate_response("updateProject", r2)
    assert r2.status_code == 412, "устаревший ETag отклоняется"
    r3 = await user.client.patch(url, json={}, headers=user.headers(if_match=r.headers["etag"]))
    assert r3.status_code == 422


async def test_idempotency_replay_reuse_and_missing(user: UserSession, app_engine: AsyncEngine) -> None:
    ws = await user.create_workspace()
    headers = user.headers(ws["id"], idem_key="proj-key-" + secrets.token_hex(4))
    first = await user.client.post("/api/v1/projects", json={"name": "Idem"}, headers=headers)
    replay = await user.client.post("/api/v1/projects", json={"name": "Idem"}, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert replay.headers["idempotency-replayed"] == "true" and replay.json() == first.json()
    r = await user.client.get("/api/v1/projects", headers={"X-Workspace-Id": ws["id"]})
    assert len(r.json()["items"]) == 1, "повтор не создаёт второй проект"
    reuse = await user.client.post("/api/v1/projects", json={"name": "Other"}, headers=headers)
    validate_response("createProject", reuse)
    assert reuse.status_code == 422 and reuse.json()["code"] == "idempotency_key_reuse"
    missing = await user.client.post("/api/v1/projects", json={"name": "NoKey"}, headers=user.headers(ws["id"]))
    validate_response("createProject", missing)
    assert missing.status_code == 400 and missing.json()["code"] == "idempotency_key_missing"


async def test_conflict_response_is_stored_for_replay(user: UserSession) -> None:
    ws = await user.create_workspace()
    await user.create_project(ws["id"], "Dup")
    headers = user.headers(ws["id"], idem=True)
    a = await user.client.post("/api/v1/projects", json={"name": "Dup"}, headers=headers)
    b = await user.client.post("/api/v1/projects", json={"name": "Dup"}, headers=headers)
    assert a.status_code == b.status_code == 409 and b.headers.get("idempotency-replayed") == "true"


async def test_role_restrictions(app: FastAPI, app_engine: AsyncEngine, user: UserSession) -> None:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    dev = await login(app, app_engine)
    r = await user.client.post(
        f"/api/v1/workspaces/{ws['id']}/members",
        json={"email": dev.email, "role": "developer"},
        headers=user.headers(idem=True),
    )
    assert r.status_code == 201
    r = await dev.client.post("/api/v1/projects", json={"name": "nope"}, headers=dev.headers(ws["id"], idem=True))
    validate_response("createProject", r)
    assert r.status_code == 403
    r = await dev.client.get(f"/api/v1/projects/{project['id']}")
    assert r.status_code == 200
    r = await dev.client.get("/api/v1/audit-events", headers={"X-Workspace-Id": ws["id"]})
    validate_response("listAuditEvents", r)
    assert r.status_code == 403
    # отказ на небезопасном методе аудируется
    r = await user.client.get(
        "/api/v1/audit-events", headers={"X-Workspace-Id": ws["id"]}, params={"action": "authz.denied"}
    )
    assert r.status_code == 200 and any(e["actor_id"] == dev.user_id for e in r.json()["items"])
    await dev.client.aclose()


async def test_attach_local_repository(user: UserSession, repo_root: Path) -> None:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo_dir = repo_root / f"repo-{secrets.token_hex(3)}"
    init_git_repo(repo_dir)
    url = f"/api/v1/projects/{project['id']}/repositories"
    r = await user.client.post(
        url, json={"provider": "local", "local_path": str(repo_dir)}, headers=user.headers(idem=True)
    )
    validate_response("attachRepository", r)
    assert r.status_code == 201, r.text
    repo = r.json()
    assert repo["local_root_ref"] == f"main/{repo_dir.name}" and repo["default_branch"] == "main"
    assert str(repo_root) not in r.text, "абсолютные пути хоста не раскрываются"
    r = await user.client.get(url)
    validate_response("listRepositories", r)
    assert [x["id"] for x in r.json()["items"]] == [repo["id"]]
    again = await user.client.post(
        url, json={"provider": "local", "local_path": f"main/{repo_dir.name}"}, headers=user.headers(idem=True)
    )
    assert again.status_code == 409


async def test_detach_repository_frees_the_directory_and_keeps_history(user: UserSession, repo_root: Path) -> None:
    """Отвязка (FR-PRJ-02): проект репозиторий больше не показывает, каталог снова свободен, история цела.

    Привязанный по ошибке репозиторий раньше оставался в проекте навсегда — а проект с двумя репозиториями
    вдобавок не принимает новые прогоны (`repository_required`, OQ-22).
    """
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo_dir = repo_root / f"detach-{secrets.token_hex(3)}"
    init_git_repo(repo_dir)
    url = f"/api/v1/projects/{project['id']}/repositories"
    attached = await user.client.post(
        url, json={"provider": "local", "local_path": f"main/{repo_dir.name}"}, headers=user.headers(idem=True)
    )
    assert attached.status_code == 201, attached.text
    repository_id = attached.json()["id"]

    detached = await user.client.delete(f"{url}/{repository_id}", headers=user.headers(ws["id"]))
    validate_response("detachRepository", detached)
    assert detached.status_code == 204, detached.text
    assert (await user.client.delete(f"{url}/{repository_id}", headers=user.headers(ws["id"]))).status_code == 204

    listed = await user.client.get(url)
    assert listed.json()["items"] == [], "отвязанный репозиторий проекту больше не принадлежит"
    roots = await user.client.get("/api/v1/repository-roots", headers={"X-Workspace-Id": ws["id"]})
    freed = {e["ref"]: e["attached"] for e in roots.json()["items"][0]["repositories"]}
    assert freed.get(f"main/{repo_dir.name}") is False, "каталог снова можно выбрать"

    # Тот же каталог привязывается заново — это новый репозиторий, прежняя строка осталась в истории.
    again = await user.client.post(
        url, json={"provider": "local", "local_path": f"main/{repo_dir.name}"}, headers=user.headers(idem=True)
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] != repository_id

    audit = await user.client.get(
        "/api/v1/audit-events", headers={"X-Workspace-Id": ws["id"]}, params={"action": "repository.detach"}
    )
    assert [e["resource_id"] for e in audit.json()["items"]] == [repository_id]

    # Чужой проект/рабочее пространство репозитория не видит — 404, а не 403/204.
    other = await user.create_project(ws["id"])
    missing = await user.client.delete(
        f"/api/v1/projects/{other['id']}/repositories/{repository_id}", headers=user.headers(ws["id"])
    )
    assert missing.status_code == 404, missing.text


async def test_detach_is_refused_while_a_run_is_unfinished(user: UserSession, repo_root: Path) -> None:
    """Прогон работает с репозиторием: отвязать его на полпути нельзя (409), после отмены — можно."""
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo_dir = repo_root / f"busy-{secrets.token_hex(3)}"
    init_git_repo(repo_dir)
    url = f"/api/v1/projects/{project['id']}/repositories"
    attached = await user.client.post(
        url, json={"provider": "local", "local_path": f"main/{repo_dir.name}"}, headers=user.headers(idem=True)
    )
    assert attached.status_code == 201, attached.text
    repository_id = attached.json()["id"]
    run = await user.client.post(
        "/api/v1/runs",
        json={"project_id": project["id"], "mode": "DEVELOP", "objective": "проверка отвязки"},
        headers=user.headers(ws["id"], idem=True),
    )
    assert run.status_code == 201, run.text

    busy = await user.client.delete(f"{url}/{repository_id}", headers=user.headers(ws["id"]))
    assert busy.status_code == 409, busy.text
    assert "незавершённых прогонов" in busy.json()["detail"]
    assert [x["id"] for x in (await user.client.get(url)).json()["items"]] == [repository_id]

    cancelled = await user.client.post(
        f"/api/v1/runs/{run.json()['run_id']}/cancel", headers=user.headers(ws["id"], idem=True)
    )
    assert cancelled.status_code == 202, cancelled.text
    assert (await user.client.delete(f"{url}/{repository_id}", headers=user.headers(ws["id"]))).status_code == 204


@pytest.mark.parametrize("case", ["traversal", "outside", "symlink", "not_git", "nested", "github"])
async def test_attach_rejects_unsafe_paths(user: UserSession, repo_root: Path, tmp_path: Path, case: str) -> None:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    outside = tmp_path / "outside-repo"
    init_git_repo(outside)
    if case == "traversal":
        body = {"provider": "local", "local_path": "main/../../etc"}
    elif case == "outside":
        body = {"provider": "local", "local_path": str(outside)}
    elif case == "symlink":
        link = repo_root / f"link-{secrets.token_hex(3)}"
        os.symlink(outside, link)
        body = {"provider": "local", "local_path": str(link)}
    elif case == "not_git":
        plain = repo_root / f"plain-{secrets.token_hex(3)}"
        plain.mkdir()
        body = {"provider": "local", "local_path": str(plain)}
    elif case == "nested":
        repo = repo_root / f"nest-{secrets.token_hex(3)}"
        init_git_repo(repo)
        (repo / "sub").mkdir()
        body = {"provider": "local", "local_path": str(repo / "sub")}
    else:
        body = {"provider": "github", "remote_url": "https://github.com/a/b"}
    r = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories", json=body, headers=user.headers(idem=True)
    )
    validate_response("attachRepository", r)
    assert r.status_code == 422, r.text
    assert str(tmp_path) not in r.text


async def test_repository_roots_show_what_can_be_attached(
    app: FastAPI, app_engine: AsyncEngine, user: UserSession, repo_root: Path
) -> None:
    """Форма привязки берёт список каталогов отсюда: `ref` каждого из них должен приниматься `attachRepository`."""
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo_dir = repo_root / f"roots-{secrets.token_hex(3)}"
    init_git_repo(repo_dir)
    (repo_root / f"plain-{secrets.token_hex(3)}").mkdir()

    r = await user.client.get("/api/v1/repository-roots", headers={"X-Workspace-Id": ws["id"]})
    validate_response("listRepositoryRoots", r)
    assert r.status_code == 200, r.text
    [root] = r.json()["items"]
    assert root["name"] == "main" and root["path"] == str(repo_root)
    offered = {entry["ref"] for entry in root["repositories"]}
    assert f"main/{repo_dir.name}" in offered
    assert not any(entry["ref"].startswith("main/plain-") for entry in root["repositories"]), "не Git — не предлагаем"

    assert all(entry["attached"] is False for entry in root["repositories"]), "ничего ещё не привязано"

    attached = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"provider": "local", "local_path": f"main/{repo_dir.name}"},
        headers=user.headers(idem=True),
    )
    validate_response("attachRepository", attached)
    assert attached.status_code == 201, attached.text

    # Каталог занят: он остаётся в списке, но помечен — форма не даст выбрать его второй раз.
    r = await user.client.get("/api/v1/repository-roots", headers={"X-Workspace-Id": ws["id"]})
    validate_response("listRepositoryRoots", r)
    taken = {e["ref"]: e["attached"] for e in r.json()["items"][0]["repositories"]}
    assert taken[f"main/{repo_dir.name}"] is True, taken


async def test_repository_roots_require_the_attach_permission(
    app: FastAPI, app_engine: AsyncEngine, user: UserSession
) -> None:
    """Список каталогов хоста — только тем, кто и так может привязывать репозитории."""
    ws = await user.create_workspace()
    viewer = await login(app, app_engine)
    added = await user.client.post(
        f"/api/v1/workspaces/{ws['id']}/members",
        json={"email": viewer.email, "role": "viewer"},
        headers=user.headers(idem=True),
    )
    assert added.status_code == 201, added.text
    r = await viewer.client.get("/api/v1/repository-roots", headers={"X-Workspace-Id": ws["id"]})
    assert r.status_code == 403, r.text
    await viewer.client.aclose()


async def test_audit_trail_records_actions(user: UserSession) -> None:
    ws = await user.create_workspace()
    await user.create_project(ws["id"])
    r = await user.client.get("/api/v1/audit-events", headers={"X-Workspace-Id": ws["id"]})
    validate_response("listAuditEvents", r)
    actions = {e["action"] for e in r.json()["items"]}
    assert {"workspace.create", "project.create"} <= actions
    r2 = await user.client.get(
        "/api/v1/audit-events", headers={"X-Workspace-Id": ws["id"]}, params={"action": "audit.query"}
    )
    assert len(r2.json()["items"]) >= 1, "чтение журнала аудируется"


async def test_health(app: FastAPI) -> None:
    async with make_client(app) as client:
        live = await client.get("/api/v1/health/live")
        ready = await client.get("/api/v1/health/ready")
    validate_response("healthLive", live)
    validate_response("healthReady", ready)
    assert ready.json() == {
        "status": "ok",
        "checks": {"postgres": "ok", "migrations": "ok", "redis": "ok", "object_storage": "ok"},
    }


async def test_security_headers_and_problem_format(user: UserSession) -> None:
    r = await user.client.get("/api/v1/projects/not-a-uuid")
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    for h in ("x-content-type-options", "x-frame-options", "cache-control", "content-security-policy", "x-request-id"):
        assert h in r.headers
    assert r.json()["type"] == "urn:aicd:problem:validation_error"
    big = await user.client.post(
        "/api/v1/workspaces",
        content=b"{" + b" " * 300_000 + b"}",
        headers={**user.headers(idem=True), "Content-Type": "application/json"},
    )
    assert big.status_code == 413
    xml = await user.client.post(
        "/api/v1/workspaces", content=b"<x/>", headers={**user.headers(idem=True), "Content-Type": "application/xml"}
    )
    assert xml.status_code == 415
