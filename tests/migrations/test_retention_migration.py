"""P3-18 / миграция 0005: обратимость, функции обслуживания (§19.6, §20) и резолверы sys_* (§19.5)."""

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
from tests.support.orchestration import connect_admin, seed_task_attempt, seed_tenant

pytestmark = pytest.mark.db

MAINT_FUNCTIONS = {
    "maint_begin",
    "maint_end",
    "maint_require_session",
    "maint_drop_partition",
    "sys_resolve_approval_workspace",
    "sys_recovery_workspaces",
}


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig5")
    yield db
    testdb.drop_database(db)


def _functions(db: testdb.TestDatabase) -> set[str]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'aicd'"
        ).fetchall()
    return {r[0] for r in rows}


def _ensure_is_definer(db: testdb.TestDatabase) -> bool:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'aicd' AND p.proname = 'maint_ensure_monthly_partitions'"
        ).fetchone()
    assert row is not None
    return bool(row[0])


def test_0005_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _functions(scratch_db) >= MAINT_FUNCTIONS
    assert _ensure_is_definer(scratch_db)
    testdb.alembic(scratch_db, "downgrade", "0004")
    assert not (MAINT_FUNCTIONS & _functions(scratch_db))
    assert not _ensure_is_definer(scratch_db)
    # После отката журнал SSE снова неизменяем для всех ролей (общий trg_fn_append_only).
    with psycopg.connect(scratch_db.superuser_url) as conn:
        fn = conn.execute(
            "SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid"
            " WHERE t.tgname = 'trg_run_event_log_append_only'"
        ).fetchone()
    assert fn == ("trg_fn_append_only",)
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _functions(scratch_db) >= MAINT_FUNCTIONS


def test_0005_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0004:0005", sql=True)
    out = buf.getvalue()
    assert "CREATE FUNCTION maint_drop_partition" in out
    assert "CREATE FUNCTION sys_resolve_approval_workspace" in out


