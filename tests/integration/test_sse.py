"""SSE `GET /runs/{run_id}/events` и `GET /runs/{run_id}/activity` (P3-09, ADR-0011, AC-08).

Поток проверяется через реальный HTTP-сервер (uvicorn в том же event loop): httpx.ASGITransport буферизует
ответ целиком и не подходит для бесконечного потока.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.main import API_PREFIX, create_app
from aicd_api.routers import events
from aicd_platform import outbox, sse
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, transaction
from aicd_platform.outbox_dispatcher import dispatch_batch
from tests.conftest import REDIS_URL, UserSession, login
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.contract import validate_response
from tests.support.events import parse_frame, validate_sse

pytestmark = [pytest.mark.db, pytest.mark.redis]


@pytest.fixture(autouse=True)
def fast_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        events,
        "CONFIG",
        events.StreamConfig(heartbeat_seconds=0.4, access_recheck_seconds=0.3, poll_seconds=0.2, replay_batch=3),
    )


@pytest.fixture(scope="module")
async def server(settings: Settings) -> AsyncIterator[str]:
    app: FastAPI = create_app(settings)
    if not any(getattr(r, "path", "") == f"{API_PREFIX}/runs/{{run_id}}/events" for r in app.routes):
        app.include_router(events.router, prefix=API_PREFIX)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="on", timeout_graceful_shutdown=1
    )
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.02)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}{API_PREFIX}"
    srv.should_exit = True
    await asyncio.wait_for(task, 10)


@pytest.fixture(scope="module")
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


def _client(base: str, session: UserSession) -> httpx.AsyncClient:
    token = session.client.cookies.get("aicd_session")
    assert token
    return httpx.AsyncClient(base_url=base, trust_env=False, timeout=10.0, headers={"Cookie": f"aicd_session={token}"})


class Env:
    def __init__(
        self,
        database: testdb.TestDatabase,
        app_engine: AsyncEngine,
        worker_engine: AsyncEngine,
        redis: Redis,
        owner: UserSession,
    ) -> None:
        self.app_engine = app_engine
        self.worker_engine = worker_engine
        self.redis = redis
        with orch.connect_admin(database) as conn:
            self.tenant = orch.seed_tenant(conn, owner_id=uuid.UUID(owner.user_id))
            self.run = orch.new_run(conn, self.tenant)
        self.version = 0

    async def emit(self, n: int, event_type: str = "run.state_changed", payload: dict[str, Any] | None = None) -> None:
        async with transaction(
            self.app_engine, DbContext(workspace_id=self.tenant.workspace_id, actor_type="system")
        ) as conn:
            for _ in range(n):
                self.version += 1
                await outbox.emit(
                    conn,
                    event_type=event_type,
                    aggregate_type="run",
                    aggregate_id=self.run,
                    workspace_id=self.tenant.workspace_id,
                    run_id=self.run,
                    payload=payload or {"from": "CREATED", "to": "DISCOVERY", "version": self.version},
                )

    async def dispatch(self) -> None:
        async def publish(event: dict[str, Any]) -> None:
            return None

        async def notify(run_id: uuid.UUID) -> None:
            await self.redis.publish(sse.run_channel(run_id), "1")

        while await dispatch_batch(self.worker_engine, publish, notify):
            pass


@pytest.fixture
async def env(
    database: testdb.TestDatabase, app_engine: AsyncEngine, worker_engine: AsyncEngine, redis: Redis, user: UserSession
) -> Env:
    return Env(database, app_engine, worker_engine, redis, user)


class Reader:
    """Кадры одного SSE-ответа (один итератор строк на весь поток)."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self._lines = response.aiter_lines()

    async def frames(self, count: int, wait: float = 5.0) -> tuple[list[dict[str, str]], bool]:
        """Кадры, пока не получено `count` событий с id (или поток не закрыт) → (кадры, закрыт)."""
        frames: list[dict[str, str]] = []
        lines: list[str] = []

        async def consume() -> bool:
            received = 0
            async for line in self._lines:
                if line:
                    lines.append(line)
                    continue
                if lines:
                    frames.append(parse_frame(lines))
                    lines.clear()
                    if "id" in frames[-1]:
                        received += 1
                        if received >= count:
                            return False
            return True

        closed = await asyncio.wait_for(consume(), wait)
        return frames, closed

    async def heartbeat(self, wait: float = 5.0) -> dict[str, str]:
        async def consume() -> dict[str, str]:
            lines: list[str] = []
            async for line in self._lines:
                if line:
                    lines.append(line)
                elif lines:
                    frame = parse_frame(lines)
                    lines.clear()
                    if frame.get("event") == "heartbeat":
                        return frame
            raise AssertionError("поток закрыт без heartbeat")

        return await asyncio.wait_for(consume(), wait)


def _events(frames: list[dict[str, str]]) -> list[dict[str, Any]]:
    result = []
    for frame in frames:
        if "data" not in frame:
            continue
        env = json.loads(frame["data"])
        validate_sse(env)
        if frame.get("event") != "heartbeat":
            assert frame["id"] == str(env["seq"]) and frame["event"] == env["type"]
            result.append(env)
        else:
            assert "id" not in frame and env["seq"] is None
    return result


