"""P1-08/P1-16: изоляция арендаторов на уровне БД (DATABASE_DESIGN §19.7, AC-12)."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from aicd_platform.ids import uuid7
from tests.support import db as testdb

pytestmark = pytest.mark.db


def _ctx(conn: psycopg.Connection, user: uuid.UUID | None, ws: uuid.UUID | None) -> None:
    conn.execute(
        "SELECT set_config('app.user_id', %s, true), set_config('app.workspace_id', %s, true)",
        (str(user) if user else "", str(ws) if ws else ""),
    )


@pytest.fixture(scope="module")
def tenants(database: testdb.TestDatabase) -> dict[str, uuid.UUID]:
    ids = {k: uuid7() for k in ("ua", "ub", "wa", "wb", "pa", "pb")}
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        for u in ("ua", "ub"):
            conn.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'x')", (ids[u], f"{ids[u]}@rls.test")
            )
        for u, w, p in (("ua", "wa", "pa"), ("ub", "wb", "pb")):
            with conn.transaction():
                _ctx(conn, ids[u], ids[w])
                conn.execute(
                    "INSERT INTO workspaces (id, name, slug, created_by) VALUES (%s, 'w', %s, %s)",
                    (ids[w], f"s-{ids[w].hex[-12:]}", ids[u]),
                )
                conn.execute(
                    "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'owner')",
                    (ids[w], ids[u]),
                )
                conn.execute(
                    "INSERT INTO projects (id, workspace_id, name, created_by) VALUES (%s, %s, 'p', %s)",
                    (ids[p], ids[w], ids[u]),
                )
                conn.execute(
                    "INSERT INTO project_settings (project_id, workspace_id) VALUES (%s, %s)", (ids[p], ids[w])
                )
                conn.execute(
                    "INSERT INTO repositories (id, workspace_id, project_id, provider, local_root_ref)"
                    " VALUES (%s, %s, %s, 'local', %s)",
                    (uuid7(), ids[w], ids[p], f"main/{ids[p].hex}"),
                )
                conn.execute(
                    "INSERT INTO artifacts (id, workspace_id, project_id, kind, storage_ref, sha256, size_bytes, mime_type)"
                    " VALUES (%s, %s, %s, 'other', %s, %s, 1, 'text/plain')",
                    (uuid7(), ids[w], ids[p], f"file://artifacts/{ids[w]}/x", "0" * 64),
                )
                conn.execute(
                    "INSERT INTO audit_events (id, workspace_id, actor_type, actor_id, action, resource_type, resource_id)"
                    " VALUES (%s, %s, 'user', %s, 'test.seed', 'project', %s)",
                    (uuid7(), ids[w], ids[u], ids[p]),
                )
    return ids


TENANT_TABLES = [
    "workspaces",
    "workspace_members",
    "projects",
    "project_settings",
    "repositories",
    "artifacts",
    "audit_events",
]


@pytest.mark.parametrize("role", ["aicd_app", "aicd_worker"])
@pytest.mark.parametrize("table", TENANT_TABLES)
def test_context_sees_only_own_workspace(
    database: testdb.TestDatabase, tenants: dict[str, uuid.UUID], role: str, table: str
) -> None:
    col = "id" if table == "workspaces" else "workspace_id"
    with psycopg.connect(database.url_for(role)) as conn:
        if role == "aicd_worker" and table == "audit_events":
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT 1 FROM {table}")  # воркер аудит только пишет
            return
        with conn.transaction():
            _ctx(conn, tenants["ua"], tenants["wa"])
            seen = {r[0] for r in conn.execute(f"SELECT {col} FROM {table}").fetchall()}
        assert seen == {tenants["wa"]}
        with conn.transaction():
            _ctx(conn, None, None)
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), "без контекста — fail-closed"


@pytest.mark.parametrize("table", ["projects", "repositories", "workspace_members", "artifacts"])
def test_cannot_modify_other_tenant(database: testdb.TestDatabase, tenants: dict[str, uuid.UUID], table: str) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, tenants["ua"], tenants["wa"])
        if table != "artifacts":
            res = conn.execute(
                f"UPDATE {table} SET workspace_id = workspace_id WHERE workspace_id = %s", (tenants["wb"],)
            )
            assert res.rowcount == 0
        if table in ("workspace_members",):
            res = conn.execute(f"DELETE FROM {table} WHERE workspace_id = %s", (tenants["wb"],))
            assert res.rowcount == 0


def test_insert_into_foreign_workspace_rejected(database: testdb.TestDatabase, tenants: dict[str, uuid.UUID]) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            _ctx(conn, tenants["ua"], tenants["wa"])
            conn.execute(
                "INSERT INTO projects (id, workspace_id, name, created_by) VALUES (%s, %s, 'evil', %s)",
                (uuid7(), tenants["wb"], tenants["ua"]),
            )


def test_composite_fk_blocks_cross_tenant_reference(
    database: testdb.TestDatabase, tenants: dict[str, uuid.UUID]
) -> None:
    """Даже владелец схемы не может связать репозиторий workspace A с проектом B (ADR-0008)."""
    with psycopg.connect(database.superuser_url) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO aicd.repositories (id, workspace_id, project_id, provider, local_root_ref)"
            " VALUES (%s, %s, %s, 'local', 'main/cross')",
            (uuid7(), tenants["wa"], tenants["pb"]),
        )


def test_security_definer_functions_only_reveal_own_memberships(
    database: testdb.TestDatabase, tenants: dict[str, uuid.UUID]
) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, tenants["ua"], None)
        ws = {r[0] for r in conn.execute("SELECT workspace_id FROM sys_list_user_workspaces()").fetchall()}
        assert ws == {tenants["wa"]}
        assert conn.execute("SELECT sys_membership_role(%s)", (tenants["wb"],)).fetchone() == (None,)
        assert conn.execute("SELECT sys_resolve_project_workspace(%s)", (tenants["pb"],)).fetchone() == (None,)
        assert conn.execute("SELECT sys_resolve_project_workspace(%s)", (tenants["pa"],)).fetchone() == (tenants["wa"],)


def test_last_owner_invariant(database: testdb.TestDatabase, tenants: dict[str, uuid.UUID]) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            _ctx(conn, tenants["ua"], tenants["wa"])
            conn.execute("DELETE FROM workspace_members WHERE workspace_id = %s", (tenants["wa"],))
