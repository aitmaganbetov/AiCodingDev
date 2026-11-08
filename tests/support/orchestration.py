"""Фабрики строк оркестрации (миграция 0003) для тестов БД.

Работают через соединение суперпользователя (RLS не действует, триггеры — действуют):
`connect_admin(database)` выставляет `search_path = aicd`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from aicd_platform.ids import uuid7
from tests.support import db as testdb

STRICT_PROFILE_ID = uuid.UUID("01a1c000-0000-7000-8000-000000000002")


def connect_admin(database: testdb.TestDatabase, *, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(database.superuser_url, autocommit=autocommit, options="-c search_path=aicd")


@dataclass(frozen=True)
class Tenant:
    workspace_id: uuid.UUID
    owner_id: uuid.UUID
    project_id: uuid.UUID
    repository_id: uuid.UUID


def seed_tenant(conn: psycopg.Connection, *, owner_id: uuid.UUID | None = None) -> Tenant:
    """Пользователь-владелец (если не передан), workspace, проект, настройки, репозиторий."""
    owner = owner_id or uuid7()
    ws, project, repo = uuid7(), uuid7(), uuid7()
    if owner_id is None:
        conn.execute(
            "INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'owner')", (owner, f"{owner}@orch.test")
        )
    conn.execute(
        "INSERT INTO workspaces (id, name, slug, created_by) VALUES (%s, 'w', %s, %s)", (ws, f"o-{ws.hex[-12:]}", owner)
    )
    conn.execute("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'owner')", (ws, owner))
    conn.execute(
        "INSERT INTO projects (id, workspace_id, name, created_by) VALUES (%s, %s, 'p', %s)", (project, ws, owner)
    )
    conn.execute("INSERT INTO project_settings (project_id, workspace_id) VALUES (%s, %s)", (project, ws))
    conn.execute(
        "INSERT INTO repositories (id, workspace_id, project_id, provider, local_root_ref)"
        " VALUES (%s, %s, %s, 'local', %s)",
        (repo, ws, project, f"main/{repo.hex}"),
    )
    return Tenant(ws, owner, project, repo)


def add_user(conn: psycopg.Connection, tenant: Tenant, role: str) -> uuid.UUID:
    user = uuid7()
    conn.execute("INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'u')", (user, f"{user}@orch.test"))
    conn.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, %s)",
        (tenant.workspace_id, user, role),
    )
    return user


def update(conn: psycopg.Connection, table: str, row_id: uuid.UUID, *, bump: bool = True, **cols: Any) -> None:
    """UPDATE по id; для агрегатов с version — с инкрементом (ADR-0007)."""
    sets = ", ".join(f"{c} = %s" for c in cols) + (", version = version + 1" if bump else "")
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = %s", (*cols.values(), row_id))


def new_run(conn: psycopg.Connection, tenant: Tenant) -> uuid.UUID:
    run = uuid7()
    conn.execute(
        "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
        " quality_profile, quality_profile_id, documentation_mode)"
        " VALUES (%s, %s, %s, %s, %s, 'DEVELOP', 'objective', 'strict', %s, 'SMART')",
        (run, tenant.workspace_id, tenant.project_id, tenant.repository_id, tenant.owner_id, STRICT_PROFILE_ID),
    )
    return run


def drive_run(conn: psycopg.Connection, run: uuid.UUID, *statuses: str) -> None:
    for status in statuses:
        extra: dict[str, Any] = {}
        if status == "PLANNED":
            extra["plan_version"] = 1
        update(conn, "runs", run, status=status, **extra)


def new_task(conn: psycopg.Connection, tenant: Tenant, run: uuid.UUID, key: str = "t1") -> uuid.UUID:
    task = uuid7()
    conn.execute(
        "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title, instructions,"
        " complexity, risk_level) VALUES (%s, %s, %s, 1, %s, 'developer', 'title', 'do it', 2, 'low')",
        (task, tenant.workspace_id, run, key),
    )
    return task


def lease_task(conn: psycopg.Connection, task: uuid.UUID, token: int = 1) -> None:
    update(conn, "tasks", task, status="READY")
    update(conn, "tasks", task, status="LEASED", lease_fencing_token=token)


def new_attempt(conn: psycopg.Connection, tenant: Tenant, task: uuid.UUID, token: int = 1) -> uuid.UUID:
    attempt = uuid7()
    conn.execute(
        "INSERT INTO task_attempts (id, workspace_id, task_id, attempt_no, provider, fencing_token)"
        " VALUES (%s, %s, %s, 1, 'codex', %s)",
        (attempt, tenant.workspace_id, task, token),
    )
    return attempt


def seed_task_attempt(database: testdb.TestDatabase, workspace_id: uuid.UUID, owner_id: uuid.UUID) -> uuid.UUID:
    """run (RUNNING) → задача (LEASED) → попытка в существующем workspace; возвращает id попытки."""
    with connect_admin(database) as conn:
        project, repo = uuid7(), uuid7()
        conn.execute(
            "INSERT INTO projects (id, workspace_id, name, created_by) VALUES (%s, %s, %s, %s)",
            (project, workspace_id, f"p-{project.hex[-8:]}", owner_id),
        )
        conn.execute(
            "INSERT INTO repositories (id, workspace_id, project_id, provider, local_root_ref)"
            " VALUES (%s, %s, %s, 'local', %s)",
            (repo, workspace_id, project, f"main/{repo.hex}"),
        )
        tenant = Tenant(workspace_id, owner_id, project, repo)
        run = new_run(conn, tenant)
        drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
        task = new_task(conn, tenant, run)
        lease_task(conn, task)
        return new_attempt(conn, tenant, task)