async def test_replay_reconnect_and_live_exactly_once(server: str, env: Env, user: UserSession) -> None:
    await env.emit(5)
    await env.dispatch()
    url = f"/runs/{env.run}/events"
    async with _client(server, user) as client:
        async with client.stream("GET", url, params={"after_seq": 0}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-store" and r.headers["x-accel-buffering"] == "no"
            frames, _ = await Reader(r).frames(3)
            assert frames[0].get("retry") == "3000"
            first = _events(frames)
        last_id = first[-1]["seq"]
        # переподключение с Last-Event-ID: остаток истории, затем live
        async with client.stream("GET", url, headers={"Last-Event-ID": str(last_id)}) as r:
            assert r.status_code == 200
            reader = Reader(r)
            frames, _ = await reader.frames(2)
            await env.emit(2)
            await env.dispatch()
            more, _ = await reader.frames(2)
            second = _events(frames + more)
    seqs = [e["seq"] for e in first + second]
    assert seqs == list(range(1, 8))  # каждое событие ровно один раз и по порядку (AC-08)
    assert len({e["event_id"] for e in first + second}) == 7
    assert all(e["run_id"] == str(env.run) for e in first + second)


async def test_heartbeat_and_terminal_close(server: str, env: Env, user: UserSession) -> None:
    async with _client(server, user) as client, client.stream("GET", f"/runs/{env.run}/events") as r:
        reader = Reader(r)
        _events([await reader.heartbeat()])
        await env.emit(1, "run.completed", {"final_status": "CANCELLED"})
        await env.dispatch()
        frames, closed = await reader.frames(10)
    events_ = _events(frames)
    assert events_[-1]["type"] == "run.completed" and closed


async def test_non_member_gets_404_and_activity(
    server: str, env: Env, user: UserSession, app: FastAPI, app_engine: AsyncEngine
) -> None:
    await env.emit(5)
    await env.dispatch()
    stranger = await login(app, app_engine)
    try:
        async with _client(server, stranger) as client:
            r = await client.get(f"/runs/{env.run}/events")
            assert r.status_code == 404
            validate_response("streamRunEvents", r)
            r = await client.get(f"/runs/{env.run}/activity")
            assert r.status_code == 404
            r = await client.get(f"/runs/{uuid.uuid4()}/events")
            assert r.status_code == 404
    finally:
        await stranger.client.aclose()
    async with _client(server, user) as client:
        r = await client.get(f"/runs/{env.run}/activity", params={"limit": 2})
        validate_response("getRunActivity", r)
        page = r.json()
        assert [i["seq"] for i in page["items"]] == [1, 2] and page["next_cursor"]
        r = await client.get(f"/runs/{env.run}/activity", params={"limit": 2, "cursor": page["next_cursor"]})
        validate_response("getRunActivity", r)
        assert [i["seq"] for i in r.json()["items"]] == [3, 4]
        r = await client.get(f"/runs/{env.run}/activity", params={"after_seq": 4})
        assert [i["seq"] for i in r.json()["items"]] == [5]
        assert r.json()["next_cursor"] is None
        r = await client.get(f"/runs/{env.run}/events", headers={"Last-Event-ID": "abc"})
        assert r.status_code == 422


async def test_revoked_member_stream_closes(
    server: str, env: Env, app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase
) -> None:
    viewer = await login(app, app_engine)
    try:
        with orch.connect_admin(database) as conn:
            conn.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'viewer')",
                (env.tenant.workspace_id, uuid.UUID(viewer.user_id)),
            )
        await env.emit(1)
        await env.dispatch()
        async with _client(server, viewer) as client, client.stream("GET", f"/runs/{env.run}/events") as r:
            assert r.status_code == 200
            reader = Reader(r)
            frames, _ = await reader.frames(1)
            assert [e["seq"] for e in _events(frames)] == [1]
            with orch.connect_admin(database) as conn:
                conn.execute(
                    "DELETE FROM workspace_members WHERE workspace_id = %s AND user_id = %s",
                    (env.tenant.workspace_id, uuid.UUID(viewer.user_id)),
                )
            _, closed = await reader.frames(100, wait=5)
            assert closed
            r2 = await client.get(f"/runs/{env.run}/events")
            assert r2.status_code == 404
    finally:
        await viewer.client.aclose()


async def test_replay_window_exceeded_and_stream_limit(
    server: str, env: Env, user: UserSession, database: testdb.TestDatabase, redis: Redis
) -> None:
    await env.emit(4)
    await env.dispatch()
    with orch.connect_admin(database) as conn:
        conn.execute("SET session_replication_role = replica")  # retention (aicd_maintenance) в обход триггера
        conn.execute("DELETE FROM run_event_log WHERE run_id = %s AND seq <= 2", (env.run,))
    async with _client(server, user) as client:
        r = await client.get(f"/runs/{env.run}/events", params={"after_seq": 0})
        assert r.status_code == 410 and r.json()["code"] == "replay_window_exceeded"
        validate_response("streamRunEvents", r)
        async with client.stream("GET", f"/runs/{env.run}/events", headers={"Last-Event-ID": "2"}) as s:
            frames, _ = await Reader(s).frames(2)
            assert [e["seq"] for e in _events(frames)] == [3, 4]
        key = f"aicd:sse:streams:{user.user_id}"
        for _ in range(100):  # закрытый клиентом поток освобождает слот
            if not await redis.exists(key):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("слот SSE-потока не освобождён после отключения клиента")
        await redis.set(key, 5)
        try:
            r = await asyncio.wait_for(client.get(f"/runs/{env.run}/events", params={"after_seq": 4}), 5)
            assert r.status_code == 429
            validate_response("streamRunEvents", r)
        finally:
            await redis.delete(key)
