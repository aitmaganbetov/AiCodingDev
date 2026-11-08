"""Миграция 0014 (Phase 9): step-up (`user_sessions.reauthenticated_at`, `revoke_reason='rotated'`),
nullable `tasks.timeout_seconds` (OQ-45), `github_verification_requests` (OQ-47 (2)) — обратимость, offline SQL,
перенос данных, RLS и права."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator
from contextlib import redirect_stdout

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from aicd_platform.ids import uuid7
from tests.support import db as testdb
from tests.support.orchestration import new_run, seed_tenant

pytestmark = [pytest.mark.db, pytest.mark.security]


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig14")
    yield db
    testdb.drop_database(db)


def _column(conn: psycopg.Connection, table: str, column: str) -> tuple[str, str | None] | None:
    row = conn.execute(
        "SELECT is_nullable, column_default FROM information_schema.columns"
        " WHERE table_schema = 'aicd' AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    return (row[0], row[1]) if row else None


def _task(conn: psycopg.Connection, ws: uuid.UUID, run: uuid.UUID, key: str, timeout: int | None) -> uuid.UUID:
    tid = uuid7()
    conn.execute(
        "INSERT INTO aicd.tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title, instructions,"
        " complexity, risk_level, timeout_seconds) VALUES (%s, %s, %s, 1, %s, 'developer', 't', 'i', 1, 'low', %s)",
        (tid, ws, run, key, timeout),
    )
    return tid


def test_0014_upgrade_downgrade_upgrade_with_data(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "0013")
    with psycopg.connect(scratch_db.superuser_url, autocommit=True, options="-c search_path=aicd") as conn:
        assert _column(conn, "tasks", "timeout_seconds") == ("NO", "1800")
        tenant = seed_tenant(conn)
        run = new_run(conn, tenant)
        implicit = _task(conn, tenant.workspace_id, run, "implicit", 1800)
        explicit = _task(conn, tenant.workspace_id, run, "explicit", 900)
    testdb.alembic(scratch_db, "upgrade", "0014")
    with psycopg.connect(scratch_db.superuser_url, autocommit=True, options="-c search_path=aicd") as conn:
        assert _column(conn, "tasks", "timeout_seconds") == ("YES", None)
        assert _column(conn, "user_sessions", "reauthenticated_at") is not None
        got: dict[uuid.UUID, int | None] = dict(conn.execute("SELECT id, timeout_seconds FROM aicd.tasks").fetchall())
        # 1800 в незавершённой задаче означал «не задано» ⇒ NULL (эффективный тайм-аут прежний); 900 — явный.
        assert got == {implicit: None, explicit: 900}
        explicit_1800 = _task(conn, tenant.workspace_id, run, "explicit1800", 1800)
        assert conn.execute("SELECT timeout_seconds FROM aicd.tasks WHERE id = %s", (explicit_1800,)).fetchone() == (
            1800,
        ), "после 0014 явное 1800 сохраняется как есть"
    testdb.alembic(scratch_db, "downgrade", "0013")
    with psycopg.connect(scratch_db.superuser_url, autocommit=True, options="-c search_path=aicd") as conn:
        assert _column(conn, "tasks", "timeout_seconds") == ("NO", "1800")
        assert _column(conn, "user_sessions", "reauthenticated_at") is None
        assert conn.execute("SELECT to_regclass('aicd.github_verification_requests')").fetchone() == (None,)
        assert conn.execute("SELECT timeout_seconds FROM aicd.tasks WHERE id = %s", (implicit,)).fetchone() == (1800,)
    testdb.alembic(scratch_db, "upgrade", "head")


def test_0014_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0013:0014", sql=True)
    out = buf.getvalue()
    assert "ADD COLUMN reauthenticated_at timestamptz" in out
    assert "ALTER COLUMN timeout_seconds DROP NOT NULL" in out
    assert "CREATE TABLE github_verification_requests" in out
    assert "FORCE ROW LEVEL SECURITY" in out


def test_verification_table_rls_grants_and_terminal_states(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url, autocommit=True, options="-c search_path=aicd") as conn:
        rls = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'aicd.github_verification_requests'::regclass"
        ).fetchone()
        assert rls == (True, True)

        def priv(role: str, p: str, table: str = "github_verification_requests") -> bool:
            row = conn.execute("SELECT has_table_privilege(%s, %s, %s)", (role, f"aicd.{table}", p)).fetchone()
            return bool(row and row[0])

        assert priv("aicd_app", "SELECT") and priv("aicd_app", "INSERT")

        def any_col(role: str, p: str) -> bool:
            row = conn.execute(
                "SELECT has_any_column_privilege(%s, 'aicd.github_verification_requests', %s)", (role, p)
            ).fetchone()
            return bool(row and row[0])

        assert not any_col("aicd_app", "UPDATE") and not priv("aicd_app", "DELETE")
        assert priv("aicd_worker", "SELECT") and any_col("aicd_worker", "UPDATE") and not priv("aicd_worker", "INSERT")
        assert not priv("aicd_app", "INSERT", "github_installations"), "OQ-47 (2): установки создаёт воркер"
        assert priv("aicd_worker", "INSERT", "repositories")

        tenant = seed_tenant(conn)
        vid = uuid7()
        conn.execute(
            "INSERT INTO aicd.github_verification_requests (id, workspace_id, kind, params, requested_by)"
            " VALUES (%s, %s, 'installation', '{\"installation_id\": 5}', %s)",
            (vid, tenant.workspace_id, tenant.owner_id),
        )
        with pytest.raises(psycopg.errors.CheckViolation):  # SUCCEEDED без result_id
            conn.execute(
                "UPDATE aicd.github_verification_requests SET status = 'SUCCEEDED', completed_at = now() WHERE id = %s",
                (vid,),
            )
        conn.execute(
            "UPDATE aicd.github_verification_requests SET status = 'FAILED', error_code = 'not_found',"
            " completed_at = now() WHERE id = %s",
            (vid,),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="terminal"):
            conn.execute(
                "UPDATE aicd.github_verification_requests SET status = 'PENDING', completed_at = NULL,"
                " error_code = NULL WHERE id = %s",
                (vid,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):  # repository без project_id
            conn.execute(
                "INSERT INTO aicd.github_verification_requests (id, workspace_id, kind, params, requested_by)"
                " VALUES (%s, %s, 'repository', '{}', %s)",
                (uuid7(), tenant.workspace_id, tenant.owner_id),
            )
    with psycopg.connect(database.url_for("aicd_app")) as app, app.transaction():
        app.execute("SELECT set_config('app.workspace_id', %s, true)", (str(uuid.uuid4()),))
        assert app.execute("SELECT count(*) FROM aicd.github_verification_requests").fetchone() == (0,)


def test_session_revoke_reason_rotated(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url, autocommit=True, options="-c search_path=aicd") as conn:
        tenant = seed_tenant(conn)
        sid = uuid7()
        conn.execute(
            "INSERT INTO aicd.user_sessions (id, user_id, token_hash, csrf_token_hash, expires_at, reauthenticated_at)"
            " VALUES (%s, %s, %s, %s, now() + interval '1 hour', now())",
            (sid, tenant.owner_id, "a" * 64, "b" * 64),
        )
        conn.execute(
            "UPDATE aicd.user_sessions SET revoked_at = now(), revoke_reason = 'rotated' WHERE id = %s", (sid,)
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE aicd.user_sessions SET reauthenticated_at = created_at - interval '1 s' WHERE id = %s", (sid,)
            )
