"""Миграция 0018 (UI redesign B6): trigram-индексы глобального поиска; обратимость и offline-SQL."""

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

INDEXES = ("ix_findings_search_trgm", "ix_runs_objective_trgm", "ix_tasks_title_trgm")


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig18")
    yield db
    testdb.drop_database(db)


def _indexes(db: testdb.TestDatabase) -> dict[str, str]:
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT indexname::text, indexdef FROM pg_indexes WHERE schemaname = 'aicd' AND indexname = ANY(%s)",
            (list(INDEXES),),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def test_0018_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "0017")
    assert _indexes(scratch_db) == {}
    testdb.alembic(scratch_db, "upgrade", "0018")
    after = _indexes(scratch_db)
    assert sorted(after) == sorted(INDEXES)
    for name, ddl in after.items():
        assert "USING gin (workspace_id" in ddl and "gin_trgm_ops" in ddl, (name, ddl)
    testdb.alembic(scratch_db, "downgrade", "0017")
    assert _indexes(scratch_db) == {}
    testdb.alembic(scratch_db, "upgrade", "0018")
    assert _indexes(scratch_db) == after


def test_search_expression_uses_trigram_index(database: testdb.TestDatabase) -> None:
    """Условия `GET /search` (то же выражение и ILIKE) обслуживаются индексами 0018.

    На пустой таблице планировщик предпочёл бы btree по workspace_id, поэтому проверяется применимость индекса к
    самому условию ILIKE: без фильтра tenant и с отключённым seqscan остаётся только GIN trigram.
    """
    with psycopg.connect(database.superuser_url, options="-c search_path=aicd") as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plans = {
            "ix_runs_objective_trgm": "SELECT id FROM runs WHERE objective ILIKE '%refund%'",
            "ix_tasks_title_trgm": "SELECT id FROM tasks WHERE title ILIKE '%refund%'",
            "ix_findings_search_trgm": (
                "SELECT id FROM findings f"
                " WHERE (f.title || ' ' || f.rule_id || ' ' || coalesce(f.cwe_id, '')) ILIKE '%sql-inj%'"
            ),
        }
        for index, sql in plans.items():
            plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + sql).fetchall())
            assert index in plan, plan
        conn.rollback()


def test_0018_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0017:0018", sql=True)
    sql = buf.getvalue()
    for name in INDEXES:
        assert name in sql
