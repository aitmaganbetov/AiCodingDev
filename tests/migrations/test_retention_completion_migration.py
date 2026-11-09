"""Миграция 0015 (P3-18 / OQ-41 (a) / OQ-47 (7)): права aicd_maintenance на retention оставшихся таблиц."""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from contextlib import redirect_stdout

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from tests.support import db as testdb

pytestmark = pytest.mark.db

TABLES = ("usage_metrics", "code_index_snapshots", "documentation_snapshots", "indexing_jobs")


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig15")
    yield db
    testdb.drop_database(db)


def _privileges(db: testdb.TestDatabase) -> dict[str, tuple[bool, bool]]:
    with psycopg.connect(db.superuser_url) as conn:
        return {
            table: tuple(
                conn.execute(
                    "SELECT has_table_privilege('aicd_maintenance', %s, 'SELECT'),"
                    " has_table_privilege('aicd_maintenance', %s, 'DELETE')",
                    (f"aicd.{table}", f"aicd.{table}"),
                ).fetchone()
                or (False, False)
            )
            for table in (*TABLES, "context_packs")
        }


def test_0015_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "0014")
    before = _privileges(scratch_db)
    assert all(select and not delete for select, delete in before.values()), "до 0015: только чтение"
    testdb.alembic(scratch_db, "upgrade", "0015")
    after = _privileges(scratch_db)
    assert all(after[t] == (True, True) for t in TABLES)
    assert after["context_packs"] == (True, False), "context_packs — только чтение (проверка ссылок на снимки)"
    testdb.alembic(scratch_db, "downgrade", "0014")
    assert _privileges(scratch_db) == before
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _privileges(scratch_db) == after


def test_0015_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0014:0015", sql=True)
    out = buf.getvalue()
    assert "GRANT DELETE ON usage_metrics, code_index_snapshots, documentation_snapshots, indexing_jobs" in out


def test_app_and_worker_roles_unchanged(database: testdb.TestDatabase) -> None:
    """0015 не расширяет права ролей приложения: удаление снимков/метрик — только retention job."""
    with psycopg.connect(database.superuser_url) as conn:
        for role in ("aicd_app", "aicd_worker"):
            for table in ("usage_metrics", "documentation_snapshots"):
                row = conn.execute("SELECT has_table_privilege(%s, %s, 'DELETE')", (role, f"aicd.{table}")).fetchone()
                assert row == (False,), f"{role} не удаляет {table}"
