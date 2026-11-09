"""Миграция 0012 (P8-01): github_installations, github_webhook_deliveries, pull_requests — обратимость, offline SQL,
RLS (T-таблицы), права ролей, SECURITY DEFINER функции приёма вебхука и резолвера установки, FK repositories,
триггеры PR и разрешение approvals GitHub-предмета в COMPLETED run (DATABASE_DESIGN §16, §19.4–19.5)."""

from __future__ import annotations

import io
import json
import os
import secrets
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
from tests.support.orchestration import Tenant, connect_admin, drive_run, new_run, seed_tenant, update

pytestmark = pytest.mark.db

TABLES = {"github_installations", "github_webhook_deliveries", "pull_requests"}
FUNCTIONS = {"sys_resolve_installation_workspace", "sys_github_webhook_receive", "trg_fn_pull_requests_state"}
RECEIVE = "SELECT sys_github_webhook_receive(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"


def _tables(db: testdb.TestDatabase) -> set[str]:
    with psycopg.connect(db.superuser_url) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'aicd' AND c.relkind IN ('r','p') AND NOT c.relispartition"
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


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig12")
    yield db
    testdb.drop_database(db)


def test_0012_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _tables(scratch_db) >= TABLES
    assert _functions(scratch_db) >= FUNCTIONS
    testdb.alembic(scratch_db, "downgrade", "0011")
    assert not (TABLES & _tables(scratch_db))
    assert not (FUNCTIONS & _functions(scratch_db))
    with psycopg.connect(scratch_db.superuser_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'fk_repositories_gh_installation'"
        ).fetchone() == (0,)
        body = conn.execute("SELECT prosrc FROM pg_proc WHERE proname = 'trg_fn_lock_run_share'").fetchone()
        assert body is not None and "github" not in body[0], "триггер возвращён к виду 0003"
        assert conn.execute("SELECT has_table_privilege('aicd_sysfn', 'aicd.outbox_events', 'INSERT')").fetchone() == (
            False,
        )
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _tables(scratch_db) >= TABLES


def test_0012_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0011:0012", sql=True)
    out = buf.getvalue()
    for table in TABLES:
        assert f"CREATE TABLE {table}" in out
    assert "CREATE FUNCTION sys_github_webhook_receive" in out
    assert "VALIDATE CONSTRAINT fk_repositories_gh_installation" in out


def test_classes_rls_and_grants(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url) as conn:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'aicd' AND c.relname = ANY(%s)",
                (sorted(TABLES),),
            ).fetchall()
        }
        assert rows == {
            "github_installations": (True, True),
            "pull_requests": (True, True),
            "github_webhook_deliveries": (False, False),  # G-класс (§19.4)
        }

        def priv(role: str, table: str, p: str) -> bool:
            row = conn.execute("SELECT has_table_privilege(%s, %s, %s)", (role, f"aicd.{table}", p)).fetchone()
            return bool(row and row[0])

        # api: прямого доступа к доставкам вебхуков нет (только sys_github_webhook_receive); PR — только чтение.
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not priv("aicd_app", "github_webhook_deliveries", p), p
        assert priv("aicd_app", "pull_requests", "SELECT")
        assert not priv("aicd_app", "pull_requests", "INSERT") and not priv("aicd_app", "pull_requests", "UPDATE")
        # 0014 (OQ-47 (2)): api регистрирует установку в статусе PENDING, данные аккаунта/права пишет воркер.
        assert priv("aicd_app", "github_installations", "INSERT")
        status_update = conn.execute(
            "SELECT has_column_privilege('aicd_app', 'aicd.github_installations', 'status', 'UPDATE'),"
            " has_column_privilege('aicd_app', 'aicd.github_installations', 'permissions', 'UPDATE')"
        ).fetchone()
        assert status_update == (True, False)  # только status (отключение / повторная проверка)
        assert not priv("aicd_app", "github_installations", "DELETE")
        # worker: не создаёт установки (заполняет проверенные), ведёт PR и статус доставок.
        assert not priv("aicd_worker", "github_installations", "INSERT")
        assert priv("aicd_worker", "pull_requests", "INSERT") and priv("aicd_worker", "pull_requests", "UPDATE")
        assert not priv("aicd_worker", "github_webhook_deliveries", "INSERT")
        assert not priv("aicd_worker", "pull_requests", "DELETE")
        assert priv("aicd_maintenance", "github_webhook_deliveries", "DELETE")
        for fn, role, expected in (
            ("sys_github_webhook_receive(uuid,uuid,uuid,text,text,bigint,boolean,text,text,text,jsonb)", "aicd_app", True),
            ("sys_github_webhook_receive(uuid,uuid,uuid,text,text,bigint,boolean,text,text,text,jsonb)", "aicd_worker", False),
            ("sys_resolve_installation_workspace(bigint)", "aicd_worker", True),
            ("sys_resolve_installation_workspace(bigint)", "aicd_app", False),
        ):  # fmt: skip
            assert conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, f"aicd.{fn}")
            ).fetchone() == (expected,), (fn, role)
        for name in ("sys_github_webhook_receive", "sys_resolve_installation_workspace"):
            secdef, owner, config = conn.execute(
                "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig FROM pg_proc p WHERE p.proname = %s",
                (name,),
            ).fetchone()  # type: ignore[misc]
            assert secdef and owner == "aicd_sysfn" and "search_path=pg_catalog, aicd" in config


