"""P5-01 / миграция 0007: code intelligence, документация, стек (DATABASE_DESIGN §6.5–6.7, §10, §11, §18.3, §18.11, §19)."""

from __future__ import annotations

import io
import os
import secrets
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

GLOBAL_TABLES = ["technologies", "technology_versions"]
TENANT_TABLES = [
    "project_technologies",
    "documentation_sources",
    "documentation_snapshots",
    "documentation_sections",
    "knowledge_entries",
    "architecture_decisions",
    "code_index_snapshots",
    "code_files",
    "code_symbols",
    "code_dependencies",
    "knowledge_chunks",
    "retrieval_queries",
    "retrieval_results",
    "context_packs",
    "indexing_jobs",
]
NEW_TABLES = set(GLOBAL_TABLES) | set(TENANT_TABLES)


def _sha() -> str:
    return secrets.token_hex(20)


def _h64() -> str:
    return secrets.token_hex(32)


def _tables(db: testdb.TestDatabase) -> set[str]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'aicd' AND c.relkind IN ('r','p') AND NOT c.relispartition"
        ).fetchall()
    return {r[0] for r in rows}


def _entity_check(db: testdb.TestDatabase) -> tuple[str, str]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'aicd.allowed_transitions'::regclass AND contype = 'c' AND conname <> "
            "'ck_allowed_transitions_not_self'"
        ).fetchone()
    assert row is not None
    return row[0], row[1]


# --- обратимость и offline-режим --------------------------------------------------------------------


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    assert url
    db = testdb.create_database(url, prefix="aicd_mig7")
    yield db
    testdb.drop_database(db)


def test_0007_upgrade_downgrade_upgrade(scratch_db: testdb.TestDatabase) -> None:
    testdb.alembic(scratch_db, "upgrade", "head")
    created = _tables(scratch_db)
    assert created >= NEW_TABLES
    assert _entity_check(scratch_db)[0] == "ck_allowed_transitions_entity"
    testdb.alembic(scratch_db, "downgrade", "0006")
    assert not (NEW_TABLES & _tables(scratch_db))
    name, definition = _entity_check(scratch_db)
    assert name == "allowed_transitions_entity_check" and "indexing_job" not in definition
    with psycopg.connect(scratch_db.superuser_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM aicd.allowed_transitions WHERE entity = 'indexing_job'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT to_regproc('aicd.trg_fn_knowledge_chunks_source')").fetchone() == (None,)
    testdb.alembic(scratch_db, "upgrade", "head")
    assert _tables(scratch_db) == created


def test_0007_offline_sql(scratch_db: testdb.TestDatabase) -> None:
    os.environ["AICD_MIGRATOR_DATABASE_URL"] = scratch_db.url_for("aicd_migrator")
    buf = io.StringIO()
    with redirect_stdout(buf):
        command.upgrade(Config(str(testdb.ALEMBIC_INI)), "0006:0007", sql=True)
    out = buf.getvalue()
    for table in NEW_TABLES:
        assert f"CREATE TABLE {table} (" in out
    assert "gin_trgm_ops" in out and "ADD CONSTRAINT ck_allowed_transitions_entity" in out


# --- схема: RLS, индексы, расширения ----------------------------------------------------------------


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


def test_search_indexes_and_extensions(database: testdb.TestDatabase) -> None:
    with psycopg.connect(database.superuser_url) as conn:
        exts = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        defs: dict[str, str] = dict(
            conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'aicd' AND indexname = ANY(%s)",
                (["ix_knowledge_chunks_fts", "ix_code_files_path_trgm", "ix_code_symbols_name_trgm"],),
            ).fetchall()
        )
    assert {"pg_trgm", "btree_gin"} <= exts
    assert "USING gin (workspace_id, search_vector)" in defs["ix_knowledge_chunks_fts"]
    assert "USING gin (path " in defs["ix_code_files_path_trgm"] and "gin_trgm_ops" in defs["ix_code_files_path_trgm"]
    assert "gin_trgm_ops" in defs["ix_code_symbols_name_trgm"]


# --- данные двух арендаторов -------------------------------------------------------------------------


@dataclass
class Side:
    tenant: Tenant
    run: uuid.UUID
    task: uuid.UUID
    attempt: uuid.UUID
    artifact: uuid.UUID
    ids: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def ws(self) -> uuid.UUID:
        return self.tenant.workspace_id


