"""P2-12: реестр установок CLI и GET /providers (без учётных данных в ответе)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters import registry
from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import worktree_resolver
from aicd_platform.db import DbContext, create_engine, transaction
from aicd_platform.ids import uuid7
from aicd_security.local_runner import LocalProcessRunner
from tests.conftest import UserSession
from tests.support import db as testdb
from tests.support.cli import MemorySink
from tests.support.contract import validate_response

pytestmark = pytest.mark.db


async def test_probe_recorded_and_listed(
    app: FastAPI, app_engine: AsyncEngine, user: UserSession, database: testdb.TestDatabase, tmp_path: Path
) -> None:
    ws = await user.create_workspace()
    worker = create_engine(database.url_for("aicd_worker"))
    adapter = FakeCLIExecutor(
        provider="claude",
        runner=LocalProcessRunner(),
        artifact_sink=MemorySink(),
        worktree_path=worktree_resolver(tmp_path),
        auth_status="AUTH_REQUIRED",
    )
    probe = await adapter.probe(installation_id=uuid7())
    async with transaction(worker, DbContext(actor_type="system")) as conn:
        host = await registry.register_host(conn, hostname="exec-test-host")
        assert await registry.register_host(conn, hostname="exec-test-host") == host  # upsert
        inst = await registry.record_probe(conn, host_id=host, probe=probe, duration_ms=12)
        assert await registry.record_probe(conn, host_id=host, probe=probe) == inst
    await worker.dispose()

    r = await user.client.get("/api/v1/providers", headers={"X-Workspace-Id": ws["id"]})
    validate_response("listProviders", r)
    assert r.status_code == 200
    by_code = {p["code"]: p for p in r.json()["items"]}
    assert set(by_code) == {"codex", "claude"}
    install = next(i for i in by_code["claude"]["installations"] if i["installation_id"] == str(inst))
    assert install["auth_status"] == "AUTH_REQUIRED" and install["availability"] == "UNAVAILABLE"
    assert install["last_error_category"] == "AUTH_REQUIRED" and install["compatibility"] == "UNTESTED"
    assert by_code["claude"]["workspace_settings"]["max_parallel_sessions"] == 1
    body = r.text.lower()
    # capability token_usage_exposed допустима; запрещены значения учётных данных и пути
    for forbidden in ("binary_path", "credential", "access_token", "api_key", "cookie", "password", "/usr/"):
        assert forbidden not in body


async def test_providers_require_membership(user: UserSession) -> None:
    r = await user.client.get("/api/v1/providers", headers={"X-Workspace-Id": str(uuid7())})
    assert r.status_code == 404