# --- данные ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Side:
    tenant: Tenant
    installation_row: uuid.UUID
    installation_id: int
    repository: uuid.UUID

    @property
    def ws(self) -> uuid.UUID:
        return self.tenant.workspace_id


def _side(conn: psycopg.Connection) -> Side:
    tenant = seed_tenant(conn)
    inst_id = secrets.randbelow(10**9) + 1
    row = uuid7()
    conn.execute(
        "INSERT INTO github_installations (id, workspace_id, installation_id, account_login, account_type)"
        " VALUES (%s, %s, %s, 'acme', 'organization')",
        (row, tenant.workspace_id, inst_id),
    )
    repo = uuid7()
    conn.execute(
        "INSERT INTO repositories (id, workspace_id, project_id, provider, remote_url, local_root_ref,"
        " github_installation_id, github_repo_id, full_name) VALUES (%s, %s, %s, 'github', 'https://github.com/acme/a',"
        " %s, %s, %s, 'acme/a')",
        (repo, tenant.workspace_id, tenant.project_id, f"main/{repo.hex}", row, secrets.randbelow(10**9) + 1),
    )
    return Side(tenant, row, inst_id, repo)


def _pr(conn: psycopg.Connection, s: Side, number: int, run: uuid.UUID | None = None) -> uuid.UUID:
    pr = uuid7()
    conn.execute(
        "INSERT INTO pull_requests (id, workspace_id, repository_id, run_id, pr_number, head_branch, base_branch,"
        " head_sha, state, url) VALUES (%s, %s, %s, %s, %s, 'aicd/x/y', 'main', %s, 'OPEN', 'https://github.com/acme/a/pull/1')",
        (pr, s.ws, s.repository, run, number, "a" * 40),
    )
    return pr


@pytest.fixture(scope="module")
def sides(database: testdb.TestDatabase) -> tuple[Side, Side]:
    with connect_admin(database) as conn:
        a, b = _side(conn), _side(conn)
        _pr(conn, a, 1)
        _pr(conn, b, 1)
    return a, b


def _ctx(conn: psycopg.Connection, ws: uuid.UUID | None, user: uuid.UUID | None = None, actor: str = "user") -> None:
    conn.execute(
        "SELECT set_config('app.user_id', %s, true), set_config('app.workspace_id', %s, true),"
        " set_config('app.actor_type', %s, true)",
        (str(user) if user else "", str(ws) if ws else "", actor),
    )


@pytest.mark.parametrize("role", ["aicd_app", "aicd_worker"])
@pytest.mark.parametrize("table", ["github_installations", "pull_requests"])
def test_rls_isolates_workspaces(
    database: testdb.TestDatabase, sides: tuple[Side, Side], role: str, table: str
) -> None:
    a, b = sides
    column = "status" if table == "github_installations" else "state"  # колонки с правом UPDATE у роли
    with psycopg.connect(database.url_for(role)) as conn:
        with conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            seen = {r[0] for r in conn.execute(f"SELECT workspace_id FROM {table}").fetchall()}
            assert seen == {a.ws}
            if (role == "aicd_worker" and table == "pull_requests") or (
                role == "aicd_app" and table == "github_installations"
            ):
                res = conn.execute(f"UPDATE {table} SET {column} = {column} WHERE workspace_id = %s", (b.ws,))
                assert res.rowcount == 0
        with conn.transaction():
            _ctx(conn, None)
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), "без контекста — fail-closed"


