"""P4-02: синхронизация справочника `agent_profiles` (миграция 0006) с версионированными профилями ролей."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.agents.profiles import PROFILES, ROLES, sync_profiles
from aicd_platform.db import create_engine
from tests.support.db import TestDatabase

pytestmark = pytest.mark.db


@pytest.fixture
async def migrator(database: TestDatabase) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database.url_for("aicd_migrator"), pool_size=1)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:  # исходное состояние для остальных тестов
            await sync_profiles(conn)
        await engine.dispose()


async def test_sync_is_idempotent_and_never_downgrades(migrator: AsyncEngine) -> None:
    async with migrator.begin() as conn:
        await conn.execute(text("DELETE FROM agent_profiles WHERE NOT EXISTS (SELECT 1 FROM agent_assignments)"))
        first = await sync_profiles(conn)
    assert [r.role for r in first] == list(ROLES)
    assert {r.action for r in first} <= {"inserted", "unchanged"}
    async with migrator.begin() as conn:
        second = await sync_profiles(conn)
        rows = {
            r["role"]: r
            for r in (
                await conn.execute(
                    text(
                        "SELECT role, instruction_template_version, allowed_tools, required_capabilities FROM agent_profiles"
                    )
                )
            ).mappings()
        }
    assert {r.action for r in second} == {"unchanged"}
    assert set(rows) == set(ROLES)
    for role, row in rows.items():
        assert row["instruction_template_version"] == PROFILES[role].template_version
        assert row["allowed_tools"] == list(PROFILES[role].allowed_tools)
        assert row["required_capabilities"] == list(PROFILES[role].required_capabilities)

    async with migrator.begin() as conn:
        await conn.execute(text("UPDATE agent_profiles SET instruction_template_version = '9.0.0' WHERE role = 'qa'"))
        await conn.execute(text("UPDATE agent_profiles SET allowed_tools = '[]'::jsonb WHERE role = 'developer'"))
        third = {r.role: r for r in await sync_profiles(conn)}
    assert third["qa"].action == "kept_newer" and third["qa"].version == "9.0.0"
    assert third["developer"].action == "updated"
    async with migrator.begin() as conn:
        await conn.execute(text("UPDATE agent_profiles SET instruction_template_version = '1.0.0' WHERE role = 'qa'"))


async def test_app_and_worker_cannot_write_profiles(database: TestDatabase, migrator: AsyncEngine) -> None:
    async with migrator.begin() as conn:
        await sync_profiles(conn)
    for role in ("aicd_app", "aicd_worker"):
        engine = create_engine(database.url_for(role), pool_size=1)
        try:
            async with engine.connect() as conn:
                count = (await conn.execute(text("SELECT count(*) FROM agent_profiles"))).scalar_one()
                assert count == len(ROLES)
            with pytest.raises(DBAPIError, match="permission denied"):
                async with engine.begin() as conn:
                    await sync_profiles(conn)
        finally:
            await engine.dispose()


def test_cli_sync_agent_profiles(database: TestDatabase) -> None:
    env = {**os.environ, "AICD_MIGRATOR_DATABASE_URL": database.url_for("aicd_migrator")}
    proc = subprocess.run(
        [sys.executable, "-m", "aicd_cli.main", "db", "sync-agent-profiles"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == len(ROLES) and all(
        line.split(": ")[1] in ("inserted", "unchanged", "updated") for line in lines
    )
    no_url = {k: v for k, v in env.items() if k != "AICD_MIGRATOR_DATABASE_URL"}
    proc = subprocess.run(
        [sys.executable, "-m", "aicd_cli.main", "db", "sync-agent-profiles"],
        text=True,
        capture_output=True,
        env=no_url,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 2 and "AICD_MIGRATOR_DATABASE_URL" in proc.stderr
