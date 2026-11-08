"""Полный профиль strict в контейнерах (P6-02/P6-04/P6-09): run DEVELOP проходит все применимые gates
глобального профиля — включая SECRETS (gitleaks) — в закреплённых по digest образах через DockerSandboxRunner.

Остальные сквозные тесты оркестрации используют профиль workspace без gates «только в контейнере»
(`tests/support/phase3.local_profile`); этот тест доказывает, что полный профиль исполним, а не только
отдельные gates в своих docker-тестах.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from aicd_platform.db import DbContext, transaction
from aicd_security.docker_runner import DockerSandboxRunner
from aicd_verification.engine import VerificationEngine
from aicd_verification.images import default_gate_images
from aicd_verification.security_gates import SECURITY_GATE_IMAGES
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.phase3 import Slice, create_run, install_both, world

__all__ = ["world"]  # фикстура pytest

DOCKER_SOCK = "/var/run/docker.sock"


def _docker_available() -> bool:
    if not Path(DOCKER_SOCK).exists():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker", timeout=3) as c:
            return c.get("/_ping").status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.db,
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="docker daemon недоступен"),
]


async def test_develop_run_completes_with_full_strict_profile_in_containers(
    world: Slice, database: testdb.TestDatabase
) -> None:
    ws = uuid.UUID(world.ws)
    with orch.connect_admin(database) as admin:  # убрать профили workspace — работает глобальный strict
        admin.execute("DELETE FROM quality_gates WHERE workspace_id = %s", (ws,))
        admin.execute("DELETE FROM quality_profiles WHERE workspace_id = %s", (ws,))
    await install_both(world.worker)
    run_id = await create_run(world)
    root = world.tmp.resolve()
    orchestrator = world.orchestrator()
    async with DockerSandboxRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(root)], require_image_digest=True
    ) as runner:
        orchestrator.verifier = VerificationEngine(
            engine=world.worker,
            store=orchestrator.artifacts,  # type: ignore[arg-type]
            runner=runner,
            images=default_gate_images(SECURITY_GATE_IMAGES),
            staging_root=root / "staging",
            local_env={"PATH": os.environ["PATH"]},
        )
        await orchestrator.on_run_created(uuid.UUID(run_id), ws)
        status = await orchestrator.drive(uuid.UUID(run_id), ws)
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        gates = dict(
            (
                await conn.execute(
                    text(
                        "SELECT q.name, g.status FROM gate_executions g JOIN quality_gates q ON q.id = g.gate_id"
                        " WHERE g.run_id = :r AND g.superseded_at IS NULL"
                    ),
                    {"r": uuid.UUID(run_id)},
                )
            ).all()
        )
    assert status == "COMPLETED", gates
    assert gates.get("gitleaks-secrets") == "PASS", "SECRETS исполнен в образе gitleaks и прошёл"
    assert gates.get("scope") == "PASS"
    assert "ERROR" not in gates.values() and "SKIPPED" not in gates.values()
