"""Миграция 0010: findings.control_refs/version, project_controls.blocking, вердикт человека по контролю
(`sys_record_compliance_review`), резолвер находок (OQ-43 (c), (d); DATABASE_DESIGN §12.6, §13.3, §19.5)."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator
from contextlib import redirect_stdout
from dataclasses import dataclass

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from aicd_platform.ids import uuid7
from tests.support import db as testdb
from tests.support import quality as q
from tests.support.orchestration import Tenant, add_user, connect_admin, drive_run, new_run, seed_tenant

pytestmark = pytest.mark.db

SCOPE_CONTROL = uuid.UUID("01a1c008-0001-7000-8000-000000000013")  # SEC-27001-01, automated_gate SCOPE
LINT_CONTROL = uuid.UUID("01a1c008-0001-7000-8000-000000000006")  # QM-25010-01, automated_gate LINT
ADR_CONTROL = uuid.UUID("01a1c008-0001-7000-8000-000000000003")  # ARCH-42010-01, document_check
REVIEW_FN = "SELECT sys_record_compliance_review(%s, %s, %s, %s, %s::uuid[])"


def _columns(db: testdb.TestDatabase, table: str) -> set[str]:
    with psycopg.connect(db.superuser_url) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'aicd' AND table_name = %s",
                (table,),
            ).fetchall()
        }


def _functions(db: testdb.TestDatabase) -> set[str]:
    with psycopg.connect(db.superuser_url) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'aicd'"
            ).fetchall()
        }


NEW_FUNCTIONS = {
    "sys_record_compliance_review",
    "sys_resolve_finding_workspace",
    "control_default_blocking",
    "trg_fn_findings_bump_version",
}


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig10")
    yield db
    testdb.drop_database(db)


def test_0010_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "head")
    assert {"control_refs", "version"} <= _columns(scratch_db, "findings")
    assert "blocking" in _columns(scratch_db, "project_controls")
    assert _functions(scratch_db) >= NEW_FUNCTIONS
    testdb.alembic(scratch_db, "downgrade", "0009")
    assert not ({"control_refs", "version"} & _columns(scratch_db, "findings"))
    assert "blocking" not in _columns(scratch_db, "project_controls")
    assert not (NEW_FUNCTIONS & _functions(scratch_db))
    with psycopg.connect(scratch_db.superuser_url) as conn:
        body = conn.execute("SELECT prosrc FROM pg_proc WHERE proname = 'trg_fn_project_controls_approval'").fetchone()
        assert body is not None and "blocking" not in body[0], "триггер утверждения возвращён к виду 0008"
        # Отозваны ровно выданные 0010 права aicd_sysfn; права 0008 (evidence id, workspace_id) остались.
        assert conn.execute(
            "SELECT has_column_privilege('aicd_sysfn', 'aicd.evidence', 'run_id', 'SELECT')"
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT has_column_privilege('aicd_sysfn', 'aicd.evidence', 'id', 'SELECT')"
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT has_table_privilege('aicd_sysfn', 'aicd.compliance_results', 'INSERT')"
        ).fetchone() == (False,)
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _functions(scratch_db) >= NEW_FUNCTIONS


def test_0010_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0009:0010", sql=True)
    out = buf.getvalue()
    assert "ADD COLUMN control_refs text[]" in out and "ADD COLUMN blocking boolean" in out
    assert "CREATE FUNCTION sys_record_compliance_review" in out
    assert "CREATE FUNCTION sys_resolve_finding_workspace" in out


# --- данные ---------------------------------------------------------------------------------------------


@dataclass
class Side:
    tenant: Tenant
    run: uuid.UUID
    commit: str
    evidence: uuid.UUID  # VERIFIED
    pending_evidence: uuid.UUID
    finding: uuid.UUID
    reviewer: uuid.UUID
    viewer: uuid.UUID

    @property
    def ws(self) -> uuid.UUID:
        return self.tenant.workspace_id


def _ctx(conn: psycopg.Connection, ws: uuid.UUID | None, user: uuid.UUID | None = None) -> None:
    conn.execute(
        "SELECT set_config('app.user_id', %s, true), set_config('app.workspace_id', %s, true)",
        (str(user) if user else "", str(ws) if ws else ""),
    )


def _approve(conn: psycopg.Connection, s: Side, control: uuid.UUID, applicability: str = "applicable") -> None:
    with conn.transaction():
        _ctx(conn, s.ws, s.tenant.owner_id)
        conn.execute(
            "INSERT INTO project_controls (workspace_id, project_id, control_id, applicability, rationale, approved_by,"
            " approved_at) VALUES (%s, %s, %s, %s, %s, %s, now()) ON CONFLICT (project_id, control_id) DO UPDATE"
            " SET applicability = EXCLUDED.applicability, rationale = EXCLUDED.rationale",
            (s.ws, s.tenant.project_id, control, applicability, None if applicability == "applicable" else "n/a",
             s.tenant.owner_id),
        )  # fmt: skip


def _side(conn: psycopg.Connection) -> Side:
    tenant = seed_tenant(conn)
    run = new_run(conn, tenant)
    drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
    commit = q.sha1()
    ev = q.evidence(conn, tenant, run, commit)
    pending = q.evidence(conn, tenant, run, commit, status="PENDING", kind="diff")
    gate = q.ensure_gate(conn, "scope", "SCOPE")
    ge = q.gate_execution(conn, tenant, run, gate, "SCOPE", "PASS", commit, evidence_id=ev)
    finding = q.finding(conn, tenant, run)
    # Актуальные вердикты движка: SCOPE — PASS (gate), LINT — UNVERIFIED, ADR — MANUAL_REVIEW.
    conn.execute(
        "INSERT INTO compliance_results (id, workspace_id, run_id, control_id, status, evidence_id, gate_execution_id)"
        " VALUES (%s, %s, %s, %s, 'PASS', %s, %s)",
        (uuid7(), tenant.workspace_id, run, SCOPE_CONTROL, ev, ge),
    )
    for control, status in ((LINT_CONTROL, "UNVERIFIED"), (ADR_CONTROL, "MANUAL_REVIEW")):
        conn.execute(
            "INSERT INTO compliance_results (id, workspace_id, run_id, control_id, status) VALUES (%s, %s, %s, %s, %s)",
            (uuid7(), tenant.workspace_id, run, control, status),
        )
    side = Side(tenant, run, commit, ev, pending, finding, add_user(conn, tenant, "reviewer"),
                add_user(conn, tenant, "viewer"))  # fmt: skip
    return side


@dataclass(frozen=True)
class World:
    a: Side
    b: Side


@pytest.fixture(scope="module")
def world(database: testdb.TestDatabase) -> World:
    with connect_admin(database) as conn:
        a, b = _side(conn), _side(conn)
    with psycopg.connect(database.superuser_url, autocommit=True, options="-c search_path=aicd") as conn:
        for s in (a, b):
            for control in (SCOPE_CONTROL, LINT_CONTROL, ADR_CONTROL):
                _approve(conn, s, control)
    return World(a, b)


# --- findings: control_refs, version -------------------------------------------------------------------


def test_findings_control_refs_check_and_version_bump(database: testdb.TestDatabase, world: World) -> None:
    a = world.a
    with connect_admin(database) as conn:
        assert conn.execute("SELECT version, control_refs FROM findings WHERE id = %s", (a.finding,)).fetchone() == (
            1,
            [],
        )
        conn.execute("UPDATE findings SET control_refs = %s WHERE id = %s", (["SEC-27002-02", "CWE-INJ-01"], a.finding))
        conn.execute("UPDATE findings SET status = 'ACKNOWLEDGED' WHERE id = %s", (a.finding,))
        assert conn.execute("SELECT version FROM findings WHERE id = %s", (a.finding,)).fetchone() == (3,)
        for bad in (["bad ref"], ["x" * 65], ["ok", None], ["r"] * 51):
            with pytest.raises(psycopg.errors.CheckViolation, match="ck_findings_control_refs"):
                conn.execute("UPDATE findings SET control_refs = %s WHERE id = %s", (bad, a.finding))
        conn.execute("UPDATE findings SET status = 'OPEN' WHERE id = %s", (a.finding,))
    # aicd_app меняет только решение человека; версию увеличивает триггер.
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with conn.transaction():
            _ctx(conn, a.ws, a.reviewer)
            conn.execute(
                "UPDATE findings SET status = 'FALSE_POSITIVE', resolved_by = %s, resolution_reason = 'тестовый код'"
                " WHERE id = %s",
                (a.reviewer, a.finding),
            )
            assert conn.execute("SELECT version FROM findings WHERE id = %s", (a.finding,)).fetchone() == (5,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            _ctx(conn, a.ws, a.reviewer)
            conn.execute("UPDATE findings SET version = 1, control_refs = '{}' WHERE id = %s", (a.finding,))


def test_project_controls_blocking_default_and_guard(database: testdb.TestDatabase, world: World) -> None:
    a = world.a
    with connect_admin(database) as conn:
        cases = {
            ("strict", "security"): True,
            ("critical", "security"): True,
            ("basic", "security"): False,
            ("strict", "qa"): False,
            ("strict", None): False,
        }
        for (profile, role), expected in cases.items():
            assert conn.execute("SELECT control_default_blocking(%s, %s)", (profile, role)).fetchone() == (expected,)
    # Утверждённую строку (в т.ч. blocking) система изменить не может; пользователь сессии — может.
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="only by a user"), conn.transaction():
            _ctx(conn, a.ws)
            conn.execute(
                "UPDATE project_controls SET blocking = false WHERE project_id = %s AND control_id = %s",
                (a.tenant.project_id, SCOPE_CONTROL),
            )
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="session user"), conn.transaction():
            _ctx(conn, a.ws, a.reviewer)  # approved_by остаётся владельцем ≠ пользователь сессии
            conn.execute(
                "UPDATE project_controls SET blocking = true WHERE project_id = %s AND control_id = %s",
                (a.tenant.project_id, SCOPE_CONTROL),
            )
        with conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            conn.execute(
                "UPDATE project_controls SET blocking = true WHERE project_id = %s AND control_id = %s",
                (a.tenant.project_id, SCOPE_CONTROL),
            )


# --- SECURITY DEFINER функции ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", ["sys_resolve_finding_workspace", "sys_record_compliance_review"])
def test_sysfn_security_attributes(database: testdb.TestDatabase, fn: str) -> None:
    with psycopg.connect(database.superuser_url) as conn:
        row = conn.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,"
            " has_function_privilege('aicd_worker', p.oid, 'EXECUTE'),"
            " has_function_privilege('aicd_maintenance', p.oid, 'EXECUTE'),"
            " has_function_privilege('aicd_app', p.oid, 'EXECUTE')"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'aicd' AND p.proname = %s",
            (fn,),
        ).fetchone()
    assert row is not None
    secdef, owner, config, worker, maintenance, app = row
    assert secdef is True and owner == "aicd_sysfn"
    assert any(c.startswith("search_path=") for c in config)
    assert (worker, maintenance, app) == (False, False, True)


def test_sys_resolve_finding_workspace_members_only(database: testdb.TestDatabase, world: World) -> None:
    a, b = world.a, world.b
    resolve = "SELECT sys_resolve_finding_workspace(%s)"
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, None, a.tenant.owner_id)
        assert conn.execute(resolve, (a.finding,)).fetchone() == (a.ws,)
        assert conn.execute(resolve, (b.finding,)).fetchone() == (None,), "чужая находка неотличима от отсутствующей"
        assert conn.execute(resolve, (uuid7(),)).fetchone() == (None,)
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, None)
        assert conn.execute(resolve, (a.finding,)).fetchone() == (None,)


def _review(
    conn: psycopg.Connection,
    s: Side,
    user: uuid.UUID,
    control: uuid.UUID,
    status: str = "PASS",
    evidence: list[uuid.UUID] | None = None,
    *,
    ws: uuid.UUID | None = None,
    run: uuid.UUID | None = None,
    notes: str = "проверено вручную",
) -> object:
    with conn.transaction():
        _ctx(conn, ws or s.ws, user)
        row = conn.execute(
            REVIEW_FN, (run or s.run, control, status, notes, [str(e) for e in (evidence or [s.evidence])])
        ).fetchone()
    assert row is not None
    return row[0]


def test_record_compliance_review(database: testdb.TestDatabase, world: World) -> None:
    a, b = world.a, world.b
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        # Прямой INSERT в compliance_results у api нет — только функция.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            _ctx(conn, a.ws, a.reviewer)
            conn.execute(
                "INSERT INTO compliance_results (id, workspace_id, run_id, control_id, status, evidence_id, reviewed_by)"
                " VALUES (%s, %s, %s, %s, 'PASS', %s, %s)",
                (uuid7(), a.ws, a.run, ADR_CONTROL, a.evidence, a.reviewer),
            )
        # document_check в MANUAL_REVIEW: reviewer записывает PASS с VERIFIED evidence и вторым evidence.
        first = _review(conn, a, a.reviewer, ADR_CONTROL, evidence=[a.evidence, a.pending_evidence])
        assert isinstance(first, uuid.UUID) and first.version == 7
        # Повторный вердикт человека заменяет прежний (FAIL допускает не VERIFIED evidence).
        second = _review(conn, a, a.tenant.owner_id, ADR_CONTROL, "FAIL", [a.pending_evidence])
        # Автоматический контроль: UNVERIFIED можно переопределить, PASS gate — нет.
        override = _review(conn, a, a.reviewer, LINT_CONTROL)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="not open for human review"):
            _review(conn, a, a.reviewer, SCOPE_CONTROL)
        # Роль ниже reviewer — 42501; не член / чужой workspace / нет пользователя — NULL (→ 404).
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="viewer"):
            _review(conn, a, a.viewer, ADR_CONTROL)
        assert _review(conn, a, b.tenant.owner_id, ADR_CONTROL) is None
        assert _review(conn, a, a.reviewer, ADR_CONTROL, ws=b.ws) is None
        assert _review(conn, a, a.reviewer, uuid7()) is None
        assert _review(conn, a, a.reviewer, ADR_CONTROL, run=b.run) is None
        # Evidence чужого run или PASS без VERIFIED evidence — 23514; неверные аргументы — 22023.
        with pytest.raises(psycopg.errors.CheckViolation, match="belong to run"):
            _review(conn, a, a.reviewer, ADR_CONTROL, evidence=[b.evidence])
        with pytest.raises(psycopg.errors.CheckViolation, match="not VERIFIED"):
            _review(conn, a, a.reviewer, ADR_CONTROL, evidence=[a.pending_evidence])
        for kw in (
            {"status": "MANUAL_REVIEW"},
            {"notes": "   "},
            {"evidence": [a.evidence, a.evidence]},
            {"evidence": [uuid7() for _ in range(21)]},
        ):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                _review(conn, a, a.reviewer, ADR_CONTROL, **kw)
        with pytest.raises(psycopg.errors.InvalidParameterValue), conn.transaction():
            _ctx(conn, a.ws, a.reviewer)
            conn.execute(REVIEW_FN, (a.run, ADR_CONTROL, "PASS", "x", []))
    with connect_admin(database) as admin:
        rows = admin.execute(
            "SELECT id, status, reviewed_by, superseded_at IS NULL, evidence_id FROM compliance_results"
            " WHERE run_id = %s AND control_id = %s ORDER BY checked_at, id",
            (a.run, ADR_CONTROL),
        ).fetchall()
        assert [(r[1], r[2], r[3]) for r in rows] == [
            ("MANUAL_REVIEW", None, False),
            ("PASS", a.reviewer, False),
            ("FAIL", a.tenant.owner_id, True),
        ]
        assert rows[1][0] == first and rows[2][0] == second and rows[1][4] == a.evidence
        links = admin.execute(
            "SELECT target_id, evidence_id, relation FROM evidence_links WHERE target_type = 'compliance_result'"
            " AND target_id = ANY(%s) ORDER BY relation, evidence_id",
            ([first, second],),
        ).fetchall()
        assert {(r[0], r[1], r[2]) for r in links} == {
            (first, a.evidence, "supports"),
            (first, a.pending_evidence, "supports"),
            (second, a.pending_evidence, "refutes"),
        }
        assert admin.execute(
            "SELECT status, reviewed_by FROM compliance_results WHERE id = %s", (override,)
        ).fetchone() == (
            "PASS",
            a.reviewer,
        )
        # Неприменимый контроль и терминальный run — 55000.
        admin.execute("SELECT set_config('app.user_id', %s, false)", (str(a.tenant.owner_id),))
        admin.execute(
            "UPDATE project_controls SET applicability = 'not_applicable', rationale = 'нет', approved_by = %s"
            " WHERE project_id = %s AND control_id = %s",
            (a.tenant.owner_id, a.tenant.project_id, LINT_CONTROL),
        )
        draft = uuid.UUID("01a1c008-0001-7000-8000-000000000014")  # SEC-27002-01 без утверждения
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        for control in (LINT_CONTROL, draft):
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="not approved"):
                _review(conn, a, a.reviewer, control)
    with connect_admin(database) as admin:
        admin.execute(
            "UPDATE runs SET status = 'CANCELLED', finished_at = now(), version = version + 1 WHERE id = %s", (b.run,)
        )
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="CANCELLED"):
            _review(conn, b, b.tenant.owner_id, ADR_CONTROL)
    # Воркер функцию не вызывает.
    with psycopg.connect(database.url_for("aicd_worker")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(REVIEW_FN, (a.run, ADR_CONTROL, "PASS", "x", [str(a.evidence)]))
