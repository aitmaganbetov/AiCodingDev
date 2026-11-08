"""P1-07: миграции обратимы, схема соответствует инвариантам DATABASE_DESIGN (§19.4, §18.9)."""

from __future__ import annotations

import io
import os
import re
from collections.abc import Iterator
from contextlib import redirect_stdout

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from aicd_platform.ids import uuid7
from tests.support import db as testdb

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig")
    yield db
    testdb.drop_database(db)


def _tables(db: testdb.TestDatabase) -> set[str]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'aicd' AND c.relkind IN ('r','p') AND NOT c.relispartition"
            " AND c.relname <> 'alembic_version'"
        ).fetchall()
    return {r[0] for r in rows}


def test_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "head")
    created = _tables(scratch_db)
    assert {
        "users",
        "workspaces",
        "workspace_members",
        "projects",
        "repositories",
        "audit_events",
        "idempotency_keys",
        "artifacts",
        "user_sessions",
        "user_credentials",
        "project_settings",
        "project_member_overrides",
        "runs",
        "tasks",
        "task_dependencies",
        "approvals",
        "user_directives",
        "outbox_events",
        "worker_leases",
        "external_actions",
        "run_event_log",
    } <= created
    # Пошаговая обратимость 0003: откат к 0002 возвращает справочник переходов к execution-строкам.
    testdb.alembic(scratch_db, "downgrade", "0002")
    with psycopg.connect(scratch_db.superuser_url) as conn:
        entities = {r[0] for r in conn.execute("SELECT DISTINCT entity FROM aicd.allowed_transitions").fetchall()}
    assert entities == {"execution"}
    assert "runs" not in _tables(scratch_db)
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _tables(scratch_db) == created
    testdb.alembic(scratch_db, "downgrade", "base")
    assert _tables(scratch_db) == set()
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _tables(scratch_db) == created


def test_offline_sql_dry_run(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "head", sql=True)
    assert "CREATE TABLE workspaces" in buf.getvalue()


def test_every_tenant_table_has_forced_rls_and_policy(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'aicd' AND c.relkind IN ('r','p') AND NOT c.relispartition
               AND (EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'workspace_id'
                           AND NOT a.attisdropped) OR c.relname = 'workspaces')
            """
        ).fetchall()
    assert rows
    for name, rls, force, policies in rows:
        assert rls and force and policies > 0, f"{name}: RLS/FORCE/policy отсутствуют"


def test_no_secret_columns(database: testdb.TestDatabase) -> None:
    """ТЗ §7.1: секреты и токены не хранятся — только хэши и ссылки."""
    with psycopg.connect(database.superuser_url) as conn:
        cols = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'aicd'"
        ).fetchall()
    bad = re.compile(r"(password|secret|token|api_?key|private_key|cookie)(?!_hash|_ref)")
    allowed_suffixes = ("_hash", "_ref", "_changed_at")  # метаданные, не значения секретов
    # Монотонный номер аренды (§18.6) и счётчики токенов LLM-контекста (§10.5, §10.8) — не секреты (§18.9).
    allowed = {"fencing_token", "lease_fencing_token", "token_budget", "token_estimate"}
    offenders = [f"{t}.{c}" for t, c in cols if bad.search(c) and not c.endswith(allowed_suffixes) and c not in allowed]
    assert offenders == []


def test_app_roles_cannot_bypass_rls(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url) as conn:
        rows: dict[str, bool] = dict(
            conn.execute(
                "SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname IN ('aicd_app','aicd_worker','aicd_migrator')"
            ).fetchall()
        )
    assert rows == {"aicd_app": False, "aicd_worker": False, "aicd_migrator": False}


def test_audit_events_append_only(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE audit_events SET action = 'x.y'")
    # Триггер страхует и от привилегированных ролей (вне RLS): строка есть, но удалить её нельзя.
    with psycopg.connect(database.superuser_url) as conn:
        conn.execute(
            "INSERT INTO aicd.audit_events (id, actor_type, action, resource_type) VALUES (%s, 'system', 'test.seed', 'x')",
            (uuid7(),),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            conn.execute("DELETE FROM aicd.audit_events")
