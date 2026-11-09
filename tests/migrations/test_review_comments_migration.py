"""Миграция 0019 (UI redesign B5, ADR-0015): inline-комментарии ревью — обратимость, offline-SQL, RLS, права, инварианты."""

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
from tests.support import quality as q
from tests.support.orchestration import Tenant, add_user, connect_admin, new_run, seed_tenant

pytestmark = pytest.mark.db

TABLES = (
    "review_threads",
    "review_comments",
    "review_comment_revisions",
    "review_submissions",
    "review_submission_threads",
)
FUNCTIONS = {
    "sys_resolve_review_thread_workspace",
    "sys_resolve_review_comment_workspace",
    "trg_fn_review_threads_guard",
    "trg_fn_review_comments_guard",
    "trg_fn_review_submissions_author",
}
SHA = "b" * 40


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig19")
    yield db
    testdb.drop_database(db)


def _state(db: testdb.TestDatabase) -> tuple[set[str], set[str], set[str], str]:
    with psycopg.connect(db.superuser_url) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename::text FROM pg_tables WHERE schemaname = 'aicd' AND tablename = ANY(%s)",
                (list(TABLES),),
            ).fetchall()
        }
        functions = {
            r[0]
            for r in conn.execute(
                "SELECT p.proname::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'aicd' AND p.proname = ANY(%s)",
                (list(FUNCTIONS),),
            ).fetchall()
        }
        entities = {r[0] for r in conn.execute("SELECT DISTINCT entity FROM aicd.allowed_transitions").fetchall()}
        domain = conn.execute(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_type t ON t.oid = c.contypid"
            " WHERE t.typname = 'sse_event_type_persisted_t'"
        ).fetchone()
    assert domain is not None
    return tables, functions, entities, domain[0]


def test_0019_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "0018")
    tables, functions, entities, domain = _state(scratch_db)
    assert tables == set() and functions == set() and "review_thread" not in entities
    assert "review.comment" not in domain
    testdb.alembic(scratch_db, "upgrade", "0019")
    tables, functions, entities, domain = _state(scratch_db)
    assert tables == set(TABLES)
    assert functions == FUNCTIONS
    assert "review_thread" in entities and "review.comment" in domain
    testdb.alembic(scratch_db, "downgrade", "0018")
    tables, functions, entities, domain = _state(scratch_db)
    assert tables == set() and functions == set() and "review_thread" not in entities
    assert "review.comment" not in domain and "chat.message" in domain
    testdb.alembic(scratch_db, "upgrade", "0019")
    assert _state(scratch_db)[0] == set(TABLES)


def test_0019_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0018:0019", sql=True)
    sql = buf.getvalue()
    for table in TABLES:
        assert f"CREATE TABLE {table}" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql and "sys_resolve_review_thread_workspace" in sql
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.downgrade(Config(str(testdb.ALEMBIC_INI)), "0019:0018", sql=True)
    assert "DROP TABLE IF EXISTS review_submission_threads" in buf.getvalue()


def test_review_tables_forced_rls_and_grants(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url) as conn:
        for table in TABLES:
            row = conn.execute(
                "SELECT c.relrowsecurity, c.relforcerowsecurity,"
                " (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid),"
                " has_table_privilege('aicd_app', c.oid, 'SELECT'), has_table_privilege('aicd_app', c.oid, 'INSERT'),"
                " has_table_privilege('aicd_app', c.oid, 'DELETE'), has_table_privilege('aicd_app', c.oid, 'TRUNCATE'),"
                " has_table_privilege('aicd_worker', c.oid, 'SELECT'), has_table_privilege('aicd_worker', c.oid, 'INSERT'),"
                " has_table_privilege('aicd_maintenance', c.oid, 'SELECT'),"
                " has_table_privilege('aicd_maintenance', c.oid, 'DELETE')"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'aicd' AND c.relname = %s",
                (table,),
            ).fetchone()
            assert row == (True, True, 1, True, True, False, False, False, False, True, False), (table, row)
        # UPDATE — только изменяемые колонки (якорь, автор и тело ревью не меняются кодом api).
        updatable = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT table_name::text, column_name::text FROM information_schema.column_privileges"
                " WHERE table_schema = 'aicd' AND grantee = 'aicd_app' AND privilege_type = 'UPDATE'"
                " AND table_name = ANY(%s)",
                (list(TABLES),),
            ).fetchall()
        }
    assert updatable == {
        ("review_threads", c) for c in ("status", "resolved_by", "resolved_at", "updated_at", "version")
    } | {("review_comments", c) for c in ("body", "revision", "updated_at", "edited_at")}


