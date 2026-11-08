"""P3-08: Runs API — идемпотентность, ETag/If-Match, RBAC/tenant-изоляция, соответствие контракту.

Плюс fuzz по контракту (Schemathesis): для операций runs/tasks сгенерированные запросы никогда не дают 5xx,
а все ответы соответствуют `packages/contracts/openapi.yaml`.
"""

from __future__ import annotations

import secrets
from typing import Any

import hypothesis
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.db import DbContext, transaction
from tests.conftest import UserSession, login
from tests.support.contract import OPENAPI_PATH, validate_response
from tests.support.phase3 import Slice, create_run, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

RUN_OPERATIONS = ("createRun", "listRuns", "getRun", "getRunTasks", "pauseRun", "resumeRun", "cancelRun", "getTask",
                  "listTaskAttempts")  # fmt: skip


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
