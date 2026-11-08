"""P4-01 / миграция 0006: агенты, ACP, лента активности (DATABASE_DESIGN §7.5, §9.1, §9.2, §9.9, §19, §20)."""

from __future__ import annotations

import io
import json
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
from tests.support.orchestration import (
    Tenant,
    connect_admin,
    drive_run,
    lease_task,
    new_attempt,
    new_run,
    new_task,
    seed_tenant,
)

pytestmark = pytest.mark.db

NEW_TABLES = {"agent_profiles", "agent_messages", "message_deliveries", "agent_activity_events"}
DEFERRED_FKS = {
    ("agent_assignments", "fk_agent_assignments_profile", "agent_profiles"),
    ("agent_decisions", "fk_agent_decisions_message", "agent_messages"),
    ("chat_messages", "fk_chat_messages_acp", "agent_messages"),
    ("directive_acknowledgements", "fk_directive_ack_message", "agent_messages"),
}
TENANT_TABLES = ["agent_messages", "message_deliveries", "agent_activity_events"]
LIMIT = 32768


def _tables(db: testdb.TestDatabase) -> set[str]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'aicd' AND c.relkind IN ('r','p') AND NOT c.relispartition"
        ).fetchall()
    return {r[0] for r in rows}


def _fks(db: testdb.TestDatabase) -> set[tuple[str, str, str]]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT c.conrelid::regclass::text, c.conname, c.confrelid::regclass::text FROM pg_constraint c"
            " JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'aicd' AND c.contype = 'f'"
        ).fetchall()
    return {(t.removeprefix("aicd."), name, ref.removeprefix("aicd.")) for t, name, ref in rows}


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig6")
    yield db
    testdb.drop_database(db)


def test_0006_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "head")
    created = _tables(scratch_db)
    assert created >= NEW_TABLES
    assert _fks(scratch_db) >= DEFERRED_FKS
    testdb.alembic(scratch_db, "downgrade", "0005")
    assert not (NEW_TABLES & _tables(scratch_db))
    assert not ({name for _, name, _ in DEFERRED_FKS} & {name for _, name, _ in _fks(scratch_db)})
    with psycopg.connect(scratch_db.superuser_url) as conn:
        # Партиции удалены вместе с родителем; права maintenance на таблицы 0003 возвращены.
        assert conn.execute("SELECT count(*) FROM pg_class WHERE relname LIKE 'agent_activity_events%'").fetchone() == (
            0,
        )
        assert conn.execute(
            "SELECT has_table_privilege('aicd_maintenance', 'aicd.chat_messages', 'DELETE'),"
            " has_table_privilege('aicd_maintenance', 'aicd.artifacts', 'INSERT')"
        ).fetchone() == (False, False)
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _tables(scratch_db) == created


def test_0006_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0005:0006", sql=True)
    out = buf.getvalue()
    for table in NEW_TABLES:
        assert f"CREATE TABLE {table}" in out
    assert "ADD CONSTRAINT fk_chat_messages_acp" in out
    assert "maint_ensure_monthly_partitions('agent_activity_events'" in out


def test_deferred_fks_and_partitions(database: testdb.TestDatabase) -> None:
    assert _fks(database) >= DEFERRED_FKS
    with psycopg.connect(database.superuser_url) as conn:
        parts = conn.execute(
            "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent"
            " WHERE p.relname = 'agent_activity_events'"
        ).fetchone()
        assert parts is not None and parts[0] >= 3
        # Партиции: RLS включён, прямого доступа у app/worker нет (§19.3).
        rows = conn.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
            " has_table_privilege('aicd_app', c.oid, 'SELECT'), has_table_privilege('aicd_worker', c.oid, 'INSERT')"
            " FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent"
            " WHERE p.relname = 'agent_activity_events'"
        ).fetchall()
    for name, rls, force, app_select, worker_insert in rows:
        assert rls and force and not app_select and not worker_insert, name


# --- данные двух арендаторов ---------------------------------------------------------------------


@dataclass(frozen=True)
class Side:
    tenant: Tenant
    run: uuid.UUID
    task: uuid.UUID
    attempt: uuid.UUID
    message: uuid.UUID
    event: uuid.UUID