def _as_user(conn: psycopg.Connection, user: uuid.UUID, workspace: uuid.UUID | None = None) -> None:
    conn.execute("SELECT set_config('app.user_id', %s, false)", (str(user),))
    if workspace is not None:
        conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(workspace),))


def _thread(conn: psycopg.Connection, tenant: Tenant, *, run: uuid.UUID | None = None, **cols: object) -> uuid.UUID:
    thread = uuid7()
    _as_user(conn, tenant.owner_id)
    values = {"id": thread, "workspace_id": tenant.workspace_id, "repository_id": tenant.repository_id,
              "run_id": run, "commit_sha": SHA, "file_path": "src/app.py", "start_line": 3, "end_line": 5,
              "created_by": tenant.owner_id, **cols}  # fmt: skip
    conn.execute(
        f"INSERT INTO review_threads ({', '.join(values)}) VALUES ({', '.join(['%s'] * len(values))})",
        tuple(values.values()),
    )
    return thread


def _comment(conn: psycopg.Connection, tenant: Tenant, thread: uuid.UUID, body: str = "nit") -> uuid.UUID:
    comment = uuid7()
    _as_user(conn, tenant.owner_id)
    conn.execute(
        "INSERT INTO review_comments (id, workspace_id, thread_id, author_id, body) VALUES (%s, %s, %s, %s, %s)",
        (comment, tenant.workspace_id, thread, tenant.owner_id, body),
    )
    return comment


