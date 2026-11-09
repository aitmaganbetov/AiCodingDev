"""P9-03 / OQ-47 (7): гибель GitHub-воркера посреди внешнего действия и recovery-сверка без Dramatiq.

Настоящий процесс-воркер (`tests/support/chaos_github_worker.py`) исполняет PR workflow / merge против
двойника GitHub. Двойник применяет эффект (push принят, PR создан, ревью/статус записаны, merge выполнен)
и задерживает ответ (`Hold`); тест убивает воркер SIGKILL — строка ledger остаётся `EXECUTING`. Повторной
доставки Dramatiq нет (сообщение «потеряно»): восстановление выполняет только
`aicd_orchestration.github_recovery.reconcile` (проход воркера orchestrator). Проверяется:

* пока аренда воркера жива, reconciler действие не трогает (fencing);
* после истечения аренды — probe: применённое действие ⇒ `CONFIRMED` (`reconciled`), неприменённое — исполняется
  через `execute_once` (probe → do) ровно один раз; цепочка PR доводится до конца (PR, ревью, commit status);
* эффекты в GitHub не дублируются (счётчики двойника), ledger сходится: каждое действие CONFIRMED с одним
  событием перехода в CONFIRMED; повторная доставка старого сообщения после сверки — без эффектов;
* «зомби» со старым fencing token не может снова начать подтверждённое действие.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_integrations_github.api import AppJwtSigner, GitHubClient, store
from aicd_orchestration import external_actions, github_flow, github_recovery
from aicd_orchestration.approvals import Approvals
from aicd_platform.db import DbContext, transaction
from tests.support import db as testdb
from tests.support import github as ghs
from tests.support.github_double import GitHubDouble, Hold
from tests.support.orchestration import Tenant, add_user, connect_admin, seed_tenant

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO = "/repos/acme/app"


@dataclass
class World:
    gh: GitHubDouble
    url: str
    key_file: Path
    local: Path
    base_sha: str
    tenant: Tenant
    repo: uuid.UUID
    admin1: uuid.UUID
    admin2: uuid.UUID


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
def world(admin: psycopg.Connection, repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    local = repo_root / f"ghchaos-{secrets.token_hex(4)}"
    base = ghs.init_repo(local)
    installation_id = secrets.randbelow(10**8) + 10_000
    gh, url = ghs.double_with_repo(tmp_path_factory.mktemp("ghchaos-remote"), local, installation_id=installation_id)
    key_file = tmp_path_factory.mktemp("ghchaos-key") / "app.pem"
    key_file.write_text(gh.private_key_pem)
    key_file.chmod(0o600)
    tenant = seed_tenant(admin)
    inst = ghs.installation(admin, tenant, installation_id, "acme")
    repo = ghs.github_repository(admin, tenant, installation_row=inst, full_name="acme/app", repo_id=1001,
                                 local_ref=f"main/{local.name}")  # fmt: skip
    yield World(gh, url, key_file, local, base, tenant, repo, add_user(admin, tenant, "admin"),
                add_user(admin, tenant, "admin"))  # fmt: skip
    gh.stop()


@pytest.fixture
async def workflow(
    world: World, worker_engine: AsyncEngine, repo_root: Path
) -> AsyncIterator[github_flow.GitHubWorkflow]:
    client = GitHubClient(api_url=world.url, signer=AppJwtSigner(world.gh.app_id, world.gh.private_key_pem))
    yield github_flow.GitHubWorkflow(worker_engine, client, repository_roots={"main": repo_root}, git_url=world.url)
    await client.aclose()


def _run(world: World, admin: psycopg.Connection) -> ghs.CompletedRun:
    return ghs.completed_run(admin, world.tenant, repository_id=world.repo, local=world.local, base_sha=world.base_sha,
                             files={f"src/c_{secrets.token_hex(3)}.py": "VALUE = 1\n"})  # fmt: skip


def _job(run: ghs.CompletedRun) -> dict[str, Any]:
    return {"run_id": str(run.run), "task_id": str(run.task), "base_branch": "main", "title": "chaos change"}


def _spawn(world: World, database: testdb.TestDatabase, repo_root: Path, **op: str) -> subprocess.Popen[bytes]:
    config = {
        "database_url": database.url_for("aicd_worker"),
        "api_url": world.url,
        "app_id": str(world.gh.app_id),
        "key_file": str(world.key_file),
        "repository_roots": json.dumps({"main": str(repo_root)}),
        "workspace_id": str(world.tenant.workspace_id),
        **op,
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.support.chaos_github_worker", json.dumps(config)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _kill_at(world: World, proc: subprocess.Popen[bytes], key: str) -> None:
    """Ждать применения эффекта двойником (hold), убить воркер SIGKILL, затем отпустить ответ."""
    hold = Hold()
    world.gh.hold_after[key] = hold
    try:
        deadline = time.monotonic() + 60
        while not hold.reached.wait(0.1):
            if proc.poll() is not None:
                err = proc.stderr.read().decode() if proc.stderr else ""
                raise AssertionError(f"воркер завершился до точки сбоя {key}: {err[-2000:]}")
            if time.monotonic() > deadline:
                raise AssertionError(f"точка сбоя {key} не достигнута")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        world.gh.hold_after.pop(key, None)
        hold.release.set()
        if proc.poll() is None:
            proc.kill()


def _actions(admin: psycopg.Connection, run: uuid.UUID) -> dict[str, tuple[uuid.UUID, str, Any, int]]:
    rows = admin.execute(
        "SELECT action_type, id, status, result, fencing_token FROM external_actions WHERE run_id = %s", (run,)
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def _expire_leases(admin: psycopg.Connection, tokens: list[int]) -> None:
    """Истечение TTL аренды `external_action` (вместо ожидания 600 с)."""
    admin.execute(
        "UPDATE worker_leases SET acquired_at = now() - interval '20 minutes', lease_until = now() - interval '1 second'"
        " WHERE fencing_token = ANY(%s) AND released_at IS NULL",
        (tokens,),
    )


def _confirmed_events(admin: psycopg.Connection, action_id: uuid.UUID) -> int:
    row = admin.execute(
        "SELECT count(*) FROM outbox_events WHERE aggregate_id = %s AND event_type = 'external_action.state_changed'"
        " AND payload->>'to' = 'CONFIRMED'",
        (action_id,),
    ).fetchone()
    return int(row[0]) if row else 0


# точка сбоя → (ключ hold двойника, действие ledger, которое остаётся EXECUTING, эффект уже применён)
PR_POINTS = {
    "after_push": ("POST /acme/app\\.git/git-receive-pack", "git_push", True),
    "before_pr_create": (f"GET {REPO}/pulls", "pr_create", False),
    "after_pr_create": (f"POST {REPO}/pulls", "pr_create", True),
    "after_review": (f"POST {REPO}/pulls/\\d+/reviews", "pr_comment", True),
    "after_status": (f"POST {REPO}/statuses/[0-9a-f]{{40}}", "pr_update", True),
}


@pytest.mark.parametrize("point", sorted(PR_POINTS))
async def test_worker_killed_mid_pr_action_is_reconciled_without_redelivery(
    point: str,
    world: World,
    workflow: github_flow.GitHubWorkflow,
    admin: psycopg.Connection,
    database: testdb.TestDatabase,
    repo_root: Path,
) -> None:
    key, interrupted, applied = PR_POINTS[point]
    gh = world.gh
    run = _run(world, admin)
    pushes, pulls = gh.pushes, gh.created_pulls
    proc = _spawn(world, database, repo_root, op="pr", job=json.dumps(_job(run)))
    _kill_at(world, proc, key)

    actions = _actions(admin, run.run)
    action_id, status, _, token = actions[interrupted]
    assert status == "EXECUTING", f"{interrupted}: воркер убит посреди вызова GitHub"
    effects = {
        "git_push": gh.branch_sha(gh.repos[1001], run.branch) == run.head_sha,
        "pr_create": any(p.head == run.branch for p in gh.pulls_of(1001)),
    }
    if interrupted in effects:
        assert effects[interrupted] is applied

    # 1. Аренда воркера ещё жива: reconciler не трогает действие (не знает, жив ли воркер).
    report = await github_recovery.reconcile(workflow)
    assert action_id not in report.reconciled + report.resumed + report.unreconciled
    assert _actions(admin, run.run)[interrupted][1] == "EXECUTING"

    # 2. TTL истёк — сверка без повторной доставки Dramatiq.
    _expire_leases(admin, [token])
    report = await github_recovery.reconcile(workflow)
    assert action_id in report.resumed and not report.unreconciled
    assert (action_id in report.reconciled) is applied, "probe подтверждает только применённое действие"

    after = _actions(admin, run.run)
    assert {k: v[1] for k, v in after.items()} == dict.fromkeys(
        ("git_push", "pr_create", "pr_comment", "pr_update"), "CONFIRMED"
    ), "цепочка PR доведена до конца"
    if applied:
        assert after[interrupted][2]["reconciled"] is True
    for aid, *_ in after.values():
        assert _confirmed_events(admin, aid) == 1, "ровно один переход в CONFIRMED (ledger сходится)"
    assert gh.pushes == pushes + 1, "push ровно один"
    assert gh.created_pulls == pulls + 1, "PR ровно один"
    [pull] = [p for p in gh.pulls_of(1001) if p.head == run.branch]
    review_marker = f"<!-- aicd:review:{after['pr_comment'][0]} -->"
    assert len([r for r in gh.reviews.get((1001, pull.number), []) if review_marker in r["body"]]) == 1
    assert gh.statuses[(1001, run.head_sha)][github_flow.STATUS_CONTEXT] == "success"
    assert admin.execute("SELECT count(*) FROM pull_requests WHERE run_id = %s", (run.run,)).fetchone() == (1,)
    assert admin.execute(
        "SELECT count(*) FROM worker_leases WHERE resource_type = 'external_action' AND released_at IS NULL"
        " AND resource_id = %s",
        (after["pr_create"][0],),
    ).fetchone() == (0,), "аренда recovery освобождена"

    # 3. Запоздалая повторная доставка исходного сообщения — без новых эффектов (AC-09).
    requests = len(gh.requests)
    assert await workflow.open_pull_request(world.tenant.workspace_id, _job(run)) == "opened"
    assert gh.pushes == pushes + 1 and gh.created_pulls == pulls + 1
    assert not [r for r in gh.requests[requests:] if r[0] != "GET" and "access_tokens" not in r[1]]

    # 4. «Зомби» со старым fencing token не начнёт подтверждённое действие заново.
    with pytest.raises(external_actions.AlreadyConfirmedError):
        async with transaction(workflow.engine, DbContext(workspace_id=world.tenant.workspace_id)) as conn:
            await external_actions.begin(conn, action_id, token)


async def test_worker_killed_after_merge_is_reconciled(
    world: World,
    workflow: github_flow.GitHubWorkflow,
    admin: psycopg.Connection,
    database: testdb.TestDatabase,
    app_engine: AsyncEngine,
    repo_root: Path,
) -> None:
    ws = world.tenant.workspace_id
    gh = world.gh
    run = _run(world, admin)
    assert await workflow.open_pull_request(ws, _job(run)) == "opened"
    row = admin.execute("SELECT id, pr_number FROM pull_requests WHERE run_id = %s", (run.run,)).fetchone()
    assert row is not None
    pr_id, number = row
    async with transaction(app_engine, DbContext(user_id=world.admin1, workspace_id=ws)) as conn:
        pr = await store.get_pull_request(conn, pr_id)
        assert pr is not None
        approval_id = await github_flow.request_merge(conn, pr, actor_id=world.admin1, method="merge")
    async with transaction(app_engine, DbContext(user_id=world.admin2, workspace_id=ws)) as conn:
        await Approvals().decide(conn, approval_id, actor_id=world.admin2, actor_role="admin", decision="APPROVED",
                                 reason=None, expected_version=None)  # fmt: skip
    merges = gh.merges
    proc = _spawn(world, database, repo_root, op="merge", approval_id=str(approval_id))
    _kill_at(world, proc, f"PUT {REPO}/pulls/{number}/merge")
    merge = admin.execute(
        "SELECT id, status, fencing_token FROM external_actions WHERE approval_id = %s AND action_type = 'pr_merge'",
        (approval_id,),
    ).fetchone()
    assert merge is not None and merge[1] == "EXECUTING"
    assert gh.merges == merges + 1, "merge в GitHub выполнен до гибели воркера"
    assert admin.execute("SELECT state FROM pull_requests WHERE id = %s", (pr_id,)).fetchone() == ("OPEN",)

    _expire_leases(admin, [merge[2]])
    report = await github_recovery.reconcile(workflow)
    assert merge[0] in report.reconciled and merge[0] in report.resumed and not report.unreconciled
    assert gh.merges == merges + 1, "второго merge нет"
    assert admin.execute(
        "SELECT status, result->>'reconciled' FROM external_actions WHERE id = %s", (merge[0],)
    ).fetchone() == ("CONFIRMED", "true")
    assert admin.execute("SELECT state, merge_action_id FROM pull_requests WHERE id = %s", (pr_id,)).fetchone() == (
        "MERGED",
        merge[0],
    ), "строка PR — по состоянию GitHub API"
    assert _confirmed_events(admin, merge[0]) == 1
    assert admin.execute(
        "SELECT count(*) FROM audit_events WHERE resource_id = %s AND action = 'external_action.reconciled'",
        (merge[0],),
    ).fetchone() == (1,)
    # Повторная доставка approval.decided после сверки — merge не вызывается снова.
    assert await workflow.merge(ws, approval_id) == "merged"
    assert gh.merges == merges + 1


async def test_unreachable_github_leaves_action_executing_and_reported(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection, database: testdb.TestDatabase,
    repo_root: Path,
) -> None:  # fmt: skip
    """GitHub недоступен во время сверки: действие не подтверждается вслепую, остаётся EXECUTING в отчёте,
    следующий проход после восстановления GitHub доводит его до конца."""
    gh = world.gh
    run = _run(world, admin)
    proc = _spawn(world, database, repo_root, op="pr", job=json.dumps(_job(run)))
    _kill_at(world, proc, f"POST {REPO}/pulls")
    action_id, status, _, token = _actions(admin, run.run)["pr_create"]
    assert status == "EXECUTING"
    _expire_leases(admin, [token])
    gh.fail_next[f"GET {REPO}/pulls"] = 503  # probe падает: состояние GitHub неизвестно
    gh.fail_next["POST /app/installations/\\d+/access_tokens"] = 502
    try:
        report = await github_recovery.reconcile(workflow)
    finally:
        gh.fail_next.clear()
    assert action_id in report.unreconciled and action_id not in report.reconciled
    assert _actions(admin, run.run)["pr_create"][1] == "EXECUTING", "без probe — без подтверждения и повтора"
    report = await github_recovery.reconcile(workflow)
    assert action_id in report.reconciled and action_id in report.resumed
    assert len([p for p in gh.pulls_of(1001) if p.head == run.branch]) == 1


async def test_orchestrator_schedule_runs_github_recovery(
    world: World, workflow: github_flow.GitHubWorkflow, worker_engine: AsyncEngine
) -> None:
    """Воркер orchestrator: сверка GitHub-действий при старте и далее по интервалу; без GitHub App — no-op."""
    import asyncio as _asyncio

    from aicd_worker.maintenance import GitHubRecoverySchedule
    from aicd_worker.runtime import Runtime

    async def dispatch(_: dict[str, Any]) -> None:
        return None

    rt = Runtime(engine=worker_engine, orchestrator=None, loop=_asyncio.get_running_loop(),  # type: ignore[arg-type]
                 dispatch_execution=dispatch)  # fmt: skip
    schedule = GitHubRecoverySchedule(interval=3600)
    assert await schedule.run_due(rt) is None, "GitHub App не настроен — сверки нет"
    rt.github = workflow
    first = await schedule.run_due(rt)
    assert isinstance(first, github_recovery.GitHubRecoveryReport)
    assert await schedule.run_due(rt) is None, "следующая сверка — через интервал"
