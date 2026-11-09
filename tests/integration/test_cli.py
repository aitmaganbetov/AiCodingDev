"""P1-09: bootstrap владельца без дефолтных паролей."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys

import pytest
from fastapi import FastAPI

from aicd_platform.config import Settings
from tests.conftest import PASSWORD, make_client
from tests.support.db import TestDatabase

pytestmark = pytest.mark.db


def _run(settings: Settings, *args: str, stdin: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "AICD_DATABASE_URL": settings.require_database_url()}
    return subprocess.run(
        [sys.executable, "-m", "aicd_cli.main", *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=60,
    )


async def test_create_owner_then_login(app: FastAPI, settings: Settings, database: TestDatabase) -> None:
    email, slug = f"owner-{secrets.token_hex(3)}@corp.local", f"team-{secrets.token_hex(3)}"
    proc = _run(
        settings,
        "admin",
        "create-owner",
        "--email",
        email,
        "--name",
        "Владелец",
        "--workspace-slug",
        slug,
        "--workspace-name",
        "Команда",
        "--password-stdin",
        stdin=PASSWORD + "\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert PASSWORD not in proc.stdout + proc.stderr
    async with make_client(app) as client:
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["workspaces"][0]["slug"] == slug and r.json()["workspaces"][0]["role"] == "owner"


def test_weak_password_rejected(settings: Settings) -> None:
    proc = _run(
        settings,
        "admin",
        "create-owner",
        "--email",
        "weak@corp.local",
        "--name",
        "x",
        "--workspace-slug",
        "weak-ws",
        "--workspace-name",
        "x",
        "--password-stdin",
        stdin="short\n",
    )
    assert proc.returncode == 1 and "символов" in proc.stderr and "Traceback" not in proc.stderr


def test_retention_schedule_rejects_too_short_interval(capsys: pytest.CaptureFixture[str]) -> None:
    """P3-18: `aicd maintenance retention --every` — расписание без cron; интервал < 60 с — ошибка конфигурации."""
    from aicd_cli.main import main

    assert main(["maintenance", "retention", "--every", "10"]) == 2
    assert "--every" in capsys.readouterr().err


def test_orphan_objects_rejects_zero_grace(capsys: pytest.CaptureFixture[str]) -> None:
    """`aicd maintenance orphan-objects`: grace < 1 ч — ошибка конфигурации (не удалять незакоммиченные объекты)."""
    from aicd_cli.main import main

    assert main(["maintenance", "orphan-objects", "--grace-hours", "0"]) == 2
    assert "--grace-hours" in capsys.readouterr().err
