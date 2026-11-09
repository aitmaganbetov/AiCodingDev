"""ST-TEN-04 (ТЗ §20 «cross-tenant access», AC-12, SC-TEN-04): обход ВСЕХ операций OpenAPI с ID другого workspace.

Злоумышленник (Mallory) — аутентифицированный владелец своего workspace. Для каждой операции контракта, которая
адресует tenant-ресурс (параметр пути, заголовок `X-Workspace-Id` или ID в теле), запрос строится из примеров
схемы (Schemathesis, детерминированно), а все ID подменяются настоящими ID ресурсов workspace жертвы (Alice).
Ожидание: `404 not_found`, тело неотличимо от ответа на несуществующий ресурс; ни одного 2xx/3xx и ни одного
403 (403 раскрыл бы существование ресурса). Ответы 400/422 допускаются только как отказ валидации запроса до
проверки tenant (тогда берётся следующий пример); каждая операция обязана хотя бы раз дойти до 404.

Операции без tenant-ресурса перечислены явно (`NOT_TENANT_SCOPED` с причиной): новая операция контракта без
классификации валит тест — классификация новых endpoint'ов обязательна.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import hypothesis
import psycopg
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.approvals import Approvals
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from tests.conftest import UserSession, login
from tests.support import db as testdb
from tests.support import github as ghs
from tests.support import quality as q
from tests.support.contract import OPENAPI_PATH, openapi, validate_response
from tests.support.orch_api import add_task, key, running_run
from tests.support.orchestration import Tenant, connect_admin, lease_task, new_attempt, seed_tenant

pytestmark = [pytest.mark.db, pytest.mark.security]

# Операции без tenant-ресурса — причина обязательна.
NOT_TENANT_SCOPED: dict[str, str] = {
    "login": "public: вход",
    "logout": "своя сессия",
    "reauthenticate": "своя сессия (step-up)",
    "getSession": "своя сессия",
    "healthLive": "public: health",
    "healthReady": "public: health",
    "listWorkspaces": "только свои членства (sys_list_user_workspaces)",
    "createWorkspace": "создаёт новый workspace, ссылок на чужие нет",
    "receiveGitHubWebhook": "public: HMAC, без workspace (SC-GH-01; ST-GH-01)",
}

_PARAM = re.compile(r"\{(\w+)\}")
_ACCEPTABLE_PRE_TENANT = {400, 413, 415, 422}
_MAX_CASES = 12


@dataclass
class Victim:
    tenant: Tenant
    ids: dict[str, str]


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
async def victim(admin: psycopg.Connection, app_engine: AsyncEngine) -> Victim:
    tenant = seed_tenant(admin)
    run = running_run(admin, tenant)
    task = add_task(admin, tenant, run, "victim")
    attempt_task = add_task(admin, tenant, run, "victim-attempt")
    lease_task(admin, attempt_task)
    attempt = new_attempt(admin, tenant, attempt_task)
    commit = "a" * 40
    evidence = q.evidence(admin, tenant, run, commit)
    finding = q.finding(admin, tenant, run)
    artifact = q.artifact(admin, tenant, run)
    async with transaction(app_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
        approval = await Approvals().request(
            conn, workspace_id=tenant.workspace_id, project_id=tenant.project_id, run_id=run, task_id=None,
            action_type="PLAN_APPROVAL", risk_level="medium", subject={"run_id": str(run), "plan_version": 1},
            requested_by_type="system", requested_by=None,
        )  # fmt: skip
    installation = ghs.installation(admin, tenant, 7_000_000 + int(uuid.uuid4().int % 1_000_000), "victimorg")
    verification = uuid7()
    admin.execute(
        "INSERT INTO github_verification_requests (id, workspace_id, kind, params, requested_by)"
        " VALUES (%s, %s, 'installation', '{\"installation_id\": 1}', %s)",
        (verification, tenant.workspace_id, tenant.owner_id),
    )
    provider = admin.execute("SELECT id FROM cli_providers ORDER BY code LIMIT 1").fetchone()
    assert provider is not None
    ids = {
        "workspace_id": str(tenant.workspace_id),
        "project_id": str(tenant.project_id),
        "repository_id": str(tenant.repository_id),
        "run_id": str(run),
        "task_id": str(task),
        "evidence_id": str(evidence),
        "finding_id": str(finding),
        "artifact_id": str(artifact),
        "approval_id": str(approval),
        "user_id": str(tenant.owner_id),
        "github_installation_id": str(installation),
        "verification_id": str(verification),
        "provider_id": str(provider[0]),
        # Ресурсы без простой фабрики: случайный ID внутри ресурса жертвы (родитель — чужой ⇒ 404 по родителю).
        "directive_id": str(uuid7()),
        "control_id": str(uuid7()),
        "probe_request_id": str(uuid7()),
        "source_id": str(uuid7()),
        "pull_request_id": str(uuid7()),
        # Попытка задачи жертвы (getAttemptContextPack): `task_id` пути — тоже ресурс жертвы.
        "attempt_id": str(attempt),
    }
    return Victim(tenant, ids)


@pytest.fixture(scope="module")
async def mallory(app: FastAPI, app_engine: AsyncEngine) -> AsyncIterator[UserSession]:
    session = await login(app, app_engine, ip="203.0.113.66")
    await session.create_workspace()
    yield session
    await session.client.aclose()


def _operations() -> list[tuple[str, str, str, dict[str, Any]]]:
    out = []
    for path, item in openapi()["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                out.append((op["operationId"], method.upper(), path, op))
    return out


def _uses_workspace_header(op: dict[str, Any]) -> bool:
    return any("WorkspaceHeader" in p.get("$ref", "") for p in op.get("parameters", []))


def test_every_operation_is_classified() -> None:
    """Новая операция контракта должна быть либо tenant-scoped (проверяется ниже), либо явно исключена."""
    for op_id, _method, path, op in _operations():
        scoped = bool(_PARAM.search(path)) or _uses_workspace_header(op) or op_id == "createRun"
        assert scoped or op_id in NOT_TENANT_SCOPED, f"{op_id}: не классифицирована для ST-TEN-04"
        assert not (scoped and op_id in NOT_TENANT_SCOPED), f"{op_id}: tenant-scoped, но исключена"


def _cases(operation_id: str) -> list[Any]:
    schema = schemathesis.openapi.from_path(OPENAPI_PATH)
    op = schema.find_operation_by_id(operation_id)
    drawn: list[Any] = []

    @hypothesis.settings(
        max_examples=_MAX_CASES, deadline=None, database=None, suppress_health_check=list(HealthCheck), derandomize=True
    )
    @given(case=op.as_strategy())
    def collect(case: Any) -> None:
        drawn.append(case)

    collect()
    return drawn


def _scoped_operations() -> list[str]:
    return [op_id for op_id, *_ in _operations() if op_id not in NOT_TENANT_SCOPED]


def _strip(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in ("instance", "request_id")}


async def _send(user: UserSession, method: str, url: str, **kwargs: Any) -> Any:
    # SSE-операции при ошибке отвечают сразу; тайм-аут защищает от «зависшего» потока в случае утечки.
    return await asyncio.wait_for(user.client.request(method, url, **kwargs), timeout=20)


@pytest.mark.parametrize("operation_id", _scoped_operations())
async def test_foreign_ids_yield_indistinguishable_404(operation_id: str, victim: Victim, mallory: UserSession) -> None:
    method, path = next((m, p) for o, m, p, _ in _operations() if o == operation_id)
    url_path = _PARAM.sub(lambda m: victim.ids[m.group(1)], path)
    unknown = await mallory.client.get(f"/api/v1/runs/{uuid7()}")
    assert unknown.status_code == 404
    reference = _strip(unknown.json())
    statuses: list[int] = []
    for case in _cases(operation_id):
        headers = {k: v for k, v in (case.headers or {}).items() if k.lower() not in ("cookie", "x-csrf-token")}
        headers["X-Workspace-Id"] = victim.ids["workspace_id"]
        headers["X-CSRF-Token"] = mallory.csrf
        headers["Idempotency-Key"] = key()
        headers["If-Match"] = '"v0"'
        headers.pop("Last-Event-ID", None)
        kwargs: dict[str, Any] = {"headers": headers, "params": case.query or None}
        if method != "GET" and method != "DELETE":
            body = case.body if isinstance(case.body, dict | list | str | int | float | bool) else None
            if isinstance(body, dict):
                for name in ("project_id", "run_id", "task_id", "approval_id", "repository_id"):
                    if name in body:
                        body[name] = victim.ids[name]
            kwargs["json"] = body
        r = await _send(mallory, method, f"/api/v1{url_path}", **kwargs)
        statuses.append(r.status_code)
        assert not 200 <= r.status_code < 400, f"{operation_id}: {r.status_code} на чужом ресурсе: {r.text[:300]}"
        assert r.status_code != 403 or r.json().get("code") == "csrf_failed", (
            f"{operation_id}: 403 раскрывает существование чужого ресурса: {r.text[:300]}"
        )
        assert r.status_code < 500, f"{operation_id}: {r.status_code} {r.text[:300]}"
        if r.status_code == 404:
            validate_response(operation_id, r)
            assert _strip(r.json()) == reference, f"{operation_id}: 404 отличается от несуществующего ресурса"
            return
        assert r.status_code in _ACCEPTABLE_PRE_TENANT, f"{operation_id}: {r.status_code} {r.text[:300]}"
    pytest.fail(f"{operation_id}: ни один пример не дошёл до проверки tenant: {statuses}")


async def test_victim_data_unchanged(victim: Victim, admin: psycopg.Connection) -> None:
    """После всех попыток Mallory ресурсы Alice не изменены."""
    row = admin.execute(
        "SELECT (SELECT status FROM approvals WHERE id = %s), (SELECT status FROM runs WHERE id = %s),"
        " (SELECT count(*) FROM workspace_members WHERE workspace_id = %s),"
        " (SELECT status FROM findings WHERE id = %s)",
        (victim.ids["approval_id"], victim.ids["run_id"], victim.ids["workspace_id"], victim.ids["finding_id"]),
    ).fetchone()
    assert row == ("PENDING", "RUNNING", 1, "OPEN")
