"""Миграция 0017 (UI redesign B1/B2): индексы read-моделей workspace и `sys_outbox_backlog()`."""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from contextlib import redirect_stdout

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from aicd_platform.ids import uuid7
from tests.support import db as testdb

pytestmark = pytest.mark.db

INDEXES = ("ix_run_event_log_ws_created", "ix_cli_executions_ws_created", "ix_tasks_ws_created")


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig17")
    yield db
    testdb.drop_database(db)


def _state(db: testdb.TestDatabase) -> dict[str, object]:
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        indexes = conn.execute(
            "SELECT array_agg(indexname::text ORDER BY indexname) FROM pg_indexes"
            " WHERE schemaname = 'aicd' AND indexname = ANY(%s)",
            (list(INDEXES),),
        ).fetchone()
        fn = conn.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig FROM pg_proc p"
            " WHERE p.proname = 'sys_outbox_backlog'"
        ).fetchone()
        app_exec = conn.execute(
            "SELECT has_function_privilege('aicd_app', oid, 'EXECUTE'),"
            " has_function_privilege('aicd_worker', oid, 'EXECUTE') FROM pg_proc WHERE proname = 'sys_outbox_backlog'"
        ).fetchone()
        sysfn_cols = conn.execute(
            "SELECT has_column_privilege('aicd_sysfn', 'aicd.outbox_events', 'published_at', 'SELECT'),"
            " has_column_privilege('aicd_sysfn', 'aicd.outbox_events', 'payload', 'SELECT')"
        ).fetchone()
    return {
        "indexes": sorted(indexes[0] or []) if indexes else [],
        "function": (fn[0], fn[1], list(fn[2] or [])) if fn else None,
        "exec": tuple(app_exec) if app_exec else None,
        "sysfn_cols": tuple(sysfn_cols) if sysfn_cols else None,
    }


def test_0017_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "0016")
    before = _state(scratch_db)
    assert before == {"indexes": [], "function": None, "exec": None, "sysfn_cols": (False, False)}
    testdb.alembic(scratch_db, "upgrade", "0017")
    after = _state(scratch_db)
    assert after["indexes"] == sorted(INDEXES)
    assert after["function"] == (True, "aicd_sysfn", ["search_path=pg_catalog, aicd"])
    assert after["exec"] == (True, False), "EXECUTE — только aicd_app"
    assert after["sysfn_cols"] == (True, False), "aicd_sysfn читает только (created_at, published_at)"
    testdb.alembic(scratch_db, "downgrade", "0016")
    assert _state(scratch_db) == before
    testdb.alembic(scratch_db, "upgrade", "0017")
    assert _state(scratch_db) == after


def test_sys_outbox_backlog_returns_only_aggregate(database: testdb.TestDatabase) -> None:
    """Функция видна aicd_app (у него нет SELECT на outbox_events) и возвращает только агрегат по всем workspace."""
    event = uuid7()
    with psycopg.connect(database.superuser_url, autocommit=True, options="-c search_path=aicd") as admin:
        admin.execute(
            # available_at в будущем — диспетчер outbox (если запущен другими тестами) не возьмёт строку.
            "INSERT INTO outbox_events (id, aggregate_type, aggregate_id, event_type, payload, created_at, available_at)"
            " VALUES (%s, 'system', %s, 'provider.health_changed', '{}'::jsonb, now() - interval '5 minutes',"
            " now() + interval '1 day')",
            (event, uuid7()),
        )
        try:
            with psycopg.connect(database.url_for("aicd_app"), autocommit=True) as app:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    app.execute("SELECT count(*) FROM outbox_events")
                row = app.execute("SELECT pending, oldest_created_at FROM sys_outbox_backlog()").fetchone()
            assert row is not None and row[0] >= 1 and row[1] is not None
            with psycopg.connect(database.url_for("aicd_worker"), autocommit=True) as worker:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    worker.execute("SELECT * FROM sys_outbox_backlog()")
        finally:
            admin.execute("DELETE FROM outbox_events WHERE id = %s", (event,))


def test_0017_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    """Offline-режим (`alembic upgrade --sql`) выдаёт DDL 0017 без подключения к БД."""
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0016:0017", sql=True)
    sql = buf.getvalue()
    for name in (*INDEXES, "sys_outbox_backlog"):
        assert name in sql
