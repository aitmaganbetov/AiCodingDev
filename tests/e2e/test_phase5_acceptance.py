"""P5-12: приёмка Phase 5 на Fake CLI — AC-02 (индекс по commit, стек, DAG) и AC-10 (документация OFF/SMART/STRICT),
NFR-DOC-01 (локальный индекс работает без кэша документации).

Агенты — процессы Fake CLI: developer сценария `agent_docs_required` заявляет BLOCKED(DOCS_REQUIRED), пока в его
промпте нет раздела docs Context Pack и в inbox нет решения оркестратора о документации (ACP §9.4). Загрузка
документации — обработчик воркера `aicd_worker.docs` с тестовым `SsrfFetcher` (имя `docs.example.com` → локальный
http-сервер, `tests.support.docs_http`); сервер считает запросы — так проверяется, что до APPROVED сеть не трогалась.
"""

from __future__ import annotations

import asyncio
import secrets
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_code_intelligence.docs.importer import DocumentationImporter
from aicd_orchestration.approvals import Approvals
from aicd_orchestration.dag import PlannedTask, PlanValidationError, topological_order, validate_plan
from aicd_orchestration.docs_ports import DocsLedger
from aicd_orchestration.engine import Orchestrator
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, create_engine, transaction
from aicd_worker import docs, runtime
from aicd_worker.indexing import index_once
from aicd_worker.settings import WorkerSettings
from tests.conftest import UserSession
from tests.integration.test_context_pack import REPO_FILES, TASK_TEXT
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.contract import validate_response
from tests.support.docs_http import DOCS_HOST, DocsHandler, docs_server, local_fetcher
from tests.support.phase3 import Slice, create_run, drive, install_both

__all__ = ["docs_server"]  # фикстура pytest

pytestmark = pytest.mark.db

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@e"]
# Код магазина (Python src-layout + TS); pytest находит пакет через pythonpath — гейт unit-тестов проходит.
SHOP_FILES = {
    **REPO_FILES,
    "pyproject.toml": (
        '[project]\nname = "shop"\nversion = "0.1.0"\ndependencies = ["fastapi>=0.110"]\n\n'
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
    ),
}
DOCS_MD = (
    "# Checkout API\n\nОбщие сведения о корзине.\n\n"
    "## checkoutOrder and discounts\n\nThe discount is applied before the order total is computed.\n"
).encode()


def _git(path: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
async def p5(
    user: UserSession, settings: Settings, database: testdb.TestDatabase, repo_root: Path, tmp_path: Path
) -> AsyncIterator[Slice]:
    """Workspace + проект + локальный репозиторий с кодом Python/TS (fastapi, react), привязанный через API."""
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo = repo_root / f"p5-{secrets.token_hex(4)}"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for name, content in SHOP_FILES.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "shop")
    r = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"provider": "local", "local_path": str(repo)},
        headers=user.headers(idem=True),
    )
    validate_response("attachRepository", r)
    assert r.status_code == 201, r.text
    assert r.json()["latest_index_snapshot"] is None, "до индексации снимка нет"
    worker = create_engine(database.url_for("aicd_worker"))
    yield Slice(user, ws["id"], project["id"], worker, tmp_path, settings, {"name": "agent_docs_required"})
    await worker.dispose()


