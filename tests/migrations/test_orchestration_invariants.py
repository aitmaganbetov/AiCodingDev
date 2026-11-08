"""P3-01: инварианты миграции 0003 на реальном PostgreSQL (DATABASE_DESIGN §18, §19)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest

from aicd_platform.ids import uuid7
from tests.support import db as testdb
from tests.support.orchestration import (
    Tenant,
    add_user,
    connect_admin,
    drive_run,
    lease_task,
    new_run,
    new_task,
    seed_tenant,
    update,
)

pytestmark = pytest.mark.db

DOC = testdb.ROOT / "docs" / "database" / "DATABASE_DESIGN.md"
SHA_A, SHA_B = "a" * 64, "b" * 64
COMMIT = "c" * 40


@dataclass(frozen=True)
class World:
    a: Tenant
    b: Tenant
    gate_id: uuid.UUID
    artifact_a: uuid.UUID


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
def world(admin: psycopg.Connection) -> World:
    a, b = seed_tenant(admin), seed_tenant(admin)
    gate, profile = uuid7(), uuid7()
    # Gate в собственном профиле workspace A (не в глобальном seed: иначе он попал бы в планы run'ов
    # других тестов и в сверку seed с реестром gates).
    admin.execute(
        "INSERT INTO quality_profiles (id, workspace_id, code, version, definition) VALUES (%s, %s, %s, 1, '{}')",
        (profile, a.workspace_id, "basic"),
    )
    admin.execute(
        "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type) VALUES (%s, %s, %s, %s, 'UNIT')",
        (gate, a.workspace_id, profile, f"unit-{gate.hex[-8:]}"),
    )
    artifact = uuid7()
    admin.execute(
        "INSERT INTO artifacts (id, workspace_id, kind, storage_ref, sha256, size_bytes, mime_type)"
        " VALUES (%s, %s, 'test_report', %s, %s, 1, 'text/plain')",
        (artifact, a.workspace_id, f"file://artifacts/{artifact}", SHA_A),
    )
    return World(a, b, gate, artifact)


def _evidence(conn: psycopg.Connection, world: World, run: uuid.UUID, *, verified: bool) -> uuid.UUID:
    ev = uuid7()
    conn.execute(
        "INSERT INTO evidence (id, workspace_id, run_id, kind, artifact_id, producer, verification_status, commit_sha,"
        " command, exit_code, started_at, finished_at, verified_at)"
        " VALUES (%s, %s, %s, 'test_report', %s, 'verification_engine', %s, %s, 'pytest', 0, now(), now(), %s)",
        (
            ev,
            world.a.workspace_id,
            run,
            world.artifact_a,
            "VERIFIED" if verified else "PENDING",
            COMMIT,
            "now" if verified else None,
        ),
    )
    return ev


def _gate(
    conn: psycopg.Connection,
    world: World,
    run: uuid.UUID,
    status: str,
    *,
    task: uuid.UUID | None = None,
    evidence: uuid.UUID | None = None,
) -> uuid.UUID:
    ge = uuid7()
    terminal = status in ("PASS", "FAIL", "ERROR", "SKIPPED", "NOT_APPLICABLE")
    conn.execute(
        "INSERT INTO gate_executions (id, workspace_id, run_id, task_id, gate_id, gate_type, required, status,"
        " commit_sha, evidence_id, finished_at) VALUES (%s, %s, %s, %s, %s, 'UNIT', true, %s, %s, %s, %s)",
        (ge, world.a.workspace_id, run, task, world.gate_id, status, COMMIT, evidence, "now" if terminal else None),
    )
    return ge


def _approval(
    conn: psycopg.Connection,
    tenant: Tenant,
    *,
    run: uuid.UUID | None = None,
    action: str = "PLAN_APPROVAL",
    required_role: str = "developer",
    requested_by: uuid.UUID | None = None,
    subject_hash: str = SHA_A,
) -> uuid.UUID:
    ap = uuid7()
    conn.execute(
        "INSERT INTO approvals (id, workspace_id, project_id, run_id, action_type, risk_level, required_role,"
        " requested_by_type, requested_by, subject_hash) VALUES (%s, %s, %s, %s, %s, 'high', %s, %s, %s, %s)",
        (
            ap,
            tenant.workspace_id,
            tenant.project_id,
            run,
            action,
            required_role,
            "user" if requested_by else "system",
            requested_by,
            subject_hash,
        ),
    )
    return ap


def _decide(conn: psycopg.Connection, approval: uuid.UUID, by: uuid.UUID, status: str = "APPROVED") -> None:
    update(conn, "approvals", approval, status=status, decided_by=by, decided_at="now", decision_reason="ok")


# --- §18.3 переходы ---------------------------------------------------------------------------


def _doc_transitions() -> set[tuple[str, str, str]]:
    section = DOC.read_text().split("### 18.3", 1)[1].split("### 18.4", 1)[0]
    rows: set[tuple[str, str, str]] = set()
    for line in section.splitlines():
        m = re.match(
            r"^\| (run|task|execution|directive|approval|external_action|indexing_job|finding) \| `([A-Z_a-z]+)` \| (.+) \|$",
            line,
        )
        if m:
            targets = re.sub(r"\([^)]*\)", "", m.group(3))  # пояснения в скобках — не переходы
            for to in re.findall(r"`([A-Z_]+)`", targets):
                rows.add((m.group(1), m.group(2), to))
    return rows


def test_allowed_transitions_match_design(admin: psycopg.Connection) -> None:
    expected = _doc_transitions()
    assert {e for e, _, _ in expected} == {
        "run",
        "task",
        "execution",
        "directive",
        "approval",
        "external_action",
        "indexing_job",
        "finding",
    }
    actual = {tuple(r) for r in admin.execute("SELECT entity, from_status, to_status FROM allowed_transitions")}
    assert actual == expected


def test_invalid_run_and_task_transitions_rejected(admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.a)
    with pytest.raises(psycopg.errors.CheckViolation, match="invalid run transition: CREATED -> RUNNING"):
        update(admin, "runs", run, status="RUNNING")
    with pytest.raises(psycopg.errors.CheckViolation, match="invalid run transition: __new__ -> RUNNING"):
        admin.execute(
            "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective, status,"
            " quality_profile, quality_profile_id, documentation_mode) SELECT %s, workspace_id, project_id,"
            " repository_id, requested_by, mode, objective, 'RUNNING', quality_profile, quality_profile_id,"
            " documentation_mode FROM runs WHERE id = %s",
            (uuid7(), run),
        )
    task = new_task(admin, world.a, run)
    with pytest.raises(psycopg.errors.CheckViolation, match="invalid task transition: PENDING -> RUNNING"):
        update(admin, "tasks", task, status="RUNNING", lease_fencing_token=1)


def test_resume_guard(admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.a)
    drive_run(admin, run, "DISCOVERY", "PLANNED", "RUNNING")
    update(admin, "runs", run, status="PAUSED", resume_status="RUNNING")
    with pytest.raises(psycopg.errors.CheckViolation, match="resume_status"):
        update(admin, "runs", run, status="PLANNED", resume_status=None)
    update(admin, "runs", run, status="RUNNING", resume_status=None)


def test_quality_profile_scope(admin: psycopg.Connection, world: World) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="quality profile"):
        admin.execute(
            "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
            " quality_profile, quality_profile_id, documentation_mode) VALUES (%s, %s, %s, %s, %s, 'DEVELOP', 'o',"
            " 'basic', '01a1c000-0000-7000-8000-000000000002', 'SMART')",
            (uuid7(), world.a.workspace_id, world.a.project_id, world.a.repository_id, world.a.owner_id),
        )


# --- §18.1/18.2 DAG ---------------------------------------------------------------------------


def _dep(conn: psycopg.Connection, tenant: Tenant, run: uuid.UUID, task: uuid.UUID, on: uuid.UUID) -> None:
    conn.execute(
        "INSERT INTO task_dependencies (workspace_id, run_id, task_id, depends_on_task_id) VALUES (%s, %s, %s, %s)",
        (tenant.workspace_id, run, task, on),
    )


def test_cross_run_dependency_rejected(admin: psycopg.Connection, world: World) -> None:
    r1, r2 = new_run(admin, world.a), new_run(admin, world.a)
    t1, t2 = new_task(admin, world.a, r1), new_task(admin, world.a, r2)
    with pytest.raises(psycopg.errors.CheckViolation, match="same run"):
        _dep(admin, world.a, r1, t1, t2)
    with pytest.raises(psycopg.errors.CheckViolation, match="no_self"):
        _dep(admin, world.a, r1, t1, t1)


def test_cycle_rejected_at_commit(database: testdb.TestDatabase, admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.a)
    a, b, c = (new_task(admin, world.a, run, key) for key in ("a", "b", "c"))
    with connect_admin(database, autocommit=False) as conn:
        _dep(conn, world.a, run, b, a)
        _dep(conn, world.a, run, c, b)
        conn.commit()  # ацикличный граф принят
        _dep(conn, world.a, run, a, c)  # цикл a→c→b→a: до commit ошибки нет (deferred)
        with pytest.raises(psycopg.errors.CheckViolation, match="cycle"):
            conn.commit()
        conn.rollback()
        # SET CONSTRAINTS IMMEDIATE: проверка на каждой строке, отметка «уже проверено» не скрывает новое ребро.
        conn.execute("SET CONSTRAINTS trg_task_dependencies_acyclic IMMEDIATE")
        d = new_task(conn, world.a, run, "d")
        _dep(conn, world.a, run, d, c)
        with pytest.raises(psycopg.errors.CheckViolation, match="cycle"):
            _dep(conn, world.a, run, a, d)
        conn.rollback()
    count = admin.execute("SELECT count(*) FROM task_dependencies WHERE run_id = %s", (run,)).fetchone()
    assert count == (2,)


# --- §18.4 guards ------------------------------------------------------------------------------


def test_run_completion_guard(admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.a)
    drive_run(admin, run, "DISCOVERY", "PLANNED")
    update(admin, "runs", run, approved_plan_version=1)
    drive_run(admin, run, "RUNNING", "VERIFYING")

    def complete() -> None:
        update(admin, "runs", run, status="COMPLETED", finished_at="now")

    with pytest.raises(psycopg.errors.CheckViolation, match="no PASS gate"):
        complete()
    failing = _gate(admin, world, run, "FAIL", evidence=_evidence(admin, world, run, verified=False))
    with pytest.raises(psycopg.errors.CheckViolation, match="required gates not passed"):
        complete()
    admin.execute("UPDATE gate_executions SET superseded_at = now() WHERE id = %s", (failing,))
    with pytest.raises(psycopg.errors.CheckViolation, match="VERIFIED evidence"):
        _gate(admin, world, run, "PASS", evidence=_evidence(admin, world, run, verified=False))
    _gate(admin, world, run, "PASS", evidence=_evidence(admin, world, run, verified=True))
    approval = _approval(admin, world.a, run=run)
    with pytest.raises(psycopg.errors.CheckViolation, match="approval policy"):
        complete()
    _decide(admin, approval, world.a.owner_id)
    complete()
    assert admin.execute("SELECT status FROM runs WHERE id = %s", (run,)).fetchone() == ("COMPLETED",)
    # §18.4 гонки: в завершённый run результаты не пишутся.
    with pytest.raises(psycopg.errors.CheckViolation, match="COMPLETED"):
        _gate(admin, world, run, "ERROR")


def test_task_success_guard(admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.a)
    drive_run(admin, run, "DISCOVERY", "PLANNED", "RUNNING")
    task = new_task(admin, world.a, run)
    lease_task(admin, task, token=5)
    update(admin, "tasks", task, status="RUNNING")
    update(admin, "tasks", task, status="VERIFYING")
    ge = _gate(admin, world, run, "RUNNING", task=task)
    with pytest.raises(psycopg.errors.CheckViolation, match="required gates not passed"):
        update(admin, "tasks", task, status="SUCCEEDED", finished_at="now")
    ev = _evidence(admin, world, run, verified=True)
    admin.execute(
        "UPDATE gate_executions SET status = 'PASS', evidence_id = %s, finished_at = now() WHERE id = %s", (ev, ge)
    )
    update(admin, "tasks", task, status="SUCCEEDED", finished_at="now")
    # §18.6: lease_fencing_token задачи не убывает.
    with pytest.raises(psycopg.errors.CheckViolation, match="fencing"):
        update(admin, "tasks", task, lease_fencing_token=4)


# --- §18.7 approvals и внешние действия --------------------------------------------------------


def test_four_eyes_and_decider_role(admin: psycopg.Connection, world: World) -> None:
    developer = add_user(admin, world.a, "developer")
    admin_user = add_user(admin, world.a, "admin")
    # Запрашивающий человек не утверждает сам.
    own = _approval(admin, world.a, action="DOCS_DOWNLOAD", requested_by=developer, subject_hash=uuid7().hex * 2)
    with pytest.raises(psycopg.errors.CheckViolation, match="four_eyes"):
        _decide(admin, own, developer)
    # Занижение required_role относительно матрицы запрещено.
    with pytest.raises(psycopg.errors.CheckViolation, match="below minimum"):
        _approval(admin, world.a, action="DESTRUCTIVE_MIGRATION", required_role="developer", subject_hash=SHA_B)
    merge = _approval(
        admin, world.a, action="DESTRUCTIVE_MIGRATION", required_role="admin", subject_hash=uuid7().hex * 2
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="decider role"):
        _decide(admin, merge, developer)
    # Понижающее переопределение в проекте учитывается.
    admin.execute(
        "INSERT INTO project_member_overrides (workspace_id, project_id, user_id, role, set_by)"
        " VALUES (%s, %s, %s, 'developer', %s)",
        (world.a.workspace_id, world.a.project_id, admin_user, world.a.owner_id),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="decider role"):
        _decide(admin, merge, admin_user)
    _decide(admin, merge, world.a.owner_id)


def test_external_action_requires_matching_approval(admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.a)
    approval = _approval(admin, world.a, run=run, action="MERGE", required_role="admin", subject_hash=SHA_B)
    action = uuid7()
    with pytest.raises(psycopg.errors.CheckViolation, match="approval_required"):
        admin.execute(
            "INSERT INTO external_actions (id, workspace_id, run_id, action_type, idempotency_key, target, request_hash)"
            " VALUES (%s, %s, %s, 'pr_merge', %s, 'o/r#1', %s)",
            (uuid7(), world.a.workspace_id, run, uuid7().hex * 2, SHA_B),
        )
    admin.execute(
        "INSERT INTO external_actions (id, workspace_id, run_id, action_type, idempotency_key, target, request_hash,"
        " approval_id) VALUES (%s, %s, %s, 'pr_merge', %s, 'o/r#1', %s, %s)",
        (action, world.a.workspace_id, run, uuid7().hex * 2, SHA_A, approval),
    )

    def execute() -> None:
        update(admin, "external_actions", action, status="EXECUTING", executing_at="now", fencing_token=7)

    with pytest.raises(psycopg.errors.CheckViolation, match="not APPROVED"):
        execute()  # approval ещё PENDING
    _decide(admin, approval, world.a.owner_id)
    with pytest.raises(psycopg.errors.CheckViolation, match="not APPROVED"):
        execute()  # subject_hash (b…) ≠ request_hash (a…): одобрение нельзя перенести на другое действие
    update(admin, "external_actions", action, request_hash=SHA_B)
    execute()
    assert admin.execute("SELECT status FROM external_actions WHERE id = %s", (action,)).fetchone() == ("EXECUTING",)


# --- §18.6 fencing, append-only, outbox, version ------------------------------------------------


def test_lease_fencing_token_monotonic(admin: psycopg.Connection) -> None:
    lease = uuid7()
    admin.execute(
        "INSERT INTO worker_leases (id, resource_type, resource_id, worker_id, fencing_token, lease_until)"
        " VALUES (%s, 'task', %s, 'w1', 10, now() + interval '1 minute')",
        (lease, uuid7()),
    )
    with pytest.raises(psycopg.errors.CheckViolation, match="must not decrease"):
        admin.execute("UPDATE worker_leases SET fencing_token = 5 WHERE id = %s", (lease,))
    with pytest.raises(psycopg.errors.CheckViolation, match="new fencing token"):
        admin.execute("UPDATE worker_leases SET worker_id = 'w2' WHERE id = %s", (lease,))
    admin.execute("UPDATE worker_leases SET worker_id = 'w2', fencing_token = 11 WHERE id = %s", (lease,))


def test_run_event_log_append_only(database: testdb.TestDatabase, admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.a)
    admin.execute(
        "INSERT INTO run_event_log (workspace_id, run_id, seq, event_id, event_type, payload, occurred_at)"
        " VALUES (%s, %s, 1, %s, 'run.state_changed', '{}', now())",
        (world.a.workspace_id, run, uuid7()),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        admin.execute("UPDATE run_event_log SET seq = 2 WHERE run_id = %s", (run,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        admin.execute("DELETE FROM run_event_log WHERE run_id = %s", (run,))
    with psycopg.connect(database.url_for("aicd_worker")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM run_event_log")


def _outbox(conn: psycopg.Connection, ws: uuid.UUID | None, payload: str = "{}") -> uuid.UUID:
    ev = uuid7()
    conn.execute(
        "INSERT INTO outbox_events (id, workspace_id, aggregate_type, aggregate_id, event_type, payload)"
        " VALUES (%s, %s, 'run', %s, 'run.state_changed', %s)",
        (ev, ws, uuid7(), payload),
    )
    return ev


def test_outbox_payload_limits_and_guard(admin: psycopg.Connection, world: World) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _outbox(admin, world.a.workspace_id, '{"x": "' + "y" * 70_000 + '"}')
    with pytest.raises(psycopg.errors.CheckViolation):
        _outbox(admin, world.a.workspace_id, "[]")
    ev = _outbox(admin, world.a.workspace_id, '{"x": "' + "y" * 60_000 + '"}')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="publication fields"):
        admin.execute("UPDATE outbox_events SET payload = '{}' WHERE id = %s", (ev,))
    admin.execute(
        "UPDATE outbox_events SET published_at = now(), publish_attempts = publish_attempts + 1 WHERE id = %s", (ev,)
    )


def test_last_event_seq_does_not_bump_version(admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.a)
    before = admin.execute("SELECT version FROM runs WHERE id = %s", (run,)).fetchone()
    for _ in range(3):
        admin.execute("UPDATE runs SET last_event_seq = last_event_seq + 1 WHERE id = %s", (run,))
    assert admin.execute("SELECT version, last_event_seq FROM runs WHERE id = %s", (run,)).fetchone() == (
        before[0] if before else None,
        3,
    )
    with pytest.raises(psycopg.errors.SerializationFailure):
        admin.execute("UPDATE runs SET objective = 'changed' WHERE id = %s", (run,))


def test_policies_immutable_and_routing_scope(admin: psycopg.Connection, world: World) -> None:
    foreign = uuid7()
    admin.execute(
        "INSERT INTO policies (id, workspace_id, policy_type, version, definition) VALUES (%s, %s, 'routing', 1, '{}')",
        (foreign, world.b.workspace_id),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="immutable"):
        admin.execute("UPDATE policies SET definition = '{\"a\": 1}' WHERE id = %s", (foreign,))
    admin.execute("UPDATE policies SET active = false WHERE id = %s", (foreign,))
    with pytest.raises(psycopg.errors.CheckViolation, match="routing policy"):
        admin.execute(
            "UPDATE project_settings SET routing_policy_id = %s, version = version + 1 WHERE project_id = %s",
            (foreign, world.a.project_id),
        )


# --- §19 RLS ------------------------------------------------------------------------------------


def _ctx(conn: psycopg.Connection, user: uuid.UUID | None, ws: uuid.UUID | None) -> None:
    conn.execute(
        "SELECT set_config('app.user_id', %s, true), set_config('app.workspace_id', %s, true)",
        (str(user) if user else "", str(ws) if ws else ""),
    )


@pytest.fixture(scope="module")
def rls_rows(admin: psycopg.Connection, world: World) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for name, tenant in (("a", world.a), ("b", world.b)):
        run = new_run(admin, tenant)
        ids[f"run_{name}"] = run
        ids[f"task_{name}"] = new_task(admin, tenant, run)
        _approval(admin, tenant, run=run, subject_hash=uuid7().hex * 2)
        ids[f"outbox_{name}"] = _outbox(admin, tenant.workspace_id)
    return ids


@pytest.mark.parametrize("role", ["aicd_app", "aicd_worker"])
@pytest.mark.parametrize("table", ["runs", "tasks", "approvals", "task_dependencies", "run_event_log"])
def test_tenant_tables_isolated(
    database: testdb.TestDatabase, world: World, rls_rows: dict[str, uuid.UUID], role: str, table: str
) -> None:
    with psycopg.connect(database.url_for(role)) as conn:
        with conn.transaction():
            _ctx(conn, world.a.owner_id, world.a.workspace_id)
            seen = {r[0] for r in conn.execute(f"SELECT DISTINCT workspace_id FROM {table}").fetchall()}
        assert seen <= {world.a.workspace_id}
        if table in ("runs", "tasks", "approvals"):
            assert seen == {world.a.workspace_id}
        with conn.transaction():
            _ctx(conn, None, None)
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), "без контекста — fail-closed"


def test_app_cannot_write_foreign_run(
    database: testdb.TestDatabase, world: World, rls_rows: dict[str, uuid.UUID]
) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with conn.transaction():
            _ctx(conn, world.a.owner_id, world.a.workspace_id)
            res = conn.execute("UPDATE runs SET version = version + 1 WHERE id = %s", (rls_rows["run_b"],))
            assert res.rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            _ctx(conn, world.a.owner_id, world.a.workspace_id)
            new_run(conn, world.b)


def test_outbox_visibility(database: testdb.TestDatabase, world: World, rls_rows: dict[str, uuid.UUID]) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT 1 FROM outbox_events")  # api только вставляет
        with conn.transaction():
            _ctx(conn, world.a.owner_id, world.a.workspace_id)
            _outbox(conn, world.a.workspace_id)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            _ctx(conn, world.a.owner_id, world.a.workspace_id)
            _outbox(conn, world.b.workspace_id)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT 1 FROM worker_leases")
    with psycopg.connect(database.url_for("aicd_worker")) as conn, conn.transaction():
        _ctx(conn, None, None)  # диспетчер outbox видит события всех workspace
        seen = {
            r[0]
            for r in conn.execute(
                "SELECT id FROM outbox_events WHERE id IN (%s, %s)", (rls_rows["outbox_a"], rls_rows["outbox_b"])
            ).fetchall()
        }
        assert seen == {rls_rows["outbox_a"], rls_rows["outbox_b"]}


def test_security_definer_resolvers(
    database: testdb.TestDatabase, world: World, rls_rows: dict[str, uuid.UUID]
) -> None:
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, world.a.owner_id, None)
        assert conn.execute("SELECT sys_resolve_run_workspace(%s)", (rls_rows["run_a"],)).fetchone() == (
            world.a.workspace_id,
        )
        assert conn.execute("SELECT sys_resolve_run_workspace(%s)", (rls_rows["run_b"],)).fetchone() == (None,)
        assert conn.execute("SELECT sys_resolve_task_workspace(%s)", (rls_rows["task_a"],)).fetchone() == (
            world.a.workspace_id,
        )
        assert conn.execute("SELECT sys_resolve_task_workspace(%s)", (rls_rows["task_b"],)).fetchone() == (None,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM sys_schedulable_workspaces(10)")


def test_schedulable_workspaces(database: testdb.TestDatabase, admin: psycopg.Connection, world: World) -> None:
    run = new_run(admin, world.b)
    task = new_task(admin, world.b, run, "ready")
    update(admin, "tasks", task, status="READY")
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        found = {r[0] for r in conn.execute("SELECT workspace_id FROM sys_schedulable_workspaces(100)").fetchall()}
    assert world.b.workspace_id in found