def test_insert_into_foreign_workspace_and_composite_fk(
    database: testdb.TestDatabase, sides: tuple[Side, Side]
) -> None:
    a, b = sides
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            _ctx(conn, a.ws, actor="system")
            conn.execute(
                "INSERT INTO pull_requests (id, workspace_id, repository_id, pr_number, head_branch, base_branch,"
                " head_sha, state, url) VALUES (%s, %s, %s, 9, 'h', 'main', %s, 'OPEN', 'https://x')",
                (uuid7(), b.ws, b.repository, "b" * 40),
            )
    with connect_admin(database) as conn:
        # Составной FK: репозиторий workspace B для PR workspace A — нарушение FK.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO pull_requests (id, workspace_id, repository_id, pr_number, head_branch, base_branch,"
                " head_sha, state, url) VALUES (%s, %s, %s, 9, 'h', 'main', %s, 'OPEN', 'https://x')",
                (uuid7(), a.ws, b.repository, "b" * 40),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "UPDATE repositories SET github_installation_id = %s WHERE id = %s", (b.installation_row, a.repository)
            )
        # UNIQUE (installation_id) глобален: одна установка — один workspace (§16.1).
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO github_installations (id, workspace_id, installation_id, account_login, account_type)"
                " VALUES (%s, %s, %s, 'acme', 'user')",
                (uuid7(), b.ws, a.installation_id),
            )
        for bad in ("bad login!", "x" * 40):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO github_installations (id, workspace_id, installation_id, account_login, account_type)"
                    " VALUES (%s, %s, %s, %s, 'user')",
                    (uuid7(), a.ws, secrets.randbelow(10**9) + 1, bad),
                )


def test_pull_request_constraints(database: testdb.TestDatabase, sides: tuple[Side, Side]) -> None:
    a, _ = sides
    with connect_admin(database) as conn:
        pr = _pr(conn, a, 50)
        with pytest.raises(psycopg.errors.CheckViolation, match="ck_pull_requests_merged"):
            conn.execute("UPDATE pull_requests SET state = 'MERGED' WHERE id = %s", (pr,))
        with pytest.raises(psycopg.errors.CheckViolation, match="ck_pull_requests_closed"):
            conn.execute("UPDATE pull_requests SET state = 'CLOSED' WHERE id = %s", (pr,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE pull_requests SET url = 'http://insecure' WHERE id = %s", (pr,))
        conn.execute(
            "UPDATE pull_requests SET state = 'MERGED', merged_at = now(), merged_commit_sha = %s WHERE id = %s",
            ("c" * 40, pr),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="terminal"):
            conn.execute(
                "UPDATE pull_requests SET state = 'OPEN', merged_at = NULL, merged_commit_sha = NULL WHERE id = %s",
                (pr,),
            )
        # merge_action_id — только pr_merge (AC-13: merge платформой — только через ledger).
        action = uuid7()
        conn.execute(
            "INSERT INTO external_actions (id, workspace_id, action_type, idempotency_key, target, request_hash)"
            " VALUES (%s, %s, 'pr_comment', %s, 't', %s)",
            (action, a.ws, secrets.token_hex(32), "0" * 64),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="pr_merge"):
            conn.execute("UPDATE pull_requests SET merge_action_id = %s WHERE id = %s", (action, pr))


# --- приём вебхука (sys_github_webhook_receive) -----------------------------------------------------------


def _receive(
    conn: psycopg.Connection, guid: uuid.UUID, *, valid: bool, status: str, event: str = "pull_request"
) -> str:
    row = conn.execute(
        RECEIVE,
        (uuid7(), uuid7(), guid, event, "opened" if valid else None, 77 if valid else None, valid, "f" * 64, status,
         None, json.dumps({"pr_number": 1} if valid else {})),
    ).fetchone()  # fmt: skip
    assert row is not None
    return str(row[0])


def test_webhook_receive_dedup_and_rejected_upgrade(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.url_for("aicd_app"), autocommit=True) as conn:
        guid = uuid.uuid4()
        assert _receive(conn, guid, valid=True, status="RECEIVED") == "accepted"
        assert _receive(conn, guid, valid=True, status="RECEIVED") == "duplicate"
        assert _receive(conn, guid, valid=False, status="REJECTED") == "rejected"
        forged = uuid.uuid4()
        assert _receive(conn, forged, valid=False, status="REJECTED") == "rejected"
        assert _receive(conn, forged, valid=True, status="RECEIVED") == "accepted", "подделка не блокирует подлинную"
        ignored = uuid.uuid4()
        assert _receive(conn, ignored, valid=True, status="IGNORED", event="workflow_run") == "ignored"
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            _receive(conn, uuid.uuid4(), valid=False, status="RECEIVED")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM github_webhook_deliveries")
    with connect_admin(database) as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT delivery_guid, status, signature_valid, processed_at IS NOT NULL FROM github_webhook_deliveries"
                " WHERE delivery_guid = ANY(%s)",
                ([guid, forged, ignored],),
            ).fetchall()
        }
        assert rows == {
            guid: ("RECEIVED", True, False),
            forged: ("RECEIVED", True, False),
            ignored: ("IGNORED", True, True),
        }
        events = conn.execute(
            "SELECT count(*) FROM outbox_events WHERE event_type = 'github.webhook.received'"
            " AND payload->>'delivery_guid' = ANY(%s)",
            ([str(guid), str(forged), str(ignored)],),
        ).fetchone()
        assert events == (2,), "событие outbox — только для принятых доставок, без дублей"


