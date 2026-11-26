"""Аренда задачи продлевается на всё время проверки гейтами (регрессия).

Гейты (тесты, покрытие) идут дольше TTL аренды. Раньше продление останавливалось после выхода CLI: reaper
объявлял попытку LEASE_LOST посреди проверки, а так как LEASE_LOST не расходует бюджет попыток, задача
перезапускалась бесконечно («вечно выполняется» в Mission Control).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from aicd_orchestration import recovery
from tests.integration.test_directive_delivery import _completed, _orchestrator, _Passed, _scene, _Scripted
from tests.support import db as testdb
from tests.support.phase3 import Slice, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

LEASE_TTL_SECONDS = 1


class _SlowVerifier:
    """Проверка втрое дольше TTL аренды; посреди неё работает reaper."""

    def __init__(self, world: Slice) -> None:
        self.world = world
        self.reaped: list[uuid.UUID] = []

    async def verify_task(self, **kwargs: Any) -> _Passed:
        del kwargs
        await asyncio.sleep(LEASE_TTL_SECONDS * 2)
        report = await recovery.reap(self.world.worker)
        self.reaped.extend(report.reaped_tasks)
        await asyncio.sleep(LEASE_TTL_SECONDS)
        return _Passed()


async def test_lease_is_renewed_while_gates_run(world: Slice, database: testdb.TestDatabase) -> None:
    executor = _Scripted({"developer": _completed(), "reviewer": _completed()})
    orch = _orchestrator(world, executor, lease_ttl_seconds=LEASE_TTL_SECONDS)
    orch.verifier = _SlowVerifier(world)  # type: ignore[assignment]
    scene = await _scene(world, database, orch)
    main_id = scene.tasks()["main"][0]

    await orch.execute_task(main_id, scene.ws)

    attempts = scene.rows(
        "SELECT attempt_no, status, failure_category FROM task_attempts WHERE task_id = %s ORDER BY attempt_no", main_id
    )
    assert attempts == [(1, "SUCCEEDED", None)], attempts  # не LEASE_LOST и без второй попытки
    assert scene.tasks()["main"][1] == "SUCCEEDED"
    assert orch.verifier.reaped == []  # type: ignore[attr-defined]  # reaper не счёл аренду истёкшей
