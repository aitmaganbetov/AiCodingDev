"""Миграция 0016 (P9-05 / OQ-51 (a), (b)): права aicd_maintenance для pg_dump и тип approval VULN_DB_UPDATE."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from scripts.backup.docker import PgTarget, streaming_tool

from tests.support import db as testdb

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig16")
    yield db
    testdb.drop_database(db)


def _state(db: testdb.TestDatabase) -> dict[str, object]:
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        missing = conn.execute(
            "SELECT count(*) FROM pg_class c WHERE c.relnamespace = 'aicd'::regnamespace AND c.relkind IN ('r','p','S')"
            " AND NOT has_table_privilege('aicd_maintenance', c.oid, 'SELECT')"
        ).fetchone()
        seq = conn.execute(
            "SELECT has_sequence_privilege('aicd_maintenance', 'aicd.worker_lease_fencing_seq', 'SELECT')"
        ).fetchone()
        domain = conn.execute(
            "SELECT pg_get_constraintdef(oid) LIKE '%VULN_DB_UPDATE%' FROM pg_constraint"
            " WHERE contypid = 'aicd.approval_action_type_t'::regtype"
        ).fetchone()
        role = conn.execute("SELECT aicd.approval_min_role('VULN_DB_UPDATE')").fetchone()
        funcs = conn.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'sys_vulndb_%'").fetchone()
    return {
        "missing_select": missing[0] if missing else None,
        "seq": seq[0] if seq else None,
        "domain": domain[0] if domain else None,
        "min_role": role[0] if role else None,
        "functions": funcs[0] if funcs else None,
    }


def _future_partition_readable(db: testdb.TestDatabase, table: str) -> bool:
    with psycopg.connect(db.url_for("aicd_maintenance"), autocommit=True) as conn:
        conn.execute("SELECT aicd.maint_ensure_monthly_partitions(%s, (current_date + interval '30 months')::date, 0)",
                     (table,))  # fmt: skip
        month = conn.execute("SELECT to_char(current_date + interval '30 months', 'YYYY\"m\"MM')").fetchone()
        assert month is not None
        part = (f"aicd.{table}_y{month[0]}",)
        row = conn.execute("SELECT has_table_privilege(%s, 'SELECT')", (part[0],)).fetchone()
    return bool(row and row[0])


def test_0016_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "0015")
    before = _state(scratch_db)
    assert before["missing_select"] and before["seq"] is False and before["domain"] is False
    assert before["min_role"] is None and before["functions"] == 0
    testdb.alembic(scratch_db, "upgrade", "0016")
    after = _state(scratch_db)
    assert after == {"missing_select": 0, "seq": True, "domain": True, "min_role": "admin", "functions": 3}
    for table in ("outbox_events", "agent_activity_events", "audit_events"):
        assert _future_partition_readable(scratch_db, table), f"новая партиция {table} читается aicd_maintenance"
    testdb.alembic(scratch_db, "downgrade", "0015")
    down = _state(scratch_db)
    assert down["seq"] is False and down["domain"] is False and down["functions"] == 0 and down["min_role"] is None
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _state(scratch_db) == after


def test_0016_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0015:0016", sql=True)
    out = buf.getvalue()
    assert "GRANT SELECT ON SEQUENCE worker_lease_fencing_seq TO aicd_maintenance" in out
    assert "'VULN_DB_UPDATE'" in out and "CREATE FUNCTION sys_vulndb_authorize" in out
    assert "__ACTIONS_V1__" not in out


@pytest.mark.skipif(shutil.which("docker") is None, reason="нужен Docker (pg_dump версии сервера из образа)")
def test_pg_dump_as_maintenance_succeeds(database: testdb.TestDatabase) -> None:
    """OQ-51 (a): логический дамп под aicd_maintenance (без суперпользователя) на схеме head."""
    url = urlparse(database.url_for("aicd_maintenance"))
    with tempfile.TemporaryDirectory() as tmp:
        password = Path(tmp) / "pw"
        password.write_text(url.password or "")
        target = PgTarget(host=url.hostname or "127.0.0.1", port=url.port or 5432, user="aicd_maintenance",
                          password_file=password, dbname=database.name, network="host")  # fmt: skip
        argv = ["pg_dump", *target.conn_args(), "-d", database.name, "-Fc"]
        with streaming_tool(target, argv) as proc:
            assert proc.stdout is not None and proc.stderr is not None
            size = len(proc.stdout.read())
            err = proc.stderr.read().decode()
            code = proc.wait()
    assert code == 0, err[-2000:]
    assert size > 10_000


def test_maintenance_cannot_modify_via_new_grants(database: testdb.TestDatabase) -> None:
    """0016 выдаёт только чтение: nextval/запись последовательности и запись в партиции недоступны."""
    with psycopg.connect(database.url_for("aicd_maintenance"), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT nextval('aicd.worker_lease_fencing_seq')")
