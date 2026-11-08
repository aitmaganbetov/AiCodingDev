"""P6-01 / миграция 0008: качество, evidence, реестр стандартов (DATABASE_DESIGN §12.5–12.8, §13, §18.3,
§18.11, §19)."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from dataclasses import dataclass, field

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from aicd_platform.ids import uuid7
from tests.support import db as testdb
from tests.support import quality as q
from tests.support.orchestration import Tenant, connect_admin, drive_run, lease_task, new_run, new_task, seed_tenant

pytestmark = pytest.mark.db

GLOBAL_TABLES = ["standards", "standard_controls"]
TENANT_TABLES = ["test_results", "findings", "evidence_links", "project_controls", "compliance_results"]
NEW_TABLES = set(GLOBAL_TABLES) | set(TENANT_TABLES)
CONTROL_ID = uuid.UUID("01a1c008-0001-7000-8000-000000000013")  # SEC-27001-01 (SCOPE)
OTHER_CONTROL_ID = uuid.UUID("01a1c008-0001-7000-8000-000000000014")  # SEC-27002-01 (SECRETS)


def _tables(db: testdb.TestDatabase) -> set[str]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'aicd' AND c.relkind IN ('r','p') AND NOT c.relispartition"
        ).fetchall()
    return {r[0] for r in rows}


def _constraint(db: testdb.TestDatabase, table: str, name_like: str) -> list[tuple[str, str]]:
    with psycopg.connect(db.superuser_url) as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = %s::regclass AND contype = 'c' AND conname LIKE %s",
                (f"aicd.{table}", name_like),
            ).fetchall()
        ]


def _has_resolver(db: testdb.TestDatabase) -> bool:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'aicd' AND p.proname = 'sys_resolve_evidence_workspace'"
        ).fetchone()
    return row == (1,)


# --- обратимость и offline-режим --------------------------------------------------------------------


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig8")
    yield db
    testdb.drop_database(db)


def test_0008_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "head")
    created = _tables(scratch_db)
    assert created >= NEW_TABLES
    assert "'finding'" in _constraint(scratch_db, "allowed_transitions", "ck_allowed_transitions_entity")[0][1]
    assert "gate_log" in _constraint(scratch_db, "artifacts", "ck_artifacts_kind")[0][1]
    assert _has_resolver(scratch_db)
    testdb.alembic(scratch_db, "downgrade", "0007")
    assert not (NEW_TABLES & _tables(scratch_db))
    assert not _has_resolver(scratch_db)
    entity = _constraint(scratch_db, "allowed_transitions", "ck_allowed_transitions_entity")
    assert len(entity) == 1 and "finding" not in entity[0][1] and "indexing_job" in entity[0][1]
    kinds = _constraint(scratch_db, "artifacts", "artifacts_kind_check")
    assert len(kinds) == 1 and "gate_log" not in kinds[0][1]
    with psycopg.connect(scratch_db.superuser_url) as conn:
        assert conn.execute("SELECT count(*) FROM aicd.allowed_transitions WHERE entity = 'finding'").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'aicd'"
            " AND table_name = 'gate_executions' AND column_name = 'error_code'"
        ).fetchone() == (0,)
        for fn in ("trg_fn_evidence_links_target", "trg_fn_findings_guard", "trg_fn_gate_executions_test_results"):
            assert conn.execute("SELECT to_regproc(%s)", (f"aicd.{fn}",)).fetchone() == (None,)
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _tables(scratch_db) == created


def test_0008_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0007:0008", sql=True)
    out = buf.getvalue()
    for table in NEW_TABLES:
        assert f"CREATE TABLE {table} (" in out
    assert "INSERT INTO standard_controls" in out and "ADD CONSTRAINT ck_artifacts_kind" in out
    assert "INSERT INTO quality_gates" not in out  # gates сидирует P6-02/P6-03
    assert "CREATE FUNCTION sys_resolve_evidence_workspace" in out


# --- схема: RLS, seed --------------------------------------------------------------------------------


def test_rls_classification(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url) as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
                " (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'aicd' AND c.relname = ANY(%s)",
                (sorted(NEW_TABLES),),
            ).fetchall()
        }
    assert set(rows) == NEW_TABLES
    for table in TENANT_TABLES:
        assert rows[table] == (True, True, 1), table
    for table in GLOBAL_TABLES:
        assert rows[table] == (False, False, 0), table


def test_seed_registry(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url, options="-c search_path=aicd") as conn:
        assert conn.execute("SELECT count(*) FROM standards").fetchone() == (18,)
        assert conn.execute("SELECT count(*) FROM standard_controls").fetchone() == (33,)
        # Ни одного длинного текста: только собственные краткие формулировки (RISK-13).
        assert conn.execute("SELECT max(length(description)) FROM standard_controls").fetchone()[0] <= 200  # type: ignore[index]
        assert conn.execute(
            "SELECT count(*) FROM standards WHERE license_note IS NULL OR reference_uri !~ '^https://'"
        ).fetchone() == (0,)
        row = conn.execute(
            "SELECT s.code, c.control_code, c.default_gate_type FROM standard_controls c"
            " JOIN standards s ON s.id = c.standard_id WHERE c.id = %s",
            (CONTROL_ID,),
        ).fetchone()
        assert row == ("ISO/IEC 27001", "SEC-27001-01", "SCOPE")


# --- данные двух арендаторов -------------------------------------------------------------------------


@dataclass
class Side:
    tenant: Tenant
    run: uuid.UUID
    task: uuid.UUID
    commit: str
    gate_id: uuid.UUID
    evidence: uuid.UUID
    gate_execution: uuid.UUID
    artifact: uuid.UUID
    ids: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def ws(self) -> uuid.UUID:
        return self.tenant.workspace_id


Inserter = Callable[[psycopg.Connection, Side], uuid.UUID]


def ins_test_results(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO test_results (id, workspace_id, gate_execution_id, suite, passed, failed, skipped,"
        " report_artifact_id) VALUES (%s, %s, %s, %s, 3, 0, 0, %s)",
        (rid, s.ws, s.gate_execution, f"suite-{rid.hex}", s.artifact),
    )
    return rid


def ins_findings(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    return q.finding(conn, s.tenant, s.run, gate=(s.gate_execution, s.gate_id), evidence_id=s.evidence)


def ins_evidence_links(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO evidence_links (id, workspace_id, evidence_id, target_type, target_id, relation)"
        " VALUES (%s, %s, %s, 'gate_execution', %s, 'produced_by')",
        (rid, s.ws, s.evidence, s.gate_execution),
    )
    return rid


def ins_project_controls(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    conn.execute(
        "INSERT INTO project_controls (workspace_id, project_id, control_id, applicability)"
        " VALUES (%s, %s, %s, 'applicable') ON CONFLICT DO NOTHING",
        (s.ws, s.tenant.project_id, CONTROL_ID),
    )
    return CONTROL_ID


def ins_compliance_results(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO compliance_results (id, workspace_id, run_id, control_id, status, evidence_id, gate_execution_id)"
        " VALUES (%s, %s, %s, %s, 'UNVERIFIED', %s, %s)",
        (rid, s.ws, s.run, OTHER_CONTROL_ID, s.evidence, s.gate_execution),
    )
    return rid


INSERTERS: dict[str, Inserter] = {t: globals()[f"ins_{t}"] for t in TENANT_TABLES}


def _side(conn: psycopg.Connection) -> Side:
    tenant = seed_tenant(conn)
    run = new_run(conn, tenant)
    drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
    task = new_task(conn, tenant, run)
    lease_task(conn, task)
    commit = q.sha1()
    gate_id = q.ensure_gate(conn, "scope", "SCOPE")
    ev = q.evidence(conn, tenant, run, commit)
    ge = q.gate_execution(conn, tenant, run, gate_id, "SCOPE", "PASS", commit, evidence_id=ev)
    side = Side(tenant, run, task, commit, gate_id, ev, ge, q.artifact(conn, tenant, run, "test_report"))
    for table in TENANT_TABLES:  # порядок списка = порядок зависимостей
        side.ids[table] = INSERTERS[table](conn, side)
    return side


@dataclass(frozen=True)
class World:
    a: Side
    b: Side


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
def world(admin: psycopg.Connection) -> World:
    return World(_side(admin), _side(admin))


def _ctx(conn: psycopg.Connection, ws: uuid.UUID | None, user: uuid.UUID | None = None) -> None:
    conn.execute(
        "SELECT set_config('app.user_id', %s, true), set_config('app.workspace_id', %s, true)",
        (str(user) if user else "", str(ws) if ws else ""),
    )


# --- RLS ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["aicd_app", "aicd_worker"])
@pytest.mark.parametrize("table", TENANT_TABLES)
def test_tenant_isolation(database: testdb.TestDatabase, world: World, role: str, table: str) -> None:
    a, b = world.a, world.b
    with psycopg.connect(database.url_for(role)) as conn:
        with conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            seen = {r[0] for r in conn.execute(f"SELECT DISTINCT workspace_id FROM {table}").fetchall()}
            assert seen == {a.ws}
            try:
                with conn.transaction():
                    res = conn.execute(
                        f"UPDATE {table} SET workspace_id = workspace_id WHERE workspace_id = %s", (b.ws,)
                    )
                    assert res.rowcount == 0
            except psycopg.errors.InsufficientPrivilege:
                pass
        with conn.transaction():
            _ctx(conn, None)
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), "без контекста — fail-closed"
        # Вставка строки чужого workspace: отказ RLS/права; триггеры, читающие цель (§18.11, §13.4),
        # не видят строк чужого workspace и отклоняют раньше RLS.
        expected: tuple[type[Exception], ...] = (psycopg.errors.InsufficientPrivilege,)
        if table == "evidence_links":
            expected += (psycopg.errors.ForeignKeyViolation,)
        if table == "compliance_results":
            expected += (psycopg.errors.CheckViolation,)
        with pytest.raises(expected), conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            INSERTERS[table](conn, b)


@pytest.mark.parametrize("role", ["aicd_app", "aicd_worker"])
def test_global_registry_readable_not_writable(database: testdb.TestDatabase, role: str) -> None:
    with psycopg.connect(database.url_for(role)) as conn:
        for table in GLOBAL_TABLES:
            with conn.transaction():
                _ctx(conn, None)  # справочник G виден без контекста workspace
                count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                assert count is not None and count[0] >= 18
            for stmt in (
                f"UPDATE {table} SET active = false",
                f"DELETE FROM {table}",
                "INSERT INTO standards (id, code, edition, title) VALUES ('01a1c008-0000-7000-8000-0000000000ff',"
                " 'X 1', '1', 't')",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                    conn.execute(stmt)


def test_worker_writes_results(database: testdb.TestDatabase, world: World) -> None:
    """worker-verify/compliance под RLS: результаты, находки, трассировка; без переписывания истории."""
    a = world.a
    with psycopg.connect(database.url_for("aicd_worker")) as conn, conn.transaction():
        _ctx(conn, a.ws)
        side = Side(a.tenant, a.run, a.task, a.commit, a.gate_id, a.evidence, a.gate_execution, a.artifact)
        ins_test_results(conn, side)
        fid = ins_findings(conn, side)
        conn.execute("UPDATE findings SET status = 'ACKNOWLEDGED' WHERE id = %s", (fid,))
        conn.execute(
            "UPDATE compliance_results SET superseded_at = now() WHERE id = %s", (a.ids["compliance_results"],)
        )
        ins_compliance_results(conn, side)
        for stmt in (
            "UPDATE test_results SET passed = 0",
            "DELETE FROM test_results",
            "UPDATE compliance_results SET notes = 'x'",
            "DELETE FROM findings",
            "DELETE FROM evidence_links",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(stmt)


def test_app_permissions(database: testdb.TestDatabase, world: World) -> None:
    a = world.a
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, a.ws, a.tenant.owner_id)
        conn.execute(
            "UPDATE findings SET status = 'ACKNOWLEDGED' WHERE id = %s AND status = 'OPEN'", (a.ids["findings"],)
        )
        side = Side(a.tenant, a.run, a.task, a.commit, a.gate_id, a.evidence, a.gate_execution, a.artifact)
        for table in ("test_results", "findings", "evidence_links", "compliance_results"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                INSERTERS[table](conn, side)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE findings SET severity = 'low'")


# --- Finding FSM и правила закрытия -------------------------------------------------------------------


def _transition(admin: psycopg.Connection, fid: uuid.UUID, status: str) -> None:
    admin.execute("UPDATE findings SET status = %s WHERE id = %s", (status, fid))


def test_finding_fsm(admin: psycopg.Connection, world: World) -> None:
    a = world.a
    rows = admin.execute("SELECT from_status, to_status FROM allowed_transitions WHERE entity = 'finding'").fetchall()
    assert ("__new__", "OPEN") in rows and ("FIXED", "VERIFIED_FIXED") in rows and len(rows) == 16
    with pytest.raises(psycopg.errors.CheckViolation, match="invalid finding transition"):
        admin.execute(
            "INSERT INTO findings (id, workspace_id, run_id, source, severity, rule_id, title, status, fingerprint)"
            " VALUES (%s, %s, %s, 'manual', 'low', 'r', 't', 'FIXED', %s)",
            (uuid7(), a.ws, a.run, q.sha256()),
        )
    fid = q.finding(admin, a.tenant, a.run, gate=(a.gate_execution, a.gate_id), evidence_id=a.evidence)
    _transition(admin, fid, "ACKNOWLEDGED")
    _transition(admin, fid, "FIXED")
    with pytest.raises(psycopg.errors.CheckViolation, match="invalid finding transition"):
        _transition(admin, fid, "ACKNOWLEDGED")
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_findings_verified_fixed"):
        _transition(admin, fid, "VERIFIED_FIXED")  # нет fixed_in_commit_sha
    # VERIFIED_FIXED опирается только на VERIFIED evidence.
    unverified = q.evidence(admin, a.tenant, a.run, a.commit, status="UNVERIFIED", producer="agent", kind="agent_claim")
    with pytest.raises(psycopg.errors.CheckViolation, match="VERIFIED evidence"):
        admin.execute(
            "UPDATE findings SET status = 'VERIFIED_FIXED', fixed_in_commit_sha = %s, evidence_id = %s WHERE id = %s",
            (a.commit, unverified, fid),
        )
    admin.execute(
        "UPDATE findings SET status = 'VERIFIED_FIXED', fixed_in_commit_sha = %s, evidence_id = %s WHERE id = %s",
        (a.commit, a.evidence, fid),
    )
    _transition(admin, fid, "OPEN")  # регрессия


def test_finding_human_resolution(database: testdb.TestDatabase, admin: psycopg.Connection, world: World) -> None:
    a = world.a
    fid = q.finding(admin, a.tenant, a.run, severity="critical")
    resolve = "UPDATE findings SET status = 'ACCEPTED_RISK', resolved_by = %s, resolution_reason = 'ok' WHERE id = %s"
    # Агент/система (worker без app.user_id) не может принять риск, даже указав человека.
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="session user"), conn.transaction():
            _ctx(conn, a.ws)
            conn.execute(resolve, (a.tenant.owner_id, fid))
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        other = uuid7()
        admin.execute("INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'x')", (other, f"{other}@q.test"))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="session user"), conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            conn.execute(resolve, (other, fid))  # от имени другого человека
        with pytest.raises(psycopg.errors.CheckViolation, match="ck_findings_human_resolution"), conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            conn.execute(
                "UPDATE findings SET status = 'FALSE_POSITIVE', resolved_by = %s WHERE id = %s",
                (a.tenant.owner_id, fid),
            )  # без обоснования
        with conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            conn.execute(resolve, (a.tenant.owner_id, fid))
    assert admin.execute("SELECT status FROM findings WHERE id = %s", (fid,)).fetchone() == ("ACCEPTED_RISK",)


# --- инварианты test_results, compliance_results, evidence_links, project_controls -------------------


def test_test_results_gate_consistency(admin: psycopg.Connection, world: World) -> None:
    a = world.a
    with pytest.raises(psycopg.errors.CheckViolation, match="contradict PASS"):
        admin.execute(
            "INSERT INTO test_results (id, workspace_id, gate_execution_id, suite, passed, failed, skipped,"
            " report_artifact_id) VALUES (%s, %s, %s, 'broken', 1, 2, 0, %s)",
            (uuid7(), a.ws, a.gate_execution, a.artifact),
        )
    gate_id = q.ensure_gate(admin, "pytest-unit", "UNIT")
    ge = q.gate_execution(admin, a.tenant, a.run, gate_id, "UNIT", "RUNNING", a.commit)
    admin.execute(
        "INSERT INTO test_results (id, workspace_id, gate_execution_id, suite, passed, failed, skipped, errors,"
        " report_artifact_id) VALUES (%s, %s, %s, 'unit', 5, 0, 0, 1, %s)",
        (uuid7(), a.ws, ge, a.artifact),
    )
    with pytest.raises(psycopg.errors.CheckViolation, match="PASS contradicts failed tests"):
        admin.execute(
            "UPDATE gate_executions SET status = 'PASS', evidence_id = %s, finished_at = now() WHERE id = %s",
            (a.evidence, ge),
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        admin.execute("UPDATE test_results SET passed = 6 WHERE gate_execution_id = %s", (ge,))
    admin.execute("UPDATE gate_executions SET superseded_at = now() WHERE id = %s", (ge,))


def test_compliance_results_rules(admin: psycopg.Connection, world: World) -> None:
    a, b = world.a, world.b

    def insert(
        status: str, evidence: uuid.UUID | None, notes: str | None = None, gate: uuid.UUID | None = None
    ) -> None:
        admin.execute(
            "INSERT INTO compliance_results (id, workspace_id, run_id, control_id, status, evidence_id,"
            " gate_execution_id, notes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (uuid7(), a.ws, a.run, CONTROL_ID, status, evidence, gate, notes),
        )

    with pytest.raises(psycopg.errors.CheckViolation, match="ck_compliance_results_pass_evidence"):
        insert("PASS", None)
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_compliance_results_na"):
        insert("NOT_APPLICABLE", None)
    agent = q.evidence(admin, a.tenant, a.run, a.commit, status="UNVERIFIED", producer="agent", kind="agent_claim")
    with pytest.raises(psycopg.errors.CheckViolation, match="not VERIFIED evidence"):
        insert("PASS", agent)  # слова агента не дают PASS контролю
    other_run = new_run(admin, a.tenant)
    other_ev = q.evidence(admin, a.tenant, other_run, a.commit)
    with pytest.raises(psycopg.errors.CheckViolation, match="not VERIFIED evidence of run"):
        insert("PASS", other_ev)  # evidence другого run
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)):
        insert("FAIL", b.evidence)  # чужой workspace (триггер раньше составного FK)
    insert("PASS", a.evidence, gate=a.gate_execution)
    with pytest.raises(psycopg.errors.UniqueViolation, match="uq_compliance_results_current"):
        insert("FAIL", a.evidence)
    rid = admin.execute(
        "SELECT id FROM compliance_results WHERE run_id = %s AND control_id = %s AND superseded_at IS NULL",
        (a.run, CONTROL_ID),
    ).fetchone()[0]  # type: ignore[index]
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="immutable"):
        admin.execute("UPDATE compliance_results SET status = 'FAIL' WHERE id = %s", (rid,))
    admin.execute("UPDATE compliance_results SET superseded_at = now() WHERE id = %s", (rid,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="immutable"):
        admin.execute("UPDATE compliance_results SET superseded_at = now() WHERE id = %s", (rid,))


def test_evidence_links_polymorphic_target(admin: psycopg.Connection, world: World) -> None:
    a, b = world.a, world.b

    def link(target_type: str, target_id: uuid.UUID, relation: str = "references") -> None:
        admin.execute(
            "INSERT INTO evidence_links (id, workspace_id, evidence_id, target_type, target_id, relation)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (uuid7(), a.ws, a.evidence, target_type, target_id, relation),
        )

    link("task", a.task, "verifies")
    link("finding", a.ids["findings"], "supports")
    link("test_result", a.ids["test_results"])
    link("compliance_result", a.ids["compliance_results"])
    for target_type, target_id in (
        ("task", uuid7()),  # нет такой задачи
        ("finding", a.task),  # тип не совпадает с таблицей
        ("gate_execution", b.gate_execution),  # чужой workspace
        ("architecture_decision", uuid7()),
        ("pull_request", uuid7()),  # таблица Phase 8 ещё не создана
    ):
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match="not found"):
            link(target_type, target_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        link("task", a.task, "approves")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        admin.execute("UPDATE evidence_links SET relation = 'refutes' WHERE workspace_id = %s", (a.ws,))


def test_project_controls_approval(database: testdb.TestDatabase, admin: psycopg.Connection, world: World) -> None:
    a = world.a
    approve = (
        "INSERT INTO project_controls (workspace_id, project_id, control_id, applicability, rationale, approved_by,"
        " approved_at) VALUES (%s, %s, %s, 'not_applicable', 'no web', %s, now())"
        " ON CONFLICT ON CONSTRAINT pk_project_controls DO UPDATE SET applicability = EXCLUDED.applicability,"
        " rationale = EXCLUDED.rationale, approved_by = EXCLUDED.approved_by, approved_at = EXCLUDED.approved_at"
    )
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="approved by the session user"):
            with conn.transaction():
                _ctx(conn, a.ws)
                conn.execute(approve, (a.ws, a.tenant.project_id, CONTROL_ID, a.tenant.owner_id))
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            conn.execute(approve, (a.ws, a.tenant.project_id, CONTROL_ID, a.tenant.owner_id))
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        for stmt in (
            "UPDATE project_controls SET applicability = 'applicable' WHERE control_id = %s",
            "DELETE FROM project_controls WHERE control_id = %s",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="only by a user"), conn.transaction():
                _ctx(conn, a.ws)
                conn.execute(stmt, (CONTROL_ID,))
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_project_controls_rationale"):
        admin.execute(
            "INSERT INTO project_controls (workspace_id, project_id, control_id, applicability)"
            " VALUES (%s, %s, %s, 'tailored')",
            (a.ws, a.tenant.project_id, OTHER_CONTROL_ID),
        )


def test_key_checks(admin: psycopg.Connection, world: World) -> None:
    a = world.a
    bad_findings = (
        ("cwe_id", "CWE-123456", "findings_cwe_id_check"),
        ("file_path", "../etc/passwd", "findings_file_path_check"),
        ("file_path", "/abs/path", "findings_file_path_check"),
    )
    for column, value, constraint in bad_findings:
        with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
            admin.execute(
                f"INSERT INTO findings (id, workspace_id, run_id, source, severity, rule_id, title, fingerprint, {column})"
                " VALUES (%s, %s, %s, 'manual', 'low', 'r', 't', %s, %s)",
                (uuid7(), a.ws, a.run, q.sha256(), value),
            )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_findings_line"):
        admin.execute(
            "INSERT INTO findings (id, workspace_id, run_id, source, severity, rule_id, title, fingerprint, line)"
            " VALUES (%s, %s, %s, 'manual', 'low', 'r', 't', %s, 3)",
            (uuid7(), a.ws, a.run, q.sha256()),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_findings_gate_source"):
        admin.execute(
            "INSERT INTO findings (id, workspace_id, run_id, source, severity, rule_id, title, fingerprint)"
            " VALUES (%s, %s, %s, 'gate', 'low', 'r', 't', %s)",
            (uuid7(), a.ws, a.run, q.sha256()),
        )
    other_gate = q.ensure_gate(admin, "ruff-lint", "LINT")
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="fk_findings_gate_execution_gate"):
        q.finding(admin, a.tenant, a.run, gate=(a.gate_execution, other_gate))  # запуск другого gate
    with pytest.raises(psycopg.errors.CheckViolation, match="test_results_passed_check"):
        admin.execute(
            "INSERT INTO test_results (id, workspace_id, gate_execution_id, suite, passed, failed, skipped,"
            " report_artifact_id) VALUES (%s, %s, %s, 's', -1, 0, 0, %s)",
            (uuid7(), a.ws, a.gate_execution, a.artifact),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_gate_executions_error_code"):
        q.gate_execution(admin, a.tenant, a.run, other_gate, "LINT", "ERROR", a.commit, error_code="OOPS")
    ge = q.gate_execution(admin, a.tenant, a.run, other_gate, "LINT", "ERROR", a.commit, error_code="TOOL_UNAVAILABLE")
    admin.execute("UPDATE gate_executions SET superseded_at = now() WHERE id = %s", (ge,))
    q.artifact(admin, a.tenant, a.run, "gate_log")
    q.artifact(admin, a.tenant, a.run, "applicability_report")
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_artifacts_kind"):
        q.artifact(admin, a.tenant, a.run, "bogus")
    with pytest.raises(psycopg.errors.CheckViolation, match="standards_code_check"):
        admin.execute("INSERT INTO standards (id, code, edition, title) VALUES (%s, 'bad;code', '1', 't')", (uuid7(),))
    std = admin.execute("SELECT id FROM standards WHERE code = 'CWE'").fetchone()[0]  # type: ignore[index]
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_standard_controls_automated"):
        admin.execute(
            "INSERT INTO standard_controls (id, standard_id, clause_ref, control_code, description,"
            " verification_method) VALUES (%s, %s, 'CWE-1', 'X-1', 'd', 'automated_gate')",
            (uuid7(), std),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="standard_controls_control_code_check"):
        admin.execute(
            "INSERT INTO standard_controls (id, standard_id, clause_ref, control_code, description,"
            " verification_method) VALUES (%s, %s, 'CWE-1', 'bad code!', 'd', 'manual')",
            (uuid7(), std),
        )


def test_error_code_check_matches_gate_error_codes(database: testdb.TestDatabase) -> None:
    """CHECK ck_gate_executions_error_code = gates.ErrorCode: движок может записать любой свой код."""
    import re
    from typing import get_args

    from aicd_verification.gates import ErrorCode

    with psycopg.connect(database.superuser_url) as conn:
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_gate_executions_error_code'"
        ).fetchone()
    assert definition is not None
    assert set(re.findall(r"'([A-Z_]+)'", definition[0])) == set(get_args(ErrorCode))


# --- SECURITY DEFINER резолвер evidence (GET /evidence/{id}, AC-12) ---------------------------------------


def test_sys_resolve_evidence_workspace_members_only(database: testdb.TestDatabase, world: World) -> None:
    a, b = world.a, world.b
    with psycopg.connect(database.superuser_url) as conn:
        row = conn.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'aicd' AND p.proname = 'sys_resolve_evidence_workspace'"
        ).fetchone()
    assert row is not None and row[0] is True and row[1] == "aicd_sysfn"
    assert any(c.startswith("search_path=") for c in row[2])
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, None, a.tenant.owner_id)  # workspace ещё не известен — резолвер его и находит
        resolve = "SELECT sys_resolve_evidence_workspace(%s)"
        assert conn.execute(resolve, (a.evidence,)).fetchone() == (a.ws,)
        assert conn.execute(resolve, (b.evidence,)).fetchone() == (None,), "чужой evidence неотличим от отсутствующего"
        assert conn.execute(resolve, (uuid7(),)).fetchone() == (None,)
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, None)  # без пользователя сессии — ничего
        assert conn.execute("SELECT sys_resolve_evidence_workspace(%s)", (a.evidence,)).fetchone() == (None,)
    for role in ("aicd_worker", "aicd_maintenance"):
        with psycopg.connect(database.url_for(role)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("SELECT sys_resolve_evidence_workspace(%s)", (a.evidence,))


def test_gate_seed_matches_registry(database: testdb.TestDatabase) -> None:
    """Seed 0009 = `registry.seed_rows()`: новые gates реестра без seed (или расхождение `required`) ломают CI."""
    from aicd_verification.registry import seed_rows

    expected = {(r["profile"], r["name"]): (r["gate_type"], r["required"], r["timeout_seconds"]) for r in seed_rows()}
    with psycopg.connect(database.superuser_url) as conn:
        rows = conn.execute(
            "SELECT p.code, g.name, g.gate_type, g.required, g.timeout_seconds FROM aicd.quality_gates g"
            " JOIN aicd.quality_profiles p ON p.id = g.profile_id WHERE g.workspace_id IS NULL AND p.version = 1"
        ).fetchall()
    actual = {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}
    assert actual == expected