def test_rls_isolates_review_rows_between_workspaces(database: testdb.TestDatabase) -> None:
    with connect_admin(database) as admin:
        alice, bob = seed_tenant(admin), seed_tenant(admin)
        a_thread = _thread(admin, alice)
        _comment(admin, alice, a_thread)
        b_thread = _thread(admin, bob)
        _comment(admin, bob, b_thread)
    with psycopg.connect(database.url_for("aicd_app"), autocommit=True) as conn:
        _as_user(conn, alice.owner_id, alice.workspace_id)
        threads = {r[0] for r in conn.execute("SELECT id FROM review_threads").fetchall()}
        comments = {r[0] for r in conn.execute("SELECT thread_id FROM review_comments").fetchall()}
        assert threads == {a_thread} and comments == {a_thread}
        # Резолвер: только член workspace получает workspace треда.
        assert conn.execute("SELECT sys_resolve_review_thread_workspace(%s)", (a_thread,)).fetchone() == (
            alice.workspace_id,
        )
        assert conn.execute("SELECT sys_resolve_review_thread_workspace(%s)", (b_thread,)).fetchone() == (None,)
        # WITH CHECK: вставка в чужой workspace отклоняется RLS.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO review_comments (id, workspace_id, thread_id, author_id, body)"
                " VALUES (%s, %s, %s, %s, 'x')",
                (uuid7(), bob.workspace_id, b_thread, alice.owner_id),
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM review_comments")
    with psycopg.connect(database.url_for("aicd_worker"), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM review_comments LIMIT 1")


def test_thread_anchor_author_and_fsm_invariants(database: testdb.TestDatabase) -> None:
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        other = add_user(admin, tenant, "reviewer")
        # Автор — только пользователь сессии.
        _as_user(admin, other)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute(
                "INSERT INTO review_threads (id, workspace_id, repository_id, commit_sha, file_path, start_line,"
                " end_line, created_by) VALUES (%s, %s, %s, %s, 'a.py', 1, 1, %s)",
                (uuid7(), tenant.workspace_id, tenant.repository_id, SHA, tenant.owner_id),
            )
        # Путь и диапазон строк проверяются CHECK.
        for path, start, end in (("/etc/passwd", 1, 1), ("a/../b.py", 1, 1), ("a.py", 5, 4), ("a.py", 1, 2000)):
            with pytest.raises(psycopg.errors.CheckViolation):
                _thread(admin, tenant, file_path=path, start_line=start, end_line=end)
        # Сторона base требует base_commit_sha.
        with pytest.raises(psycopg.errors.CheckViolation):
            _thread(admin, tenant, side="base")
        # Run другого репозитория — отказ.
        other_repo = uuid7()
        admin.execute(
            "INSERT INTO repositories (id, workspace_id, project_id, provider, local_root_ref)"
            " VALUES (%s, %s, %s, 'local', %s)",
            (other_repo, tenant.workspace_id, tenant.project_id, f"o/{other_repo.hex}"),
        )
        run = new_run(admin, tenant)
        with pytest.raises(psycopg.errors.CheckViolation):
            _thread(admin, tenant, run=run, repository_id=other_repo)
        # Находка другого run — отказ.
        foreign_run = new_run(admin, tenant)
        finding = q.finding(admin, tenant, foreign_run)
        with pytest.raises(psycopg.errors.CheckViolation):
            _thread(admin, tenant, run=run, finding_id=finding)

        thread = _thread(admin, tenant, run=run)
        # Якорь неизменяем.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("UPDATE review_threads SET start_line = 1 WHERE id = %s", (thread,))
        # FSM: OPEN → RESOLVED (решивший — пользователь сессии) → OPEN; version растёт.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute(
                "UPDATE review_threads SET status = 'RESOLVED', resolved_by = %s, resolved_at = now() WHERE id = %s",
                (other, thread),
            )
        _as_user(admin, tenant.owner_id)
        admin.execute(
            "UPDATE review_threads SET status = 'RESOLVED', resolved_by = %s, resolved_at = now() WHERE id = %s",
            (tenant.owner_id, thread),
        )
        admin.execute(
            "UPDATE review_threads SET status = 'OPEN', resolved_by = NULL, resolved_at = NULL WHERE id = %s", (thread,)
        )
        row = admin.execute("SELECT status, version FROM review_threads WHERE id = %s", (thread,)).fetchone()
        assert row == ("OPEN", 3)
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute("UPDATE review_threads SET status = 'CLOSED' WHERE id = %s", (thread,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("DELETE FROM review_threads WHERE id = %s", (thread,))


def test_comment_edit_history_is_append_only(database: testdb.TestDatabase) -> None:
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        other = add_user(admin, tenant, "developer")
        thread = _thread(admin, tenant)
        comment = _comment(admin, tenant, thread, "first")
        _as_user(admin, tenant.owner_id)
        admin.execute("UPDATE review_comments SET body = 'second' WHERE id = %s", (comment,))
        admin.execute("UPDATE review_comments SET body = 'third' WHERE id = %s", (comment,))
        row = admin.execute(
            "SELECT body, revision, edited_at IS NOT NULL FROM review_comments WHERE id = %s", (comment,)
        ).fetchone()
        assert row == ("third", 3, True)
        history = admin.execute(
            "SELECT revision, body, superseded_by FROM review_comment_revisions WHERE comment_id = %s ORDER BY revision",
            (comment,),
        ).fetchall()
        assert history == [(1, "first", tenant.owner_id), (2, "second", tenant.owner_id)]
        # Только автор правит тело; история неизменяема; удалить комментарий нельзя.
        _as_user(admin, other)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("UPDATE review_comments SET body = 'hijack' WHERE id = %s", (comment,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("UPDATE review_comment_revisions SET body = 'x' WHERE comment_id = %s", (comment,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("DELETE FROM review_comments WHERE id = %s", (comment,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute(
                "INSERT INTO review_comments (id, workspace_id, thread_id, author_id, body) VALUES (%s, %s, %s, %s, 'x')",
                (uuid7(), tenant.workspace_id, thread, tenant.owner_id),
            )


def test_review_submission_effect_constraint(database: testdb.TestDatabase) -> None:
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        run = new_run(admin, tenant)
        _as_user(admin, tenant.owner_id)
        admin.execute(
            "INSERT INTO review_submissions (id, workspace_id, run_id, verdict, submitted_by)"
            " VALUES (%s, %s, %s, 'COMMENT', %s)",
            (uuid7(), tenant.workspace_id, run, tenant.owner_id),
        )
        for verdict in ("APPROVE", "REQUEST_CHANGES"):
            with pytest.raises(psycopg.errors.CheckViolation):
                admin.execute(
                    "INSERT INTO review_submissions (id, workspace_id, run_id, verdict, submitted_by)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (uuid7(), tenant.workspace_id, run, verdict, tenant.owner_id),
                )
