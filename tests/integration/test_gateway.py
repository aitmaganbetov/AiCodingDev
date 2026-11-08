"""P2-11: CLI Gateway с PostgreSQL — Execution FSM, артефакты, лимиты, fencing, идемпотентность."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import (
    ConcurrencyLimitReachedError,
    ExecutionGateway,
    StaleFencingTokenError,
    worktree_resolver,
)
from aicd_cli_adapters.models import ExecutionRequest
from aicd_orchestration.worktrees import WorktreeManager
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import create_engine
from aicd_platform.ids import uuid7
from aicd_security.local_runner import LocalProcessRunner
from tests.conftest import init_git_repo
from tests.support import db as testdb
from tests.support.cli import make_request

pytestmark = pytest.mark.db


@dataclass
class Env:
    engine: AsyncEngine
    gateway: ExecutionGateway
    store: FileSystemArtifactStore
    worktrees: WorktreeManager
    repo: Path
    base: str
    workspace_id: uuid.UUID
    other_workspace_id: uuid.UUID
    installation_id: uuid.UUID
    database: testdb.TestDatabase


def _seed(database: testdb.TestDatabase) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    ws, ws2, user, host, inst = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        conn.execute("INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'gw')", (user, f"{user}@gw.test"))
        for w in (ws, ws2):
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('app.user_id', %s, true), set_config('app.workspace_id', %s, true)",
                    (str(user), str(w)),
                )
                conn.execute(
                    "INSERT INTO workspaces (id, name, slug, created_by) VALUES (%s, 'w', %s, %s)",
                    (w, f"gw-{w.hex[-10:]}", user),
                )
                conn.execute(
                    "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'owner')", (w, user)
                )
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        conn.execute(
            "INSERT INTO worker_hosts (id, hostname, kind) VALUES (%s, %s, 'exec')", (host, f"h-{host.hex[-8:]}")
        )
        conn.execute(
            "INSERT INTO cli_installations (id, provider_id, host_id, binary_path, auth_status, max_concurrency)"
            " SELECT %s, id, %s, '/usr/bin/python3', 'AUTHENTICATED', 1 FROM cli_providers WHERE code = 'codex'",
            (inst, host),
        )
    return ws, ws2, inst


@pytest.fixture
async def env(database: testdb.TestDatabase, tmp_path: Path) -> AsyncIterator[Env]:
    ws, ws2, inst = _seed(database)
    engine = create_engine(database.url_for("aicd_worker"))
    store = FileSystemArtifactStore(tmp_path / "artifacts")
    worktrees = WorktreeManager(tmp_path / "worktrees")
    repo = tmp_path / "repo"
    init_git_repo(repo)
    base = subprocess.run(  # noqa: ASYNC221 — фикстура, короткая команда
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    async def diff_provider(request: ExecutionRequest) -> bytes:
        return worktrees.diff(worktrees.path_for(request.workspace.worktree_id), base)

    gateway = ExecutionGateway(engine=engine, store=store, diff_provider=diff_provider)
    yield Env(engine, gateway, store, worktrees, repo, base, ws, ws2, inst, database)
    await engine.dispose()


def _adapter(env: Env, scenario: str) -> FakeCLIExecutor:
    return FakeCLIExecutor(
        scenario=scenario,
        runner=LocalProcessRunner(),
        artifact_sink=env.gateway.store_artifact,
        worktree_path=worktree_resolver(env.worktrees.path_for(uuid7()).parent),
        env_source={"PATH": os.environ["PATH"]},
    )


def _worktree(env: Env) -> uuid.UUID:
    wid = uuid7()
    env.worktrees.create(env.repo, worktree_id=wid, run_id=uuid7(), task_id=None, base_commit_sha=env.base)
    return wid


async def _row(env: Env, execution_id: uuid.UUID, ws: uuid.UUID) -> dict[str, object]:
    async with env.engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(ws)})
        row = (
            (await conn.execute(text("SELECT * FROM cli_executions WHERE id = :id"), {"id": execution_id}))
            .mappings()
            .one()
        )
        usage = (
            await conn.execute(
                text(
                    "SELECT metric_name, metric_value, source FROM usage_metrics WHERE execution_id = :id"
                    " ORDER BY metric_name"
                ),
                {"id": execution_id},
            )
        ).all()
    return {**row, "usage": [tuple(u) for u in usage]}


async def test_success_persists_fsm_artifacts_diff_usage(env: Env) -> None:
    request = make_request(worktree_id=_worktree(env), workspace_id=env.workspace_id)
    events: list[str] = []

    async def on_event(e: object) -> None:
        events.append(e.type)  # type: ignore[attr-defined]

    outcome = await env.gateway.run(
        request, _adapter(env, "success_diff"), installation_id=env.installation_id, on_event=on_event
    )
    row = await _row(env, request.execution_id, env.workspace_id)
    assert row["status"] == "COLLECTED" and row["exit_code"] == 0 and row["failure_category"] is None
    assert row["output_artifact_id"] and row["raw_log_artifact_id"] and row["finished_at"] and row["pid_ref"]
    types = {a.type for a in outcome.result.artifacts}
    assert {"raw_log", "git_diff"} <= types
    diff_ref = next(a for a in outcome.result.artifacts if a.type == "git_diff")
    async with env.engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(env.workspace_id)})
        art = (
            (
                await conn.execute(
                    text("SELECT storage_ref, sha256, kind FROM artifacts WHERE id = :id"),
                    {"id": uuid.UUID(diff_ref.ref.removeprefix("artifact:"))},
                )
            )
            .mappings()
            .one()
        )
    diff = b"".join(env.store.open(art["storage_ref"], expected_sha256=art["sha256"]))
    assert art["kind"] == "git_diff" and b"FAKE_CHANGE.md" in diff, "diff вычислен платформой по worktree"
    usage = row["usage"]
    assert isinstance(usage, list)
    assert ("tokens.total", None, "not_exposed_by_cli") in usage
    assert any(name == "duration.wall" for name, _, _ in usage)
    assert events[0] == "started" and events[-1] == "finished"
    # Повторная доставка того же задания: процесс не запускается, результат тот же.
    again = await env.gateway.run(request, _adapter(env, "nonzero_exit"), installation_id=env.installation_id)
    assert again.reused and again.result.status == "completed"


@pytest.mark.parametrize(
    ("scenario", "status", "failure"),
    [
        ("auth_required", "AUTH_REQUIRED", "AUTH_REQUIRED"),
        ("rate_limited", "RATE_LIMITED", "RATE_LIMITED"),
        ("nonzero_exit", "COLLECTED", "PROCESS_FAILED"),
    ],
)
async def test_failures_recorded(env: Env, scenario: str, status: str, failure: str) -> None:
    request = make_request(worktree_id=_worktree(env), workspace_id=env.workspace_id)
    outcome = await env.gateway.run(request, _adapter(env, scenario), installation_id=env.installation_id)
    row = await _row(env, request.execution_id, env.workspace_id)
    assert row["status"] == status and row["failure_category"] == failure and outcome.result.status == "failed"


async def test_timeout_recorded(env: Env) -> None:
    request = make_request(
        worktree_id=_worktree(env), workspace_id=env.workspace_id, small_limits={"timeout_seconds": 1}
    )
    adapter = _adapter(env, "timeout")
    adapter._grace = 0.5
    await env.gateway.run(request, adapter, installation_id=env.installation_id)
    row = await _row(env, request.execution_id, env.workspace_id)
    assert row["status"] == "TIMED_OUT" and row["failure_category"] == "TIMEOUT"


async def test_concurrency_limit_counts_all_workspaces(env: Env) -> None:
    """max_concurrency=1: активное исполнение ДРУГОГО workspace занимает слот (sys_active_executions)."""
    async with env.engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(env.other_workspace_id)})
        await conn.execute(
            text(
                "INSERT INTO cli_executions (id, workspace_id, task_attempt_id, installation_id, command_profile,"
                " request_hash, timeout_seconds, fencing_token) VALUES (:id, :ws, :a, :i, 'busy', :h, 60, 1)"
            ),
            {"id": uuid7(), "ws": env.other_workspace_id, "a": uuid7(), "i": env.installation_id, "h": "0" * 64},
        )
    request = make_request(worktree_id=_worktree(env), workspace_id=env.workspace_id)
    with pytest.raises(ConcurrencyLimitReachedError):
        await env.gateway.run(request, _adapter(env, "success_diff"), installation_id=env.installation_id)


async def test_db_rejects_invalid_transition_and_stale_token(env: Env) -> None:
    request = make_request(worktree_id=_worktree(env), workspace_id=env.workspace_id)
    token = await env.gateway._admit(request, env.installation_id)
    with pytest.raises(StaleFencingTokenError):
        await env.gateway._transition(request, token + 1000, "STARTING")
    with pytest.raises(Exception, match="invalid execution transition"):
        await env.gateway._transition(request, token, "COLLECTED")


async def test_execution_rows_isolated_by_workspace(env: Env) -> None:
    request = make_request(worktree_id=_worktree(env), workspace_id=env.workspace_id)
    await env.gateway.run(request, _adapter(env, "success_diff"), installation_id=env.installation_id)
    async with env.engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(env.other_workspace_id)})
        seen = (
            await conn.execute(text("SELECT count(*) FROM cli_executions WHERE id = :id"), {"id": request.execution_id})
        ).scalar_one()
    assert seen == 0