@dataclass(frozen=True)
class World:
    a: Side
    b: Side


def _message(
    conn: psycopg.Connection,
    side_ws: uuid.UUID,
    run: uuid.UUID,
    *,
    task: uuid.UUID | None = None,
    attempt: uuid.UUID | None = None,
    sender_role: str = "developer",
    provider: str | None = "codex",
    user_id: uuid.UUID | None = None,
    payload: str = '{"summary": "ok"}',
    reply_to: uuid.UUID | None = None,
    message_type: str = "HANDOFF",
) -> uuid.UUID:
    msg = uuid7()
    conn.execute(
        "INSERT INTO agent_messages (id, workspace_id, run_id, task_id, task_attempt_id, sender_role, sender_provider,"
        " sender_user_id, recipient_role, message_type, correlation_id, reply_to_id, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'orchestrator', %s, %s, %s, %s)",
        (msg, side_ws, run, task, attempt, sender_role, provider, user_id, message_type, msg, reply_to, payload),
    )
    return msg


def _event(
    conn: psycopg.Connection, ws: uuid.UUID, run: uuid.UUID, *, event_type: str = "task.state_changed", **kw: str
) -> uuid.UUID:
    ev = uuid7()
    conn.execute(
        "INSERT INTO agent_activity_events (id, workspace_id, run_id, event_type, cli_event, payload, occurred_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, now())",
        (ev, ws, run, event_type, kw.get("cli_event"), kw.get("payload", "{}")),
    )
    return ev


def _side(conn: psycopg.Connection) -> Side:
    tenant = seed_tenant(conn)
    run = new_run(conn, tenant)
    drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
    task = new_task(conn, tenant, run)
    lease_task(conn, task)
    attempt = new_attempt(conn, tenant, task)
    msg = _message(conn, tenant.workspace_id, run, task=task, attempt=attempt)
    conn.execute(
        "INSERT INTO message_deliveries (workspace_id, message_id, consumer_id) VALUES (%s, %s, 'chat.projector')",
        (tenant.workspace_id, msg),
    )
    return Side(tenant, run, task, attempt, msg, _event(conn, tenant.workspace_id, run))


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
def world(admin: psycopg.Connection) -> World:
    return World(_side(admin), _side(admin))


def _ctx(conn: psycopg.Connection, user: uuid.UUID | None, ws: uuid.UUID | None) -> None:
    conn.execute(
        "SELECT set_config('app.user_id', %s, true), set_config('app.workspace_id', %s, true)",
        (str(user) if user else "", str(ws) if ws else ""),
    )


@pytest.mark.parametrize("role", ["aicd_app", "aicd_worker"])
@pytest.mark.parametrize("table", TENANT_TABLES)
def test_tenant_isolation(database: testdb.TestDatabase, world: World, role: str, table: str) -> None:
    a, b = world.a, world.b
    with psycopg.connect(database.url_for(role)) as conn:
        with conn.transaction():
            _ctx(conn, a.tenant.owner_id, a.tenant.workspace_id)
            seen = {r[0] for r in conn.execute(f"SELECT DISTINCT workspace_id FROM {table}").fetchall()}
            assert seen == {a.tenant.workspace_id}
        with conn.transaction():
            _ctx(conn, None, None)
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), "без контекста — fail-closed"
        if role == "aicd_worker" and table == "message_deliveries":
            with conn.transaction():
                _ctx(conn, a.tenant.owner_id, a.tenant.workspace_id)
                res = conn.execute(
                    "UPDATE message_deliveries SET delivery_count = delivery_count + 1 WHERE workspace_id = %s",
                    (b.tenant.workspace_id,),
                )
                assert res.rowcount == 0
        # Вставка строки чужого workspace: worker — отказ RLS (WITH CHECK), app — нет права INSERT.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            _ctx(conn, a.tenant.owner_id, a.tenant.workspace_id)
            if table == "agent_messages":
                _message(conn, b.tenant.workspace_id, b.run)
            elif table == "message_deliveries":
                conn.execute(
                    "INSERT INTO message_deliveries (workspace_id, message_id, consumer_id) VALUES (%s, %s, 'evil')",
                    (b.tenant.workspace_id, b.message),
                )
            else:
                _event(conn, b.tenant.workspace_id, b.run)


