"""AC-01 (P9-01): локальный репозиторий из allowlist → проект → `POST /runs` DEVELOP → 201 `CREATED` →
run переходит в `DISCOVERY`, событие `run.state_changed` получено клиентом по SSE (живой поток, не replay).

Поток читается через настоящий HTTP-сервер (uvicorn в том же event loop, как в tests/integration/test_sse.py),
оркестратор — тот же, что в сквозном срезе Phase 3 (Fake CLI, worktree, verification).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from redis.asyncio import Redis

from aicd_platform import sse
from aicd_platform.outbox_dispatcher import dispatch_batch
from tests.acceptance.support import rows
from tests.conftest import REDIS_URL
from tests.integration.test_sse import Reader, _client, fast_stream, server
from tests.support.contract import validate_response
from tests.support.events import validate_sse
from tests.support.phase3 import Slice, drive, install_both, world

__all__ = ["fast_stream", "server", "world"]  # фикстуры pytest

pytestmark = [pytest.mark.db, pytest.mark.redis]


async def _dispatch(s: Slice, redis: Redis) -> None:
    """Outbox → run_event_log + уведомление SSE-потоков (как воркер outbox-dispatcher)."""

    async def publish(event: dict[str, Any]) -> None:
        return None

    async def notify(run_id: uuid.UUID) -> None:
        await redis.publish(sse.run_channel(run_id), "1")

    for _ in range(50):
        if await dispatch_batch(s.worker, publish, notify) == 0:
            break


async def _until(reader: Reader, wanted: str, seen: list[dict[str, Any]], wait: float = 15.0) -> dict[str, Any]:
    async def loop() -> dict[str, Any]:
        while True:
            frames, closed = await reader.frames(1, wait)
            for frame in frames:
                if "data" not in frame or frame.get("event") == "heartbeat":
                    continue
                env: dict[str, Any] = json.loads(frame["data"])
                validate_sse(env)
                assert frame["id"] == str(env["seq"]) and frame["event"] == env["type"]
                seen.append(env)
                if env["type"] == "run.state_changed" and env["payload"]["to_status"] == wanted:
                    return env
            assert not closed, f"поток закрыт до перехода в {wanted}"

    return await asyncio.wait_for(loop(), wait)


async def test_ac01_attach_local_repo_create_project_start_develop_run(server: str, world: Slice) -> None:
    # Проект и привязка локального репозитория из allowlist (repository_roots) выполнены фикстурой `world`
    # через API (POST /projects, POST /projects/{id}/repositories → 201); проверяем результат через API.
    repos = await world.user.client.get(f"/api/v1/projects/{world.project}/repositories")
    validate_response("listRepositories", repos)
    (repo,) = repos.json()["items"]
    assert repo["provider"] == "local"

    # Репозиторий вне allowlist — отказ (не только «счастливый путь»).
    outside = await world.user.client.post(
        f"/api/v1/projects/{world.project}/repositories",
        json={"provider": "local", "local_path": "/etc"},
        headers=world.user.headers(idem=True),
    )
    validate_response("attachRepository", outside)
    assert outside.status_code == 422, outside.text

    await install_both(world.worker)
    created = await world.user.client.post(
        "/api/v1/runs",
        json={"project_id": world.project, "mode": "DEVELOP", "objective": "Добавить документ"},
        headers=world.user.headers(idem=True),
    )
    validate_response("createRun", created)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "CREATED"
    run_id = body["run_id"]
    assert body["event_stream"].endswith(f"/runs/{run_id}/events")

    redis = Redis.from_url(REDIS_URL)
    seen: list[dict[str, Any]] = []
    try:
        async with _client(server, world.user) as client:
            async with client.stream("GET", f"/runs/{run_id}/events", params={"after_seq": 0}) as stream:
                assert stream.status_code == 200
                reader = Reader(stream)
                await reader.heartbeat()  # поток открыт до старта run: событие придёт вживую
                orch = world.orchestrator()
                await orch.on_run_created(uuid.UUID(run_id), uuid.UUID(world.ws))
                await _dispatch(world, redis)
                event = await _until(reader, "DISCOVERY", seen)
    finally:
        await redis.aclose()

    assert event["run_id"] == run_id and event["payload"]["from_status"] == "CREATED"
    assert [e["seq"] for e in seen] == list(range(1, len(seen) + 1)), "события по порядку seq, без пропусков"
    changes = [e for e in seen if e["type"] == "run.state_changed"]
    transitions = [(e["payload"]["from_status"], e["payload"]["to_status"]) for e in changes]
    assert transitions[:2] == [(None, "CREATED"), ("CREATED", "DISCOVERY")], transitions
    logged = await rows(
        world,
        "SELECT seq, event_type FROM run_event_log WHERE run_id = :r ORDER BY seq",
        r=uuid.UUID(run_id),
    )
    assert {"seq": event["seq"], "event_type": "run.state_changed"} in logged, "событие SSE = запись журнала run"

    # Тот же run доводится до конца (DEVELOP на Fake CLI), статус виден через API.
    assert await drive(world, run_id) == "COMPLETED"
    final = await world.user.client.get(f"/api/v1/runs/{run_id}")
    validate_response("getRun", final)
    assert final.json()["mode"] == "DEVELOP" and final.json()["status"] == "COMPLETED"
