"""OQ-45 (c), (f): ветки/история репозитория, workspace GET/PATCH, технологии workspace, probe провайдера.

Каждый ответ проверяется по контракту (`validate_response`); RBAC по ролям, 404 для чужого workspace,
fuzz по контракту (Schemathesis) — без 5xx.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import hypothesis
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters import registry
from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import worktree_resolver
from aicd_cli_adapters.models import ProbeResult
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.local_runner import LocalProcessRunner
from aicd_worker import handlers, runtime
from tests.conftest import UserSession, login
from tests.integration.test_docs_tech_api import _detect_and_store
from tests.integration.test_repository_explorer import _git, _make_repo
from tests.support.cli import MemorySink
from tests.support.contract import OPENAPI_PATH, validate_response

pytestmark = pytest.mark.db


async def _member(owner: UserSession, ws: str, other: UserSession, role: str) -> None:
    r = await owner.client.post(
        f"/api/v1/workspaces/{ws}/members", json={"email": other.email, "role": role}, headers=owner.headers(idem=True)
    )
    assert r.status_code == 201, r.text


def _cases(operation_id: str, count: int) -> list[Any]:
    schema = schemathesis.openapi.from_path(OPENAPI_PATH)
    op = schema.find_operation_by_id(operation_id)
    drawn: list[Any] = []

    @hypothesis.settings(
        max_examples=count, deadline=None, database=None, suppress_health_check=list(HealthCheck), derandomize=True
    )
    @given(case=op.as_strategy())
    def collect(case: Any) -> None:
        drawn.append(case)

    collect()
    return drawn


async def _fuzz(client_session: UserSession, operation_id: str, real: dict[str, str], ws: str | None) -> None:
    for n, case in enumerate(_cases(operation_id, 20)):
        path = case.formatted_path
        if n % 2 == 0:
            for name, value in (case.path_parameters or {}).items():
                path = path.replace(str(value), real.get(name, str(value)), 1)
        headers = {
            k: v
            for k, v in (case.headers or {}).items()
            if k.lower() not in ("cookie", "x-csrf-token", "last-event-id")
        }
        if ws is not None and n % 2 == 0:
            headers["X-Workspace-Id"] = ws
        r = await client_session.client.get(f"/api/v1{path}", headers=headers, params=case.query or None)
        assert r.status_code < 500, f"{operation_id} {path} {case.query!r}: {r.status_code} {r.text}"
        validate_response(operation_id, r)


# --- Repository Explorer: ветки и история (OQ-45 (c)) -----------------------------------------------------


async def _setup(user: UserSession, repo_root: Path) -> tuple[str, dict[str, Any], str, str, Path]:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo_dir = repo_root / f"oq45-{secrets.token_hex(3)}"
    first, second = _make_repo(repo_dir)
    r = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"provider": "local", "local_path": str(repo_dir)},
        headers=user.headers(idem=True),
    )
    assert r.status_code == 201, r.text
    return f"/api/v1/projects/{project['id']}/repositories/{r.json()['id']}", r.json(), first, second, repo_dir


async def test_branches_and_commits(user: UserSession, repo_root: Path) -> None:
    base, repo, first, second, repo_dir = await _setup(user, repo_root)
    _git(repo_dir, "branch", "feature/x", first)

    r = await user.client.get(f"{base}/branches")
    validate_response("listRepositoryBranches", r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repository_id"] == repo["id"] and body["default_branch"] == "main" and body["truncated"] is False
    names = [(b["name"], b["commit_sha"], b["is_default"]) for b in body["items"]]
    assert names[0] == ("main", second, True)
    assert ("feature/x", first, False) in names and len(names) == 2
    assert all(b["committed_at"] for b in body["items"])
    page1 = await user.client.get(f"{base}/branches", params={"limit": 1})
    validate_response("listRepositoryBranches", page1)
    page2 = await user.client.get(f"{base}/branches", params={"limit": 1, "cursor": page1.json()["next_cursor"]})
    validate_response("listRepositoryBranches", page2)
    assert [b["name"] for b in page1.json()["items"] + page2.json()["items"]] == ["main", "feature/x"]
    assert page2.json()["next_cursor"] is None

    r = await user.client.get(f"{base}/commits")
    validate_response("listRepositoryCommits", r)
    commits = r.json()
    assert commits["commit_sha"] == second
    assert [c["sha"] for c in commits["items"]] == [second, first]
    assert commits["items"][0]["parents"] == [first] and commits["items"][1]["parents"] == []
    assert commits["items"][0]["subject"] == "second" and commits["items"][0]["author_name"] == "t"
    assert "t@e" not in r.text  # email автора не отдаётся
    by_branch = await user.client.get(f"{base}/commits", params={"commit": "feature/x"})
    assert [c["sha"] for c in by_branch.json()["items"]] == [first]
    p1 = await user.client.get(f"{base}/commits", params={"limit": 1})
    validate_response("listRepositoryCommits", p1)
    # Новый commit на main не сдвигает страницы курсора (SHA зафиксирован в курсоре).
    (repo_dir / "new.txt").write_text("x\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-qm", "token=ghp_" + "a" * 36)
    p2 = await user.client.get(f"{base}/commits", params={"limit": 1, "cursor": p1.json()["next_cursor"]})
    validate_response("listRepositoryCommits", p2)
    assert [c["sha"] for c in p1.json()["items"] + p2.json()["items"]] == [second, first]
    assert p2.json()["next_cursor"] is None
    head = await user.client.get(f"{base}/commits", params={"limit": 1})
    assert "ghp_" + "a" * 36 not in head.text  # секрет в теме commit редактируется

    for bad, status in (("--output=/tmp/x", 422), ("HEAD~1", 422), ("nosuchbranch", 404)):
        r = await user.client.get(f"{base}/commits", params={"commit": bad})
        validate_response("listRepositoryCommits", r)
        assert r.status_code == status, (bad, r.text)
    garbage = await user.client.get(f"{base}/commits", params={"cursor": "garbage"})
    validate_response("listRepositoryCommits", garbage)
    assert garbage.status_code == 422


async def test_branches_commits_isolation_and_fuzz(
    app: FastAPI, app_engine: AsyncEngine, user: UserSession, repo_root: Path
) -> None:
    base, repo, _first, _second, _dir = await _setup(user, repo_root)
    project_id = base.split("/")[4]
    stranger = await login(app, app_engine)
    viewer = await login(app, app_engine)
    try:
        ws = (await user.client.get(f"/api/v1/projects/{project_id}")).json()["workspace_id"]
        await _member(user, ws, viewer, "viewer")
        for op_id, suffix in (("listRepositoryBranches", "branches"), ("listRepositoryCommits", "commits")):
            r = await stranger.client.get(f"{base}/{suffix}")
            validate_response(op_id, r)
            assert r.status_code == 404 and "main" not in r.text
            r = await viewer.client.get(f"{base}/{suffix}")
            validate_response(op_id, r)
            assert r.status_code == 200
            other_ws = await user.create_workspace()
            foreign = await user.create_project(other_ws["id"])
            r = await user.client.get(f"/api/v1/projects/{foreign['id']}/repositories/{repo['id']}/{suffix}")
            validate_response(op_id, r)
            assert r.status_code == 404
            await _fuzz(user, op_id, {"project_id": project_id, "repository_id": repo["id"]}, None)
    finally:
        await stranger.client.aclose()
        await viewer.client.aclose()


# --- Workspace GET/PATCH (OQ-45 (f)) ----------------------------------------------------------------------


async def _audit_actions(engine: AsyncEngine, ws: str, user_id: str) -> list[tuple[str, dict[str, Any]]]:
    async with transaction(engine, DbContext(user_id=uuid.UUID(user_id), workspace_id=uuid.UUID(ws))) as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT action, metadata FROM audit_events WHERE workspace_id = :ws"
                    " AND action IN ('workspace.update', 'policy.changed', 'authz.denied') ORDER BY created_at, id"
                ),
                {"ws": ws},
            )
        ).all()
    return [(r[0], dict(r[1])) for r in rows]


async def test_workspace_get_and_patch(app: FastAPI, app_engine: AsyncEngine, user: UserSession) -> None:
    ws = await user.create_workspace()
    assert ws["version"] == 0
    url = f"/api/v1/workspaces/{ws['id']}"
    r = await user.client.get(url)
    validate_response("getWorkspace", r)
    assert r.status_code == 200 and r.headers["etag"] == '"v0"'
    assert r.json()["role"] == "owner" and r.json()["version"] == 0 and r.json()["settings"] == {}

    # If-Match: нет ⇒ 428, неверный ⇒ 412 с актуальным ETag.
    r = await user.client.patch(url, json={"name": "Новое имя"}, headers=user.headers())
    validate_response("updateWorkspace", r)
    assert r.status_code == 428
    r = await user.client.patch(url, json={"name": "Новое имя"}, headers=user.headers(if_match='"v7"'))
    validate_response("updateWorkspace", r)
    assert r.status_code == 412 and r.headers["etag"] == '"v0"'

    r = await user.client.patch(
        url,
        json={"name": "Новое имя", "settings": {"allow_single_member_self_approval": True}},
        headers=user.headers(if_match='"v0"'),
    )
    validate_response("updateWorkspace", r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Новое имя" and body["version"] == 1 and r.headers["etag"] == '"v1"'
    assert body["settings"]["allow_single_member_self_approval"] is True
    # Повтор без изменений — no-op без смены версии.
    r = await user.client.patch(url, json={"name": "Новое имя"}, headers=user.headers(if_match='"v1"'))
    assert r.status_code == 200 and r.json()["version"] == 1
    listed = await user.client.get("/api/v1/workspaces")
    assert next(w for w in listed.json()["items"] if w["id"] == ws["id"])["name"] == "Новое имя"

    for payload, pointer in (
        ({}, "/"),
        ({"name": None}, "/name"),
        ({"settings": {}}, None),
        ({"slug": "x"}, None),
        ({"settings": {"unknown": 1}}, None),
        ({"name": ""}, None),
    ):
        r = await user.client.patch(url, json=payload, headers=user.headers(if_match='"v1"'))
        validate_response("updateWorkspace", r)
        assert r.status_code == 422, (payload, r.text)
        if pointer:
            assert r.json()["errors"][0]["pointer"] == pointer

    actions = await _audit_actions(app_engine, ws["id"], user.user_id)
    assert actions[0] == ("workspace.update", {**actions[0][1], "changed": ["name", "settings"]})
    assert actions[1][0] == "policy.changed"
    assert actions[1][1]["policy"] == "workspace.allow_single_member_self_approval"
    assert actions[1][1]["from"] is None and actions[1][1]["to"] is True


async def test_workspace_rbac_and_isolation(app: FastAPI, app_engine: AsyncEngine, user: UserSession) -> None:
    ws = await user.create_workspace()
    url = f"/api/v1/workspaces/{ws['id']}"
    sessions: dict[str, UserSession] = {}
    try:
        for role in ("admin", "developer", "reviewer", "viewer"):
            sessions[role] = await login(app, app_engine)
            await _member(user, ws["id"], sessions[role], role)
        version = 0
        for role, session in sessions.items():
            r = await session.client.get(url)
            validate_response("getWorkspace", r)
            assert r.status_code == 200 and r.json()["role"] == role
            r = await session.client.patch(
                url, json={"name": f"by-{role}"}, headers=session.headers(if_match=f'"v{version}"')
            )
            validate_response("updateWorkspace", r)
            if role == "admin":
                assert r.status_code == 200, r.text
                version = r.json()["version"]
            else:
                assert r.status_code == 403, (role, r.text)
        # admin не меняет политики workspace (только owner) — 403 и authz.denied.
        admin = sessions["admin"]
        r = await admin.client.patch(
            url,
            json={"settings": {"allow_single_member_self_approval": True}},
            headers=admin.headers(if_match=f'"v{version}"'),
        )
        validate_response("updateWorkspace", r)
        assert r.status_code == 403
        denied = [m for a, m in await _audit_actions(app_engine, ws["id"], user.user_id) if a == "authz.denied"]
        assert any(m.get("permission") == "workspace:policy" for m in denied)

        stranger = await login(app, app_engine)
        try:
            await stranger.create_workspace()
            r = await stranger.client.get(url)
            validate_response("getWorkspace", r)
            assert r.status_code == 404
            r = await stranger.client.patch(url, json={"name": "x"}, headers=stranger.headers(if_match='"v0"'))
            validate_response("updateWorkspace", r)
            assert r.status_code == 404
            missing = await user.client.get(f"/api/v1/workspaces/{uuid7()}")
            assert missing.status_code == 404 and missing.json()["code"] == "not_found"
        finally:
            await stranger.client.aclose()
        await _fuzz(user, "getWorkspace", {"workspace_id": ws["id"]}, None)
    finally:
        for s in sessions.values():
            await s.client.aclose()


# --- Технологии workspace (OQ-45 (c)) ---------------------------------------------------------------------


async def test_workspace_technologies(
    app: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, user: UserSession, tmp_path: Path
) -> None:
    ws = await user.create_workspace()
    url = "/api/v1/technologies"
    r = await user.client.get(url, headers={"X-Workspace-Id": ws["id"]})
    validate_response("listWorkspaceTechnologies", r)
    assert r.status_code == 200 and r.json() == {"items": [], "next_cursor": None}

    p1 = await user.create_project(ws["id"])
    p2 = await user.create_project(ws["id"])
    await _detect_and_store(worker_engine, tmp_path, ws["id"], p1["id"], "python")
    await _detect_and_store(worker_engine, tmp_path, ws["id"], p2["id"], "python")
    # Другой workspace с другим стеком — его технологии не видны.
    other = await user.create_workspace()
    p3 = await user.create_project(other["id"])
    await _detect_and_store(worker_engine, tmp_path, other["id"], p3["id"], "php")

    r = await user.client.get(url, headers={"X-Workspace-Id": ws["id"]}, params={"limit": 200})
    validate_response("listWorkspaceTechnologies", r)
    items = {(i["ecosystem"], i["name"]): i for i in r.json()["items"]}
    fastapi = items[("python", "fastapi")]
    assert fastapi["project_count"] == 2 and fastapi["source_count"] == 0
    assert not any(name == "laravel/framework" for _, name in items)

    # Источник документации увеличивает source_count.
    src = await user.client.post(
        "/api/v1/documentation/sources",
        json={
            "technology_version_id": fastapi["technology_version_id"],
            "source_type": "url",
            "uri_or_file_ref": f"https://docs.example.com/{secrets.token_hex(3)}",
            "trust_level": "official",
            "refresh_policy": "manual",
        },
        headers=user.headers(ws["id"], idem=True),
    )
    assert src.status_code == 201, src.text
    r = await user.client.get(url, headers={"X-Workspace-Id": ws["id"]}, params={"q": "FASTA"})
    validate_response("listWorkspaceTechnologies", r)
    assert [(i["name"], i["source_count"]) for i in r.json()["items"]] == [("fastapi", 1)]
    none = await user.client.get(url, headers={"X-Workspace-Id": ws["id"]}, params={"q": "%_"})
    assert none.json()["items"] == []

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await user.client.get(url, headers={"X-Workspace-Id": ws["id"]}, params=params)
        validate_response("listWorkspaceTechnologies", page)
        seen += [i["technology_version_id"] for i in page.json()["items"]]
        cursor = page.json()["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == len(items)

    viewer = await login(app, app_engine)
    stranger = await login(app, app_engine)
    try:
        await _member(user, ws["id"], viewer, "viewer")
        assert (await viewer.client.get(url, headers={"X-Workspace-Id": ws["id"]})).status_code == 200
        r = await stranger.client.get(url, headers={"X-Workspace-Id": ws["id"]})
        validate_response("listWorkspaceTechnologies", r)
        assert r.status_code == 404
        r = await user.client.get(url)
        validate_response("listWorkspaceTechnologies", r)
        assert r.status_code == 422  # нет X-Workspace-Id
        await _fuzz(user, "listWorkspaceTechnologies", {}, ws["id"])
    finally:
        await viewer.client.aclose()
        await stranger.client.aclose()


# --- Probe провайдера и его статус (OQ-45 (c), API_DESIGN §15) -------------------------------------------


async def _provider_id(user: UserSession, ws: str, code: str = "claude") -> str:
    r = await user.client.get("/api/v1/providers", headers={"X-Workspace-Id": ws})
    return str(next(p["provider_id"] for p in r.json()["items"] if p["code"] == code))


def _probe_runtime(worker_engine: AsyncEngine, host_id: uuid.UUID, tmp_path: Path, *, fail: bool = False) -> Any:
    async def prober(provider: str) -> ProbeResult:
        if fail:
            raise RuntimeError("probe crashed")
        adapter = FakeCLIExecutor(
            provider="claude" if provider == "claude" else "codex",
            runner=LocalProcessRunner(),
            artifact_sink=MemorySink(),
            worktree_path=worktree_resolver(tmp_path),
            auth_status="AUTHENTICATED",
        )
        return await adapter.probe(installation_id=uuid7())

    return runtime.Runtime(
        engine=worker_engine,
        orchestrator=None,  # type: ignore[arg-type]
        loop=asyncio.get_running_loop(),
        dispatch_execution=None,  # type: ignore[arg-type]
        provider_prober=prober,
        worker_host_id=host_id,
    )


async def _outbox_event(app_engine: AsyncEngine, worker_engine: AsyncEngine, request_id: str) -> dict[str, Any]:
    """Сообщение outbox `provider.probe_requested` так, как его получает актор exec (через dispatch_batch)."""
    del app_engine
    async with transaction(worker_engine, DbContext(actor_type="system")) as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, workspace_id, aggregate_id, event_type, payload FROM outbox_events"
                        " WHERE event_type = 'provider.probe_requested' AND payload ->> 'probe_request_id' = :r"
                    ),
                    {"r": request_id},
                )
            )
            .mappings()
            .one()
        )
    return {
        "event_id": str(row["id"]),
        "event_type": row["event_type"],
        "workspace_id": str(row["workspace_id"]),
        "aggregate_id": str(row["aggregate_id"]),
        "payload": dict(row["payload"]),
    }


async def test_probe_request_status_lifecycle(
    app: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, user: UserSession, tmp_path: Path
) -> None:
    ws = await user.create_workspace()
    provider = await _provider_id(user, ws["id"])
    r = await user.client.post(f"/api/v1/providers/{provider}/probe", headers=user.headers(ws["id"], idem=True))
    validate_response("probeProvider", r)
    assert r.status_code == 202, r.text
    accepted = r.json()
    request_id = accepted["probe_request_id"]
    assert accepted["status_url"] == f"/api/v1/providers/{provider}/probes/{request_id}"

    status = await user.client.get(accepted["status_url"], headers={"X-Workspace-Id": ws["id"]})
    validate_response("getProviderProbe", status)
    assert status.status_code == 200
    assert status.json()["status"] == "PENDING" and status.json()["installation"] is None

    async with transaction(worker_engine, DbContext(actor_type="system")) as conn:
        host = await registry.register_host(conn, hostname=f"exec-{secrets.token_hex(3)}")
    event = await _outbox_event(app_engine, worker_engine, request_id)
    rt = _probe_runtime(worker_engine, host, tmp_path)
    assert await handlers.probe_provider(rt, event) == "provider_probed"
    assert await handlers.probe_provider(rt, event) == "duplicate"  # повторная доставка — no-op

    status = await user.client.get(accepted["status_url"], headers={"X-Workspace-Id": ws["id"]})
    validate_response("getProviderProbe", status)
    done = status.json()
    assert done["status"] == "COMPLETED" and done["completed_at"] and done["error_category"] is None
    assert (
        done["installation"]["auth_status"] == "AUTHENTICATED" and done["installation"]["compatibility"] == "UNTESTED"
    )
    for forbidden in ("binary_path", "/usr/", "credential", 'token"'):
        assert forbidden not in status.text

    # Сбой проверки ⇒ FAILED с категорией ошибки; без ответа воркера дольше 15 мин ⇒ EXPIRED.
    r = await user.client.post(f"/api/v1/providers/{provider}/probe", headers=user.headers(ws["id"], idem=True))
    failing = r.json()["probe_request_id"]
    event = await _outbox_event(app_engine, worker_engine, failing)
    assert await handlers.probe_provider(_probe_runtime(worker_engine, host, tmp_path, fail=True), event) == (
        "provider_probed"
    )
    status = await user.client.get(
        f"/api/v1/providers/{provider}/probes/{failing}", headers={"X-Workspace-Id": ws["id"]}
    )
    validate_response("getProviderProbe", status)
    assert status.json()["status"] == "FAILED" and status.json()["error_category"] == "PROCESS_FAILED"
    r = await user.client.post(f"/api/v1/providers/{provider}/probe", headers=user.headers(ws["id"], idem=True))
    stale = uuid.UUID(r.json()["probe_request_id"])
    async with transaction(
        app_engine, DbContext(user_id=uuid.UUID(user.user_id), workspace_id=uuid.UUID(ws["id"]))
    ) as c:
        later = await registry.probe_status(
            c, provider_id=uuid.UUID(provider), probe_request_id=stale, now=utcnow() + timedelta(minutes=16)
        )
    assert later is not None and later["status"] == "EXPIRED"

    # Запрос чужого провайдера/workspace и несуществующий — 404.
    other = await _provider_id(user, ws["id"], "codex")
    r = await user.client.get(f"/api/v1/providers/{other}/probes/{request_id}", headers={"X-Workspace-Id": ws["id"]})
    validate_response("getProviderProbe", r)
    assert r.status_code == 404
    ws2 = await user.create_workspace()
    r = await user.client.get(accepted["status_url"], headers={"X-Workspace-Id": ws2["id"]})
    assert r.status_code == 404
    r = await user.client.post(f"/api/v1/providers/{uuid7()}/probe", headers=user.headers(ws["id"], idem=True))
    validate_response("probeProvider", r)
    assert r.status_code == 404


async def test_probe_rbac_rate_limit_and_audit(app: FastAPI, app_engine: AsyncEngine, user: UserSession) -> None:
    ws = await user.create_workspace()
    provider = await _provider_id(user, ws["id"], "codex")
    developer = await login(app, app_engine)
    viewer = await login(app, app_engine)
    stranger = await login(app, app_engine)
    try:
        await _member(user, ws["id"], developer, "developer")
        await _member(user, ws["id"], viewer, "viewer")
        r = await developer.client.post(
            f"/api/v1/providers/{provider}/probe", headers=developer.headers(ws["id"], idem=True)
        )
        validate_response("probeProvider", r)
        assert r.status_code == 403
        r = await stranger.client.post(
            f"/api/v1/providers/{provider}/probe", headers=stranger.headers(ws["id"], idem=True)
        )
        assert r.status_code == 404
        r = await user.client.post(f"/api/v1/providers/{provider}/probe", headers=user.headers(ws["id"]))
        validate_response("probeProvider", r)
        assert r.status_code == 400  # без Idempotency-Key
        key = "probe-" + secrets.token_hex(4)
        first = await user.client.post(
            f"/api/v1/providers/{provider}/probe", headers=user.headers(ws["id"], idem_key=key)
        )
        replay = await user.client.post(
            f"/api/v1/providers/{provider}/probe", headers=user.headers(ws["id"], idem_key=key)
        )
        assert first.status_code == replay.status_code == 202
        assert replay.json() == first.json() and replay.headers.get("idempotency-replayed") == "true"
        # viewer читает статус, но не запускает проверку.
        r = await viewer.client.get(first.json()["status_url"], headers={"X-Workspace-Id": ws["id"]})
        validate_response("getProviderProbe", r)
        assert r.status_code == 200
        # Лимит 6/час на workspace+провайдера (developer, повтор и первый запрос уже израсходовали 3).
        codes = []
        for _ in range(6):
            r = await user.client.post(f"/api/v1/providers/{provider}/probe", headers=user.headers(ws["id"], idem=True))
            codes.append(r.status_code)
        assert codes[-1] == 429 and 202 in codes
        validate_response("probeProvider", r)
        async with transaction(
            app_engine, DbContext(user_id=uuid.UUID(user.user_id), workspace_id=uuid.UUID(ws["id"]))
        ) as conn:
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM audit_events WHERE action = 'provider.probe_requested' AND resource_id = :p"
                    ),
                    {"p": provider},
                )
            ).scalar_one()
        assert n == codes.count(202) + 1
        await _fuzz(
            user,
            "getProviderProbe",
            {"provider_id": provider, "probe_request_id": first.json()["probe_request_id"]},
            ws["id"],
        )
    finally:
        for s in (developer, viewer, stranger):
            await s.client.aclose()
