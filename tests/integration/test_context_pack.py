"""P5-06/P5-11 на PostgreSQL (миграция 0007), роль `aicd_worker`: retrieval и Context Pack, stale-политика,
кэш artifact-а, индекс перед исполнением, воркер индексации (+ определение стека), исполнитель на Fake CLI.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.models import ExecutionRequest
from aicd_code_intelligence.context import snapshot as snapshot_mod
from aicd_code_intelligence.context.ranking import Budget
from aicd_code_intelligence.context.retrieval import (
    ContextPack,
    RetrievalRequest,
    build_context_pack,
    extract_terms,
)
from aicd_code_intelligence.context.snapshot import ensure_snapshot
from aicd_code_intelligence.context.store import record_retrieval, store_context_pack
from aicd_code_intelligence.index.jobs import enqueue_indexing_job, run_indexing_job
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import DbContext, create_engine, transaction
from aicd_platform.ids import uuid7
from aicd_security.paths import resolve_ref
from aicd_worker.indexing import index_once
from aicd_worker.settings import WorkerSettings
from tests.integration.test_code_index_db import FILES, commit, init_repo
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.phase3 import Slice, build_executor, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

REPO_FILES = {
    **FILES,
    "pyproject.toml": '[project]\nname = "shop"\nversion = "0.1.0"\ndependencies = ["fastapi>=0.110"]\n',
    "package.json": '{"name": "web", "dependencies": {"react": "18.3.1"}}\n',
}
TASK_TEXT = "Исправить `checkoutOrder`: итоговая сумма корзины должна учитывать скидку"


@dataclass
class Env:
    database: testdb.TestDatabase
    engine: AsyncEngine
    tenant: orch.Tenant
    roots: dict[str, Path]
    repo: Path
    tmp: Path
    store: FileSystemArtifactStore

    @property
    def ctx(self) -> DbContext:
        return DbContext(workspace_id=self.tenant.workspace_id, actor_type="system")

    async def index(self, sha: str) -> uuid.UUID:
        async with transaction(self.engine, self.ctx) as conn:
            job, _ = await enqueue_indexing_job(
                conn, workspace_id=self.tenant.workspace_id, repository_id=self.tenant.repository_id, commit_sha=sha
            )
        result = await run_indexing_job(self.engine, self.tenant.workspace_id, job, repository_roots=self.roots)
        assert result.status == "SUCCEEDED" and result.snapshot_id is not None, result.error
        return result.snapshot_id

    def attempt(self) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        with orch.connect_admin(self.database) as conn:
            run = orch.new_run(conn, self.tenant)
            task = orch.new_task(conn, self.tenant, run)
            orch.lease_task(conn, task)
            attempt = orch.new_attempt(conn, self.tenant, task)
        return run, task, attempt

    def request(self, base: str, **kw: Any) -> RetrievalRequest:
        return RetrievalRequest(
            workspace_id=self.tenant.workspace_id,
            project_id=self.tenant.project_id,
            repository_id=self.tenant.repository_id,
            base_commit=base,
            role=kw.pop("role", "developer"),
            task_text=kw.pop("task_text", TASK_TEXT),
            **kw,
        )

    async def build(self, base: str, **kw: Any) -> ContextPack:
        return await build_context_pack(
            self.engine,
            self.request(base, **kw),
            repository_roots=self.roots,
            export_cache_root=self.tmp / "export",
            artifacts=self.store,
        )

    async def persist(self, pack: ContextPack) -> tuple[uuid.UUID, Any]:
        run, task, attempt = self.attempt()
        async with transaction(self.engine, self.ctx) as conn:
            await record_retrieval(conn, pack, run_id=run, task_id=task, task_attempt_id=attempt)
            stored = await store_context_pack(conn, self.store, pack, task_attempt_id=attempt, run_id=run)
        return attempt, stored

    def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        with orch.connect_admin(self.database) as conn:
            return list(conn.execute(sql, params).fetchall())


@pytest.fixture
async def env(database: testdb.TestDatabase, tmp_path: Path) -> AsyncIterator[Env]:
    root = (tmp_path / "roots").resolve()
    root.mkdir()
    with orch.connect_admin(database) as conn:
        tenant = orch.seed_tenant(conn)
    repo = root / tenant.repository_id.hex
    init_repo(repo)
    engine = create_engine(database.url_for("aicd_worker"))
    yield Env(database, engine, tenant, {"main": root}, repo, tmp_path, FileSystemArtifactStore(tmp_path / "art"))
    await engine.dispose()


def test_extract_terms() -> None:
    terms = extract_terms(['Fix `total_price` in src/shop/cart.py and ShoppingCart.checkoutOrder; "exact phrase" here'])
    assert terms[:2] == ["total_price", "exact phrase"]
    assert "src/shop/cart.py" in terms and "ShoppingCart.checkoutOrder" in terms
    assert extract_terms(["Добавить документ про корзину"]) == []


async def test_pack_exact_dependency_tests_in_rank_order_within_budget(env: Env) -> None:
    sha = commit(env.repo, REPO_FILES, "init")
    await env.index(sha)
    pack = await env.build(sha)
    frags = pack.manifest["fragments"]
    kinds = [f["kind"] for f in frags]
    assert kinds[0] == "map" and frags[0]["section"] == "repo_map"
    order = {"map": -1, "exact": 0, "dependency": 1, "task_relevance": 2, "recency": 3, "docs": 4}
    assert [order[k] for k in kinds] == sorted(order[k] for k in kinds), "ранжирование exact > dependency > …"
    by = {(f["kind"], f["section"], f["path"]) for f in frags}
    assert ("exact", "code", "src/shop/cart.py") in by
    assert ("dependency", "code", "src/shop/pricing.py") in by  # импорт файла-фокуса
    assert ("dependency", "tests", "tests/test_cart.py") in by  # test link
    assert ("dependency", "symbols_graph", "src/shop/cart.py") in by
    exact = next(f for f in pack.fragments if f.kind == "exact")
    assert "def checkoutOrder" in exact.text and exact.commit_sha == sha
    assert all(not f["stale"] for f in frags) and pack.manifest["omitted"] == []
    budget = pack.request.effective_budget
    assert 0 < pack.selection.size_bytes <= budget.max_bytes
    assert pack.manifest["docs"] == "allowed" and "checkoutOrder" in pack.manifest["terms"]

    attempt, stored = await env.persist(pack)
    assert stored is not None and not stored.cache_hit
    row = env.rows(
        "SELECT snapshot_id, source_commit, policy_version, size_bytes, content_hash, artifact_id, manifest"
        " FROM context_packs WHERE task_attempt_id = %s",
        attempt,
    )[0]
    assert row[1] == sha and row[3] == pack.selection.size_bytes and row[4] == pack.content_hash
    assert row[5] == stored.artifact_id and row[6]["cache"] == {"hit": False}
    body = json.loads(
        b"".join(env.store.open(env.rows("SELECT storage_ref FROM artifacts WHERE id = %s", row[5])[0][0]))
    )
    assert [f["path"] for f in body["fragments"]] == [f.path for f in pack.fragments]
    strategies = {
        r[0]: r[1]
        for r in env.rows(
            "SELECT q.strategy, count(r.chunk_id) FROM retrieval_queries q"
            " LEFT JOIN retrieval_results r ON r.query_id = q.id WHERE q.task_attempt_id = %s GROUP BY q.strategy",
            attempt,
        )
    }
    assert {"exact", "symbol", "graph", "docs"} <= set(strategies) and strategies["exact"] >= 1
    assert env.rows(
        "SELECT bool_and(selected), bool_or(is_stale) FROM retrieval_results r JOIN retrieval_queries q"
        " ON q.id = r.query_id WHERE q.task_attempt_id = %s AND q.strategy = 'exact'",
        attempt,
    ) == [(True, False)]

    # Малый бюджет: exact сохраняется, остальное — в omitted с причиной.
    small = await env.build(sha, budget=Budget(max_bytes=700, max_files=2))
    assert small.selection.size_bytes <= 700
    assert any(f.kind == "exact" for f in small.fragments)
    assert {o["reason"] for o in small.selection.omitted} & {"max_bytes", "max_files"}


async def test_fragment_of_other_commit_is_excluded_or_marked_stale(env: Env) -> None:
    """P5-06: снимок есть только для старого commit — его фрагменты исключаются (по умолчанию) или маркируются."""
    old = commit(env.repo, REPO_FILES, "init")
    await env.index(old)
    base = commit(env.repo, {"src/shop/pricing.py": "def total_price(items: list[int]) -> int:\n    return 0\n"})
    excluded = await env.build(base)
    assert excluded.snapshot_commit == old and "snapshot_commit_differs_from_base" in excluded.warnings
    assert excluded.fragments == []
    assert excluded.selection.omitted and {o["reason"] for o in excluded.selection.omitted} == {"stale_commit"}
    assert all(o["commit"] == old for o in excluded.selection.omitted)

    marked = await env.build(base, stale_policy="mark")
    assert marked.fragments and all(f["stale"] for f in marked.manifest["fragments"])
    attempt, stored = await env.persist(marked)
    assert stored is not None
    assert env.rows(
        "SELECT bool_and(r.is_stale), count(*) > 0 FROM retrieval_results r JOIN retrieval_queries q"
        " ON q.id = r.query_id WHERE q.task_attempt_id = %s",
        attempt,
    ) == [(True, True)]


async def test_cache_hit_reuses_artifact(env: Env) -> None:
    sha = commit(env.repo, REPO_FILES, "init")
    await env.index(sha)
    first_attempt, first = await env.persist(await env.build(sha))
    second_attempt, second = await env.persist(await env.build(sha))
    assert first is not None and second is not None
    assert second.cache_hit and second.artifact_id == first.artifact_id and second.sha256 == first.sha256
    assert second.context_pack_id != first.context_pack_id
    (manifest,) = env.rows("SELECT manifest FROM context_packs WHERE task_attempt_id = %s", second_attempt)[0]
    assert manifest["cache"] == {"hit": True, "source_context_pack_id": str(first.context_pack_id)}
    assert env.rows(
        "SELECT count(*) FROM artifacts WHERE kind = 'context_pack' AND workspace_id = %s", env.tenant.workspace_id
    ) == [(1,)]
    # Другая роль (другой бюджет) — другой ключ кэша.
    _, reviewer = await env.persist(await env.build(sha, role="reviewer"))
    assert reviewer is not None and not reviewer.cache_hit
    del first_attempt


async def test_docs_not_queried_in_off_mode(env: Env) -> None:
    sha = commit(env.repo, REPO_FILES, "init")
    await env.index(sha)
    pack = await env.build(sha, documentation_mode="OFF")
    assert pack.manifest["docs"] == "off" and all(q.strategy != "docs" for q in pack.queries)
    attempt, _ = await env.persist(pack)
    assert env.rows(
        "SELECT count(*) FROM retrieval_queries WHERE task_attempt_id = %s AND strategy = 'docs'", attempt
    ) == [(0,)]


async def test_ensure_snapshot_indexes_inline_and_never_raises(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = commit(env.repo, REPO_FILES, "init")
    kw: dict[str, Any] = {
        "workspace_id": env.tenant.workspace_id,
        "repository_id": env.tenant.repository_id,
        "commit_sha": sha,
    }
    first = await ensure_snapshot(env.engine, repository_roots=env.roots, **kw)
    assert first.snapshot_id is not None and first.indexed_now and first.warning is None
    again = await ensure_snapshot(env.engine, repository_roots=env.roots, **kw)
    assert again.snapshot_id == first.snapshot_id and not again.indexed_now
    # стек определён вместе с индексацией
    assert env.rows("SELECT count(*) > 0 FROM project_technologies WHERE project_id = %s", env.tenant.project_id) == [
        (True,)
    ]

    other = commit(env.repo, {"x.py": "X = 1\n"})
    broken = await ensure_snapshot(
        env.engine, repository_roots={"main": env.tmp / "missing"}, **{**kw, "commit_sha": other}
    )
    assert broken.snapshot_id is None and broken.warning and broken.warning.startswith("index_failed")

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("индексатор упал")

    monkeypatch.setattr(snapshot_mod, "run_job_with_stack", boom)
    third = commit(env.repo, {"y.py": "Y = 1\n"})
    failed = await ensure_snapshot(env.engine, repository_roots=env.roots, **{**kw, "commit_sha": third})
    assert failed.snapshot_id is None and failed.warning == "index_failed:RuntimeError"


async def test_index_worker_iteration_processes_queued_job_and_detects_stack(env: Env) -> None:
    sha = commit(env.repo, REPO_FILES, "init")
    async with transaction(env.engine, env.ctx) as conn:
        job, _ = await enqueue_indexing_job(
            conn, workspace_id=env.tenant.workspace_id, repository_id=env.tenant.repository_id, commit_sha=sha
        )
    settings = WorkerSettings(
        environment="test",
        database_url=env.database.url_for("aicd_worker"),
        repository_roots=env.roots,
        index_export_cache_root=env.tmp / "export",
        index_workspace_batch=1000,
    )
    result = await index_once(settings, env.engine)
    mine = [o for o in result.outcomes if o.result.job_id == job]
    assert len(mine) == 1 and mine[0].result.status == "SUCCEEDED", mine and mine[0].result.error
    assert mine[0].technologies and mine[0].stack_error is None
    assert env.rows("SELECT status FROM indexing_jobs WHERE id = %s", job) == [("SUCCEEDED",)]
    names = {
        r[0]
        for r in env.rows(
            "SELECT t.name FROM project_technologies pt JOIN technology_versions v ON v.id = pt.technology_version_id"
            " JOIN technologies t ON t.id = v.technology_id WHERE pt.project_id = %s",
            env.tenant.project_id,
        )
    }
    assert {"fastapi", "react"} <= {n.lower() for n in names}, names
    assert (env.tmp / "export").exists() or result.pruned == []


# --- Исполнитель на Fake CLI (P5-11 «Integration на Fake») ------------------------------------------------


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def _fake_scene(
    world: Slice, database: testdb.TestDatabase
) -> tuple[uuid.UUID, uuid.UUID, str, dict[str, uuid.UUID]]:
    await install_both(world.worker)
    ws, project = uuid.UUID(world.ws), uuid.UUID(world.project)
    with orch.connect_admin(database) as conn:
        repo = conn.execute("SELECT id, local_root_ref FROM repositories WHERE project_id = %s", (project,)).fetchone()
        owner = conn.execute("SELECT created_by FROM workspaces WHERE id = %s", (ws,)).fetchone()
        installs = conn.execute(
            "SELECT p.code, i.id FROM cli_installations i JOIN cli_providers p ON p.id = i.provider_id WHERE i.enabled"
        ).fetchall()
    assert repo is not None and owner is not None
    path = resolve_ref(repo[1], world.settings.repository_roots)
    for name, content in REPO_FILES.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "shop")
    base = _git(path, "rev-parse", "HEAD")
    run = uuid7()
    with orch.connect_admin(database) as conn:
        conn.execute(
            "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
            " quality_profile, quality_profile_id, documentation_mode, base_commit_sha)"
            " VALUES (%s, %s, %s, %s, %s, 'DEVELOP', %s, 'strict', %s, 'OFF', %s)",
            (run, ws, project, repo[0], owner[0], TASK_TEXT, orch.STRICT_PROFILE_ID, base),
        )
        orch.drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
    return ws, run, base, {str(c): i for c, i in installs}


def _task(database: testdb.TestDatabase, ws: uuid.UUID, run: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    task, attempt = uuid7(), uuid7()
    with orch.connect_admin(database) as conn:
        conn.execute(
            "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title, instructions,"
            " complexity, risk_level, acceptance_criteria)"
            " VALUES (%s, %s, %s, 1, %s, 'developer', 'Скидка', %s, 2, 'low', '[\"Тест test_checkout проходит\"]')",
            (task, ws, run, f"dev-{task.hex[-6:]}", TASK_TEXT),
        )
        orch.lease_task(conn, task, token=7)
        conn.execute(
            "INSERT INTO task_attempts (id, workspace_id, task_id, attempt_no, provider, fencing_token)"
            " VALUES (%s, %s, %s, 1, 'codex', 7)",
            (attempt, ws, task),
        )
    return task, attempt


async def _execute(
    world: Slice,
    executor: Any,
    ws: uuid.UUID,
    run: uuid.UUID,
    task: uuid.UUID,
    attempt: uuid.UUID,
    installation: uuid.UUID,
) -> tuple[Any, list[ExecutionRequest]]:
    captured: list[ExecutionRequest] = []
    original = executor.gateway.run

    async def spy(request: ExecutionRequest, *args: Any, **kwargs: Any) -> Any:
        captured.append(request)
        return await original(request, *args, **kwargs)

    object.__setattr__(executor.gateway, "run", spy)
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        task_row = (await conn.execute(text("SELECT * FROM tasks WHERE id = :id"), {"id": task})).mappings().one()
        run_row = (await conn.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run})).mappings().one()
    outcome = await executor.execute(
        task=dict(task_row),
        run=dict(run_row),
        attempt_id=attempt,
        provider="codex",
        installation_id=installation,
        register_cancel=lambda hook: None,
    )
    return outcome, captured


async def test_executor_builds_context_pack_on_fake_cli(world: Slice, database: testdb.TestDatabase) -> None:
    ws, run, base, installs = await _fake_scene(world, database)
    task, attempt = _task(database, ws, run)
    executor = build_executor(world.worker, world.tmp, world.settings.repository_roots, "agent")
    executor.index_export_cache_root = world.tmp / "export"
    outcome, captured = await _execute(world, executor, ws, run, task, attempt, installs["codex"])
    assert outcome.status == "completed" and outcome.agent_io is not None

    (request,) = captured
    assert request.input.context_pack is not None
    with orch.connect_admin(database) as conn:
        row = conn.execute(
            "SELECT id, artifact_id, source_commit, manifest FROM context_packs WHERE task_attempt_id = %s", (attempt,)
        ).fetchone()
        assert row is not None
        art = conn.execute("SELECT sha256, kind, is_sensitive FROM artifacts WHERE id = %s", (row[1],)).fetchone()
        snapshot = conn.execute(
            "SELECT count(*) FROM code_index_snapshots WHERE commit_sha = %s AND status = 'READY'", (base,)
        ).fetchone()
        prompt_event = conn.execute(
            "SELECT payload -> 'context_pack' FROM agent_activity_events WHERE task_id = %s"
            " AND event_type = 'agent.prompt_assembled'",
            (task,),
        ).fetchone()
        prompt_ref = conn.execute(
            "SELECT storage_ref, sha256 FROM artifacts WHERE id = %s", (outcome.agent_io.prompt_artifact_id,)
        ).fetchone()
    assert snapshot == (1,), "индекс построен перед исполнением"
    assert request.input.context_pack.context_pack_id == row[0] == outcome.agent_io.context_pack_id
    assert request.input.context_pack.artifact_ref == f"artifact:{row[1]}"
    assert art is not None and request.input.context_pack.sha256 == art[0] and art[1:] == ("context_pack", True)
    assert row[2] == base and row[3]["prompt_artifact_id"] == str(outcome.agent_io.prompt_artifact_id)
    assert row[3]["docs"] == "off"
    assert prompt_event is not None and prompt_event[0]["context_pack_id"] == str(row[0])
    assert prompt_event[0]["size_bytes"] > 0 and prompt_event[0]["by_section"]["code"] >= 1

    assert prompt_ref is not None
    prompt = b"".join(FileSystemArtifactStore(world.tmp / "artifacts").open(prompt_ref[0])).decode()
    assert "# section: repo_map" in prompt and "# section: code" in prompt and "# section: tests" in prompt
    assert "path=src/shop/cart.py" in prompt and f"commit={base}" in prompt
    assert "def checkoutOrder" in prompt


async def test_executor_continues_when_indexing_fails(
    world: Slice, database: testdb.TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, run, _, installs = await _fake_scene(world, database)
    task, attempt = _task(database, ws, run)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("индексатор упал")

    monkeypatch.setattr(snapshot_mod, "run_job_with_stack", boom)
    executor = build_executor(world.worker, world.tmp, world.settings.repository_roots, "agent")
    executor.index_export_cache_root = world.tmp / "export"
    outcome, captured = await _execute(world, executor, ws, run, task, attempt, installs["codex"])
    assert outcome.status == "completed", "сбой индексации не роняет задачу"
    (request,) = captured
    assert request.input.context_pack is None
    assert outcome.agent_io is not None and outcome.agent_io.context_pack_id is None
    with orch.connect_admin(database) as conn:
        assert conn.execute("SELECT count(*) FROM context_packs WHERE task_attempt_id = %s", (attempt,)).fetchone() == (
            0,
        )
        event = conn.execute(
            "SELECT payload -> 'context_pack' FROM agent_activity_events WHERE task_id = %s"
            " AND event_type = 'agent.prompt_assembled'",
            (task,),
        ).fetchone()
    assert event is not None
    payload = event[0]
    assert "index_failed:RuntimeError" in payload["warnings"] and "no_index_snapshot" in payload["warnings"]
