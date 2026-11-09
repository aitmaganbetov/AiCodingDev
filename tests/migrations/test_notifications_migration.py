"""Миграция 0020 (UI redesign B8): `notifications` — обратимость, offline SQL, RLS (только свои строки workspace),
права ролей (app: SELECT + UPDATE(read_at); worker: SELECT/INSERT; maintenance: SELECT/DELETE), уникальность
(повторная проекция события), каскад при удалении участника (DATABASE_DESIGN §9.8, §19.4)."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator
from contextlib import redirect_stdout

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from aicd_platform.ids import uuid7
from tests.support import db as testdb
from tests.support.orchestration import Tenant, add_user, connect_admin, seed_tenant

pytestmark = pytest.mark.db

INSERT = (
    "INSERT INTO notifications (id, workspace_id, user_id, kind, severity, title, resource_type, resource_id,"
    " source_event_id) VALUES (%s, %s, %s, 'run.completed', 'success', 'Run завершён', 'run', %s, %s)"
)


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig20")
    yield db
    testdb.drop_database(db)


def _state(db: testdb.TestDatabase) -> dict[str, object]:
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        table = conn.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'aicd' AND c.relname = 'notifications'"
        ).fetchone()
        if table is None:
            return {"table": None}
        policies = conn.execute(
            "SELECT polname, polroles::regrole[]::text[] FROM pg_policy WHERE polrelid = 'aicd.notifications'::regclass"
            " ORDER BY polname"
        ).fetchall()
        privileges = {
            role: sorted(
                p
                for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
                if conn.execute("SELECT has_table_privilege(%s, 'aicd.notifications', %s)", (role, p)).fetchone()[0]  # type: ignore[index]
            )
            for role in ("aicd_app", "aicd_worker", "aicd_maintenance")
        }
        app_update_cols = [
            r[0]
            for r in conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = 'aicd.notifications'::regclass AND attnum > 0"
                " AND NOT attisdropped AND has_column_privilege('aicd_app', attrelid, attnum, 'UPDATE') ORDER BY attname"
            ).fetchall()
        ]
        indexes = [
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'aicd' AND tablename = 'notifications'"
                " ORDER BY indexname"
            ).fetchall()
        ]
    return {
        "table": tuple(table),
        "policies": [(name, list(roles)) for name, roles in policies],
        "privileges": privileges,
        "app_update_cols": app_update_cols,
        "indexes": indexes,
    }


def test_0020_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "0019")
    assert _state(scratch_db) == {"table": None}
    testdb.alembic(scratch_db, "upgrade", "0020")
    after = _state(scratch_db)
    assert after["table"] == (True, True), "RLS ENABLE + FORCE"
    assert after["policies"] == [
        ("p_notifications_owner", ["aicd_app"]),
        ("p_notifications_worker", ["aicd_worker"]),
    ]
    assert after["privileges"] == {
        "aicd_app": ["SELECT"],  # UPDATE — только колонка read_at (ниже)
        "aicd_worker": ["INSERT", "SELECT"],
        "aicd_maintenance": ["DELETE", "SELECT"],
    }
    assert after["app_update_cols"] == ["read_at"]
    assert after["indexes"] == sorted(
        [
            "pk_notifications",
            "uq_notifications_source",
            "ix_notifications_feed",
            "ix_notifications_unread",
            "ix_notifications_created_at",
        ]
    )
    testdb.alembic(scratch_db, "downgrade", "0019")
    assert _state(scratch_db) == {"table": None}
    testdb.alembic(scratch_db, "upgrade", "0020")
    assert _state(scratch_db) == after


def test_0020_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0019:0020", sql=True)
    sql = buf.getvalue()
    for fragment in ("CREATE TABLE notifications", "FORCE ROW LEVEL SECURITY", "p_notifications_owner",
                     "GRANT SELECT, UPDATE (read_at) ON notifications TO aicd_app"):  # fmt: skip
        assert fragment in sql


def _ctx(conn: psycopg.Connection, ws: uuid.UUID | None, user: uuid.UUID | None = None) -> None:
    conn.execute(
        "SELECT set_config('app.workspace_id', %s, false), set_config('app.user_id', %s, false)",
        (str(ws) if ws else "", str(user) if user else ""),
    )


@pytest.fixture(scope="module")
def world(database: testdb.TestDatabase) -> tuple[Tenant, uuid.UUID, Tenant]:
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        member = add_user(admin, tenant, "viewer")
        other = seed_tenant(admin)
    return tenant, member, other


def test_rls_owner_sees_only_own_rows_and_only_marks_read(
    database: testdb.TestDatabase, world: tuple[Tenant, uuid.UUID, Tenant]
) -> None:
    tenant, member, other = world
    ws = tenant.workspace_id
    own, foreign_user, foreign_ws = uuid7(), uuid7(), uuid7()
    with psycopg.connect(database.url_for("aicd_worker"), autocommit=True) as worker:
        _ctx(worker, ws)
        worker.execute(INSERT, (own, ws, tenant.owner_id, uuid7(), uuid7()))
        worker.execute(INSERT, (foreign_user, ws, member, uuid7(), uuid7()))
        # Воркер пишет только в workspace контекста (WITH CHECK).
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute(INSERT, (uuid7(), other.workspace_id, other.owner_id, uuid7(), uuid7()))
        _ctx(worker, other.workspace_id)
        worker.execute(INSERT, (foreign_ws, other.workspace_id, other.owner_id, uuid7(), uuid7()))
        # Получатель — только участник workspace (FK на workspace_members).
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            worker.execute(INSERT, (uuid7(), other.workspace_id, member, uuid7(), uuid7()))
        # Воркер не меняет и не удаляет уведомления.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("UPDATE notifications SET read_at = now()")
    with psycopg.connect(database.url_for("aicd_app"), autocommit=True) as app:
        _ctx(app, ws, tenant.owner_id)
        assert [r[0] for r in app.execute("SELECT id FROM notifications").fetchall()] == [own]
        assert app.execute("UPDATE notifications SET read_at = now() WHERE id = ANY(%s)",
                           ([own, foreign_user, foreign_ws],)).rowcount == 1  # fmt: skip
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("UPDATE notifications SET title = 'x' WHERE id = %s", (own,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("DELETE FROM notifications WHERE id = %s", (own,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(INSERT, (uuid7(), ws, tenant.owner_id, uuid7(), uuid7()))
        # Без контекста пользователя — ничего (fail-closed).
        _ctx(app, ws, None)
        assert app.execute("SELECT count(*) FROM notifications").fetchone() == (0,)
        _ctx(app, other.workspace_id, tenant.owner_id)
        assert app.execute("SELECT count(*) FROM notifications").fetchone() == (0,)
    with connect_admin(database) as admin:
        row = admin.execute("SELECT read_at IS NOT NULL FROM notifications WHERE id = %s", (foreign_user,)).fetchone()
        assert row == (False,), "чужое уведомление не отмечено"


def test_unique_source_and_cascade_on_member_removal(
    database: testdb.TestDatabase, world: tuple[Tenant, uuid.UUID, Tenant]
) -> None:
    tenant, _member, _other = world
    ws = tenant.workspace_id
    with connect_admin(database) as admin:
        leaving = add_user(admin, tenant, "reviewer")
    source = uuid7()
    with psycopg.connect(database.url_for("aicd_worker"), autocommit=True) as worker:
        _ctx(worker, ws)
        worker.execute(INSERT, (uuid7(), ws, leaving, uuid7(), source))
        with pytest.raises(psycopg.errors.UniqueViolation):
            worker.execute(INSERT, (uuid7(), ws, leaving, uuid7(), source))
        with pytest.raises(psycopg.errors.CheckViolation):
            worker.execute(
                "INSERT INTO notifications (id, workspace_id, user_id, kind, severity, title, resource_type,"
                " resource_id, source_event_id) VALUES (%s, %s, %s, 'chat.message', 'info', 't', 'run', %s, %s)",
                (uuid7(), ws, leaving, uuid7(), uuid7()),
            )
    with psycopg.connect(database.url_for("aicd_app"), autocommit=True) as app:
        # Удаление участника (как removeMember: контекст workspace) каскадно удаляет его уведомления, хотя у
        # aicd_app нет DELETE на notifications (RI-действие выполняется от владельца таблицы).
        _ctx(app, ws, tenant.owner_id)
        app.execute("DELETE FROM workspace_members WHERE workspace_id = %s AND user_id = %s", (ws, leaving))
    with connect_admin(database) as admin:
        assert admin.execute("SELECT count(*) FROM notifications WHERE user_id = %s", (leaving,)).fetchone() == (0,)
