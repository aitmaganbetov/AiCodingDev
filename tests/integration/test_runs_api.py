"""P3-08: Runs API — идемпотентность, ETag/If-Match, RBAC/tenant-изоляция, соответствие контракту.

Плюс fuzz по контракту (Schemathesis): для операций runs/tasks сгенерированные запросы никогда не дают 5xx,
а все ответы соответствуют `packages/contracts/openapi.yaml`.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import uuid
from pathlib import Path
from typing import Any

import hypothesis
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from tests.conftest import UserSession, init_git_repo, login
from tests.support.contract import OPENAPI_PATH, validate_response
from tests.support.orchestration import connect_admin, drive_run
from tests.support.phase3 import Slice, create_run, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

RUN_OPERATIONS = ("createRun", "listRuns", "getRun", "getRunTasks", "pauseRun", "resumeRun", "cancelRun", "getTask",
                  "listTaskAttempts", "createFixRun")  # fmt: skip


async def test_create_run_is_idempotent_and_key_reuse_with_other_body_is_rejected(world: Slice) -> None:
    key = secrets.token_hex(16)
    body = {"project_id": world.project, "mode": "DEVELOP", "objective": "Идемпотентный запуск"}
    first = await world.user.client.post("/api/v1/runs", json=body, headers=world.user.headers(idem_key=key))
    validate_response("createRun", first)
    assert first.status_code == 201 and first.headers["ETag"] == '"v0"'
    replay = await world.user.client.post("/api/v1/runs", json=body, headers=world.user.headers(idem_key=key))
    assert replay.status_code == 201 and replay.json() == first.json()
    assert replay.headers.get("Idempotency-Replayed") == "true"
    other = await world.user.client.post(
        "/api/v1/runs", json={**body, "objective": "Другое"}, headers=world.user.headers(idem_key=key)
    )
    assert other.status_code == 422, other.text
    missing = await world.user.client.post("/api/v1/runs", json=body, headers=world.user.headers())
    assert missing.status_code == 400, "POST-команда без Idempotency-Key"
    async with transaction(world.worker, DbContext(actor_type="system")) as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM outbox_events WHERE event_type = 'run.created' AND aggregate_id = :r"),
                {"r": first.json()["run_id"]},
            )
        ).scalar_one()
    assert count == 1, "повтор запроса не порождает второе событие run.created"


def _git_env(repo: Path) -> dict[str, str]:
    return {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo), "PATH": os.environ["PATH"]}


def _init_without_commits(repo: Path) -> None:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_git_env(repo))


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=_git_env(repo)
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    env = _git_env(repo)
    (repo / f"{message}.txt").write_text(message)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", message],
        check=True,
        env=env,
    )
    return _head(repo)


async def test_run_starts_from_the_current_head_not_from_the_moment_of_attach(
    user: UserSession, repo_root: Path
) -> None:
    """База run — HEAD рабочей копии сейчас.

    `last_synced_commit_sha` пишется при привязке и ничем не обновляется, поэтому иначе run навсегда
    стартовал бы со старого коммита, а репозиторий, привязанный пустым (новый проект: `git init` и привязка
    до первого коммита), остался бы неработоспособным — отвязать его из UI нечем.
    """
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo = repo_root / f"head-{secrets.token_hex(3)}"
    init_git_repo(repo)
    attached = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"provider": "local", "local_path": f"main/{repo.name}"},
        headers=user.headers(idem=True),
    )
    assert attached.status_code == 201, attached.text
    at_attach = _head(repo)

    moved = _commit(repo, "after-attach")
    assert moved != at_attach
    created = await user.client.post(
        "/api/v1/runs",
        json={"project_id": project["id"], "mode": "DEVELOP", "objective": "Работа с текущего HEAD"},
        headers=user.headers(idem=True),
    )
    validate_response("createRun", created)
    assert created.status_code == 201, created.text
    run = await user.client.get(f"/api/v1/runs/{created.json()['run_id']}")
    validate_response("getRun", run)
    assert run.json()["base_commit_sha"] == moved, "run взял коммит на момент запуска, а не на момент привязки"


async def test_repository_attached_before_the_first_commit_becomes_runnable_after_it(
    user: UserSession, repo_root: Path
) -> None:
    """Новый проект: `git init`, привязка, первый коммит — и run должен стартовать."""
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo = repo_root / f"empty-{secrets.token_hex(3)}"
    _init_without_commits(repo)
    attached = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"provider": "local", "local_path": f"main/{repo.name}"},
        headers=user.headers(idem=True),
    )
    validate_response("attachRepository", attached)
    assert attached.status_code == 201, "пустой репозиторий привязывается"

    first = _commit(repo, "first")
    created = await user.client.post(
        "/api/v1/runs",
        json={"project_id": project["id"], "mode": "DEVELOP", "objective": "Первый запуск"},
        headers=user.headers(idem=True),
    )
    assert created.status_code == 201, created.text
    run = await user.client.get(f"/api/v1/runs/{created.json()['run_id']}")
    assert run.json()["base_commit_sha"] == first


async def test_get_run_etag_and_commands_with_if_match(world: Slice) -> None:
    run_id = await create_run(world)
    r = await world.user.client.get(f"/api/v1/runs/{run_id}")
    validate_response("getRun", r)
    tag = r.headers["ETag"]
    not_modified = await world.user.client.get(f"/api/v1/runs/{run_id}", headers={"If-None-Match": tag})
    assert not_modified.status_code == 304
    stale = await world.user.client.post(
        f"/api/v1/runs/{run_id}/cancel", json={}, headers=world.user.headers(idem=True, if_match='"v999"')
    )
    validate_response("cancelRun", stale)
    assert stale.status_code == 412 and stale.headers["ETag"] == tag
    ok = await world.user.client.post(
        f"/api/v1/runs/{run_id}/cancel", json={"reason": "тест"}, headers=world.user.headers(idem=True, if_match=tag)
    )
    validate_response("cancelRun", ok)
    assert ok.status_code == 202, ok.text
    after = await world.user.client.get(f"/api/v1/runs/{run_id}")
    assert after.json()["status"] == "CANCELLED" and after.headers["ETag"] != tag
    again = await world.user.client.post(
        f"/api/v1/runs/{run_id}/resume", json={}, headers=world.user.headers(idem=True)
    )
    validate_response("resumeRun", again)
    assert again.status_code == 409, "терминальный run не возобновляется"


async def test_runs_are_tenant_isolated_and_role_checked(
    app: FastAPI, app_engine: AsyncEngine, world: Slice, database: Any
) -> None:
    run_id = await create_run(world)
    outsider = await login(app, app_engine)
    for path in (f"/api/v1/runs/{run_id}", f"/api/v1/runs/{run_id}/tasks"):
        r = await outsider.client.get(path)
        assert r.status_code == 404, f"{path}: чужой run неотличим от несуществующего"
    r = await outsider.client.post(f"/api/v1/runs/{run_id}/pause", json={}, headers=outsider.headers(idem=True))
    assert r.status_code == 404
    viewer = await login(app, app_engine)
    added = await world.user.client.post(
        f"/api/v1/workspaces/{world.ws}/members",
        json={"email": viewer.email, "role": "viewer"},
        headers=world.user.headers(world.ws, idem=True),
    )
    assert added.status_code == 201, added.text
    seen = await viewer.client.get(f"/api/v1/runs/{run_id}")
    assert seen.status_code == 200
    denied = await viewer.client.post(
        "/api/v1/runs",
        json={"project_id": world.project, "mode": "DEVELOP", "objective": "нельзя"},
        headers=viewer.headers(idem=True),
    )
    validate_response("createRun", denied)
    assert denied.status_code == 403
    denied_cmd = await viewer.client.post(f"/api/v1/runs/{run_id}/pause", json={}, headers=viewer.headers(idem=True))
    assert denied_cmd.status_code == 403


def _cases(operation_id: str, count: int) -> list[Any]:
    schema = schemathesis.openapi.from_path(OPENAPI_PATH)
    op = schema.find_operation_by_id(operation_id)
    drawn: list[Any] = []

    @hypothesis.settings(
        max_examples=count,
        deadline=None,
        database=None,
        suppress_health_check=list(HealthCheck),
        derandomize=True,
    )
    @given(case=op.as_strategy())
    def collect(case: Any) -> None:
        drawn.append(case)

    collect()
    return drawn


async def _send(user: UserSession, case: Any, ws: str) -> Any:
    path = case.formatted_path
    headers = {k: v for k, v in (case.headers or {}).items() if k.lower() not in ("cookie", "x-csrf-token")}
    headers.update({k: v for k, v in user.headers(ws).items() if k.lower() in ("x-csrf-token", "x-workspace-id")})
    if case.method.upper() == "POST":
        headers.setdefault("Idempotency-Key", secrets.token_hex(16))
    kwargs: dict[str, Any] = {"headers": headers, "params": case.query or None}
    if isinstance(case.body, (dict, list)) and case.method.upper() != "GET":  # NotSet — тела нет
        kwargs["json"] = case.body
    return await user.client.request(case.method.upper(), f"/api/v1{path}", **kwargs)


@pytest.mark.parametrize("operation_id", RUN_OPERATIONS)
async def test_contract_fuzz_never_5xx_and_responses_conform(world: Slice, operation_id: str) -> None:
    for case in _cases(operation_id, 25):
        r = await _send(world.user, case, world.ws)
        assert r.status_code < 500, f"{operation_id} {case.formatted_path} {case.body!r}: {r.status_code} {r.text}"
        if r.status_code not in (304,):
            validate_response(operation_id, r)


async def test_fix_run_is_created_from_failures_of_a_failed_run(
    app: FastAPI, app_engine: AsyncEngine, world: Slice, database: Any
) -> None:
    """«Передать ИИ на исправление»: только для FAILED run с находками; цель составляет платформа."""
    run_id = await create_run(world)
    url = f"/api/v1/runs/{run_id}/fix"

    early = await world.user.client.post(url, json={}, headers=world.user.headers(idem=True))
    validate_response("createFixRun", early)
    assert early.status_code == 409, "run ещё не завершился отказом"

    with connect_admin(database) as conn:
        drive_run(conn, uuid.UUID(run_id), "DISCOVERY", "PLANNED", "RUNNING")
        conn.execute(
            "UPDATE runs SET status = 'FAILED', finished_at = now(), version = version + 1 WHERE id = %s", (run_id,)
        )
    nothing = await world.user.client.post(url, json={}, headers=world.user.headers(idem=True))
    assert nothing.status_code == 409, "нет проваленных проверок и находок — исправлять нечего"

    with connect_admin(database) as conn:
        conn.execute(
            "INSERT INTO findings (id, workspace_id, run_id, source, severity, rule_id, title, file_path, line,"
            " fingerprint) VALUES (%s, %s, %s, 'manual', 'high', 'CKV_DOCKER_3', 'Container runs as root',"
            " 'backend/Dockerfile', 7, %s)",
            (uuid7(), world.ws, run_id, "a" * 64),
        )
    outsider = await login(app, app_engine)
    hidden = await outsider.client.post(url, json={}, headers=outsider.headers(idem=True))
    assert hidden.status_code == 404, "чужой run неотличим от несуществующего"

    key = secrets.token_hex(16)
    created = await world.user.client.post(
        url, json={"note": "сначала Dockerfile"}, headers=world.user.headers(idem_key=key)
    )
    validate_response("createFixRun", created)
    assert created.status_code == 201, created.text
    replay = await world.user.client.post(
        url, json={"note": "сначала Dockerfile"}, headers=world.user.headers(idem_key=key)
    )
    assert replay.json() == created.json(), "повтор с тем же ключом не создаёт второй run"

    fix = (await world.user.client.get(f"/api/v1/runs/{created.json()['run_id']}")).json()
    source = (await world.user.client.get(f"/api/v1/runs/{run_id}")).json()
    assert fix["mode"] == "DEVELOP" and fix["project_id"] == source["project_id"]
    assert fix["quality_profile"] == source["quality_profile"]
    objective = fix["objective"]
    assert run_id in objective and source["objective"] in objective and "сначала Dockerfile" in objective
    assert "[high] CKV_DOCKER_3 — Container runs as root (backend/Dockerfile:7)" in objective
