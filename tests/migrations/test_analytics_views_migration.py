"""Миграция 0013 (P8-07, OQ-46 (a)): отчётные представления Analytics — обратимость, offline SQL,
`security_invoker` (RLS таблиц действует через представления), права только у aicd_app."""

from __future__ import annotations

import importlib.util
import io
import os
import uuid
from collections.abc import Iterator
from contextlib import redirect_stdout
from datetime import UTC, datetime

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from aicd_analytics.summary import QUALITY_FAILURES
from aicd_orchestration.agent_io import ACTIVITY_RESULT
from aicd_orchestration.quality import FAILURES
from tests.support import db as testdb
from tests.support.orchestration import connect_admin, drive_run, new_run, seed_tenant, update

pytestmark = pytest.mark.db

_spec = importlib.util.spec_from_file_location(
    "m0013", testdb.ALEMBIC_INI.parent / "versions" / "0013_analytics_views.py"
)
assert _spec and _spec.loader
_m0013 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m0013)
VIEWS: tuple[str, ...] = _m0013.VIEWS


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig13")
    yield db
    testdb.drop_database(db)


def _views(db: testdb.TestDatabase) -> dict[str, list[str]]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT c.relname, c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'aicd' AND c.relkind = 'v' AND c.relname LIKE 'v\\_%'"
        ).fetchall()
    return {name: list(opts or []) for name, opts in rows}


def test_0013_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "head")
    views = _views(scratch_db)
    assert set(views) == set(VIEWS)
    assert all("security_invoker=true" in opts for opts in views.values()), views
    testdb.alembic(scratch_db, "downgrade", "0012")
    assert _views(scratch_db) == {}
    testdb.alembic(scratch_db, "upgrade", "head")
    assert set(_views(scratch_db)) == set(VIEWS)


def test_0013_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0012:0013", sql=True)
    out = buf.getvalue()
    for view in VIEWS:
        assert f"CREATE VIEW {view} WITH (security_invoker = true)" in out
    assert "GRANT SELECT ON v_run_outcomes" in out


def test_views_granted_only_to_app(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url) as conn:
        for view in VIEWS:
            for role, expected in (("aicd_app", True), ("aicd_worker", False), ("aicd_maintenance", False)):
                got = conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"aicd.{view}")).fetchone()
                assert got == (expected,), (view, role)
            for priv in ("INSERT", "UPDATE", "DELETE"):
                got = conn.execute("SELECT has_table_privilege('aicd_app', %s, %s)", (f"aicd.{view}", priv)).fetchone()
                assert got == (False,), (view, priv)
            public = conn.execute(
                "SELECT count(*) FROM information_schema.role_table_grants"
                " WHERE table_schema = 'aicd' AND table_name = %s AND grantee = 'PUBLIC'",
                (view,),
            ).fetchone()
            assert public == (0,), view


def test_rls_applies_through_views(database: testdb.TestDatabase) -> None:
    """security_invoker: пользователь видит через представления только свой workspace; без контекста — ничего."""
    with connect_admin(database) as admin:
        a, b = seed_tenant(admin), seed_tenant(admin)
        run_a, run_b = new_run(admin, a), new_run(admin, b)
        drive_run(admin, run_a, "DISCOVERY")
        update(admin, "runs", run_a, status="FAILED", finished_at=datetime.now(UTC))
    with psycopg.connect(database.url_for("aicd_app"), options="-c search_path=aicd") as conn:
        for view in VIEWS:
            assert conn.execute(f"SELECT count(*) FROM {view}").fetchone() == (0,), f"{view} без контекста"
        conn.execute(
            "SELECT set_config('app.workspace_id', %s, true), set_config('app.user_id', %s, true),"
            " set_config('app.actor_type', 'user', true)",
            (str(a.workspace_id), str(a.owner_id)),
        )
        rows = conn.execute("SELECT run_id, workspace_id, project_id, status FROM v_run_outcomes").fetchall()
        assert {r[0] for r in rows} == {run_a}
        assert rows[0][1:] == (a.workspace_id, a.project_id, "FAILED")
        assert conn.execute("SELECT count(*) FROM v_run_outcomes WHERE run_id = %s", (run_b,)).fetchone() == (0,)
        dur = conn.execute("SELECT duration_seconds FROM v_run_outcomes WHERE run_id = %s", (run_a,)).fetchone()
        assert dur is not None and dur[0] is not None and dur[0] >= 0
    assert isinstance(run_b, uuid.UUID)


def test_view_constants_match_owner_modules() -> None:
    """Литералы представлений/запросов Analytics совпадают с источниками истины модулей-владельцев."""
    assert f"e.event_type = '{ACTIVITY_RESULT}'" in _m0013.UPGRADE
    assert set(QUALITY_FAILURES) == set(FAILURES)
