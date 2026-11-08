"""P5-02/P5-04/P5-05 с реальным PostgreSQL (миграция 0007): задание индексации под ролью `aicd_worker`
(RLS), инкрементальная переиндексация (меняются только изменённые файлы), FTS/pg_trgm, ripgrep по экспорту
снимка, жизненный цикл `indexing_jobs` и изоляция workspace.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_code_intelligence.index import INDEX_VERSION
from aicd_code_intelligence.index.jobs import enqueue_indexing_job, run_indexing_job
from aicd_code_intelligence.index.search import (
    fts_search,
    materialize_export,
    ripgrep_search,
    snapshot_text_files,
    trigram_search,
)
from aicd_platform.db import DbContext, create_engine, transaction
from tests.support import db as testdb
from tests.support.orchestration import Tenant, connect_admin, seed_tenant

pytestmark = pytest.mark.db

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": os.environ["PATH"],
}

FILES = {
    "src/shop/__init__.py": "",
    "src/shop/cart.py": (
        "from .pricing import total_price\n\n\nclass ShoppingCart:\n"
        '    """Корзина покупателя."""\n\n'
        "    def checkoutOrder(self) -> int:\n        return total_price([1, 2])\n"
    ),
    "src/shop/pricing.py": "def total_price(items: list[int]) -> int:\n    return sum(items)\n",
    "tests/test_cart.py": "from shop.cart import ShoppingCart\n\n\ndef test_checkout() -> None:\n    assert ShoppingCart()\n",
    "web/api.ts": 'import { get } from "./http";\nexport function loadCart(): unknown {\n  return get("/cart");\n}\n',
    "web/http.ts": "export function get(url: string): unknown {\n  return url;\n}\n",
    "php/Cart.php": "<?php\nnamespace Shop;\n\nfinal class Cart\n{\n    public function add(): void {}\n}\n",
    "db/schema.sql": "CREATE TABLE carts (id bigint);\n",
    "README.md": "# Shop\n\n## Checkout flow\n",
    "node_modules/x/index.js": "module.exports = 1;\n",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        capture_output=True,
        text=True,
        check=True,
        env=GIT_ENV,
    ).stdout.strip()


def init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=GIT_ENV)


def commit(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@dataclass
class Env:
    database: testdb.TestDatabase
    engine: AsyncEngine
    tenant: Tenant
    roots: dict[str, Path]
    repo: Path
    tmp: Path

    @property
    def ctx(self) -> DbContext:
        return DbContext(workspace_id=self.tenant.workspace_id, actor_type="system")

    async def enqueue(self, sha: str, kind: str = "incremental") -> uuid.UUID:
        async with transaction(self.engine, self.ctx) as conn:
            job_id, created = await enqueue_indexing_job(
                conn,
                workspace_id=self.tenant.workspace_id,
                repository_id=self.tenant.repository_id,
                commit_sha=sha,
                kind=kind,
            )
        assert created
        return job_id

    async def scalar(self, sql: str, **params: object) -> object:
        async with transaction(self.engine, self.ctx) as conn:
            return (await conn.execute(text(sql), params)).scalar()


@pytest.fixture
async def env(database: testdb.TestDatabase, tmp_path: Path) -> AsyncIterator[Env]:
    root = (tmp_path / "roots").resolve()
    root.mkdir()
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn)  # local_root_ref = main/<repository_id.hex>
    repo = root / tenant.repository_id.hex
    init_repo(repo)
    engine = create_engine(database.url_for("aicd_worker"))
    yield Env(database, engine, tenant, {"main": root}, repo, tmp_path)
    await engine.dispose()


async def test_full_then_incremental_indexing(env: Env) -> None:
    sha1 = commit(env.repo, FILES, "init")
    job1 = await env.enqueue(sha1)
    r1 = await run_indexing_job(env.engine, env.tenant.workspace_id, job1, repository_roots=env.roots)
    assert r1.status == "SUCCEEDED", r1.error
    assert r1.kind == "full" and r1.base_snapshot_id is None  # первой базы нет
    assert r1.stats is not None and r1.stats.files_reused == 0
    assert r1.stats.skipped == {"vendored": 1}
    snap1 = r1.snapshot_id
    assert snap1 is not None

    async with transaction(env.engine, env.ctx) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, index_version, file_count, symbol_count, commit_sha, completed_at IS NOT NULL"
                    " FROM code_index_snapshots WHERE id = :id"
                ),
                {"id": snap1},
            )
        ).one()
        assert row[0] == "READY" and row[1] == INDEX_VERSION and row[4] == sha1 and row[5]
        assert row[2] == len(FILES) - 1
        job = (
            await conn.execute(
                text(
                    "SELECT status, snapshot_id, attempt_count, kind, finished_at IS NOT NULL FROM indexing_jobs WHERE id = :id"
                ),
                {"id": job1},
            )
        ).one()
        assert tuple(job) == ("SUCCEEDED", snap1, 1, "full", True)
        edges = {
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT d.edge_type, s.file_path, t.file_path, d.target_ref FROM code_dependencies d"
                    "  JOIN code_symbols s ON s.id = d.source_symbol_id"
                    "  LEFT JOIN code_symbols t ON t.id = d.target_symbol_id WHERE d.snapshot_id = :s"
                ),
                {"s": snap1},
            )
        }
        assert ("imports", "src/shop/cart.py", "src/shop/pricing.py", ".pricing.total_price") in edges
        assert ("imports", "web/api.ts", "web/http.ts", "./http") in edges
        assert ("tests", "tests/test_cart.py", "src/shop/cart.py", None) in edges
        kinds = {
            tuple(r)
            for r in await conn.execute(
                text("SELECT symbol_kind, qualified_name FROM code_symbols WHERE snapshot_id = :s"), {"s": snap1}
            )
        }
        assert ("method", "shop.cart.ShoppingCart.checkoutOrder") in kinds
        assert ("class", "Shop\\Cart") in kinds
        assert ("type", "carts") in kinds
        assert ("test", "test_cart.test_checkout") in kinds

        # FTS: camelCase разбит на части; pg_trgm: опечатка в имени символа
        hits = await fts_search(conn, snapshot_id=snap1, query="checkout order")
        assert any(h.metadata.get("symbol") == "checkoutOrder" for h in hits)
        hits = await fts_search(conn, snapshot_id=snap1, query="checkoutOrder")
        assert hits and all(h.text_ref.startswith("git:") for h in hits)
        hits = await fts_search(conn, snapshot_id=snap1, query="'; DROP TABLE code_files; --")
        assert hits == []
        trgm = await trigram_search(conn, snapshot_id=snap1, query="ShopingCart")
        assert trgm and trgm[0].metadata.get("symbol") == "ShoppingCart"

        export_files = await snapshot_text_files(conn, snapshot_id=snap1)
    export = materialize_export(env.repo, sha1, export_files, env.tmp / "export-cache")
    matches = ripgrep_search(export, "total_price(")
    assert sorted({m.path for m in matches.matches}) == ["src/shop/cart.py", "src/shop/pricing.py"]
    assert not (export / "node_modules").exists()

    # Изменён один файл → разбирается только он, остальное копируется из базового снимка.
    sha2 = commit(env.repo, {"src/shop/pricing.py": "def total_price(items: list[int]) -> int:\n    return 0\n"})
    job2 = await env.enqueue(sha2)
    r2 = await run_indexing_job(env.engine, env.tenant.workspace_id, job2, repository_roots=env.roots)
    assert r2.status == "SUCCEEDED", r2.error
    assert r2.kind == "incremental" and r2.base_snapshot_id == snap1
    assert r2.stats is not None
    assert (r2.stats.blobs_read, r2.stats.files_parsed) == (1, 1)
    assert r2.stats.files_reused == len(FILES) - 2
    assert r2.persisted["copied_symbols"] > 0 and r2.persisted["copied_chunks"] > 0
    snap2 = r2.snapshot_id
    assert snap2 is not None
    async with transaction(env.engine, env.ctx) as conn:
        parent = (
            await conn.execute(
                text("SELECT parent_snapshot_id FROM code_index_snapshots WHERE id = :id"), {"id": snap2}
            )
        ).scalar()
        assert parent == snap1
        # тот же набор символов/рёбер, что и при полном построении (переиспользование не теряет данных)
        for table, cols in (
            ("code_symbols", "file_path, symbol_name, qualified_name, symbol_kind, start_line, end_line, content_hash"),
            ("code_files", "path, language, blob_sha, content_hash, is_test, is_generated, is_binary"),
        ):
            a = (
                await conn.execute(
                    text(f"SELECT {cols} FROM {table} WHERE snapshot_id = :s ORDER BY 1, 2, 3, 4"), {"s": snap1}
                )
            ).all()
            b = (
                await conn.execute(
                    text(f"SELECT {cols} FROM {table} WHERE snapshot_id = :s ORDER BY 1, 2, 3, 4"), {"s": snap2}
                )
            ).all()
            changed = {r[0] for r in set(a) ^ set(b)}
            assert changed == {"src/shop/pricing.py"}, table
        n1 = (
            await conn.execute(text("SELECT count(*) FROM code_dependencies WHERE snapshot_id = :s"), {"s": snap1})
        ).scalar()
        n2 = (
            await conn.execute(text("SELECT count(*) FROM code_dependencies WHERE snapshot_id = :s"), {"s": snap2})
        ).scalar()
        assert n1 == n2
        c1 = (
            await conn.execute(text("SELECT count(*) FROM knowledge_chunks WHERE snapshot_id = :s"), {"s": snap1})
        ).scalar()
        c2 = (
            await conn.execute(text("SELECT count(*) FROM knowledge_chunks WHERE snapshot_id = :s"), {"s": snap2})
        ).scalar()
        assert c1 == c2
        hits = await fts_search(conn, snapshot_id=snap2, query="checkoutOrder")
        assert hits  # скопированные search_vector работают

    # Повторная доставка завершённого задания — no-op; новое задание того же commit берёт готовый снимок.
    again = await run_indexing_job(env.engine, env.tenant.workspace_id, job2, repository_roots=env.roots)
    assert again.status == "SUCCEEDED" and again.snapshot_id is None
    job3 = await env.enqueue(sha2, kind="full")
    r3 = await run_indexing_job(env.engine, env.tenant.workspace_id, job3, repository_roots=env.roots)
    assert (r3.status, r3.snapshot_id) == ("SUCCEEDED", snap2)
    assert (
        await env.scalar(
            "SELECT count(*) FROM code_index_snapshots WHERE repository_id = :r", r=env.tenant.repository_id
        )
        == 2
    )


async def test_enqueue_deduplicates_active_job(env: Env) -> None:
    sha = commit(env.repo, {"a.py": "x = 1\n"})
    job = await env.enqueue(sha)
    async with transaction(env.engine, env.ctx) as conn:
        same, created = await enqueue_indexing_job(
            conn, workspace_id=env.tenant.workspace_id, repository_id=env.tenant.repository_id, commit_sha=sha
        )
    assert (same, created) == (job, False)


async def test_missing_commit_fails_job_without_snapshot(env: Env) -> None:
    commit(env.repo, {"a.py": "x = 1\n"})
    job = await env.enqueue("f" * 40)
    result = await run_indexing_job(env.engine, env.tenant.workspace_id, job, repository_roots=env.roots)
    assert result.status == "FAILED"
    assert result.error and "недоступен" in result.error
    row = await env.scalar(
        "SELECT status || '|' || error || '|' || (finished_at IS NOT NULL) FROM indexing_jobs WHERE id = :id", id=job
    )
    assert str(row).startswith("FAILED|") and str(row).endswith("|true")
    assert (
        await env.scalar(
            "SELECT count(*) FROM code_index_snapshots WHERE repository_id = :r", r=env.tenant.repository_id
        )
        == 0
    )


async def test_repository_outside_roots_fails(env: Env) -> None:
    sha = commit(env.repo, {"a.py": "x = 1\n"})
    job = await env.enqueue(sha)
    result = await run_indexing_job(env.engine, env.tenant.workspace_id, job, repository_roots={"other": env.tmp})
    assert result.status == "FAILED" and result.error == "путь репозитория вне разрешённых корней"


async def test_rls_isolates_index(env: Env) -> None:
    sha = commit(env.repo, {"a.py": "def alpha():\n    return 1\n"})
    job = await env.enqueue(sha)
    result = await run_indexing_job(env.engine, env.tenant.workspace_id, job, repository_roots=env.roots)
    assert result.status == "SUCCEEDED" and result.snapshot_id is not None
    with connect_admin(env.database) as conn:
        other = seed_tenant(conn)
    async with transaction(env.engine, DbContext(workspace_id=other.workspace_id, actor_type="system")) as conn:
        for table in ("code_index_snapshots", "code_files", "code_symbols", "knowledge_chunks", "indexing_jobs"):
            assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == 0
        assert await fts_search(conn, snapshot_id=result.snapshot_id, query="alpha") == []
    with pytest.raises(LookupError):
        await run_indexing_job(env.engine, other.workspace_id, job, repository_roots=env.roots)
