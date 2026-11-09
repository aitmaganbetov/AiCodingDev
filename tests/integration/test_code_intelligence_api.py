"""B3 Code Intelligence read API (UI_REDESIGN_PLAN §3.6, G-06…G-09) на реальном PostgreSQL и индексе git-репозитория:
поиск символов, граф зависимостей, очередь индексации, manifest Context Pack попытки; контракт, RBAC, изоляция
workspace (404), отсутствие текста фрагментов и путей хоста в ответах, Schemathesis-фазз (нет 5xx).
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_code_intelligence.context.retrieval import RetrievalRequest, build_context_pack
from aicd_code_intelligence.context.store import manifest_view, record_retrieval, store_context_pack
from aicd_code_intelligence.index import queries
from aicd_code_intelligence.index.jobs import run_indexing_job
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from tests.conftest import UserSession, login
from tests.integration.test_code_index_db import FILES, commit, init_repo
from tests.integration.test_oq45_api import _fuzz, _member
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.contract import validate_response

pytestmark = pytest.mark.db

REPO_FILES = {**FILES, "src/shop/util.py": "import json\n\n\ndef dump(x: object) -> str:\n    return json.dumps(x)\n"}


@dataclass
class Indexed:
    ws: str
    project: str
    repo: str
    base: str
    sha: str
    tenant: orch.Tenant
    repo_dir: Path
    user: UserSession

    @property
    def repo_base(self) -> str:
        return f"/api/v1/projects/{self.project}/repositories/{self.repo}"


async def _attach(user: UserSession, repo_root: Path, files: dict[str, str] | None) -> tuple[Indexed, str | None]:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo_dir = repo_root / f"ci-{secrets.token_hex(4)}"
    init_repo(repo_dir)
    sha = commit(repo_dir, files, "init") if files else None
    r = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"provider": "local", "local_path": str(repo_dir)},
        headers=user.headers(idem=True),
    )
    assert r.status_code == 201, r.text
    tenant = orch.Tenant(
        uuid.UUID(ws["id"]), uuid.UUID(user.user_id), uuid.UUID(project["id"]), uuid.UUID(r.json()["id"])
    )
    base = f"/api/v1/projects/{project['id']}/repositories/{r.json()['id']}"
    return Indexed(ws["id"], project["id"], r.json()["id"], base, sha or "", tenant, repo_dir, user), sha


async def _index_all(user: UserSession, ix: Indexed, engine: AsyncEngine, settings: Settings) -> None:
    jobs = (await user.client.get(f"{ix.repo_base}/indexing-jobs", params={"status": "QUEUED"})).json()["items"]
    assert jobs, "attachRepository ставит задание индексации"
    for job in jobs:
        result = await run_indexing_job(
            engine, ix.tenant.workspace_id, uuid.UUID(job["id"]), repository_roots=settings.repository_roots
        )
        assert result.status == "SUCCEEDED", result.error


@pytest.fixture
async def indexed(user: UserSession, repo_root: Path, worker_engine: AsyncEngine, settings: Settings) -> Indexed:
    ix, _ = await _attach(user, repo_root, REPO_FILES)
    await _index_all(user, ix, worker_engine, settings)
    return ix


# --- Очередь индексации (G-09) ---------------------------------------------------------------------------


async def test_indexing_jobs_queue_then_success_with_timings(
    user: UserSession, repo_root: Path, worker_engine: AsyncEngine, settings: Settings
) -> None:
    ix, sha = await _attach(user, repo_root, REPO_FILES)
    r = await user.client.get(f"{ix.repo_base}/indexing-jobs")
    validate_response("listIndexingJobs", r)
    assert r.status_code == 200, r.text
    (queued,) = r.json()["items"]
    assert queued["status"] == "QUEUED" and queued["commit_sha"] == sha and queued["kind"] == "full"
    assert queued["started_at"] is None and queued["queue_seconds"] is None and queued["duration_seconds"] is None
    assert queued["snapshot_id"] is None and queued["file_count"] is None and queued["error"] is None

    await _index_all(user, ix, worker_engine, settings)
    r = await user.client.get(f"{ix.repo_base}/indexing-jobs")
    validate_response("listIndexingJobs", r)
    (done,) = r.json()["items"]
    assert done["id"] == queued["id"] and done["status"] == "SUCCEEDED" and done["attempt_count"] == 1
    assert done["snapshot_id"] and done["file_count"] and done["file_count"] > 5 and done["symbol_count"]
    assert done["queue_seconds"] is not None and done["duration_seconds"] is not None
    r = await user.client.get(f"{ix.repo_base}/indexing-jobs", params={"status": "FAILED"})
    assert r.status_code == 200 and r.json()["items"] == []


async def test_indexing_job_error_is_redacted_and_paginated(
    user: UserSession, repo_root: Path, database: testdb.TestDatabase
) -> None:
    ix, _sha = await _attach(user, repo_root, REPO_FILES)
    with orch.connect_admin(database) as admin:
        for n in range(3):
            jid = uuid7()
            admin.execute(
                "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha, kind) VALUES (%s, %s, %s, %s,"
                " 'full')",
                (jid, ix.tenant.workspace_id, ix.tenant.repository_id, f"{n:x}" * 40),
            )
            admin.execute(
                "UPDATE indexing_jobs SET status = 'FAILED', finished_at = now(), error = %s WHERE id = %s",
                ("git failed at /srv/aicd/repos/secret-team/x.git: token=ghp_" + "a" * 30, jid),
            )
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2, "status": "FAILED"}
        if cursor:
            params["cursor"] = cursor
        r = await user.client.get(f"{ix.repo_base}/indexing-jobs", params=params)
        validate_response("listIndexingJobs", r)
        for job in r.json()["items"]:
            assert "/srv/" not in job["error"] and "ghp_" not in job["error"], job["error"]
            assert "[path]" in job["error"] and "[REDACTED]" in job["error"]
            seen.append(job["id"])
        cursor = r.json()["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 3
    bad = await user.client.get(f"{ix.repo_base}/indexing-jobs", params={"cursor": cursor or "x.y", "status": "QUEUED"})
    assert bad.status_code == 422


# --- Поиск символов (G-06) -------------------------------------------------------------------------------


async def test_symbol_search_ranks_exact_then_partial(indexed: Indexed) -> None:
    ix = indexed
    r = await ix_get(ix, "symbols", q="ShoppingCart")
    validate_response("listRepositorySymbols", r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshot"]["commit_sha"] == ix.sha and body["snapshot"]["status"] == "READY"
    top = body["items"][0]
    assert (top["name"], top["kind"], top["path"], top["score"], top["match"]) == (
        "ShoppingCart",
        "class",
        "src/shop/cart.py",
        1.0,
        "name",
    )
    assert top["start_line"] >= 1 and top["end_line"] >= top["start_line"]
    scores = [i["score"] for i in body["items"]]
    assert scores == sorted(scores, reverse=True)

    r = await ix_get(ix, "symbols", q="checkout")
    names = {i["name"] for i in r.json()["items"]}
    assert {"checkoutOrder", "test_checkout"} <= names
    r = await ix_get(ix, "symbols", q="checkout", kind="method")
    validate_response("listRepositorySymbols", r)
    assert [i["name"] for i in r.json()["items"]] == ["checkoutOrder"]
    r = await ix_get(ix, "symbols", q="checkout", path="tests")
    assert {i["path"] for i in r.json()["items"]} == {"tests/test_cart.py"}
    # FTS по фрагментам: слово из docstring символа
    r = await ix_get(ix, "symbols", q="Корзина")
    assert any(i["name"] == "ShoppingCart" and i["match"] == "text" for i in r.json()["items"]), r.json()
    # Wildcard LIKE — литерал, а не шаблон
    r = await ix_get(ix, "symbols", q="%")
    assert r.status_code == 200 and r.json()["items"] == []


async def ix_get(ix: Indexed, what: str, **params: Any) -> Any:
    return await ix.user.client.get(f"{ix.repo_base}/{what}", params=params)


async def test_symbol_search_pagination_commit_prefix_and_errors(indexed: Indexed) -> None:
    ix = indexed
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        params: dict[str, Any] = {"q": "t", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = await ix_get(ix, "symbols", **params)
        validate_response("listRepositorySymbols", r)
        seen += [i["id"] for i in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) > 2
    assert (await ix_get(ix, "symbols", q="x", cursor=cursor or "bad.cursor")).status_code == 422

    r = await ix_get(ix, "symbols", q="ShoppingCart", commit=ix.sha[:8])
    validate_response("listRepositorySymbols", r)
    assert r.status_code == 200 and r.json()["snapshot"]["commit_sha"] == ix.sha
    r = await ix_get(ix, "symbols", q="ShoppingCart", commit="f" * 40 if not ix.sha.startswith("f") else "e" * 40)
    validate_response("listRepositorySymbols", r)
    assert r.status_code == 404
    for params in ({"q": "x", "commit": "HEAD"}, {"q": "x", "path": "../etc"}, {"q": "a\nb"}, {"q": ""}):
        r = await ix_get(ix, "symbols", **params)
        validate_response("listRepositorySymbols", r)
        assert r.status_code == 422, (params, r.text)


async def test_unindexed_repository_reports_no_snapshot(user: UserSession, repo_root: Path) -> None:
    ix, _ = await _attach(user, repo_root, {"a.py": "def f() -> None:\n    pass\n"})
    r = await ix_get(ix, "symbols", q="f")
    validate_response("listRepositorySymbols", r)
    assert r.status_code == 200 and r.json()["snapshot"] is None and r.json()["items"] == []
    r = await ix_get(ix, "dependencies", path="a.py")
    validate_response("getRepositoryDependencies", r)
    assert r.json()["snapshot"] is None and r.json()["nodes"] == [] and r.json()["truncated"] is False


# --- Граф зависимостей (G-07) ----------------------------------------------------------------------------


async def test_dependency_graph_directions_depth_and_externals(indexed: Indexed) -> None:
    ix = indexed
    r = await ix_get(ix, "dependencies", path="src/shop/cart.py", direction="out")
    validate_response("getRepositoryDependencies", r)
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["roots"] == ["src/shop/cart.py"] and g["truncated"] is False
    assert {"source": "src/shop/cart.py", "target": "src/shop/pricing.py", "edge_type": "imports"} in g["edges"]
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes["src/shop/cart.py"]["depth"] == 0 and nodes["src/shop/pricing.py"]["depth"] == 1
    assert nodes["src/shop/pricing.py"]["language"] == "python" and nodes["src/shop/pricing.py"]["kind"] == "file"

    r = await ix_get(ix, "dependencies", path="src/shop/cart.py", direction="in")
    g = r.json()
    assert {e["source"] for e in g["edges"]} == {"tests/test_cart.py"}
    assert {e["edge_type"] for e in g["edges"]} >= {"tests"}
    assert {n["id"]: n for n in g["nodes"]}["tests/test_cart.py"]["is_test"] is True

    # depth=2 из теста доходит до pricing через cart
    r = await ix_get(ix, "dependencies", path="tests/test_cart.py", direction="out", depth=2)
    nodes = {n["id"]: n["depth"] for n in r.json()["nodes"]}
    assert nodes.get("src/shop/cart.py") == 1 and nodes.get("src/shop/pricing.py") == 2
    r = await ix_get(ix, "dependencies", path="tests/test_cart.py", direction="out", depth=1)
    assert "src/shop/pricing.py" not in {n["id"] for n in r.json()["nodes"]}

    r = await ix_get(ix, "dependencies", path="src/shop/util.py", direction="out")
    g = r.json()
    ext = [n for n in g["nodes"] if n["kind"] == "external"]
    assert [(n["id"], n["ref"], n["path"]) for n in ext] == [("external:json", "json", None)]
    assert {"source": "src/shop/util.py", "target": "external:json", "edge_type": "imports"} in g["edges"]

    # каталог как корень; символ как корень
    r = await ix_get(ix, "dependencies", path="web")
    assert r.json()["roots"] == ["web/api.ts", "web/http.ts"]
    assert {"source": "web/api.ts", "target": "web/http.ts", "edge_type": "imports"} in r.json()["edges"]
    sym = (await ix_get(ix, "symbols", q="total_price")).json()["items"][0]
    r = await ix_get(ix, "dependencies", symbol=sym["id"])
    validate_response("getRepositoryDependencies", r)
    assert r.json()["roots"] == ["src/shop/pricing.py"]


async def test_dependency_graph_validation_and_bounds(indexed: Indexed, app_engine: AsyncEngine) -> None:
    ix = indexed
    for params in (
        {},
        {"path": "a.py", "symbol": str(uuid7())},
        {"path": "x", "depth": 4},
        {"path": "x", "direction": "up"},
    ):
        r = await ix_get(ix, "dependencies", **params)
        validate_response("getRepositoryDependencies", r)
        assert r.status_code == 422, (params, r.text)
    for params in ({"path": "nope/missing.py"}, {"symbol": str(uuid7())}):
        r = await ix_get(ix, "dependencies", **params)
        validate_response("getRepositoryDependencies", r)
        assert r.status_code == 404, (params, r.text)
    # Пределы узлов/рёбер: граф помечается truncated, а не обрезается молча.
    snap = uuid.UUID((await ix_get(ix, "symbols", q="x")).json()["snapshot"]["id"])
    async with transaction(app_engine, DbContext(user_id=ix.tenant.owner_id, workspace_id=ix.tenant.workspace_id)) as c:
        g = await queries.dependency_graph(c, snapshot_id=snap, path="src", direction="both", max_nodes=2)
        assert g.truncated and len(g.nodes) == 2
        g = await queries.dependency_graph(c, snapshot_id=snap, path="src", direction="both", max_edges=1)
        assert g.truncated and len(g.edges) == 1


# --- Context Pack попытки (G-08) -------------------------------------------------------------------------


async def _attempt_with_pack(
    ix: Indexed, database: testdb.TestDatabase, engine: AsyncEngine, settings: Settings, tmp: Path
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    with orch.connect_admin(database) as admin:
        run = orch.new_run(admin, ix.tenant)
        task = orch.new_task(admin, ix.tenant, run)
        orch.lease_task(admin, task)
        attempt = orch.new_attempt(admin, ix.tenant, task)
    req = RetrievalRequest(
        workspace_id=ix.tenant.workspace_id,
        project_id=ix.tenant.project_id,
        repository_id=ix.tenant.repository_id,
        base_commit=ix.sha,
        role="developer",
        task_text="Исправить `checkoutOrder`: итоговая сумма корзины должна учитывать скидку",
    )
    store = FileSystemArtifactStore(tmp / "art")
    pack = await build_context_pack(
        engine, req, repository_roots=settings.repository_roots, export_cache_root=tmp / "export", artifacts=store
    )
    async with transaction(engine, DbContext(workspace_id=ix.tenant.workspace_id, actor_type="system")) as conn:
        await record_retrieval(conn, pack, run_id=run, task_id=task, task_attempt_id=attempt)
        stored = await store_context_pack(conn, store, pack, task_attempt_id=attempt, run_id=run)
    assert stored is not None
    return task, attempt, pack.manifest


async def test_context_pack_manifest_without_fragment_text(
    indexed: Indexed, database: testdb.TestDatabase, worker_engine: AsyncEngine, settings: Settings, tmp_path: Path
) -> None:
    ix = indexed
    task, attempt, manifest = await _attempt_with_pack(ix, database, worker_engine, settings, tmp_path)
    r = await ix.user.client.get(f"/api/v1/tasks/{task}/attempts/{attempt}/context-pack")
    validate_response("getAttemptContextPack", r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == str(task) and body["attempt_id"] == str(attempt)
    assert body["base_commit"] == ix.sha and body["snapshot_commit"] == ix.sha
    assert body["fragments"] and body["fragments_total"] == len(manifest["fragments"])
    assert body["size_bytes"] == manifest["size_bytes"] and body["cache_hit"] is False
    paths = {f["path"] for f in body["fragments"]}
    assert "src/shop/cart.py" in paths
    for f in body["fragments"]:
        assert set(f) == {"section", "kind", "path", "lines", "commit", "sha256", "bytes", "stale", "source_ref"}
        assert f["stale"] is False and f["commit"] == ix.sha
    # Текст фрагментов и термины задачи не отдаются; пути — относительные пути снимка.
    raw = r.text
    assert "def checkoutOrder" not in raw and '"text"' not in raw and '"terms"' not in raw
    assert str(ix.repo_dir) not in raw

    other_task, other_attempt = uuid7(), uuid7()
    for url in (
        f"/api/v1/tasks/{task}/attempts/{other_attempt}/context-pack",
        f"/api/v1/tasks/{other_task}/attempts/{attempt}/context-pack",
    ):
        nf = await ix.user.client.get(url)
        validate_response("getAttemptContextPack", nf)
        assert nf.status_code == 404
    with orch.connect_admin(database) as admin:
        run = orch.new_run(admin, ix.tenant)
        task2 = orch.new_task(admin, ix.tenant, run)
        orch.lease_task(admin, task2)
        no_pack = orch.new_attempt(admin, ix.tenant, task2)
    r = await ix.user.client.get(f"/api/v1/tasks/{task2}/attempts/{no_pack}/context-pack")
    validate_response("getAttemptContextPack", r)
    assert r.status_code == 404
    # Попытка другой задачи того же workspace — 404 (атрибут попытки проверяется по задаче пути).
    r = await ix.user.client.get(f"/api/v1/tasks/{task2}/attempts/{attempt}/context-pack")
    assert r.status_code == 404


def test_manifest_view_keeps_only_safe_fields() -> None:
    sha = "a" * 40
    view = manifest_view(
        {
            "schema": 1,
            "stale_policy": "mark",
            "fragments": [
                {"section": "code", "kind": "exact", "path": "a.py", "lines": [1, 2], "commit": sha, "sha256": "b" * 64,
                 "bytes": 10, "stale": True, "source_ref": "x", "text": "SECRET BODY"},
                {"section": "bogus", "kind": "exact", "path": "b.py"},
                "garbage",
            ],
            "omitted": [{"section": "code", "kind": "recency", "path": "c.py", "commit": "zz", "reason": "max_files"}],
            "warnings": ["git_read_failed:/srv/host/path boom", "git_read_failed:other", "bad warning!"],
            "terms": ["secret-term"],
            "cache": {"hit": True},
        }
    )  # fmt: skip
    assert view["fragments"] == [
        {"section": "code", "kind": "exact", "path": "a.py", "lines": [1, 2], "commit": sha, "sha256": "b" * 64,
         "bytes": 10, "stale": True, "source_ref": "x"}
    ]  # fmt: skip
    assert view["omitted"] == [
        {"section": "code", "kind": "recency", "path": "c.py", "commit": None, "reason": "max_files"}
    ]
    assert view["warnings"] == ["git_read_failed"] and view["cache_hit"] is True and view["fragments_total"] == 3
    assert "terms" not in view and "SECRET" not in repr(view) and "/srv/" not in repr(view)


# --- Изоляция, RBAC, фазз --------------------------------------------------------------------------------


async def test_cross_workspace_404_and_viewer_access(
    indexed: Indexed, app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase,
    worker_engine: AsyncEngine, settings: Settings, tmp_path: Path,
) -> None:  # fmt: skip
    ix = indexed
    task, attempt, _ = await _attempt_with_pack(ix, database, worker_engine, settings, tmp_path)
    urls = [
        f"{ix.repo_base}/symbols?q=Cart",
        f"{ix.repo_base}/dependencies?path=src",
        f"{ix.repo_base}/indexing-jobs",
        f"/api/v1/tasks/{task}/attempts/{attempt}/context-pack",
    ]
    stranger = await login(app, app_engine)
    viewer = await login(app, app_engine)
    try:
        await stranger.create_workspace()
        await _member(ix.user, ix.ws, viewer, "viewer")
        for url in urls:
            r = await stranger.client.get(url)
            assert r.status_code == 404, (url, r.text)
            assert r.json()["code"] == "not_found"
            r = await stranger.client.get(url, headers={"X-Workspace-Id": ix.ws})
            assert r.status_code == 404, url
            r = await viewer.client.get(url)
            assert r.status_code == 200, (url, r.text)
        # Репозиторий не этого проекта — 404 (проект свой, id репозитория чужой).
        other = await viewer.create_workspace()
        own_project = await viewer.create_project(other["id"])
        r = await viewer.client.get(f"/api/v1/projects/{own_project['id']}/repositories/{ix.repo}/symbols?q=a")
        assert r.status_code == 404
    finally:
        await stranger.client.aclose()
        await viewer.client.aclose()


async def test_code_intelligence_fuzz_no_5xx(
    indexed: Indexed, database: testdb.TestDatabase, worker_engine: AsyncEngine, settings: Settings, tmp_path: Path
) -> None:
    ix = indexed
    task, attempt, _ = await _attempt_with_pack(ix, database, worker_engine, settings, tmp_path)
    real = {"project_id": ix.project, "repository_id": ix.repo, "task_id": str(task), "attempt_id": str(attempt)}
    for op_id in ("listRepositorySymbols", "getRepositoryDependencies", "listIndexingJobs", "getAttemptContextPack"):
        await _fuzz(ix.user, op_id, real, None)
