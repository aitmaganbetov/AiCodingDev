"""`GET /artifacts/{id}/download` (P7-08/P7-10): поток через API, RBAC, 404 для чужого/истёкшего/пропавшего,
`cli_raw_log` только admin/owner, audit `artifact.download`, обрыв потока при расхождении sha256."""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.artifacts import ArtifactStore
from aicd_platform.ids import uuid7
from tests.conftest import UserSession, login, make_client
from tests.support import db as testdb
from tests.support.contract import validate_response
from tests.support.orchestration import Tenant, connect_admin, seed_tenant

pytestmark = pytest.mark.db

DIFF = b"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


@dataclass
class Seed:
    owner: UserSession
    tenant: Tenant
    diff: uuid.UUID
    raw: uuid.UUID
    expired: uuid.UUID
    missing: uuid.UUID


def _store(app: FastAPI) -> ArtifactStore:
    store: ArtifactStore = app.state.aicd.artifacts
    return store


def _put(
    conn: psycopg.Connection,
    store: ArtifactStore,
    tenant: Tenant,
    content: bytes,
    *,
    kind: str,
    mime: str = "text/x-diff",
    sensitive: bool = False,
    expired: bool = False,
    sha256: str | None = None,
) -> uuid.UUID:
    art = uuid7()
    stored = store.put(tenant.workspace_id, art, io.BytesIO(content))
    conn.execute(
        "INSERT INTO artifacts (id, workspace_id, project_id, kind, storage_ref, sha256, size_bytes, mime_type,"
        " is_sensitive, retention_class, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ephemeral', CASE WHEN %s THEN now() - interval '1 hour' END)",
        (
            art,
            tenant.workspace_id,
            tenant.project_id,
            kind,
            stored.storage_ref,
            sha256 or stored.sha256,
            stored.size_bytes,
            mime,
            sensitive,
            expired,
        ),
    )
    return art


@pytest.fixture(scope="module")
async def seed(app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase) -> AsyncIterator[Seed]:
    owner = await login(app, app_engine)
    store = _store(app)
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn, owner_id=uuid.UUID(owner.user_id))
        diff = _put(conn, store, tenant, DIFF, kind="git_diff")
        raw = _put(conn, store, tenant, b"raw cli log", kind="cli_raw_log", mime="text/plain", sensitive=True)
        expired = _put(conn, store, tenant, b"old", kind="other", mime="text/plain", expired=True)
        missing = uuid7()
        conn.execute(
            "INSERT INTO artifacts (id, workspace_id, project_id, kind, storage_ref, sha256, size_bytes, mime_type)"
            " VALUES (%s, %s, %s, 'other', %s, %s, 1, 'text/plain')",
            (missing, tenant.workspace_id, tenant.project_id, f"file://artifacts/{tenant.workspace_id}/gone", "0" * 64),
        )
    yield Seed(owner, tenant, diff, raw, expired, missing)
    await owner.client.aclose()


async def _member(
    app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase, s: Seed, role: str
) -> UserSession:
    member = await login(app, app_engine)
    with connect_admin(database) as conn:
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, %s)",
            (s.tenant.workspace_id, member.user_id, role),
        )
    return member


def _audit_count(database: testdb.TestDatabase, action: str, resource: uuid.UUID) -> int:
    with connect_admin(database) as conn:
        row = conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND resource_id = %s", (action, resource)
        ).fetchone()
    return int(row[0]) if row else 0


async def test_download_streams_content_with_headers_and_audit(seed: Seed, database: testdb.TestDatabase) -> None:
    r = await seed.owner.client.get(f"/api/v1/artifacts/{seed.diff}/download")
    validate_response("downloadArtifact", r)
    assert r.status_code == 200 and r.content == DIFF
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == f'attachment; filename="git_diff-{seed.diff}.diff"'
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert _audit_count(database, "artifact.download", seed.diff) >= 1


async def test_raw_cli_log_requires_admin(
    app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase, seed: Seed
) -> None:
    owner = await seed.owner.client.get(f"/api/v1/artifacts/{seed.raw}/download")
    assert owner.status_code == 200 and owner.content == b"raw cli log"
    for role, expected in (("developer", 403), ("viewer", 403), ("admin", 200)):
        member = await _member(app, app_engine, database, seed, role)
        try:
            r = await member.client.get(f"/api/v1/artifacts/{seed.raw}/download")
            validate_response("downloadArtifact", r)
            assert r.status_code == expected, (role, r.text)
            ok = await member.client.get(f"/api/v1/artifacts/{seed.diff}/download")
            assert ok.status_code == 200, role  # artifact:download — всем ролям workspace
        finally:
            await member.client.aclose()
    with connect_admin(database) as conn:
        denied = conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'authz.denied' AND workspace_id = %s",
            (seed.tenant.workspace_id,),
        ).fetchone()
    assert denied and denied[0] >= 2, "отказ в сыром логе аудируется"


async def test_expired_missing_foreign_and_unknown_are_404(
    app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase, seed: Seed
) -> None:
    for art in (seed.expired, seed.missing, uuid7()):
        r = await seed.owner.client.get(f"/api/v1/artifacts/{art}/download")
        validate_response("downloadArtifact", r)
        assert r.status_code == 404, r.text
    stranger = await login(app, app_engine)
    try:
        r = await stranger.client.get(f"/api/v1/artifacts/{seed.diff}/download")
        assert r.status_code == 404, "чужой workspace неотличим от отсутствующего (AC-12)"
    finally:
        await stranger.client.aclose()
    anonymous = make_client(app, "203.0.113.50")
    try:
        r = await anonymous.get(f"/api/v1/artifacts/{seed.diff}/download")
        assert r.status_code == 401
    finally:
        await anonymous.aclose()
    bad = await seed.owner.client.get("/api/v1/artifacts/not-a-uuid/download")
    assert bad.status_code == 422


async def test_integrity_mismatch_aborts_stream(app: FastAPI, database: testdb.TestDatabase, seed: Seed) -> None:
    with connect_admin(database) as conn:
        tampered = _put(conn, _store(app), seed.tenant, b"payload", kind="other", mime="text/plain", sha256="f" * 64)
    try:
        r = await seed.owner.client.get(f"/api/v1/artifacts/{tampered}/download")
    except Exception:  # noqa: BLE001 — транспорт оборвал ответ: подменённые данные не доставлены целиком
        return
    assert r.status_code != 200 or r.content != b"payload", "поток с неверным sha256 не должен завершаться успешно"
