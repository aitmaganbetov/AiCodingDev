"""AC-07 (P9-01): Fake CLI зависает (`timeout`) → `task_attempts.failure_category = TIMEOUT`, задача
`RETRY_WAIT` между попытками до исчерпания `max_attempts`, затем `FAILED`; run не `COMPLETED`; число попыток
не превышает бюджет.

Сквозной путь: оркестратор → CliTaskExecutor → ExecutionGateway → FakeCLIExecutor (настоящий процесс, убивается
по wall-clock тайм-ауту). Схема ExecutionRequest требует тайм-аут ≥ 30 с; чтобы тест шёл секунды, адаптер
этого теста сокращает только `limits.timeout_seconds` уже собранного запроса (как tests/support/cli.make_request
`small_limits`) — логика тайм-аута, классификация сбоя и бюджет повторов — продуктовые.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.models import ExecutionRequest
from tests.acceptance.support import rows
from tests.support import phase3
from tests.support.contract import validate_response
from tests.support.phase3 import Slice, create_run, drive, install_cli, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


class ShortTimeoutFake(FakeCLIExecutor):
    async def execute(self, request: ExecutionRequest) -> Any:
        limits = request.limits.model_copy(update={"timeout_seconds": 1})
        self._grace = 0.5
        return await super().execute(request.model_copy(update={"limits": limits}))


async def test_ac07_cli_timeout_records_category_respects_retry_budget_and_never_completes(
    world: Slice, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phase3, "FakeCLIExecutor", ShortTimeoutFake)
    await install_cli(world.worker)
    world.scenario["name"] = "timeout"
    run_id = await create_run(world)
    assert await drive(world, run_id) == "FAILED"

    run = await world.user.client.get(f"/api/v1/runs/{run_id}")
    validate_response("getRun", run)
    assert run.json()["status"] == "FAILED", "run не объявлен успешным"
    tasks = (await world.user.client.get(f"/api/v1/runs/{run_id}/tasks")).json()["tasks"]
    main = next(t for t in tasks if t["agent_role"] == "developer")
    budget = await rows(world, "SELECT max_attempts FROM tasks WHERE id = :t", t=uuid.UUID(main["id"]))
    max_attempts = budget[0]["max_attempts"]
    assert main["status"] == "FAILED" and main["attempt_count"] == max_attempts == 3

    attempts = await world.user.client.get(f"/api/v1/tasks/{main['id']}/attempts")
    validate_response("listTaskAttempts", attempts)
    items = attempts.json()["items"]
    assert len(items) == max_attempts, "число попыток не превышает бюджет"
    assert [a["failure_category"] for a in items] == ["TIMEOUT"] * max_attempts
    assert all(a["executions"] and a["executions"][0]["status"] == "TIMED_OUT" for a in items)
    executions = await rows(
        world,
        "SELECT e.status, e.failure_category FROM cli_executions e JOIN task_attempts a ON a.id = e.task_attempt_id"
        " WHERE a.task_id = :t",
        t=uuid.UUID(main["id"]),
    )
    assert executions == [{"status": "TIMED_OUT", "failure_category": "TIMEOUT"}] * max_attempts

    transitions = [
        (r["payload"]["from"], r["payload"]["to"])
        for r in await rows(
            world,
            "SELECT payload FROM outbox_events WHERE event_type = 'task.state_changed' AND aggregate_id = :t"
            " ORDER BY created_at, id",
            t=uuid.UUID(main["id"]),
        )
    ]
    assert transitions.count(("RUNNING", "RETRY_WAIT")) == max_attempts - 1, transitions
    assert transitions[-1] == ("RUNNING", "FAILED"), "после исчерпания бюджета — FAILED, без новой попытки"
    assert ("RUNNING", "SUCCEEDED") not in transitions
    review = next(t for t in tasks if t["agent_role"] == "reviewer")
    assert review["status"] != "SUCCEEDED" and review["attempt_count"] == 0, "зависимая задача не исполнялась"