def test_maintenance_functions_require_session_and_role(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT maint_begin(%s, 'retention', 'x')", (uuid7(),))
    with psycopg.connect(database.url_for("aicd_worker")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT maint_ensure_monthly_partitions('audit_events', current_date, 1)")
    with psycopg.connect(database.url_for("aicd_maintenance"), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="maint_begin"):
            conn.execute(
                "SELECT maint_drop_partition('outbox_events', 'outbox_events_y2001m01', '2001-02-01', NULL, NULL)"
            )
        # Без сессии обслуживания журнал SSE неизменяем и для aicd_maintenance.
        run_id = _run_with_event(database)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            conn.execute("DELETE FROM run_event_log WHERE run_id = %s", (run_id,))
        conn.execute("SELECT maint_begin(%s, 'retention', 'test')", (uuid7(),))
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            conn.execute("SELECT maint_begin(%s, 'retention', 'twice')", (uuid7(),))
        # audit_events без архива не удаляется; текущий месяц и будущее — никогда.
        conn.execute("SELECT maint_ensure_monthly_partitions('audit_events', '2001-01-01', 0)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="archived"):
            conn.execute(
                "SELECT maint_drop_partition('audit_events', 'audit_events_y2001m01', '2001-02-01', NULL, NULL)"
            )
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="not expired"):
            conn.execute(
                "SELECT maint_drop_partition('audit_events', 'audit_events_y2001m01', '2001-01-15', NULL, NULL)"
            )
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="cutoff"):
            conn.execute(
                "SELECT maint_drop_partition('audit_events', 'audit_events_y2001m01', '2999-01-01', NULL, NULL)"
            )
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="not configured"):
            conn.execute("SELECT maint_drop_partition('runs', 'runs_y2001m01', '2001-02-01', NULL, NULL)")
        conn.execute("SELECT maint_end(%s, 'success', '{}')", (uuid7(),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT maint_require_session()")
    with connect_admin(database) as admin:
        assert admin.execute("SELECT count(*) FROM pg_class WHERE relname = 'audit_events_y2001m01'").fetchone() == (1,)
        admin.execute("ALTER TABLE audit_events DETACH PARTITION audit_events_y2001m01")
        admin.execute("DROP TABLE audit_events_y2001m01")


def _run_with_event(database: testdb.TestDatabase) -> uuid.UUID:
    """Run со строкой run_event_log (через суперпользователя); возвращает run_id."""
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        run = uuid7()
        admin.execute(
            "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
            " quality_profile, quality_profile_id, documentation_mode) VALUES (%s, %s, %s, %s, %s, 'DEVELOP',"
            " 'o', 'strict', '01a1c000-0000-7000-8000-000000000002', 'SMART')",
            (run, tenant.workspace_id, tenant.project_id, tenant.repository_id, tenant.owner_id),
        )
        admin.execute(
            "INSERT INTO run_event_log (workspace_id, run_id, seq, event_id, event_type, payload, occurred_at)"
            " VALUES (%s, %s, 1, %s, 'run.state_changed', '{}', now())",
            (tenant.workspace_id, run, uuid7()),
        )
    return run


def test_sys_resolve_approval_workspace_members_only(database: testdb.TestDatabase) -> None:
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        attempt = seed_task_attempt(database, tenant.workspace_id, tenant.owner_id)
        run = admin.execute(
            "SELECT r.id, r.project_id FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
            " JOIN runs r ON r.id = t.run_id WHERE a.id = %s",
            (attempt,),
        ).fetchone()
        assert run is not None
        approval = uuid7()
        admin.execute(
            "INSERT INTO approvals (id, workspace_id, project_id, run_id, action_type, risk_level, required_role,"
            " subject, subject_hash, requested_by_type) VALUES (%s, %s, %s, %s, 'MANUAL_REVIEW', 'low',"
            " approval_min_role('MANUAL_REVIEW'), '{}', %s, 'system')",
            (approval, tenant.workspace_id, run[1], run[0], "a" * 64),
        )
        stranger = seed_tenant(admin).owner_id
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        for user, expected in ((tenant.owner_id, tenant.workspace_id), (stranger, None)):
            conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user),))
            assert conn.execute("SELECT sys_resolve_approval_workspace(%s)", (approval,)).fetchone() == (expected,)
            conn.rollback()
    with psycopg.connect(database.url_for("aicd_worker")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT sys_resolve_approval_workspace(%s)", (approval,))


def test_sys_recovery_workspaces(database: testdb.TestDatabase) -> None:
    host, inst, execution = uuid7(), uuid7(), uuid7()
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        attempt = seed_task_attempt(database, tenant.workspace_id, tenant.owner_id)
        admin.execute(
            "INSERT INTO worker_hosts (id, hostname, kind) VALUES (%s, %s, 'exec')", (host, f"r-{host.hex[-8:]}")
        )
        admin.execute(
            "INSERT INTO cli_installations (id, provider_id, host_id, binary_path, auth_status, max_concurrency)"
            " SELECT %s, id, %s, '/usr/bin/true', 'AUTHENTICATED', 1 FROM cli_providers WHERE code = 'codex'",
            (inst, host),
        )
        # Самое старое исполнение во всей БД: функция с limit 1 обязана вернуть именно этот workspace.
        admin.execute(
            "INSERT INTO cli_executions (id, workspace_id, task_attempt_id, installation_id, command_profile,"
            " request_hash, timeout_seconds, fencing_token, created_at)"
            " VALUES (%s, %s, %s, %s, 'rec', %s, 60, 1, '2000-01-01')",
            (execution, tenant.workspace_id, attempt, inst, "0" * 64),
        )
        try:
            for status in ("STARTING", "RUNNING"):
                admin.execute("UPDATE cli_executions SET status = %s WHERE id = %s", (status, execution))
            with psycopg.connect(database.url_for("aicd_worker")) as conn:
                assert conn.execute("SELECT * FROM sys_recovery_workspaces(1)").fetchall() == [(tenant.workspace_id,)]
                assert conn.execute("SELECT count(*) FROM sys_recovery_workspaces(0)").fetchone() == (0,)
        finally:
            # Строка из 2000 года не должна влиять на порядок recovery в других тестах сессии.
            admin.execute("DELETE FROM cli_executions WHERE id = %s", (execution,))
    with psycopg.connect(database.url_for("aicd_app")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT * FROM sys_recovery_workspaces(10)")
