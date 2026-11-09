"""P9-03 / P3-18: сборка объектов-сирот хранилища артефактов (`aicd maintenance orphan-objects`), FS и S3."""

from __future__ import annotations

import io
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.artifact_gc import sweep_orphan_objects
from aicd_platform.artifacts import FileSystemArtifactStore, S3ArtifactStore
from aicd_platform.clock import utcnow
from aicd_platform.db import create_engine
from aicd_platform.ids import uuid7
from tests.support import db as testdb
from tests.support.orchestration import connect_admin, seed_tenant

pytestmark = pytest.mark.db


@pytest.fixture
async def maintenance_engine(database: testdb.TestDatabase) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database.url_for("aicd_maintenance"), pool_size=1)
    yield engine
    await engine.dispose()


def _register(admin: psycopg.Connection, ws: uuid.UUID, art: uuid.UUID, ref: str, sha: str, size: int) -> None:
    admin.execute(
        "INSERT INTO artifacts (id, workspace_id, kind, storage_ref, sha256, size_bytes, mime_type)"
        " VALUES (%s, %s, 'report', %s, %s, %s, 'text/plain')",
        (art, ws, ref, sha, size),
    )


def _audit(admin: psycopg.Connection, session: uuid.UUID) -> list[tuple[object, ...]]:
    return admin.execute(
        "SELECT action, metadata->>'orphans', metadata->>'deleted', metadata->>'dry_run' FROM audit_events"
        " WHERE resource_id = %s ORDER BY created_at, id",
        (session,),
    ).fetchall()


async def test_fs_orphans_deleted_after_grace_with_dry_run_and_audit(
    database: testdb.TestDatabase, maintenance_engine: AsyncEngine, tmp_path: Path
) -> None:
    store = FileSystemArtifactStore(tmp_path / "artifacts")
    old = time.time() - 3 * 86400
    with connect_admin(database) as admin:
        ws = seed_tenant(admin).workspace_id
        kept_id = uuid7()
        kept = store.put(ws, kept_id, io.BytesIO(b"registered"))
        _register(admin, ws, kept_id, kept.storage_ref, kept.sha256, kept.size_bytes)
    orphan = store.put(ws, uuid7(), io.BytesIO(b"orphan from rolled back transaction"))
    fresh = store.put(ws, uuid7(), io.BytesIO(b"put in progress, row not committed yet"))
    archive = tmp_path / "artifacts" / "archive" / "global" / "audit_events" / "2002-03.ndjson.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"archive")
    for ref in (kept.storage_ref, orphan.storage_ref):
        os.utime(tmp_path / "artifacts" / ref.removeprefix(store.prefix), (old, old))
    os.utime(archive, (old, old))

    dry = await sweep_orphan_objects(maintenance_engine, store, dry_run=True, reason="dry run test")
    assert (dry.scanned, dry.too_recent, dry.orphans, dry.deleted) == (3, 1, 1, 0)
    assert dry.orphan_refs == [orphan.storage_ref] and store.exists(orphan.storage_ref)

    report = await sweep_orphan_objects(maintenance_engine, store, grace=timedelta(hours=24))
    assert (report.orphans, report.deleted, report.failed) == (1, 1, 0)
    assert not store.exists(orphan.storage_ref), "сирота удалена"
    assert store.exists(kept.storage_ref) and store.exists(fresh.storage_ref), "со строкой и моложе grace — целы"
    assert archive.exists(), "архивы retention не рассматриваются"
    with connect_admin(database) as admin:
        assert _audit(admin, dry.session_id) == [
            ("maintenance.session_started", None, None, None),
            ("maintenance.orphan_objects_deleted", "1", "0", "true"),
            ("maintenance.session_finished", "1", "0", "true"),  # итоги прохода
        ]
        assert [r[0] for r in _audit(admin, report.session_id)] == [
            "maintenance.session_started", "maintenance.orphan_objects_deleted", "maintenance.session_finished",
        ]  # fmt: skip
    again = await sweep_orphan_objects(maintenance_engine, store)
    assert (again.orphans, again.deleted) == (0, 0), "повторный проход идемпотентен"


async def test_sweep_requires_maintenance_role(app_engine: AsyncEngine, tmp_path: Path) -> None:
    with pytest.raises(DBAPIError, match=r"permission denied|aicd_maintenance"):
        await sweep_orphan_objects(app_engine, FileSystemArtifactStore(tmp_path / "a"))


@pytest.mark.s3
async def test_s3_orphans_deleted(database: testdb.TestDatabase, maintenance_engine: AsyncEngine) -> None:
    endpoint = os.environ.get("AICD_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("AICD_TEST_S3_ENDPOINT не задан")
    store = S3ArtifactStore(endpoint_url=endpoint, bucket=f"aicd-gc-{secrets.token_hex(3)}",
                            access_key=os.environ["AICD_TEST_S3_ACCESS_KEY"],
                            secret_key=os.environ["AICD_TEST_S3_SECRET_KEY"])  # fmt: skip
    store.ensure_bucket()
    with connect_admin(database) as admin:
        ws = seed_tenant(admin).workspace_id
        kept_id = uuid7()
        kept = store.put(ws, kept_id, io.BytesIO(b"registered"))
        _register(admin, ws, kept_id, kept.storage_ref, kept.sha256, kept.size_bytes)
    orphan = store.put(ws, uuid7(), io.BytesIO(b"orphan"))
    # LastModified в S3 не подделать: проход «через 2 дня» — оба объекта старше grace.
    report = await sweep_orphan_objects(maintenance_engine, store, now=utcnow() + timedelta(days=2))
    assert (report.scanned, report.orphans, report.deleted) == (2, 1, 1)
    assert not store.exists(orphan.storage_ref) and store.exists(kept.storage_ref)
    recent = await sweep_orphan_objects(maintenance_engine, store, dry_run=True)
    assert (recent.too_recent, recent.orphans) == (1, 0)