def test_worker_writes_own_workspace(database: testdb.TestDatabase, world: World) -> None:
    a = world.a
    with psycopg.connect(database.url_for("aicd_worker")) as conn, conn.transaction():
        _ctx(conn, None, a.tenant.workspace_id)
        msg = _message(conn, a.tenant.workspace_id, a.run, task=a.task, attempt=a.attempt)
        conn.execute(
            "INSERT INTO message_deliveries (workspace_id, message_id, consumer_id) VALUES (%s, %s, 'orchestrator')",
            (a.tenant.workspace_id, msg),
        )
        res = conn.execute(
            "UPDATE message_deliveries SET delivery_count = 1, acked_at = now() WHERE message_id = %s", (msg,)
        )
        assert res.rowcount == 1
        _event(conn, a.tenant.workspace_id, a.run, event_type="cli.output", cli_event="output")
    with psycopg.connect(database.url_for("aicd_app")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT 1 FROM agent_messages FOR UPDATE")


def test_cross_tenant_composite_fks(admin: psycopg.Connection, world: World) -> None:
    a, b = world.a, world.b
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _message(admin, a.tenant.workspace_id, b.run)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _message(admin, a.tenant.workspace_id, a.run, reply_to=b.message)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO message_deliveries (workspace_id, message_id, consumer_id) VALUES (%s, %s, 'x')",
            (a.tenant.workspace_id, b.message),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _event(admin, a.tenant.workspace_id, b.run)
    # Отложенные FK 0003 → 0006: ссылки на ACP-сообщение чужого workspace и на несуществующий профиль.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO agent_decisions (id, workspace_id, run_id, decision_type, rationale, acp_message_id,"
            " decided_by_type) VALUES (%s, %s, %s, 'plan', 'r', %s, 'system')",
            (uuid7(), a.tenant.workspace_id, a.run, b.message),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO agent_assignments (id, workspace_id, task_id, agent_profile_id, provider, reason)"
            " VALUES (%s, %s, %s, %s, 'codex', 'r')",
            (uuid7(), a.tenant.workspace_id, a.task, uuid7()),
        )
    thread = uuid7()
    admin.execute(
        "INSERT INTO chat_threads (id, workspace_id, run_id, scope) VALUES (%s, %s, %s, 'all_agents')",
        (thread, a.tenant.workspace_id, a.run),
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, body, message_kind,"
            " acp_message_id) VALUES (%s, %s, %s, %s, 'system', 'x', 'acp_message', %s)",
            (uuid7(), a.tenant.workspace_id, a.run, thread, b.message),
        )
    admin.execute(
        "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, body, message_kind,"
        " acp_message_id) VALUES (%s, %s, %s, %s, 'system', 'x', 'acp_message', %s)",
        (uuid7(), a.tenant.workspace_id, a.run, thread, a.message),
    )


def test_payload_size_limit_32_kib(admin: psycopg.Connection, world: World) -> None:
    a = world.a
    ws = a.tenant.workspace_id

    def payload(total: int) -> str:
        doc = '{"s": ""}'
        return '{"s": "' + "x" * (total - len(doc)) + '"}'

    assert len(payload(LIMIT).encode()) == LIMIT
    _message(admin, ws, a.run, task=a.task, payload=payload(LIMIT))
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_agent_messages_payload_size"):
        _message(admin, ws, a.run, task=a.task, payload=payload(LIMIT + 1))
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_agent_messages_payload_object"):
        _message(admin, ws, a.run, payload="[]")
    _event(admin, ws, a.run, payload=payload(LIMIT))
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_activity_payload"):
        _event(admin, ws, a.run, payload=payload(LIMIT + 1))