def test_installation_resolver(database: testdb.TestDatabase, sides: tuple[Side, Side]) -> None:
    a, b = sides
    with psycopg.connect(database.url_for("aicd_worker"), autocommit=True) as conn:
        assert conn.execute("SELECT sys_resolve_installation_workspace(%s)", (a.installation_id,)).fetchone() == (a.ws,)
        assert conn.execute("SELECT sys_resolve_installation_workspace(%s)", (b.installation_id,)).fetchone() == (b.ws,)
        assert conn.execute("SELECT sys_resolve_installation_workspace(1)").fetchone() == (None,)


def test_github_approvals_allowed_in_completed_run(database: testdb.TestDatabase) -> None:
    """MERGE/MANUAL_REVIEW с subject.scope=github — после COMPLETED run; прочие записи — по-прежнему запрещены."""
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn)
        run = new_run(conn, tenant)
        drive_run(conn, run, "DISCOVERY", "PLANNED")
        update(conn, "runs", run, approved_plan_version=1)
        drive_run(conn, run, "RUNNING", "VERIFYING")
        commit = q.sha1()
        gate = q.ensure_gate(conn, "ruff-lint", "LINT")
        q.gate_execution(
            conn, tenant, run, gate, "LINT", "PASS", commit, evidence_id=q.evidence(conn, tenant, run, commit)
        )
        update(conn, "runs", run, status="COMPLETED", finished_at="now")

        def approval(action: str, subject: dict[str, str]) -> uuid.UUID:
            aid = uuid7()
            conn.execute(
                "INSERT INTO approvals (id, workspace_id, project_id, run_id, action_type, risk_level, required_role,"
                " requested_by_type, requested_by, subject_hash, subject) VALUES (%s, %s, %s, %s, %s, 'high', 'admin',"
                " 'user', %s, %s, %s)",
                (aid, tenant.workspace_id, tenant.project_id, run, action, tenant.owner_id, secrets.token_hex(32),
                 json.dumps(subject)),
            )  # fmt: skip
            return aid

        merge = approval("MERGE", {"scope": "github", "kind": "pr_merge"})
        approval("MANUAL_REVIEW", {"scope": "github", "kind": "ci_config_push"})
        with pytest.raises(psycopg.errors.CheckViolation, match="COMPLETED"):
            approval("MERGE", {"kind": "other"})
        with pytest.raises(psycopg.errors.CheckViolation, match="COMPLETED"):
            approval("DESTRUCTIVE_MIGRATION", {"scope": "github"})
        reviewer = uuid7()
        conn.execute(
            "INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'a')", (reviewer, f"{reviewer}@t.test")
        )
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'admin')",
            (tenant.workspace_id, reviewer),
        )
        update(conn, "approvals", merge, status="APPROVED", decided_by=reviewer, decided_at="now")
        with pytest.raises(psycopg.errors.CheckViolation, match="COMPLETED"):
            conn.execute("INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title,"
                         " instructions, complexity, risk_level) VALUES (%s, %s, %s, 1, 'late', 'developer', 't', 'i', 1,"
                         " 'low')", (uuid7(), tenant.workspace_id, run))  # fmt: skip