Inserter = Callable[[psycopg.Connection, Side], uuid.UUID]


def _tv(conn: psycopg.Connection) -> uuid.UUID:
    tech, tv = uuid7(), uuid7()
    conn.execute(
        "INSERT INTO technologies (id, name, ecosystem) VALUES (%s, %s, 'python')", (tech, f"lib-{tech.hex[-10:]}")
    )
    conn.execute(
        "INSERT INTO technology_versions (id, technology_id, version_label) VALUES (%s, %s, '1.0')", (tv, tech)
    )
    return tv


def ins_project_technologies(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    tv = s.ids.get("tv") or uuid7()
    conn.execute(
        "INSERT INTO project_technologies (workspace_id, project_id, technology_version_id, detected_from)"
        " VALUES (%s, %s, %s, 'pyproject.toml')",
        (s.ws, s.tenant.project_id, tv),
    )
    return tv


def ins_documentation_sources(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO documentation_sources (id, workspace_id, source_type, uri_or_file_ref, created_by)"
        " VALUES (%s, %s, 'url', %s, %s)",
        (rid, s.ws, f"https://docs.example.org/{rid}", s.tenant.owner_id),
    )
    return rid


def ins_documentation_snapshots(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO documentation_snapshots (id, workspace_id, source_id) VALUES (%s, %s, %s)",
        (rid, s.ws, s.ids["documentation_sources"]),
    )
    return rid


def ins_documentation_sections(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO documentation_sections (id, workspace_id, snapshot_id, section_path, body_ref, content_hash)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (rid, s.ws, s.ids["documentation_snapshots"], f"guide/{rid}", f"artifact:{s.artifact}#bytes=0-99", _h64()),
    )
    return rid


def ins_knowledge_entries(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO knowledge_entries (id, workspace_id, project_id, kind, title, body_ref, content_hash)"
        " VALUES (%s, %s, %s, 'note', 'n', %s, %s)",
        (rid, s.ws, s.tenant.project_id, s.artifact, _h64()),
    )
    return rid


def ins_architecture_decisions(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO architecture_decisions (id, workspace_id, project_id, adr_number, title, content_ref)"
        " VALUES (%s, %s, %s, %s, 'adr', %s)",
        (rid, s.ws, s.tenant.project_id, secrets.randbelow(10**6) + 1, s.artifact),
    )
    return rid


def ins_code_index_snapshots(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO code_index_snapshots (id, workspace_id, repository_id, commit_sha, index_version)"
        " VALUES (%s, %s, %s, %s, 1)",
        (rid, s.ws, s.tenant.repository_id, _sha()),
    )
    return rid


def ins_code_files(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO code_files (id, workspace_id, snapshot_id, path, language, blob_sha, content_hash, size_bytes)"
        " VALUES (%s, %s, %s, %s, 'python', %s, %s, 10)",
        (rid, s.ws, s.ids["code_index_snapshots"], f"src/pkg/mod_{rid.hex}.py", _sha(), _h64()),
    )
    return rid


def ins_code_symbols(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO code_symbols (id, workspace_id, snapshot_id, file_id, file_path, symbol_name, symbol_kind,"
        " start_line, end_line, content_hash) VALUES (%s, %s, %s, %s, 'src/x.py', %s, 'function', 1, 3, %s)",
        (rid, s.ws, s.ids["code_index_snapshots"], s.ids["code_files"], f"compute_total_{rid.hex[-6:]}", _h64()),
    )
    return rid


def ins_code_dependencies(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO code_dependencies (id, workspace_id, snapshot_id, source_symbol_id, target_ref, edge_type)"
        " VALUES (%s, %s, %s, %s, 'os.path', 'imports')",
        (rid, s.ws, s.ids["code_index_snapshots"], s.ids["code_symbols"]),
    )
    return rid


def ins_knowledge_chunks(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO knowledge_chunks (id, workspace_id, project_id, source_type, source_id, snapshot_id, chunk_index,"
        " text_ref, content_hash, search_vector) VALUES (%s, %s, %s, 'code_symbol', %s, %s, %s, %s, %s,"
        " to_tsvector('simple', 'compute total invoice amount'))",
        (
            rid,
            s.ws,
            s.tenant.project_id,
            s.ids["code_symbols"],
            s.ids["code_index_snapshots"],
            secrets.randbelow(10**6),
            f"git:{_sha()}#L1-L3",
            _h64(),
        ),
    )
    return rid


def ins_retrieval_queries(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO retrieval_queries (id, workspace_id, run_id, task_id, task_attempt_id, query_text, strategy,"
        " snapshot_id, documentation_mode) VALUES (%s, %s, %s, %s, %s, 'total invoice', 'fts', %s, 'SMART')",
        (rid, s.ws, s.run, s.task, s.attempt, s.ids["code_index_snapshots"]),
    )
    return rid


def ins_retrieval_results(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    conn.execute(
        "INSERT INTO retrieval_results (workspace_id, query_id, chunk_id, rank, score) VALUES (%s, %s, %s, %s, 0.5)",
        (s.ws, s.ids["retrieval_queries"], s.ids["knowledge_chunks"], secrets.randbelow(10**6) + 1),
    )
    return s.ids["retrieval_queries"]


def ins_context_packs(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO context_packs (id, workspace_id, task_attempt_id, snapshot_id, source_commit, policy_version,"
        " documentation_mode, token_budget, size_bytes, manifest, content_hash, artifact_id)"
        " VALUES (%s, %s, %s, %s, %s, 1, 'SMART', 8000, 1024, '{}', %s, %s)",
        (rid, s.ws, s.attempt, s.ids["code_index_snapshots"], _sha(), _h64(), s.artifact),
    )
    return rid


def ins_indexing_jobs(conn: psycopg.Connection, s: Side) -> uuid.UUID:
    rid = uuid7()
    conn.execute(
        "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha, kind) VALUES (%s, %s, %s, %s, 'full')",
        (rid, s.ws, s.tenant.repository_id, _sha()),
    )
    return rid


INSERTERS: dict[str, Inserter] = {t: globals()[f"ins_{t}"] for t in TENANT_TABLES}


def _artifact(conn: psycopg.Connection, tenant: Tenant) -> uuid.UUID:
    art = uuid7()
    conn.execute(
        "INSERT INTO artifacts (id, workspace_id, project_id, kind, storage_ref, sha256, size_bytes, mime_type)"
        " VALUES (%s, %s, %s, 'context_pack', %s, %s, 1, 'application/json')",
        (art, tenant.workspace_id, tenant.project_id, f"file://artifacts/{tenant.workspace_id}/{art}", _h64()),
    )
    return art


def _side(conn: psycopg.Connection) -> Side:
    tenant = seed_tenant(conn)
    run = new_run(conn, tenant)
    drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
    task = new_task(conn, tenant, run)
    lease_task(conn, task)
    side = Side(tenant, run, task, new_attempt(conn, tenant, task), _artifact(conn, tenant))
    side.ids["tv"] = _tv(conn)
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
            # Чужие строки не изменяются (для ролей без UPDATE — отказ в праве, а не 0 строк).
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
        # Вставка строки чужого workspace: worker — отказ RLS (WITH CHECK), app — RLS или нет права INSERT.
        # knowledge_chunks: триггер §18.11 не видит источник чужого workspace раньше проверки RLS.
        expected: tuple[type[Exception], ...] = (psycopg.errors.InsufficientPrivilege,)
        if table == "knowledge_chunks":
            expected = (psycopg.errors.InsufficientPrivilege, psycopg.errors.ForeignKeyViolation)
        with pytest.raises(expected), conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            INSERTERS[table](conn, b)


def test_worker_builds_index_in_own_workspace(database: testdb.TestDatabase, world: World) -> None:
    """Сценарий worker-index/context pack под RLS: снимок → файл → символ → связь → фрагмент → поиск."""
    a = world.a
    with psycopg.connect(database.url_for("aicd_worker")) as conn, conn.transaction():
        _ctx(conn, a.ws)
        side = Side(
            a.tenant, a.run, a.task, a.attempt, a.artifact, {"documentation_sources": a.ids["documentation_sources"]}
        )
        for table in (
            "code_index_snapshots",
            "code_files",
            "code_symbols",
            "code_dependencies",
            "knowledge_chunks",
            "retrieval_queries",
            "retrieval_results",
            "indexing_jobs",
            "documentation_snapshots",
            "documentation_sections",
            "knowledge_entries",
            "architecture_decisions",
        ):
            side.ids[table] = INSERTERS[table](conn, side)
        side.ids["tv"] = _tv(conn)  # G: worker регистрирует обнаруженную технологию
        ins_project_technologies(conn, side)
        conn.execute(
            "UPDATE code_index_snapshots SET status = 'READY', completed_at = now(), file_count = 1 WHERE id = %s",
            (side.ids["code_index_snapshots"],),
        )
        conn.execute(
            "UPDATE retrieval_results SET selected = true WHERE query_id = %s", (side.ids["retrieval_queries"],)
        )
        for stmt in (
            "UPDATE code_files SET is_test = true",
            "DELETE FROM code_symbols",
            "UPDATE retrieval_results SET score = 1",
            "UPDATE documentation_sources SET enabled = false",
            "UPDATE technologies SET name = 'x'",
            "DELETE FROM technology_versions",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(stmt)


def test_app_commands(database: testdb.TestDatabase, world: World) -> None:
    a = world.a
    with psycopg.connect(database.url_for("aicd_app")) as conn, conn.transaction():
        _ctx(conn, a.ws, a.tenant.owner_id)
        side = Side(a.tenant, a.run, a.task, a.attempt, a.artifact, dict(a.ids))
        ins_documentation_sources(conn, side)
        ins_knowledge_entries(conn, side)
        ins_architecture_decisions(conn, side)
        job = ins_indexing_jobs(conn, side)
        conn.execute("UPDATE indexing_jobs SET status = 'CANCELLED', finished_at = now() WHERE id = %s", (job,))
        res = conn.execute(
            "UPDATE project_technologies SET confirmed_by = %s, confirmed_at = now() WHERE project_id = %s",
            (a.tenant.owner_id, a.tenant.project_id),
        )
        assert res.rowcount >= 1
        for table in ("code_index_snapshots", "code_files", "knowledge_chunks", "context_packs", "retrieval_queries"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                INSERTERS[table](conn, side)


@pytest.mark.parametrize("role", ["aicd_app", "aicd_worker"])
def test_global_tables_readable_not_writable(database: testdb.TestDatabase, world: World, role: str) -> None:
    with psycopg.connect(database.url_for(role)) as conn:
        for table in GLOBAL_TABLES:
            with conn.transaction():
                _ctx(conn, None)  # справочник G виден без контекста workspace
                count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                assert count is not None and count[0] >= 2
            for stmt in (f"UPDATE {table} SET created_at = now()", f"DELETE FROM {table}"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                    conn.execute(stmt)
        if role == "aicd_app":
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                _tv(conn)


def test_cross_tenant_composite_fks(admin: psycopg.Connection, world: World) -> None:
    a, b = world.a, world.b
    mixed = Side(a.tenant, a.run, a.task, a.attempt, a.artifact, dict(a.ids))
    mixed.ids["code_index_snapshots"] = b.ids["code_index_snapshots"]
    task = new_task(admin, a.tenant, a.run, key="t-fk")
    lease_task(admin, task)
    mixed.attempt = new_attempt(admin, a.tenant, task)  # свободная попытка: uq_context_packs_attempt не мешает
    for table in ("code_files", "retrieval_queries", "context_packs"):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            INSERTERS[table](admin, mixed)
    mixed = Side(a.tenant, b.run, b.task, a.attempt, b.artifact, dict(a.ids))
    for table in ("retrieval_queries", "knowledge_entries", "architecture_decisions"):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            INSERTERS[table](admin, mixed)
    # Символ и файл из разных снимков одного workspace — составной FK по snapshot_id.
    other_snapshot = ins_code_index_snapshots(admin, a)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO code_symbols (id, workspace_id, snapshot_id, file_id, file_path, symbol_name, symbol_kind,"
            " start_line, end_line, content_hash) VALUES (%s, %s, %s, %s, 'x', 's', 'class', 1, 1, %s)",
            (uuid7(), a.ws, other_snapshot, a.ids["code_files"], _h64()),
        )


def test_knowledge_chunk_polymorphic_source(admin: psycopg.Connection, world: World) -> None:
    a, b = world.a, world.b

    def chunk(source_type: str, source_id: uuid.UUID, snapshot: uuid.UUID | None, doc: uuid.UUID | None) -> None:
        admin.execute(
            "INSERT INTO knowledge_chunks (id, workspace_id, source_type, source_id, snapshot_id,"
            " documentation_snapshot_id, chunk_index, text_ref, content_hash, search_vector)"
            " VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, to_tsvector('simple', 'x'))",
            (uuid7(), a.ws, source_type, source_id, snapshot, doc, f"artifact:{a.artifact}#bytes=0-1", _h64()),
        )

    snap, doc = a.ids["code_index_snapshots"], a.ids["documentation_snapshots"]
    chunk("code_file", a.ids["code_files"], snap, None)
    chunk("documentation_section", a.ids["documentation_sections"], None, doc)
    chunk("knowledge_entry", a.ids["knowledge_entries"], None, None)
    chunk("architecture_decision", a.ids["architecture_decisions"], None, None)
    for source_type, source_id, s, d in (
        ("code_symbol", uuid7(), snap, None),  # нет такого символа
        ("code_symbol", a.ids["code_files"], snap, None),  # тип не совпадает с таблицей
        ("code_file", a.ids["code_files"], ins_code_index_snapshots(admin, a), None),  # другой снимок
        ("knowledge_entry", b.ids["knowledge_entries"], None, None),  # чужой workspace
    ):
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match="not found"):
            chunk(source_type, source_id, s, d)
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_knowledge_chunks_freshness_anchor"):
        chunk("knowledge_entry", a.ids["knowledge_entries"], snap, None)  # фрагмент записи не привязан к снимку


def test_key_checks(admin: psycopg.Connection, world: World) -> None:
    a = world.a
    snap = a.ids["code_index_snapshots"]
    for path in ("/etc/passwd", "../x.py", "src/../../x", "a/.."):
        with pytest.raises(psycopg.errors.CheckViolation, match="code_files"):
            admin.execute(
                "INSERT INTO code_files (id, workspace_id, snapshot_id, path, blob_sha, content_hash, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, %s, 0)",
                (uuid7(), a.ws, snap, path, _sha(), _h64()),
            )
    admin.execute(
        "INSERT INTO code_files (id, workspace_id, snapshot_id, path, blob_sha, content_hash, size_bytes)"
        " VALUES (%s, %s, %s, 'docs/..hidden/a..b.md', %s, %s, 0)",
        (uuid7(), a.ws, snap, _sha(), _h64()),
    )
    for uri in ("http://docs.example.org/x", "file:///etc/passwd", "gopher://x"):
        with pytest.raises(psycopg.errors.CheckViolation, match="ck_doc_sources_uri"):
            admin.execute(
                "INSERT INTO documentation_sources (id, workspace_id, source_type, uri_or_file_ref, created_by)"
                " VALUES (%s, %s, 'url', %s, %s)",
                (uuid7(), a.ws, uri, a.tenant.owner_id),
            )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_retrieval_queries_docs_off"):
        admin.execute(
            "INSERT INTO retrieval_queries (id, workspace_id, run_id, task_id, query_text, strategy,"
            " documentation_mode) VALUES (%s, %s, %s, %s, 'q', 'docs', 'OFF')",
            (uuid7(), a.ws, a.run, a.task),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_adr_approved"):
        admin.execute(
            "UPDATE architecture_decisions SET status = 'ACCEPTED' WHERE id = %s", (a.ids["architecture_decisions"],)
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_doc_snapshots_ready"):
        admin.execute(
            "UPDATE documentation_snapshots SET status = 'READY' WHERE id = %s", (a.ids["documentation_snapshots"],)
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_code_dependencies_target"):
        admin.execute(
            "INSERT INTO code_dependencies (id, workspace_id, snapshot_id, source_symbol_id, edge_type)"
            " VALUES (%s, %s, %s, %s, 'calls')",
            (uuid7(), a.ws, snap, a.ids["code_symbols"]),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_cis_completed"):
        admin.execute("UPDATE code_index_snapshots SET status = 'READY' WHERE id = %s", (snap,))
    with pytest.raises(psycopg.errors.CheckViolation, match="knowledge_chunks_text_ref"):
        admin.execute(
            "INSERT INTO knowledge_chunks (id, workspace_id, source_type, source_id, chunk_index, text_ref,"
            " content_hash, search_vector) VALUES (%s, %s, 'knowledge_entry', %s, 0, 'file:///etc/passwd', %s, '')",
            (uuid7(), a.ws, a.ids["knowledge_entries"], _h64()),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="technologies_ecosystem"):
        admin.execute("INSERT INTO technologies (id, name, ecosystem) VALUES (%s, 'x', 'Not Valid')", (uuid7(),))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="immutable"):
        admin.execute("UPDATE code_files SET workspace_id = %s WHERE id = %s", (world.b.ws, a.ids["code_files"]))


def test_indexing_job_fsm(admin: psycopg.Connection, world: World) -> None:
    a = world.a
    job = ins_indexing_jobs(admin, a)
    with pytest.raises(psycopg.errors.UniqueViolation, match="uq_indexing_jobs_active"):
        admin.execute(
            "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha, kind)"
            " SELECT %s, workspace_id, repository_id, commit_sha, 'full' FROM indexing_jobs WHERE id = %s",
            (uuid7(), job),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="invalid indexing_job transition: QUEUED -> SUCCEEDED"):
        admin.execute("UPDATE indexing_jobs SET status = 'SUCCEEDED', finished_at = now() WHERE id = %s", (job,))
    admin.execute("UPDATE indexing_jobs SET status = 'RUNNING', started_at = now() WHERE id = %s", (job,))
    admin.execute("UPDATE indexing_jobs SET status = 'QUEUED', attempt_count = 1 WHERE id = %s", (job,))  # повтор
    admin.execute("UPDATE indexing_jobs SET status = 'RUNNING' WHERE id = %s", (job,))
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_indexing_jobs_finished"):
        admin.execute("UPDATE indexing_jobs SET status = 'SUCCEEDED' WHERE id = %s", (job,))
    admin.execute(
        "UPDATE indexing_jobs SET status = 'SUCCEEDED', finished_at = now(), snapshot_id = %s WHERE id = %s",
        (a.ids["code_index_snapshots"], job),
    )
    for target in ("RUNNING", "QUEUED", "FAILED"):
        with pytest.raises(psycopg.errors.CheckViolation, match=f"SUCCEEDED -> {target}"):
            admin.execute("UPDATE indexing_jobs SET status = %s WHERE id = %s", (target, job))
    with pytest.raises(psycopg.errors.CheckViolation, match="__new__ -> RUNNING"):
        admin.execute(
            "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha, kind, status)"
            " VALUES (%s, %s, %s, %s, 'full', 'RUNNING')",
            (uuid7(), a.ws, a.tenant.repository_id, _sha()),
        )
    # Инкрементальная задача в RUNNING требует базовый снимок.
    inc = uuid7()
    admin.execute(
        "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha) VALUES (%s, %s, %s, %s)",
        (inc, a.ws, a.tenant.repository_id, _sha()),
    )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_indexing_jobs_kind"):
        admin.execute("UPDATE indexing_jobs SET status = 'RUNNING' WHERE id = %s", (inc,))


def test_fts_and_trigram_search(database: testdb.TestDatabase, world: World) -> None:
    a = world.a
    with psycopg.connect(database.url_for("aicd_worker")) as conn, conn.transaction():
        _ctx(conn, a.ws)
        hits = conn.execute(
            "SELECT id FROM knowledge_chunks WHERE workspace_id = %s AND search_vector @@ to_tsquery('simple', 'invoice')",
            (a.ws,),
        ).fetchall()
        assert a.ids["knowledge_chunks"] in {r[0] for r in hits}
        paths = conn.execute(
            "SELECT path FROM code_files WHERE %s <%% path", ("pkg/mod",)
        ).fetchall()  # word_similarity
        assert paths and all(p[0].startswith("src/pkg/mod_") for p in paths)
        names = conn.execute("SELECT id FROM code_symbols WHERE symbol_name ILIKE %s", ("%total%",)).fetchall()
        assert a.ids["code_symbols"] in {r[0] for r in names}


def test_snapshot_retention_semantics(admin: psycopg.Connection, world: World) -> None:
    """§1.6/§10.8: удаление снимка каскадирует содержимое, но запрещено, пока на него ссылается context pack."""
    a = world.a
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="fk_context_packs_snapshot"):
        admin.execute("DELETE FROM code_index_snapshots WHERE id = %s", (a.ids["code_index_snapshots"],))
    s = Side(a.tenant, a.run, a.task, a.attempt, a.artifact, {})
    for table in ("code_index_snapshots", "code_files", "code_symbols", "code_dependencies", "knowledge_chunks"):
        s.ids[table] = INSERTERS[table](admin, s)
    admin.execute("DELETE FROM code_index_snapshots WHERE id = %s", (s.ids["code_index_snapshots"],))
    for table in ("code_files", "code_symbols", "code_dependencies", "knowledge_chunks"):
        assert admin.execute(f"SELECT count(*) FROM {table} WHERE id = %s", (s.ids[table],)).fetchone() == (0,)


# --- sys_plan_docs_download (POST /documentation/sources/{id}/refresh) ---------------------------------


def _plan(
    conn: psycopg.Connection, source: uuid.UUID, key: str = "k1", request: str = '{"reason": "refresh"}'
) -> object:
    row = conn.execute("SELECT sys_plan_docs_download(%s, %s, %s::jsonb)", (source, key, request)).fetchone()
    assert row is not None
    return row[0]


def test_plan_docs_download(database: testdb.TestDatabase, admin: psycopg.Connection, world: World) -> None:
    a, b = world.a, world.b
    source = a.ids["documentation_sources"]
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            first = _plan(conn, source)
            assert isinstance(first, uuid.UUID) and first.version == 7
            assert _plan(conn, source) == first, "повтор с тем же ключом идемпотентен"
            assert _plan(conn, source, key="k2") != first
        # Член другого workspace: в своём контексте источник не виден, в чужом — не член.
        with conn.transaction():
            _ctx(conn, b.ws, b.tenant.owner_id)
            assert _plan(conn, source) is None
        with conn.transaction():
            _ctx(conn, a.ws, b.tenant.owner_id)
            assert _plan(conn, source) is None
        with conn.transaction():
            _ctx(conn, None, a.tenant.owner_id)
            assert _plan(conn, source) is None
        for key, request in (("", "{}"), ("k", "[]"), ("k", '{"s": "' + "x" * 16400 + '"}')):
            with pytest.raises(psycopg.errors.InvalidParameterValue), conn.transaction():
                _ctx(conn, a.ws, a.tenant.owner_id)
                _plan(conn, source, key=key, request=request)
        # Прямой INSERT в external_actions у api нет.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            _ctx(conn, a.ws, a.tenant.owner_id)
            conn.execute(
                "INSERT INTO external_actions (id, workspace_id, action_type, idempotency_key, target, request_hash)"
                " VALUES (%s, %s, 'docs_download', %s, 'https://x', %s)",
                (uuid7(), a.ws, _h64(), _h64()),
            )
    row = admin.execute(
        "SELECT workspace_id, run_id, action_type, status, target FROM external_actions WHERE id = %s", (first,)
    ).fetchone()
    assert row == (a.ws, None, "docs_download", "PLANNED", f"https://docs.example.org/{source}")
    # Роль ниже developer — 42501; отключённый источник — 55000.
    reviewer, developer = uuid7(), uuid7()
    for user, role in ((reviewer, "reviewer"), (developer, "developer")):
        admin.execute("INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'u')", (user, f"{user}@ci.test"))
        admin.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, %s)", (a.ws, user, role)
        )
    disabled = ins_documentation_sources(admin, a)
    admin.execute("UPDATE documentation_sources SET enabled = false WHERE id = %s", (disabled,))
    with psycopg.connect(database.url_for("aicd_app")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="reviewer"), conn.transaction():
            _ctx(conn, a.ws, reviewer)
            _plan(conn, source, key="k3")
        with conn.transaction():
            _ctx(conn, a.ws, developer)
            assert isinstance(_plan(conn, source, key="k3"), uuid.UUID)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState), conn.transaction():
            _ctx(conn, a.ws, developer)
            _plan(conn, disabled)
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _plan(conn, source)


def test_sys_indexing_workspaces_lists_queued_and_stale_jobs(database: testdb.TestDatabase) -> None:
    """Воркер индексации находит workspace с заданиями вне своего RLS-контекста; aicd_app — нет."""
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn)
        repo = conn.execute(
            "SELECT id FROM repositories WHERE workspace_id = %s LIMIT 1", (tenant.workspace_id,)
        ).fetchone()
        if repo is None:
            pytest.skip("seed_tenant без репозитория")
        conn.execute(
            "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha, kind) VALUES (%s, %s, %s, %s, 'full')",
            (uuid7(), tenant.workspace_id, repo[0], "c" * 40),
        )
    with psycopg.connect(database.url_for("aicd_worker")) as worker:
        found = {r[0] for r in worker.execute("SELECT workspace_id FROM sys_indexing_workspaces(1000, 600)")}
    assert tenant.workspace_id in found
    with psycopg.connect(database.url_for("aicd_app")) as app, pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("SELECT * FROM sys_indexing_workspaces(10, 600)")