def test_sender_and_activity_checks(admin: psycopg.Connection, world: World) -> None:
    a = world.a
    ws = a.tenant.workspace_id
    _message(admin, ws, a.run, sender_role="orchestrator", provider=None, message_type="TASK_ASSIGN")
    _message(admin, ws, a.run, sender_role="user", provider=None, user_id=a.tenant.owner_id, message_type="QUESTION")
    for role, provider, user in (
        ("orchestrator", "codex", None),
        ("user", None, None),
        ("user", "claude", a.tenant.owner_id),
        ("developer", None, None),
        ("developer", "codex", a.tenant.owner_id),
    ):
        with pytest.raises(psycopg.errors.CheckViolation, match="ck_agent_messages_sender"):
            _message(admin, ws, a.run, sender_role=role, provider=provider, user_id=user)
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_agent_messages_attempt"):
        _message(admin, ws, a.run, attempt=a.attempt)  # попытка без задачи
    with pytest.raises(psycopg.errors.NotNullViolation):
        admin.execute(
            "INSERT INTO agent_messages (id, workspace_id, run_id, sender_role, recipient_role, message_type, payload)"
            " VALUES (%s, %s, %s, 'orchestrator', 'user', 'BLOCKED', '{}')",
            (uuid7(), ws, a.run),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_activity_cli_event"):
        _event(admin, ws, a.run, event_type="cli.output")
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_activity_cli_event"):
        _event(admin, ws, a.run, cli_event="output")


def test_append_only(database: testdb.TestDatabase, admin: psycopg.Connection, world: World) -> None:
    a = world.a
    for stmt in (
        "UPDATE agent_messages SET payload = '{}' WHERE id = %s",
        "DELETE FROM agent_messages WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            admin.execute(stmt, (a.message,))
    for stmt in (
        "UPDATE agent_activity_events SET payload = '{}' WHERE id = %s",
        "DELETE FROM agent_activity_events WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            admin.execute(stmt, (a.event,))
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        for stmt in ("UPDATE agent_messages SET payload = '{}'", "DELETE FROM agent_activity_events"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                _ctx(conn, None, a.tenant.workspace_id)
                conn.execute(stmt)
    # aicd_maintenance без сессии обслуживания тоже не удаляет (§19.6).
    with psycopg.connect(database.url_for("aicd_maintenance"), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            conn.execute("DELETE FROM agent_messages WHERE id = %s", (a.message,))


def test_agent_profiles_global_policy(database: testdb.TestDatabase) -> None:
    """Справочник G: политика профиля только глобальная; app/worker только читают (транзакция откатывается)."""
    with connect_admin(database, autocommit=False) as conn:
        tenant = seed_tenant(conn)
        global_policy, ws_policy = uuid7(), uuid7()
        conn.execute(
            "INSERT INTO policies (id, workspace_id, policy_type, name, version, definition)"
            " VALUES (%s, NULL, 'review', %s, 1, '{}'), (%s, %s, 'review', 'default', 1, '{}')",
            (global_policy, f"p-{global_policy.hex[-8:]}", ws_policy, tenant.workspace_id),
        )
        # Одна строка на роль: профиль мог уже создать sync (общая тестовая БД) — upsert; транзакция откатывается.
        row = conn.execute(
            "INSERT INTO agent_profiles (id, role, instruction_template_version, policy_profile_id, allowed_tools)"
            " VALUES (%s, 'documentation', '1.0.0', %s, %s) ON CONFLICT ON CONSTRAINT uq_agent_profiles_role"
            " DO UPDATE SET policy_profile_id = EXCLUDED.policy_profile_id RETURNING id",
            (uuid7(), global_policy, json.dumps(["read"])),
        ).fetchone()
        assert row is not None
        profile = row[0]
        with pytest.raises(psycopg.errors.CheckViolation, match="global"):
            with conn.transaction():
                conn.execute("UPDATE agent_profiles SET policy_profile_id = %s WHERE id = %s", (ws_policy, profile))
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO agent_profiles (id, role, instruction_template_version) VALUES (%s, 'qa', 'v1')",
                (uuid7(),),
            )
        conn.rollback()
    for role in ("aicd_app", "aicd_worker"):
        with psycopg.connect(database.url_for(role)) as conn:
            conn.execute("SELECT count(*) FROM agent_profiles")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO agent_profiles (id, role, instruction_template_version) VALUES (%s, 'qa', '1.0.0')",
                    (uuid7(),),
                )
