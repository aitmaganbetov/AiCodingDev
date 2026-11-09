"""Миграция 0021 (UI redesign, решение D-8): `v_gate_outcomes.evidence_verification_status` — обратимость,
offline SQL, `security_invoker` и права сохранены, значения колонки и разбиение PASS в сводке Analytics
(`passed_verified` / `passed_unverified`)."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_analytics import summary
from aicd_platform.db import DbContext, transaction
from tests.support import db as testdb
from tests.support import quality as q
from tests.support.orchestration import Tenant, connect_admin, drive_run, new_run, seed_tenant

pytestmark = pytest.mark.db

ORIGINAL_COLUMNS = [
    "gate_execution_id",
    "workspace_id",
    "run_id",
    "project_id",
    "task_id",
    "gate_type",
    "required",
    "status",
    "started_at",
    "finished_at",
    "duration_seconds",
]


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig21")
    yield db
    testdb.drop_database(db)


def _view_state(db: testdb.TestDatabase) -> dict[str, object]:
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        opts = conn.execute(
            "SELECT c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'aicd' AND c.relname = 'v_gate_outcomes'"
        ).fetchone()
        columns = [
            r[0]
            for r in conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = 'aicd.v_gate_outcomes'::regclass AND attnum > 0"
                " ORDER BY attnum"
            ).fetchall()
        ]
        privileges = {
            role: conn.execute("SELECT has_table_privilege(%s, 'aicd.v_gate_outcomes', 'SELECT')", (role,)).fetchone()
            for role in ("aicd_app", "aicd_worker", "aicd_maintenance")
        }
        public = conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants"
            " WHERE table_schema = 'aicd' AND table_name = 'v_gate_outcomes' AND grantee = 'PUBLIC'"
        ).fetchone()
    assert opts is not None
    return {"options": list(opts[0] or []), "columns": columns, "privileges": privileges, "public": public}


def test_0021_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "0020")
    before = _view_state(scratch_db)
    assert before["columns"] == ORIGINAL_COLUMNS
    testdb.alembic(scratch_db, "upgrade", "0021")
    after = _view_state(scratch_db)
    assert after["columns"] == [*ORIGINAL_COLUMNS, "evidence_verification_status"]
    assert after["options"] == ["security_invoker=true"]
    assert after["privileges"] == {"aicd_app": (True,), "aicd_worker": (False,), "aicd_maintenance": (False,)}
    assert after["public"] == (0,)
    testdb.alembic(scratch_db, "downgrade", "0020")
    assert _view_state(scratch_db) == before
    testdb.alembic(scratch_db, "upgrade", "0021")
    assert _view_state(scratch_db) == after


def test_0021_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0020:0021", sql=True)
        command.downgrade(Config(str(testdb.ALEMBIC_INI)), "0021:0020", sql=True)
    sql = buf.getvalue()
    for fragment in (
        "CREATE OR REPLACE VIEW v_gate_outcomes WITH (security_invoker = true)",
        "e.verification_status::text AS evidence_verification_status",
        "DROP VIEW v_gate_outcomes",
        "GRANT SELECT ON v_gate_outcomes TO aicd_app",
    ):
        assert fragment in sql


def _seed(database: testdb.TestDatabase) -> tuple[Tenant, dict[str, uuid.UUID]]:
    """SCOPE: PASS (VERIFIED) ×2, PASS без VERIFIED evidence ×1 (строка до триггера evidence-first — вставка в
    обход триггеров как суперпользователь); SECRETS: FAIL; SAST: PASS (VERIFIED) + заменённое повтором исполнение."""
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn)
        run = new_run(conn, tenant)
        drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
        commit = q.sha1()
        profile = (q.tenant_profile(conn, tenant), tenant.workspace_id)
        scope = q.ensure_gate(conn, "scope", "SCOPE", profile=profile)
        scope2 = q.ensure_gate(conn, "scope-2", "SCOPE", profile=profile)
        scope3 = q.ensure_gate(conn, "scope-3", "SCOPE", profile=profile)
        leaks = q.ensure_gate(conn, "gitleaks", "SECRETS", profile=profile)
        sast = q.ensure_gate(conn, "semgrep", "SAST", profile=profile)
        ids = {
            "pass_verified": q.gate_execution(
                conn, tenant, run, scope, "SCOPE", "PASS", commit, evidence_id=q.evidence(conn, tenant, run, commit)
            ),
            "pass_verified_2": q.gate_execution(
                conn, tenant, run, scope2, "SCOPE", "PASS", commit, evidence_id=q.evidence(conn, tenant, run, commit)
            ),
        }
        scan = q.evidence(conn, tenant, run, commit, kind="scan_report")
        ids["fail"] = q.gate_execution(conn, tenant, run, leaks, "SECRETS", "FAIL", commit, evidence_id=scan)
        pending = q.evidence(conn, tenant, run, commit, status="PENDING")
        conn.execute("SET session_replication_role = replica")
        try:
            ids["pass_unverified"] = q.gate_execution(
                conn, tenant, run, scope3, "SCOPE", "PASS", commit, evidence_id=pending
            )
        finally:
            conn.execute("SET session_replication_role = DEFAULT")
        old = q.gate_execution(
            conn, tenant, run, sast, "SAST", "FAIL", commit, evidence_id=q.evidence(conn, tenant, run, commit)
        )
        conn.execute("UPDATE gate_executions SET superseded_at = now() WHERE id = %s", (old,))
        ids["sast_pass"] = q.gate_execution(
            conn, tenant, run, sast, "SAST", "PASS", commit, evidence_id=q.evidence(conn, tenant, run, commit)
        )
    return tenant, ids


def test_view_exposes_evidence_verification_status(database: testdb.TestDatabase) -> None:
    tenant, ids = _seed(database)
    with psycopg.connect(database.url_for("aicd_app"), options="-c search_path=aicd") as conn:
        assert conn.execute("SELECT count(*) FROM v_gate_outcomes").fetchone() == (0,), "без контекста — ничего"
        conn.execute(
            "SELECT set_config('app.workspace_id', %s, true), set_config('app.user_id', %s, true),"
            " set_config('app.actor_type', 'user', true)",
            (str(tenant.workspace_id), str(tenant.owner_id)),
        )
        rows: dict[uuid.UUID, str | None] = dict(
            conn.execute(
                "SELECT gate_execution_id, evidence_verification_status FROM v_gate_outcomes WHERE workspace_id = %s",
                (tenant.workspace_id,),
            ).fetchall()
        )
    assert rows == {
        ids["pass_verified"]: "VERIFIED",
        ids["pass_verified_2"]: "VERIFIED",
        ids["fail"]: "VERIFIED",
        ids["pass_unverified"]: "PENDING",
        ids["sast_pass"]: "VERIFIED",
    }


async def test_summary_splits_pass_by_evidence(database: testdb.TestDatabase, app_engine: AsyncEngine) -> None:
    tenant, _ = _seed(database)
    now = datetime.now(UTC)
    window = summary.Window(now - timedelta(hours=1), now + timedelta(hours=1), tenant.project_id)
    async with transaction(app_engine, DbContext(user_id=tenant.owner_id, workspace_id=tenant.workspace_id)) as conn:
        gates = {g["gate_type"]: g for g in await summary.gates_section(conn, window)}
    assert set(gates) == {"SCOPE", "SECRETS", "SAST"}
    assert (gates["SCOPE"]["passed"], gates["SCOPE"]["passed_verified"], gates["SCOPE"]["passed_unverified"]) == (
        3,
        2,
        1,
    )
    assert (gates["SECRETS"]["passed"], gates["SECRETS"]["passed_verified"], gates["SECRETS"]["passed_unverified"]) == (
        0,
        0,
        0,
    )
    assert gates["SAST"]["total"] == 1, "заменённое повтором исполнение не учитывается"
    assert (gates["SAST"]["passed_verified"], gates["SAST"]["passed_unverified"]) == (1, 0)