def _sql(database: testdb.TestDatabase, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with orch.connect_admin(database) as conn:
        return list(conn.execute(sql, params).fetchall())


def _head(database: testdb.TestDatabase, s: Slice) -> tuple[uuid.UUID, str]:
    (row,) = _sql(database, "SELECT id, last_synced_commit_sha FROM repositories WHERE project_id = %s", s.project)
    return row[0], row[1]


async def _index(s: Slice, database: testdb.TestDatabase) -> None:
    """Одна итерация воркера индексации (`aicd-worker index`)."""
    settings = WorkerSettings(
        environment="test",
        database_url=database.url_for("aicd_worker"),
        repository_roots=s.settings.repository_roots,
        index_export_cache_root=s.tmp / "export",
        index_workspace_batch=1000,
        index_jobs_per_workspace=100,
    )
    result = await index_once(settings, s.worker)
    assert result.outcomes, "задание индексации обработано"


async def _set_mode(s: Slice, mode: str) -> None:
    url = f"/api/v1/projects/{s.project}/documentation-policy"
    tag = (await s.user.client.get(url)).headers["etag"]
    r = await s.user.client.patch(url, json={"documentation_mode": mode}, headers=s.user.headers(if_match=tag))
    assert r.status_code == 200 and r.json()["documentation_mode"] == mode, r.text


async def _register_source(s: Slice, database: testdb.TestDatabase) -> tuple[str, str]:
    """Источник документации технологии проекта (fastapi из project_technologies); регистрация не загружает."""
    rows = _sql(
        database,
        "SELECT pt.technology_version_id FROM project_technologies pt JOIN technology_versions v"
        " ON v.id = pt.technology_version_id JOIN technologies t ON t.id = v.technology_id"
        " WHERE pt.project_id = %s AND t.name = 'fastapi'",
        s.project,
    )
    assert rows, "стек проекта определён индексатором"
    path = f"/p5/{secrets.token_hex(4)}.md"
    DocsHandler.routes[path] = ("text/markdown", DOCS_MD)
    r = await s.user.client.post(
        "/api/v1/documentation/sources",
        json={
            "technology_version_id": str(rows[0][0]),
            "source_type": "url",
            "uri_or_file_ref": f"https://{DOCS_HOST}{path}",
            "trust_level": "official",
        },
        headers=s.user.headers(s.ws, idem=True),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"]), path


async def _decide(s: Slice, app_engine: AsyncEngine, run_id: str, action: str, decision: str) -> uuid.UUID:
    ws = uuid.UUID(s.ws)
    async with transaction(s.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        approval_id, version = (
            await conn.execute(
                text("SELECT id, version FROM approvals WHERE run_id = :r AND action_type = :a AND status = 'PENDING'"),
                {"r": uuid.UUID(run_id), "a": action},
            )
        ).one()
    actor = uuid.UUID(s.user.user_id)
    async with transaction(app_engine, DbContext(user_id=actor, workspace_id=ws)) as conn:
        await Approvals().decide(
            conn,
            approval_id,
            actor_id=actor,
            actor_role="owner",
            decision=decision,
            reason=None if decision == "APPROVED" else "документация не нужна",
            expected_version=version,
        )
    return uuid.UUID(str(approval_id))


async def _docs_handler(s: Slice, database: testdb.TestDatabase, orchestrator: Orchestrator, port: int) -> list[str]:
    """Обработчик воркера для последнего события refresh_requested; возвращает поставленные в exec задачи."""
    (row,) = _sql(
        database,
        "SELECT id, event_type, workspace_id, run_id, aggregate_id, payload FROM outbox_events"
        " WHERE workspace_id = %s AND event_type = 'documentation_source.refresh_requested'"
        " ORDER BY created_at DESC LIMIT 1",
        uuid.UUID(s.ws),
    )
    event = {
        "event_id": str(row[0]),
        "event_type": row[1],
        "workspace_id": str(row[2]),
        "run_id": None,
        "aggregate_id": str(row[4]),
        "payload": row[5],
    }
    dispatched: list[str] = []

    async def dispatch(message: dict[str, Any]) -> None:
        dispatched.append(message["task_id"])

    rt = runtime.Runtime(
        engine=s.worker,
        orchestrator=orchestrator,
        loop=asyncio.get_running_loop(),
        dispatch_execution=dispatch,
        docs_importer=DocumentationImporter(
            s.worker, FileSystemArtifactStore(s.tmp / "artifacts"), local_fetcher(port), DocsLedger()
        ),
    )
    assert await docs.import_documentation(rt, event) == "imported"
    assert await docs.import_documentation(rt, event) == "duplicate", "повторная доставка события — no-op"
    return dispatched


def _developer_attempts(database: testdb.TestDatabase, run_id: str) -> list[tuple[Any, ...]]:
    """(attempt_no, status, failure_category, context_pack_id, docs-раздел manifest, prompt artifact) developer."""
    return _sql(
        database,
        "SELECT a.attempt_no, a.status, a.failure_category, cp.id, cp.manifest ->> 'docs', cp.snapshot_id,"
        " cp.manifest ->> 'prompt_artifact_id' FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
        " LEFT JOIN context_packs cp ON cp.task_attempt_id = a.id"
        " WHERE t.run_id = %s AND t.task_key = 'main' ORDER BY a.attempt_no",
        uuid.UUID(run_id),
    )


def _prompt(database: testdb.TestDatabase, s: Slice, artifact_id: str) -> str:
    ((ref,),) = _sql(database, "SELECT storage_ref FROM artifacts WHERE id = %s", uuid.UUID(artifact_id))
    return b"".join(FileSystemArtifactStore(s.tmp / "artifacts").open(ref)).decode()


def _counts(database: testdb.TestDatabase, s: Slice) -> dict[str, int]:
    ws = uuid.UUID(s.ws)
    q = {
        "approvals": "SELECT count(*) FROM approvals WHERE workspace_id = %s AND action_type = 'DOCS_DOWNLOAD'",
        "actions": "SELECT count(*) FROM external_actions WHERE workspace_id = %s AND action_type = 'docs_download'",
        "snapshots": "SELECT count(*) FROM documentation_snapshots WHERE workspace_id = %s",
        "docs_queries": "SELECT count(*) FROM retrieval_queries WHERE workspace_id = %s AND strategy = 'docs'",
    }
    return {k: _sql(database, sql, ws)[0][0] for k, sql in q.items()}


def _decisions(database: testdb.TestDatabase, run_id: str) -> list[str]:
    return [
        r[0]
        for r in _sql(
            database,
            "SELECT m.payload ->> 'chosen_option' FROM agent_messages m JOIN tasks t ON t.id = m.recipient_task_id"
            " WHERE m.run_id = %s AND m.message_type = 'DECISION_RECORDED' AND m.sender_role = 'orchestrator'"
            " AND t.task_key = 'main' ORDER BY m.created_at",
            uuid.UUID(run_id),
        )
    ]


# --- AC-02 ---------------------------------------------------------------------------------------------------


async def test_ac02_attach_index_stack_and_context_pack(p5: Slice, database: testdb.TestDatabase) -> None:
    repo_id, head = _head(database, p5)
    jobs = _sql(database, "SELECT status, commit_sha FROM indexing_jobs WHERE repository_id = %s", repo_id)
    assert jobs == [("QUEUED", head)], "привязка репозитория ставит индексацию HEAD"

    await _index(p5, database)
    assert _sql(database, "SELECT status FROM indexing_jobs WHERE repository_id = %s", repo_id) == [("SUCCEEDED",)]
    snaps = _sql(
        database,
        "SELECT id, status, symbol_count FROM code_index_snapshots WHERE repository_id = %s AND commit_sha = %s",
        repo_id,
        head,
    )
    assert len(snaps) == 1 and snaps[0][1] == "READY" and snaps[0][2] > 0
    snapshot_id = snaps[0][0]
    symbols = {r[0] for r in _sql(database, "SELECT symbol_name FROM code_symbols WHERE snapshot_id = %s", snapshot_id)}
    assert {"ShoppingCart", "checkoutOrder", "total_price", "loadCart"} <= symbols

    r = await p5.user.client.get(f"/api/v1/projects/{p5.project}/technologies", params={"limit": 200})
    validate_response("listTechnologies", r)
    names = {(i["ecosystem"], i["name"]) for i in r.json()["items"]}
    assert {("python", "fastapi"), ("npm", "react")} <= names, names

    r = await p5.user.client.get(f"/api/v1/projects/{p5.project}/repositories")
    validate_response("listRepositories", r)
    (repo,) = r.json()["items"]
    assert repo["latest_index_snapshot"] == {"snapshot_id": str(snapshot_id), "commit_sha": head, "status": "READY"}

    await install_both(p5.worker)
    p5.scenario["name"] = "agent"
    run_id = await create_run(p5, TASK_TEXT)
    assert await drive(p5, run_id) == "COMPLETED"
    tasks = _sql(database, "SELECT task_key, agent_role, complexity FROM tasks WHERE run_id = %s", uuid.UUID(run_id))
    edges = _sql(
        database,
        "SELECT t.task_key, p.task_key FROM task_dependencies d JOIN tasks t ON t.id = d.task_id"
        " JOIN tasks p ON p.id = d.depends_on_task_id WHERE d.run_id = %s",
        uuid.UUID(run_id),
    )
    planned = [PlannedTask(k, role, c, tuple(p for t, p in edges if t == k)) for k, role, c in tasks]
    assert set(topological_order(planned)) == {t[0] for t in tasks}, "план — DAG без циклов"
    with pytest.raises(PlanValidationError) as cycle:
        validate_plan([PlannedTask("a", "developer", 2, ("b",)), PlannedTask("b", "reviewer", 2, ("a",))])
    assert cycle.value.code == "PLAN_CYCLE_DETECTED"

    attempts = _developer_attempts(database, run_id)
    assert attempts and attempts[0][3] is not None, "Context Pack попытки сохранён"
    assert attempts[0][5] == snapshot_id, "Context Pack ссылается на снимок HEAD, построенный воркером"
    assert _sql(database, "SELECT source_commit FROM context_packs WHERE id = %s", attempts[0][3]) == [(head,)]


# --- AC-10: OFF (+ NFR-DOC-01) -----------------------------------------------------------------------------


async def test_ac10_off_uses_local_index_only(p5: Slice, database: testdb.TestDatabase, docs_server: int) -> None:
    DocsHandler.requests.clear()
    await _index(p5, database)
    await _register_source(p5, database)  # даже при зарегистрированном источнике OFF не загружает
    await _set_mode(p5, "OFF")
    await install_both(p5.worker)
    run_id = await create_run(p5, TASK_TEXT)
    assert await drive(p5, run_id) == "COMPLETED"

    assert _counts(database, p5) == {"approvals": 0, "actions": 0, "snapshots": 0, "docs_queries": 0}
    assert DocsHandler.requests == [], "OFF: обращений к источникам документации нет"
    assert _decisions(database, run_id) == ["local_index"], "агенту сообщено: работать по локальному индексу"
    attempts = _developer_attempts(database, run_id)
    assert [a[1] for a in attempts] == ["CANCELLED", "SUCCEEDED"], "попытка с DOCS_REQUIRED закрыта без сбоя"
    assert _sql(
        database, "SELECT attempt_count FROM tasks WHERE run_id = %s AND task_key = 'main'", uuid.UUID(run_id)
    ) == [(1,)], "попытка DOCS_REQUIRED не расходует max_attempts"
    # NFR-DOC-01: кэш документации пуст, а Context Pack собран из локального индекса (код есть, docs — off).
    final = attempts[-1]
    assert final[3] is not None and final[4] == "off"
    prompt = _prompt(database, p5, final[6])
    assert "# section: code" in prompt and "def checkoutOrder" in prompt
    assert "# section: docs" not in prompt


async def test_ac10_repeated_docs_request_fails_attempt(p5: Slice, database: testdb.TestDatabase) -> None:
    """Защита от цикла: после решения оркестратора повторный DOCS_REQUIRED ⇒ POLICY_DENIED, задача FAILED."""
    await _set_mode(p5, "OFF")
    await install_both(p5.worker)
    p5.scenario["name"] = "agent_docs_insist"
    run_id = await create_run(p5, TASK_TEXT)
    assert await drive(p5, run_id) == "FAILED"
    attempts = _developer_attempts(database, run_id)
    assert [(a[1], a[2]) for a in attempts] == [("CANCELLED", None), ("FAILED", "POLICY_DENIED")]
    assert _decisions(database, run_id) == ["local_index"]
    assert _counts(database, p5)["approvals"] == 0


# --- AC-10: SMART -------------------------------------------------------------------------------------------


async def test_ac10_smart_downloads_only_after_approval(
    p5: Slice, app_engine: AsyncEngine, database: testdb.TestDatabase, docs_server: int
) -> None:
    DocsHandler.requests.clear()
    await _index(p5, database)
    source_id, path = await _register_source(p5, database)
    await install_both(p5.worker)
    run_id = await create_run(p5, TASK_TEXT)
    assert await drive(p5, run_id) == "BLOCKED"

    assert _sql(
        database, "SELECT status, blocked_reason FROM tasks WHERE run_id = %s AND task_key = 'main'", uuid.UUID(run_id)
    ) == [("BLOCKED", "DOCS_REQUIRED")]
    pending = _sql(
        database,
        "SELECT a.status, a.subject ->> 'source_id', a.requested_by_role FROM approvals a JOIN tasks t"
        " ON t.id = a.task_id WHERE a.run_id = %s AND a.action_type = 'DOCS_DOWNLOAD'",
        uuid.UUID(run_id),
    )
    assert pending == [("PENDING", source_id, "developer")]
    counts = _counts(database, p5)
    assert counts["actions"] == 0 and counts["snapshots"] == 0, "до APPROVED загрузка не планируется"
    assert DocsHandler.requests == [], "до APPROVED обращений к источнику нет"

    orchestrator = p5.orchestrator()
    approval = await _decide(p5, app_engine, run_id, "DOCS_DOWNLOAD", "APPROVED")
    await orchestrator.on_task_approval_decided(approval, uuid.UUID(p5.ws))
    actions = _sql(
        database,
        "SELECT e.status, e.approval_id, e.request_hash = a.subject_hash FROM external_actions e"
        " JOIN approvals a ON a.id = e.approval_id WHERE e.run_id = %s AND e.action_type = 'docs_download'",
        uuid.UUID(run_id),
    )
    assert actions == [("PLANNED", approval, True)], "загрузка запланирована под approval (request_hash = subject)"
    assert DocsHandler.requests == [], "загрузку выполняет воркер docs, не оркестратор"

    dispatched = await _docs_handler(p5, database, orchestrator, docs_server)
    assert DocsHandler.requests == [path]
    snap = _sql(database, "SELECT status FROM documentation_snapshots WHERE source_id = %s", uuid.UUID(source_id))
    assert snap == [("READY",)]
    main = _sql(database, "SELECT id, status FROM tasks WHERE run_id = %s AND task_key = 'main'", uuid.UUID(run_id))
    assert main[0][1] == "READY" and dispatched == [str(main[0][0])], "задача разблокирована и поставлена в exec"

    assert await orchestrator.drive(uuid.UUID(run_id), uuid.UUID(p5.ws), fast_forward_backoff=True) == "COMPLETED"
    attempts = _developer_attempts(database, run_id)
    assert [a[1] for a in attempts] == ["CANCELLED", "SUCCEEDED"]
    assert attempts[-1][4] == "allowed"
    prompt = _prompt(database, p5, attempts[-1][6])
    assert "# section: docs" in prompt and "checkoutOrder and discounts" in prompt
    assert _decisions(database, run_id) == ["docs_cache"]


async def test_ac10_smart_rejected_completes_without_docs(
    p5: Slice, app_engine: AsyncEngine, database: testdb.TestDatabase, docs_server: int
) -> None:
    DocsHandler.requests.clear()
    await _index(p5, database)
    await _register_source(p5, database)
    await install_both(p5.worker)
    run_id = await create_run(p5, TASK_TEXT)
    assert await drive(p5, run_id) == "BLOCKED"
    orchestrator = p5.orchestrator()
    approval = await _decide(p5, app_engine, run_id, "DOCS_DOWNLOAD", "REJECTED")
    await orchestrator.on_task_approval_decided(approval, uuid.UUID(p5.ws))
    assert await orchestrator.drive(uuid.UUID(run_id), uuid.UUID(p5.ws), fast_forward_backoff=True) == "COMPLETED"
    counts = _counts(database, p5)
    assert counts["actions"] == 0 and counts["snapshots"] == 0
    assert DocsHandler.requests == []
    assert _decisions(database, run_id) == ["local_index"]
    prompt = _prompt(database, p5, _developer_attempts(database, run_id)[-1][6])
    assert "# section: code" in prompt and "# section: docs" not in prompt


# --- AC-10: STRICT ------------------------------------------------------------------------------------------


async def test_ac10_strict_blocks_until_approved_source(
    p5: Slice, app_engine: AsyncEngine, database: testdb.TestDatabase, docs_server: int
) -> None:
    DocsHandler.requests.clear()
    await _index(p5, database)
    source_id, path = await _register_source(p5, database)
    await _set_mode(p5, "STRICT")
    await install_both(p5.worker)
    run_id = await create_run(p5, TASK_TEXT)
    assert await drive(p5, run_id) == "BLOCKED"
    orchestrator = p5.orchestrator()
    ws, rid = uuid.UUID(p5.ws), uuid.UUID(run_id)
    # Без утверждённого источника задача стоит: повторный прогон ничего не исполняет.
    assert await orchestrator.drive(rid, ws) == "BLOCKED"
    assert _sql(database, "SELECT blocked_reason FROM runs WHERE id = %s", rid) == [("DOCS_REQUIRED",)]
    assert _counts(database, p5)["actions"] == 0 and DocsHandler.requests == []

    approval = await _decide(p5, app_engine, run_id, "DOCS_DOWNLOAD", "APPROVED")
    await orchestrator.on_task_approval_decided(approval, ws)
    await _docs_handler(p5, database, orchestrator, docs_server)
    assert DocsHandler.requests == [path]
    assert _sql(database, "SELECT status FROM documentation_snapshots WHERE source_id = %s", uuid.UUID(source_id)) == [
        ("READY",)
    ]
    assert await orchestrator.drive(rid, ws, fast_forward_backoff=True) == "COMPLETED"
    prompt = _prompt(database, p5, _developer_attempts(database, run_id)[-1][6])
    assert "# section: docs" in prompt


async def test_ac10_strict_rejected_goes_to_manual_review(
    p5: Slice, app_engine: AsyncEngine, database: testdb.TestDatabase, docs_server: int
) -> None:
    DocsHandler.requests.clear()
    await _index(p5, database)
    await _register_source(p5, database)
    await _set_mode(p5, "STRICT")
    await install_both(p5.worker)
    run_id = await create_run(p5, TASK_TEXT)
    assert await drive(p5, run_id) == "BLOCKED"
    orchestrator = p5.orchestrator()
    ws, rid = uuid.UUID(p5.ws), uuid.UUID(run_id)
    approval = await _decide(p5, app_engine, run_id, "DOCS_DOWNLOAD", "REJECTED")
    await orchestrator.on_task_approval_decided(approval, ws)
    assert _sql(database, "SELECT status, blocked_reason FROM tasks WHERE run_id = %s AND task_key = 'main'", rid) == [
        ("BLOCKED", "MANUAL_REVIEW")
    ]
    assert _counts(database, p5)["actions"] == 0 and DocsHandler.requests == []
    # Человек решает продолжить без документации — новая попытка по локальному индексу.
    manual = await _decide(p5, app_engine, run_id, "MANUAL_REVIEW", "APPROVED")
    await orchestrator.on_task_approval_decided(manual, ws)
    assert await orchestrator.drive(rid, ws, fast_forward_backoff=True) == "COMPLETED"
    assert _decisions(database, run_id) == ["local_index"]
