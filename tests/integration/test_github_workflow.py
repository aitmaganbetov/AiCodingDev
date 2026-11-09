"""P8-04, P8-05, P8-03 (обработка), P8-09: PR workflow и merge через ledger `external_actions` на двойнике GitHub.

* push ветки → PR → ревью-комментарий → commit status, всё через ledger (CONFIRMED не повторяется);
* crash-тест: падение между «PR создан в GitHub» и CONFIRMED (и между push и CONFIRMED) → повтор сверяет
  состояние GitHub — ровно один PR / один push (двойник считает вызовы);
* AC-13: merge без APPROVED approval невозможен (триггер БД + воркер), four-eyes, branch protection (required
  checks), сдвиг head после approval, повтор после merge;
* вебхуки: payload — подсказка, состояние перечитывается из API; повтор доставки — без обработки;
* токены GitHub не попадают в БД, git config, логи.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_integrations_github.api import AppJwtSigner, GitHubClient, PullInfo, RepoRef, store
from aicd_orchestration import external_actions, github_flow
from aicd_orchestration.approvals import Approvals
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7
from aicd_worker import github as worker_github
from aicd_worker.broker import GITHUB_QUEUE, ORCHESTRATOR_QUEUE, build_broker, register_actors
from aicd_worker.runtime import Runtime
from tests.support import db as testdb
from tests.support import github as ghs
from tests.support.github_double import GitHubDouble, git
from tests.support.orchestration import Tenant, add_user, connect_admin, seed_tenant

pytestmark = pytest.mark.db


class Crash(BaseException):
    """Процесс воркера «умер» в середине действия (ledger остаётся EXECUTING)."""


class CrashingClient(GitHubClient):
    """Настоящий клиент; после успешного вызова GitHub — однократный «крах» до записи CONFIRMED."""

    crash_after: set[str]
    crash_exc: type[BaseException] = Crash

    async def create_pull(self, *args: Any, **kwargs: Any) -> PullInfo:
        pull = await super().create_pull(*args, **kwargs)
        if "create_pull" in self.crash_after:
            self.crash_after.discard("create_pull")
            raise self.crash_exc("worker failed after PR was created on GitHub")
        return pull

    async def merge(self, *args: Any, **kwargs: Any) -> str:
        sha = await super().merge(*args, **kwargs)
        if "merge" in self.crash_after:
            self.crash_after.discard("merge")
            raise Crash("worker killed after merge on GitHub")
        return sha


@dataclass
class World:
    gh: GitHubDouble
    url: str
    local: Path
    base_sha: str
    tenant: Tenant
    repo: uuid.UUID
    installation_id: int
    admin1: uuid.UUID
    admin2: uuid.UUID


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
def world(admin: psycopg.Connection, repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    local = repo_root / f"ghwf-{secrets.token_hex(4)}"
    base = ghs.init_repo(local)
    installation_id = secrets.randbelow(10**8) + 10_000
    gh, url = ghs.double_with_repo(tmp_path_factory.mktemp("ghwf-remote"), local, installation_id=installation_id)
    tenant = seed_tenant(admin)
    inst = ghs.installation(admin, tenant, installation_id, "acme")
    repo = ghs.github_repository(admin, tenant, installation_row=inst, full_name="acme/app", repo_id=1001,
                                 local_ref=f"main/{local.name}")  # fmt: skip
    a1, a2 = add_user(admin, tenant, "admin"), add_user(admin, tenant, "admin")
    yield World(gh, url, local, base, tenant, repo, installation_id, a1, a2)
    gh.stop()


@pytest.fixture
async def workflow(
    world: World, worker_engine: AsyncEngine, repo_root: Path
) -> AsyncIterator[github_flow.GitHubWorkflow]:
    client = CrashingClient(api_url=world.url, signer=AppJwtSigner(world.gh.app_id, world.gh.private_key_pem))
    client.crash_after = set()
    wf = github_flow.GitHubWorkflow(worker_engine, client, repository_roots={"main": repo_root}, git_url=world.url)
    yield wf
    await client.aclose()


def _run(world: World, admin: psycopg.Connection, files: dict[str, str] | None = None) -> ghs.CompletedRun:
    return ghs.completed_run(admin, world.tenant, repository_id=world.repo, local=world.local, base_sha=world.base_sha,
                             files=files or {f"src/m_{secrets.token_hex(3)}.py": "VALUE = 1\n"})  # fmt: skip


def _job(run: ghs.CompletedRun) -> dict[str, Any]:
    return {"run_id": str(run.run), "task_id": str(run.task), "base_branch": "main", "title": "AiCodingDev change"}


def _actions(admin: psycopg.Connection, run: uuid.UUID) -> dict[str, tuple[str, Any]]:
    rows = admin.execute(
        "SELECT action_type, status, result FROM external_actions WHERE run_id = %s", (run,)
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


async def test_pull_request_workflow_through_ledger(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    run = _run(world, admin)
    assert await workflow.open_pull_request(world.tenant.workspace_id, _job(run)) == "opened"
    gh = world.gh
    repo = gh.repos[1001]
    assert gh.branch_sha(repo, run.branch) == run.head_sha, "ветка aicd/* запушена на проверенный коммит"
    [pull] = [p for p in gh.pulls_of(1001) if p.head == run.branch]
    assert pull.base == "main" and f"aicd:pr:{run.run}:{run.task}" in pull.body
    [review] = gh.reviews[(1001, pull.number)]
    assert "итоги проверок" in review["body"] and "| LINT | PASS | 1 |" in review["body"]
    assert gh.statuses[(1001, run.head_sha)] == {"aicd/verification": "success"}
    actions = _actions(admin, run.run)
    assert {k: v[0] for k, v in actions.items()} == {
        "git_push": "CONFIRMED", "pr_create": "CONFIRMED", "pr_comment": "CONFIRMED", "pr_update": "CONFIRMED",
    }  # fmt: skip
    row = admin.execute(
        "SELECT state, pr_number, head_sha, head_branch, create_action_id IS NOT NULL, url FROM pull_requests"
        " WHERE run_id = %s",
        (run.run,),
    ).fetchone()
    assert row == (
        "OPEN",
        pull.number,
        run.head_sha,
        run.branch,
        True,
        f"https://github.test/acme/app/pull/{pull.number}",
    )
    # Повтор (redelivery) — CONFIRMED-действия не исполняются повторно.
    created, pushes, reviews = gh.created_pulls, gh.pushes, len(gh.reviews[(1001, pull.number)])
    assert await workflow.open_pull_request(world.tenant.workspace_id, _job(run)) == "opened"
    assert (gh.created_pulls, gh.pushes, len(gh.reviews[(1001, pull.number)])) == (created, pushes, reviews)
    # Токены: только в памяти — нет в БД, git config локальной копии и логах (SC-GH-04, P8-09).
    assert gh.tokens
    dump = json.dumps(
        [
            admin.execute("SELECT request, result, last_error, external_ref FROM external_actions").fetchall(),
            admin.execute("SELECT metadata FROM audit_events").fetchall(),
            admin.execute("SELECT payload, last_error FROM outbox_events").fetchall(),
            admin.execute("SELECT * FROM pull_requests").fetchall(),
        ],
        default=str,
    )
    config = (world.local / ".git" / "config").read_text()
    for value in gh.tokens:
        assert value not in dump and value not in config and value not in caplog.text
    # Минимальные права: каждый токен — на один репозиторий, без прав вне App.
    assert all(r.get("repository_ids") == [1001] for r in gh.token_requests)
    scopes = {json.dumps(r["permissions"], sort_keys=True) for r in gh.token_requests}
    assert json.dumps({"contents": "write", "metadata": "read"}, sort_keys=True) in scopes


@pytest.mark.parametrize("failure", ["crash", "exception"])
async def test_crash_between_pr_created_and_confirmed_creates_one_pr(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection, failure: str
) -> None:
    run = _run(world, admin)
    client = workflow.client
    assert isinstance(client, CrashingClient)
    before = world.gh.created_pulls
    client.crash_after = {"create_pull"}
    client.crash_exc = Crash if failure == "crash" else RuntimeError
    try:
        with pytest.raises(client.crash_exc):
            await workflow.open_pull_request(world.tenant.workspace_id, _job(run))
    finally:
        client.crash_exc = Crash
    # crash — процесс погиб (EXECUTING); exception — do() упал после вызова (FAILED с executing_at).
    expected = "EXECUTING" if failure == "crash" else "FAILED"
    assert _actions(admin, run.run)["pr_create"][0] == expected
    assert world.gh.created_pulls == before + 1, "PR в GitHub создан"
    assert admin.execute("SELECT count(*) FROM pull_requests WHERE run_id = %s", (run.run,)).fetchone() == (0,)
    # Повтор (Dramatiq redelivery / recovery): сверка по head-ветке — ровно один PR.
    assert await workflow.open_pull_request(world.tenant.workspace_id, _job(run)) == "opened"
    assert world.gh.created_pulls == before + 1, "второй PR не создан"
    assert len([p for p in world.gh.pulls_of(1001) if p.head == run.branch]) == 1
    status, result = _actions(admin, run.run)["pr_create"]
    assert status == "CONFIRMED" and result["reconciled"] is True
    assert admin.execute("SELECT count(*) FROM pull_requests WHERE run_id = %s", (run.run,)).fetchone() == (1,)


async def test_crash_after_push_is_reconciled_without_second_push(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(world, admin)
    real = github_flow.push_ref  # type: ignore[attr-defined]
    crashed: list[bool] = []

    def push_then_die(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        if not crashed:
            crashed.append(True)
            raise Crash("killed after push")

    monkeypatch.setattr(github_flow, "push_ref", push_then_die)
    pushes = world.gh.pushes
    with pytest.raises(Crash):
        await workflow.open_pull_request(world.tenant.workspace_id, _job(run))
    assert world.gh.pushes == pushes + 1
    assert await workflow.open_pull_request(world.tenant.workspace_id, _job(run)) == "opened"
    assert world.gh.pushes == pushes + 1, "push не повторён: ветка уже на нужном SHA"
    assert _actions(admin, run.run)["git_push"][1]["reconciled"] is True


async def _approval(app_engine: AsyncEngine, world: World, pr_id: uuid.UUID, requester: uuid.UUID) -> uuid.UUID:
    async with transaction(app_engine, DbContext(user_id=requester, workspace_id=world.tenant.workspace_id)) as conn:
        pr = await store.get_pull_request(conn, pr_id)
        assert pr is not None
        return await github_flow.request_merge(conn, pr, actor_id=requester, method="merge")


async def _decide(app_engine: AsyncEngine, world: World, approval_id: uuid.UUID, who: uuid.UUID) -> None:
    async with transaction(app_engine, DbContext(user_id=who, workspace_id=world.tenant.workspace_id)) as conn:
        await Approvals().decide(conn, approval_id, actor_id=who, actor_role="admin", decision="APPROVED",
                                 reason=None, expected_version=None)  # fmt: skip


async def _opened(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection
) -> tuple[ghs.CompletedRun, uuid.UUID, int]:
    run = _run(world, admin)
    assert await workflow.open_pull_request(world.tenant.workspace_id, _job(run)) == "opened"
    row = admin.execute("SELECT id, pr_number FROM pull_requests WHERE run_id = %s", (run.run,)).fetchone()
    assert row is not None
    return run, row[0], row[1]


async def test_merge_requires_approval_four_eyes_and_branch_protection(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection, app_engine: AsyncEngine,
    worker_engine: AsyncEngine,
) -> None:  # fmt: skip
    """AC-13: merge без APPROVED невозможен; инициатор не утверждает; защита ветки не обходится."""
    ws = world.tenant.workspace_id
    run, pr_id, number = await _opened(world, workflow, admin)
    gh, merges = world.gh, world.gh.merges
    approval_id = await _approval(app_engine, world, pr_id, world.admin1)
    # PENDING approval: воркер не исполняет merge.
    assert await workflow.merge(ws, approval_id) == "denied"
    assert gh.merges == merges
    # Даже прямой вызов ledger: триггер БД не пускает pr_merge в EXECUTING без APPROVED (AC-13).
    subject = admin.execute("SELECT subject FROM approvals WHERE id = %s", (approval_id,)).fetchone()[0]  # type: ignore[index]
    async with transaction(worker_engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        action = await external_actions.plan(
            conn, workspace_id=ws, run_id=run.run, task_id=None, action_type="pr_merge", target=f"probe-{approval_id}",
            plan_version=None, request=subject, approval_id=approval_id,
        )  # fmt: skip
    with pytest.raises(external_actions.ApprovalRequiredError):
        async with transaction(worker_engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            await external_actions.begin(conn, action.id, 10**12)
    # Four-eyes: инициатор (admin1) не может утвердить свой merge.
    with pytest.raises(ProblemError) as err:
        await _decide(app_engine, world, approval_id, world.admin1)
    assert err.value.code == "four_eyes_violation"
    await _decide(app_engine, world, approval_id, world.admin2)
    # Branch protection: обязательная проверка не пройдена — merge не выполняется, обхода нет.
    gh.repos[1001].protection["main"] = ("ci/required",)
    try:
        assert await workflow.merge(ws, approval_id) == "blocked"
        assert gh.merges == merges
        merge_rows = admin.execute(
            "SELECT status, last_error FROM external_actions WHERE approval_id = %s AND action_type = 'pr_merge'"
            " AND target NOT LIKE 'probe-%%'",
            (approval_id,),
        ).fetchall()
        assert [r[0] for r in merge_rows] == ["FAILED"] and "ci/required" in merge_rows[0][1]
        gh.statuses.setdefault((1001, run.head_sha), {})["ci/required"] = "success"
        assert await workflow.merge(ws, approval_id) == "merged"
    finally:
        gh.repos[1001].protection.pop("main", None)
    assert gh.merges == merges + 1
    pull = next(p for p in gh.pulls_of(1001) if p.number == number)
    assert pull.merged and gh.branch_sha(gh.repos[1001], "main") == run.head_sha
    row = admin.execute(
        "SELECT p.state, p.merged_commit_sha, a.action_type, a.status FROM pull_requests p"
        " JOIN external_actions a ON a.id = p.merge_action_id WHERE p.id = %s",
        (pr_id,),
    ).fetchone()
    assert row == ("MERGED", run.head_sha, "pr_merge", "CONFIRMED")
    # Повтор после CONFIRMED — merge не вызывается снова (AC-09).
    assert await workflow.merge(ws, approval_id) == "merged"
    assert gh.merges == merges + 1
    audit = {
        r[0]
        for r in admin.execute(
            "SELECT action FROM audit_events WHERE resource_id = %s AND action LIKE 'github.pr.%%'", (pr_id,)
        ).fetchall()
    }
    assert {"github.pr.merge_request", "github.pr.merge_blocked", "github.pr.merged"} <= audit


async def test_merge_rejected_when_head_moved_and_crash_after_merge(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection, app_engine: AsyncEngine
) -> None:
    ws = world.tenant.workspace_id
    gh = world.gh
    # Сдвиг head после approval (TOCTOU): нужен новый approval.
    _, pr_id, number = await _opened(world, workflow, admin)
    approval_id = await _approval(app_engine, world, pr_id, world.admin1)
    await _decide(app_engine, world, approval_id, world.admin2)
    pull = next(p for p in gh.pulls_of(1001) if p.number == number)
    original = pull.head_sha
    pull.head_sha = "f" * 40
    merges = gh.merges
    try:
        assert await workflow.merge(ws, approval_id) == "blocked"
    finally:
        pull.head_sha = original
    assert gh.merges == merges
    # Крах между merge в GitHub и CONFIRMED: повтор сверяет состояние PR — второго merge нет.
    _, pr_id, number = await _opened(world, workflow, admin)
    approval_id = await _approval(app_engine, world, pr_id, world.admin1)
    await _decide(app_engine, world, approval_id, world.admin2)
    client = workflow.client
    assert isinstance(client, CrashingClient)
    client.crash_after = {"merge"}
    with pytest.raises(Crash):
        await workflow.merge(ws, approval_id)
    assert gh.merges == merges + 1
    assert await workflow.merge(ws, approval_id) == "merged"
    assert gh.merges == merges + 1
    assert admin.execute("SELECT state FROM pull_requests WHERE id = %s", (pr_id,)).fetchone() == ("MERGED",)


async def _deliver(
    app_engine: AsyncEngine, admin: psycopg.Connection, event: str, hint: dict[str, Any], installation_id: int
) -> dict[str, Any]:
    guid = uuid.uuid4()
    async with transaction(app_engine, DbContext()) as conn:
        outcome = await store.receive_delivery(
            conn, delivery_guid=guid, event=event, action="closed", installation_id=installation_id,
            signature_valid=True, payload_sha256="0" * 64, status="RECEIVED", error=None, hint=hint,
        )  # fmt: skip
    assert outcome == "accepted"
    row = admin.execute(
        "SELECT id, event_type, payload FROM outbox_events WHERE payload->>'delivery_guid' = %s", (str(guid),)
    ).fetchone()
    assert row is not None
    return {"event_id": str(row[0]), "event_type": row[1], "payload": row[2], "workspace_id": None}


async def test_webhook_processing_rereads_state_and_dedups(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection, app_engine: AsyncEngine
) -> None:
    run, pr_id, number = await _opened(world, workflow, admin)
    hint = {"repository_id": 1001, "pr_number": number, "head_sha": "e" * 40}
    # Подлинный, но «ложный» payload (merged) — PR в GitHub открыт: состояние не меняется (SC-GH-03).
    event = await _deliver(app_engine, admin, "pull_request", hint, world.installation_id)
    assert await workflow.process_webhook(event) == "processed"
    assert admin.execute("SELECT state, head_sha FROM pull_requests WHERE id = %s", (pr_id,)).fetchone() == (
        "OPEN",
        run.head_sha,
    )
    assert await workflow.process_webhook(event) == "duplicate", "повтор доставки не обрабатывается"
    # PR закрыт человеком в GitHub — состояние из API.
    next(p for p in world.gh.pulls_of(1001) if p.number == number).state = "closed"
    event = await _deliver(app_engine, admin, "pull_request", hint, world.installation_id)
    assert await workflow.process_webhook(event) == "processed"
    assert admin.execute("SELECT state FROM pull_requests WHERE id = %s", (pr_id,)).fetchone() == ("CLOSED",)
    statuses = {
        r[0]
        for r in admin.execute(
            "SELECT status FROM github_webhook_deliveries WHERE id = ANY(%s)",
            ([uuid.UUID(event["payload"]["delivery_id"])],),
        ).fetchall()
    }
    assert statuses == {"PROCESSED"}
    # Неизвестная установка — IGNORED.
    event = await _deliver(app_engine, admin, "pull_request", hint, 1)
    assert await workflow.process_webhook(event) == "ignored"
    # Установка удалена в GitHub — REVOKED (по API, не по payload).
    other = secrets.randbelow(10**8) + 20_000_000
    world.gh.add_installation(other, "acme")
    inst = ghs.installation(admin, world.tenant, other, "acme")
    world.gh.installations[other].deleted = True
    event = await _deliver(app_engine, admin, "installation", {}, other)
    assert await workflow.process_webhook(event) == "processed"
    assert admin.execute("SELECT status FROM github_installations WHERE id = %s", (inst,)).fetchone() == ("REVOKED",)


async def test_worker_routing_and_handler(
    world: World, workflow: github_flow.GitHubWorkflow, admin: psycopg.Connection, worker_engine: AsyncEngine
) -> None:
    approved: dict[str, Any] = {"event_type": "approval.decided", "run_id": str(uuid7()), "event_id": str(uuid7()),
                "payload": {"action_type": "MERGE", "status": "APPROVED", "subject": {"scope": "github"}}}  # fmt: skip
    assert worker_github.is_github_event(approved)
    assert not worker_github.is_github_event({**approved, "payload": {**approved["payload"], "status": "REJECTED"}})
    assert not worker_github.is_github_event(
        {**approved, "payload": {"action_type": "PLAN_APPROVAL", "status": "APPROVED"}}
    )
    assert worker_github.is_github_event({"event_type": "github.webhook.received"})
    broker = build_broker(None, stub=True)
    actors = register_actors(broker)
    await actors.publish(approved)
    await actors.publish({"event_type": "github.pr.requested", "run_id": str(uuid7()), "event_id": str(uuid7())})
    queues = {name: broker.queues[name].qsize() for name in (GITHUB_QUEUE, ORCHESTRATOR_QUEUE)}
    assert queues == {GITHUB_QUEUE: 2, ORCHESTRATOR_QUEUE: 1}, "MERGE-решение — и в GitHub, и в оркестратор"
    # Обработчик очереди: отметка processed_events отсекает повторную доставку.
    run = _run(world, admin)
    event = {"event_type": "github.pr.requested", "event_id": str(uuid7()), "workspace_id": str(world.tenant.workspace_id),
             "run_id": str(run.run), "payload": _job(run)}  # fmt: skip
    rt = Runtime(engine=worker_engine, orchestrator=None, loop=asyncio.get_running_loop(),  # type: ignore[arg-type]
                 dispatch_execution=_noop, github=workflow)  # fmt: skip
    assert await worker_github.handle_event(rt, event) == "opened"
    assert await worker_github.handle_event(rt, event) == "duplicate"
    rt.github = None
    with pytest.raises(RuntimeError, match="не настроен"):
        await worker_github.handle_event(rt, {**event, "event_id": str(uuid7())})


async def _noop(message: dict[str, Any]) -> None:
    del message


def test_branch_ref_on_remote_matches_git(world: World) -> None:
    """Санити двойника: ветки на «GitHub» — настоящие refs bare-репозитория."""
    assert git(world.gh.repos[1001].bare, "rev-parse", "refs/heads/main") == world.gh.branch_sha(
        world.gh.repos[1001], "main"
    )
    assert RepoRef.parse("acme/app").path == "/repos/acme/app"
