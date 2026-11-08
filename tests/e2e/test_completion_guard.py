"""P6-09: guard завершения run в оркестраторе (engine.tick, блок VERIFYING → COMPLETED).

Все задачи SUCCEEDED и их gates PASS, но в run открыта находка critical (FP-17) — run не получает
COMPLETED, а завершается FAILED (GATE_FAILED) с причинами guard'а.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_verification.engine import VerificationEngine, VerificationOutcome
from tests.support.phase3 import Slice, create_run, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


class _FindingAfterVerify:
    """Настоящий VerificationEngine + открытая critical-находка ревью после проверки задачи."""

    def __init__(self, inner: VerificationEngine, world: Slice) -> None:
        self.inner = inner
        self.world = world

    async def verify_task(self, **kw: Any) -> VerificationOutcome:
        outcome = await self.inner.verify_task(**kw)
        async with transaction(self.world.worker, DbContext(workspace_id=kw["workspace_id"], actor_type="system")) as c:
            await c.execute(
                text(
                    "INSERT INTO findings (id, workspace_id, run_id, task_id, source, severity, rule_id, title, fingerprint)"
                    " VALUES (:id, :ws, :r, :t, 'agent_review', 'critical', 'review/sql-injection', 'SQL injection', :fp)"
                    " ON CONFLICT DO NOTHING"
                ),
                {"id": uuid7(), "ws": kw["workspace_id"], "r": kw["run_id"], "t": kw["task_id"], "fp": "a" * 64},
            )
        return outcome


async def test_open_critical_finding_blocks_completion(world: Slice) -> None:
    await install_both(world.worker)
    run_id = uuid.UUID(await create_run(world))
    ws = uuid.UUID(world.ws)
    orch = world.orchestrator()
    assert isinstance(orch.verifier, VerificationEngine)
    orch.verifier = _FindingAfterVerify(orch.verifier, world)
    await orch.on_run_created(run_id, ws)
    assert await orch.drive(run_id, ws, fast_forward_backoff=True) == "FAILED"
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        run = (
            (
                await conn.execute(
                    text("SELECT failure_category, failure_message FROM runs WHERE id = :r"), {"r": run_id}
                )
            )
            .mappings()
            .one()
        )
        tasks = (await conn.execute(text("SELECT status FROM tasks WHERE run_id = :r"), {"r": run_id})).scalars().all()
    assert set(tasks) == {"SUCCEEDED"}  # задачи и их gates зелёные — блокирует только находка
    assert run["failure_category"] == "GATE_FAILED"
    assert "open critical finding review/sql-injection" in run["failure_message"]
