"""UI redesign B1/B2/B7: агрегаты workspace, состояние платформы, профили агентов.

`GET /tasks`, `GET /findings`, `GET /providers/{id}/executions`, `GET /activity`, `GET /system/health`,
`GET /agent-profiles`: ответы соответствуют контракту (validate_response), фильтры и keyset-пагинация по реальным
строкам БД, матрица RBAC, изоляция workspace (чужой `X-Workspace-Id` ⇒ 404, ссылки на чужие run/проекты в фильтрах ⇒
пустая выборка), fuzz по контракту (никогда 5xx), кэш и отдельный лимит `GET /system/health`.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import hypothesis
import psycopg
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api import EXPECTED_SCHEMA_REVISION
from aicd_api.routers import system
from aicd_orchestration.agents.profiles import PROFILES, ROLES, load_template
from aicd_platform.ids import uuid7
from tests.conftest import UserSession, login
from tests.support import db as testdb
from tests.support import quality as q
from tests.support.contract import OPENAPI_PATH, validate_response
from tests.support.orch_api import Team, add_task, make_team, running_run
from tests.support.orchestration import Tenant, connect_admin, lease_task, update

pytestmark = pytest.mark.db

ROLES_ALL = ("owner", "admin", "developer", "reviewer", "viewer")


@dataclass
class World:
    team: Team
    tenant: Tenant
    project2: uuid.UUID
    run1: uuid.UUID
    run2: uuid.UUID
    tasks: dict[str, uuid.UUID]
    attempts: dict[str, uuid.UUID]
    findings: dict[str, uuid.UUID]
    executions: dict[str, uuid.UUID]
    events: dict[str, uuid.UUID]
    codex_id: str
    claude_id: str

    @property
    def ws(self) -> str:
        return self.team.ws

    @property
    def owner(self) -> UserSession:
        return self.team.users["owner"]


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


def _second_project(admin: psycopg.Connection, tenant: Tenant) -> Tenant:
    project, repo = uuid7(), uuid7()
    admin.execute(
        "INSERT INTO projects (id, workspace_id, name, created_by) VALUES (%s, %s, %s, %s)",
        (project, tenant.workspace_id, f"p2-{project.hex[-6:]}", tenant.owner_id),
    )
    admin.execute(
        "INSERT INTO project_settings (project_id, workspace_id) VALUES (%s, %s)", (project, tenant.workspace_id)
    )
    admin.execute(
        "INSERT INTO repositories (id, workspace_id, project_id, provider, local_root_ref)"
        " VALUES (%s, %s, %s, 'local', %s)",
        (repo, tenant.workspace_id, project, f"main/{repo.hex}"),
    )
    return Tenant(tenant.workspace_id, tenant.owner_id, project, repo)


def _running_task(admin: psycopg.Connection, tenant: Tenant, run: uuid.UUID, key: str, role: str, provider: str) -> tuple[uuid.UUID, uuid.UUID]:  # fmt: skip
    task = add_task(admin, tenant, run, key, role=role)
    lease_task(admin, task)
    update(admin, "tasks", task, status="RUNNING")
    attempt = uuid7()
    admin.execute(
        "INSERT INTO task_attempts (id, workspace_id, task_id, attempt_no, provider, fencing_token)"
        " VALUES (%s, %s, %s, 1, %s, 1)",
        (attempt, tenant.workspace_id, task, provider),
    )
    return task, attempt


def _execution(admin: psycopg.Connection, tenant: Tenant, attempt: uuid.UUID, installation: uuid.UUID, path: tuple[str, ...], **final: Any) -> uuid.UUID:  # fmt: skip
    execution = uuid7()
    admin.execute(
        "INSERT INTO cli_executions (id, workspace_id, task_attempt_id, installation_id, command_profile, request_hash,"
        " timeout_seconds, fencing_token) VALUES (%s, %s, %s, %s, 'exec.default', %s, 600, 1)",
        (execution, tenant.workspace_id, attempt, installation, "0" * 64),
    )
    for status in path[:-1]:
        extra = ", started_at = now() - interval '3 seconds'" if status == "STARTING" else ""
        admin.execute(f"UPDATE cli_executions SET status = %s{extra} WHERE id = %s", (status, execution))
    sets = ", ".join(f"{k} = %s" for k in final)
    admin.execute(
        f"UPDATE cli_executions SET status = %s, {sets} WHERE id = %s", (path[-1], *final.values(), execution)
    )
    return execution


def _event(admin: psycopg.Connection, tenant: Tenant, run: uuid.UUID, seq: int, event_type: str, age_s: int, task: uuid.UUID | None = None) -> uuid.UUID:  # fmt: skip
    event = uuid7()
    admin.execute(
        "INSERT INTO run_event_log (workspace_id, run_id, seq, event_id, event_type, task_id, payload, occurred_at,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now() - make_interval(secs => %s),"
        " now() - make_interval(secs => %s))",
        (tenant.workspace_id, run, seq, event, event_type, task, json.dumps({"run_id": str(run)}), age_s, age_s),
    )
    return event


@pytest.fixture(scope="module")
async def world(app: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection) -> AsyncIterator[World]:
    team = await make_team(app, app_engine, admin, ("admin", "developer", "reviewer", "viewer"))
    tenant = team.tenant
    tenant2 = _second_project(admin, tenant)
    run1 = running_run(admin, tenant)
    run2 = running_run(admin, tenant2)
    dev_task, dev_attempt = _running_task(admin, tenant, run1, "dev", "developer", "codex")
    qa_task, qa_attempt = _running_task(admin, tenant2, run2, "qa", "qa", "claude")
    pending = add_task(admin, tenant, run1, "review", role="reviewer")
    critical = q.finding(admin, tenant, run1, severity="critical")
    low = q.finding(admin, tenant2, run2, severity="low")
    manual = uuid7()
    admin.execute(
        "INSERT INTO findings (id, workspace_id, run_id, source, severity, rule_id, title, fingerprint)"
        " VALUES (%s, %s, %s, 'manual', 'medium', 'manual.x', 'ручная находка', %s)",
        (manual, tenant.workspace_id, run1, q.sha256()),
    )
    providers: dict[str, str] = dict(admin.execute("SELECT code, id::text FROM cli_providers").fetchall())
    host, installation = uuid7(), uuid7()
    admin.execute(
        "INSERT INTO worker_hosts (id, hostname, kind) VALUES (%s, %s, 'exec')", (host, f"agg-{host.hex[-8:]}")
    )
    # Установка выключена: не участвует в маршрутизации других тестов, но исполнения на неё ссылаются.
    admin.execute(
        "INSERT INTO cli_installations (id, provider_id, host_id, binary_path, auth_status, max_concurrency, enabled)"
        " VALUES (%s, %s, %s, '/usr/bin/true', 'AUTHENTICATED', 1, false)",
        (installation, providers["codex"], host),
    )
    exited = _execution(
        admin,
        tenant,
        dev_attempt,
        installation,
        ("STARTING", "RUNNING", "EXITED"),
        exit_code=0,
        exited_at=datetime.now(UTC),
    )
    timed_out = _execution(
        admin, tenant, dev_attempt, installation, ("STARTING", "TIMED_OUT"), failure_category="TIMEOUT",
        finished_at=datetime.now(UTC),
    )  # fmt: skip
    events = {
        "run1_state": _event(admin, tenant, run1, 1, "run.state_changed", 30),
        "run1_task": _event(admin, tenant, run1, 2, "task.state_changed", 20, dev_task),
        "run2_gate": _event(admin, tenant, run2, 1, "gate.completed", 10),
    }
    yield World(
        team=team, tenant=tenant, project2=tenant2.project_id, run1=run1, run2=run2,
        tasks={"dev": dev_task, "qa": qa_task, "pending": pending},
        attempts={"dev": dev_attempt, "qa": qa_attempt},
        findings={"critical": critical, "low": low, "manual": manual},
        executions={"exited": exited, "timed_out": timed_out}, events=events,
        codex_id=providers["codex"], claude_id=providers["claude"],
    )  # fmt: skip
    for session in team.users.values():
        await session.client.aclose()


@pytest.fixture(scope="module")
async def mallory(app: FastAPI, app_engine: AsyncEngine) -> AsyncIterator[tuple[UserSession, str]]:
    session = await login(app, app_engine, ip="203.0.113.77")
    ws = await session.create_workspace()
    yield session, ws["id"]
    await session.client.aclose()


async def _get(user: UserSession, ws: str, path: str, op_id: str, **params: Any) -> Any:
    r = await user.client.get(f"/api/v1{path}", params=params or None, headers={"X-Workspace-Id": ws})
    validate_response(op_id, r)
    return r


# --- GET /tasks ----------------------------------------------------------------------------------------------


async def test_list_tasks_running_agents_filters_and_pagination(world: World) -> None:
    r = await _get(world.owner, world.ws, "/tasks", "listTasks", status=["LEASED", "RUNNING"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {str(world.tasks["dev"]), str(world.tasks["qa"])}
    dev = by_id[str(world.tasks["dev"])]
    assert dev["agent_role"] == "developer" and dev["status"] == "RUNNING" and dev["run_id"] == str(world.run1)
    assert dev["project_id"] == str(world.tenant.project_id) and dev["run_status"] == "RUNNING"
    assert dev["current_provider"] == "codex"
    assert dev["current_attempt"]["id"] == str(world.attempts["dev"])
    assert dev["current_attempt"]["status"] == "RUNNING" and dev["current_attempt"]["provider"] == "codex"
    assert dev["current_attempt"]["started_at"] and dev["current_attempt"]["finished_at"] is None

    claude = await _get(world.owner, world.ws, "/tasks", "listTasks", provider="claude")
    assert [i["id"] for i in claude.json()["items"]] == [str(world.tasks["qa"])]
    role = await _get(world.owner, world.ws, "/tasks", "listTasks", agent_role="reviewer")
    pending = role.json()["items"]
    assert [i["id"] for i in pending] == [str(world.tasks["pending"])] and pending[0]["current_attempt"] is None
    by_project = await _get(world.owner, world.ws, "/tasks", "listTasks", project_id=str(world.project2))
    assert [i["id"] for i in by_project.json()["items"]] == [str(world.tasks["qa"])]
    by_run = await _get(world.owner, world.ws, "/tasks", "listTasks", run_id=str(world.run1))
    assert {i["id"] for i in by_run.json()["items"]} == {str(world.tasks["dev"]), str(world.tasks["pending"])}

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await _get(world.owner, world.ws, "/tasks", "listTasks", **params)
        body = page.json()
        seen += [i["id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 3, "keyset-пагинация без пропусков и повторов"
    full = await _get(world.owner, world.ws, "/tasks", "listTasks")
    assert [i["id"] for i in full.json()["items"]] == seen, "порядок — created_at desc"
    first = await _get(world.owner, world.ws, "/tasks", "listTasks", limit=1)
    mismatch = await _get(
        world.owner, world.ws, "/tasks", "listTasks", limit=1, status="RUNNING", cursor=first.json()["next_cursor"]
    )
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "invalid_cursor"


# --- GET /findings -------------------------------------------------------------------------------------------


async def test_list_findings_workspace_wide_filters(world: World) -> None:
    r = await _get(world.owner, world.ws, "/findings", "listFindings")
    assert r.status_code == 200, r.text
    ids = [i["id"] for i in r.json()["items"]]
    assert ids == [str(world.findings[k]) for k in ("critical", "manual", "low")], "severity desc"
    crit = await _get(world.owner, world.ws, "/findings", "listFindings", severity=["critical", "high"])
    assert [i["id"] for i in crit.json()["items"]] == [str(world.findings["critical"])]
    manual = await _get(world.owner, world.ws, "/findings", "listFindings", source="manual")
    assert [i["source"] for i in manual.json()["items"]] == ["manual"]
    proj = await _get(world.owner, world.ws, "/findings", "listFindings", project_id=str(world.project2))
    assert [i["id"] for i in proj.json()["items"]] == [str(world.findings["low"])]
    run = await _get(world.owner, world.ws, "/findings", "listFindings", run_id=str(world.run1), status="OPEN")
    assert {i["id"] for i in run.json()["items"]} == {str(world.findings["critical"]), str(world.findings["manual"])}
    none = await _get(world.owner, world.ws, "/findings", "listFindings", status="ACCEPTED_RISK")
    assert none.json() == {"items": [], "next_cursor": None}
    page1 = await _get(world.owner, world.ws, "/findings", "listFindings", limit=2)
    assert len(page1.json()["items"]) == 2 and page1.json()["next_cursor"]
    page2 = await _get(world.owner, world.ws, "/findings", "listFindings", limit=2, cursor=page1.json()["next_cursor"])
    assert [i["id"] for i in page2.json()["items"]] == [str(world.findings["low"])]
    assert page2.json()["next_cursor"] is None


# --- GET /providers/{id}/executions --------------------------------------------------------------------------


async def test_provider_executions_history_without_output(world: World) -> None:
    url = f"/providers/{world.codex_id}/executions"
    r = await _get(world.owner, world.ws, url, "listProviderExecutions")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [str(world.executions["timed_out"]), str(world.executions["exited"])]
    timed_out, exited = items
    assert timed_out["status"] == "TIMED_OUT" and timed_out["failure_category"] == "TIMEOUT"
    assert exited["status"] == "EXITED" and exited["exit_code"] == 0 and exited["failure_category"] is None
    assert exited["provider"] == "codex" and exited["agent_role"] == "developer"
    assert exited["task_id"] == str(world.tasks["dev"]) and exited["run_id"] == str(world.run1)
    assert exited["task_attempt_id"] == str(world.attempts["dev"]) and exited["attempt_no"] == 1
    assert exited["duration_ms"] is not None and exited["duration_ms"] >= 2000
    assert exited["links"]["task"] == f"/api/v1/tasks/{world.tasks['dev']}"
    for forbidden in (
        "raw_log_artifact_id",
        "command_profile",
        "request_hash",
        "pid_ref",
        "container_ref",
        "worktree_ref",
    ):
        assert forbidden not in exited, f"{forbidden} не отдаётся"
    only = await _get(world.owner, world.ws, url, "listProviderExecutions", status="EXITED")
    assert [i["id"] for i in only.json()["items"]] == [str(world.executions["exited"])]
    other_run = await _get(world.owner, world.ws, url, "listProviderExecutions", run_id=str(world.run2))
    assert other_run.json()["items"] == []
    claude = await _get(world.owner, world.ws, f"/providers/{world.claude_id}/executions", "listProviderExecutions")
    assert claude.json()["items"] == []
    future = await _get(world.owner, world.ws, url, "listProviderExecutions", created_after="2999-01-01T00:00:00Z")
    assert future.json()["items"] == []
    page = await _get(world.owner, world.ws, url, "listProviderExecutions", limit=1)
    nxt = await _get(world.owner, world.ws, url, "listProviderExecutions", limit=1, cursor=page.json()["next_cursor"])
    assert [i["id"] for i in nxt.json()["items"]] == [str(world.executions["exited"])]
    unknown = await _get(world.owner, world.ws, f"/providers/{uuid7()}/executions", "listProviderExecutions")
    assert unknown.status_code == 404


# --- GET /activity -------------------------------------------------------------------------------------------


async def test_workspace_activity_newest_first_with_filters(world: World) -> None:
    r = await _get(world.owner, world.ws, "/activity", "listActivity")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [str(world.events[k]) for k in ("run2_gate", "run1_task", "run1_state")]
    assert items[1]["task_id"] == str(world.tasks["dev"]) and items[1]["seq"] == 2
    typed = await _get(
        world.owner, world.ws, "/activity", "listActivity", event_type=["gate.completed", "run.completed"]
    )
    assert [i["event_type"] for i in typed.json()["items"]] == ["gate.completed"]
    run = await _get(world.owner, world.ws, "/activity", "listActivity", run_id=str(world.run1))
    assert {i["run_id"] for i in run.json()["items"]} == {str(world.run1)}
    proj = await _get(world.owner, world.ws, "/activity", "listActivity", project_id=str(world.project2))
    assert [i["id"] for i in proj.json()["items"]] == [str(world.events["run2_gate"])]
    page = await _get(world.owner, world.ws, "/activity", "listActivity", limit=2)
    rest = await _get(world.owner, world.ws, "/activity", "listActivity", limit=2, cursor=page.json()["next_cursor"])
    assert [i["id"] for i in rest.json()["items"]] == [str(world.events["run1_state"])]
    bad = await _get(world.owner, world.ws, "/activity", "listActivity", event_type="heartbeat")
    assert bad.status_code == 422, "heartbeat не хранится в run_event_log"


# --- GET /agent-profiles -------------------------------------------------------------------------------------


async def test_agent_profiles_from_platform_data(world: World, admin: psycopg.Connection) -> None:
    viewer = world.team.users["viewer"]
    r = await _get(viewer, world.ws, "/agent-profiles", "listAgentProfiles")
    assert r.status_code == 200, r.text
    items = {i["role"]: i for i in r.json()["items"]}
    assert list(items) == list(ROLES)
    dev = items["developer"]
    assert dev["permissions"]["write_scope"] == "worktree" and dev["permissions"]["network"] == "restricted"
    assert dev["template_sha256"] == load_template("developer").sha256
    assert dev["template_ref"] == PROFILES["developer"].template_ref
    assert items["reviewer"]["must_differ_from_author"] is True
    assert items["reviewer"]["permissions"]["write_scope"] == "none"
    assert items["architect"]["prototype_permissions"]["write_scope"] == "worktree"
    assert items["planner"]["independent_check_from"] is None
    catalog: dict[str, str] = dict(
        admin.execute("SELECT role::text, instruction_template_version FROM agent_profiles").fetchall()
    )
    for role, item in items.items():
        expected = catalog.get(role)
        assert (item["catalog"] or {}).get("template_version") == expected, role
    enabled: dict[str, bool] = dict(admin.execute("SELECT code::text, enabled FROM cli_providers").fetchall())
    # Ограничиваем claude ролью reviewer в этом workspace (PATCH /providers/{id}/settings) — список меняется.
    patch = await world.owner.client.patch(
        f"/api/v1/providers/{world.claude_id}/settings",
        json={"allowed_roles": ["reviewer"]},
        headers=world.owner.headers(world.ws, if_match='"v0"'),
    )
    assert patch.status_code == 200, patch.text
    after = {
        i["role"]: i for i in (await _get(viewer, world.ws, "/agent-profiles", "listAgentProfiles")).json()["items"]
    }
    assert after["developer"]["eligible_providers"] == [c for c in ("codex",) if enabled["codex"]]
    assert after["reviewer"]["eligible_providers"] == [c for c in ("claude", "codex") if enabled[c]]


# --- GET /system/health --------------------------------------------------------------------------------------


async def test_system_health_measures_components_and_caches(world: World, app: FastAPI) -> None:
    admin_user = world.team.users["admin"]
    app.state.aicd_system_health = None  # свежая проверка
    r = await _get(admin_user, world.ws, "/system/health", "getSystemHealth")
    assert r.status_code == 200, r.text
    body = r.json()
    components = {c["name"]: c for c in body["components"]}
    assert list(components) == ["api", "database", "redis", "object_storage", "outbox", "workers", "queues", "backup"]
    db = components["database"]
    assert db["status"] == "ok" and db["measurements"]["schema_revision"] == EXPECTED_SCHEMA_REVISION
    assert db["latency_ms"] is not None
    assert components["redis"]["status"] == "ok" and components["object_storage"]["status"] == "ok"
    outbox = components["outbox"]
    assert outbox["status"] in ("ok", "degraded", "down")
    assert isinstance(outbox["measurements"]["pending_events"], int)
    assert body["status"] in ("ok", "degraded"), "критичные компоненты (database, object_storage) работают"
    hosts = {h["hostname"] for h in body["worker_hosts"]}
    assert any(h.startswith("agg-") for h in hosts), "строки worker_hosts из БД"
    assert {q["queue"] for q in body["queues"]} == set(system.QUEUES)
    assert components["queues"]["status"] in ("ok", "degraded")
    assert body["backups"] is None, "файл статуса бэкапа в тестовом api не смонтирован"
    assert components["backup"]["status"] in ("unknown", "ok", "degraded")
    again = await _get(world.owner, world.ws, "/system/health", "getSystemHealth")
    assert again.json()["checked_at"] == body["checked_at"], "повтор в пределах TTL — из кэша"
    app.state.aicd_system_health.expires = float("-inf")
    fresh = await _get(world.owner, world.ws, "/system/health", "getSystemHealth")
    assert fresh.json()["checked_at"] != body["checked_at"]


async def test_backup_component_reads_status_file(app_engine: AsyncEngine, tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    now = int(time.time())
    status.write_text(
        json.dumps(
            {
                "components": {
                    "postgres_base": {"last_status": "success", "last_success_unixtime": now - 3600},
                    "artifacts": {"last_status": "failure", "last_success_unixtime": now - 3600},
                    "unknown_component": {"last_status": "success"},
                }
            }
        )
    )
    state: Any = SimpleNamespace(settings=SimpleNamespace(backup_status_file=status), engine=app_engine)
    component, entries = await system.check_backup(state)
    assert entries is not None
    assert [e["component"] for e in entries] == ["postgres_base", "artifacts"]
    assert entries[0]["last_status"] == "success" and entries[0]["stale"] is False
    assert entries[1]["last_status"] == "failure"
    assert component.status == "degraded" and component.detail and "artifacts" in component.detail
    status.write_text(
        json.dumps({"components": {"postgres_base": {"last_status": "success", "last_success_unixtime": now}}})
    )
    ok, _ = await system.check_backup(state)
    wal_failing = ok.measurements.get("wal_last_failed_at") is not None and (
        ok.measurements.get("wal_last_archived_at") is None
        or ok.measurements["wal_last_failed_at"] > ok.measurements["wal_last_archived_at"]
    )
    assert ok.status == ("degraded" if wal_failing else "ok")


async def test_system_health_rate_limited_per_user(world: World, app: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection) -> None:  # fmt: skip
    session = await login(app, app_engine)
    admin.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'admin')",
        (world.tenant.workspace_id, uuid.UUID(session.user_id)),
    )
    try:
        statuses = []
        for _ in range(system.SYSTEM_HEALTH_PER_MINUTE + 1):
            r = await session.client.get("/api/v1/system/health", headers={"X-Workspace-Id": world.ws})
            statuses.append(r.status_code)
            if r.status_code == 429:
                validate_response("getSystemHealth", r)
                assert r.headers.get("Retry-After")
                break
        assert statuses[-1] == 429 and statuses.count(200) == system.SYSTEM_HEALTH_PER_MINUTE
    finally:
        await session.client.aclose()


# --- RBAC и изоляция ------------------------------------------------------------------------------------------

OPERATIONS: dict[str, tuple[str, frozenset[str]]] = {
    "listTasks": ("/tasks", frozenset(ROLES_ALL)),
    "listFindings": ("/findings", frozenset(ROLES_ALL)),
    "listActivity": ("/activity", frozenset(ROLES_ALL)),
    "listAgentProfiles": ("/agent-profiles", frozenset(ROLES_ALL)),
    "getSystemHealth": ("/system/health", frozenset({"owner", "admin"})),
}


def _path(world: World, op_id: str) -> str:
    if op_id == "listProviderExecutions":
        return f"/providers/{world.codex_id}/executions"
    return OPERATIONS[op_id][0]


@pytest.mark.parametrize("op_id", [*OPERATIONS, "listProviderExecutions"])
async def test_rbac_matrix(world: World, op_id: str) -> None:
    allowed = OPERATIONS[op_id][1] if op_id in OPERATIONS else frozenset(ROLES_ALL)
    for role in ROLES_ALL:
        r = await _get(world.team.users[role], world.ws, _path(world, op_id), op_id)
        expected = 200 if role in allowed else 403
        assert r.status_code == expected, f"{op_id} {role}: {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("op_id", [*OPERATIONS, "listProviderExecutions"])
async def test_foreign_workspace_is_404_and_foreign_filters_are_empty(
    world: World, mallory: tuple[UserSession, str], op_id: str
) -> None:
    user, own_ws = mallory
    r = await _get(user, world.ws, _path(world, op_id), op_id)
    assert r.status_code == 404, f"{op_id}: чужой workspace неотличим от несуществующего"
    missing = await _get(user, str(uuid7()), _path(world, op_id), op_id)
    assert {k: v for k, v in r.json().items() if k not in ("instance", "request_id")} == {
        k: v for k, v in missing.json().items() if k not in ("instance", "request_id")
    }
    if op_id in ("listTasks", "listFindings", "listActivity", "listProviderExecutions"):
        leaked = await _get(user, own_ws, _path(world, op_id), op_id, run_id=str(world.run1))
        assert leaked.status_code == 200 and leaked.json()["items"] == [], f"{op_id}: строки чужого run не видны"
    if op_id in ("listTasks", "listFindings", "listActivity"):
        leaked = await _get(user, own_ws, _path(world, op_id), op_id, project_id=str(world.tenant.project_id))
        assert leaked.status_code == 200 and leaked.json()["items"] == []


# --- Fuzz по контракту -----------------------------------------------------------------------------------------


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


@pytest.mark.parametrize("op_id", [*OPERATIONS, "listProviderExecutions"])
async def test_contract_fuzz_never_5xx_and_responses_conform(world: World, op_id: str) -> None:
    user = world.team.users["admin"] if op_id == "getSystemHealth" else world.owner
    for n, case in enumerate(_cases(op_id, 10 if op_id == "getSystemHealth" else 25)):
        headers = {k: v for k, v in (case.headers or {}).items() if k.lower() not in ("cookie", "x-csrf-token")}
        headers["X-Workspace-Id"] = world.ws
        path = case.formatted_path
        if op_id == "listProviderExecutions" and n % 2:  # половина примеров — существующий провайдер
            path = f"/providers/{world.codex_id}/executions"
        r = await user.client.get(f"/api/v1{path}", headers=headers, params=case.query or None)
        assert r.status_code < 500, f"{op_id} {path} {case.query!r}: {r.status_code} {r.text}"
        validate_response(op_id, r)
