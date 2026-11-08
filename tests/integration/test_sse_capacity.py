"""P9-04: ёмкость SSE процесса API — общий pub/sub-приёмник (`aicd_api.sse_hub`) и 503 вместо 500.

Базовый замер показал потолок ~100 потоков на процесс: каждый поток держал своё pub/sub-соединение, пул
`redis.asyncio` исчерпывался и 101-й поток (а заодно учёт слотов и rate limiter) получал HTTP 500. Теперь:

* потоков больше прежнего потолка (130) открываются и получают live-события через одно pub/sub-соединение;
* поток сверх лимита процесса — `503 service_unavailable` + `Retry-After`, ни одного 5xx кроме него;
* сбой Redis при учёте слотов — тоже 503 (не 500);
* приёмник переподключается после рестарта Redis и заново подписывается на каналы.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import MaxConnectionsError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.main import API_PREFIX, create_app
from aicd_api.routers import events
from aicd_api.sse_hub import SignalHub
from aicd_platform import outbox, sse
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.outbox_dispatcher import dispatch_batch
from tests.conftest import REDIS_URL, login
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.processes import RedisProcess

pytestmark = [pytest.mark.db, pytest.mark.redis]

STREAMS = 130  # больше прежнего потолка ~100 (пул redis.asyncio)


@pytest.fixture
def capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        events,
        "CONFIG",
        events.StreamConfig(
            heartbeat_seconds=5, access_recheck_seconds=30, poll_seconds=30, max_streams_per_process=STREAMS
        ),
    )


@pytest.fixture
async def server(settings: Settings) -> AsyncIterator[tuple[str, FastAPI]]:
    app = create_app(settings)
    srv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on", timeout_graceful_shutdown=1)
    )
    task = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.02)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}{API_PREFIX}", app
    srv.should_exit = True
    await asyncio.wait_for(task, 15)


async def test_more_streams_than_old_pool_then_503_beyond_process_limit(
    capacity: None,
    server: tuple[str, FastAPI],
    app: FastAPI,
    app_engine: AsyncEngine,
    worker_engine: AsyncEngine,
    database: testdb.TestDatabase,
) -> None:
    base, served = server
    owner = await login(app, app_engine)
    with orch.connect_admin(database) as admin:
        tenant = orch.seed_tenant(admin, owner_id=uuid.UUID(owner.user_id))
        run = orch.new_run(admin, tenant)
    users = [owner]
    for _ in range(STREAMS // events.CONFIG.max_streams_per_user):  # 5 потоков на пользователя
        member = await login(app, app_engine)
        with orch.connect_admin(database) as admin:
            admin.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'viewer')",
                (tenant.workspace_id, uuid.UUID(member.user_id)),
            )
        users.append(member)
    clients = [
        httpx.AsyncClient(
            base_url=base, timeout=30, headers={"Cookie": f"aicd_session={u.client.cookies.get('aicd_session')}"}
        )
        for u in users
    ]
    streams: list[tuple[httpx.Response, AsyncIterator[str]]] = []
    statuses: list[int] = []
    async with contextlib.AsyncExitStack() as stack:
        for i in range(STREAMS):
            client = clients[i // events.CONFIG.max_streams_per_user]
            resp = await stack.enter_async_context(client.stream("GET", f"/runs/{run}/events"))
            statuses.append(resp.status_code)
            streams.append((resp, resp.aiter_lines()))
        assert statuses == [200] * STREAMS, "все потоки до лимита процесса открыты (прежний потолок ~100 снят)"
        hub = served.state.aicd_sse_hub
        assert isinstance(hub, SignalHub) and hub.subscriptions == STREAMS and hub.connected

        extra = await clients[-1].get(f"/runs/{run}/events")
        assert extra.status_code == 503, extra.text
        assert extra.json()["code"] == "service_unavailable" and extra.headers["retry-after"] == "5"

        # Live-событие доходит до всех потоков: один pub/sub-сигнал, раздача приёмником.
        async with transaction(app_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
            event_id = await outbox.emit(conn, event_type="run.state_changed", aggregate_type="run", aggregate_id=run,
                                         workspace_id=tenant.workspace_id, run_id=run,
                                         payload={"from": "CREATED", "to": "DISCOVERY"})  # fmt: skip
        redis = Redis.from_url(REDIS_URL)

        async def publish(_: dict[str, object]) -> None:
            return None

        async def notify(run_id: uuid.UUID) -> None:
            await redis.publish(sse.run_channel(run_id), "1")

        try:
            await dispatch_batch(worker_engine, publish, notify)
        finally:
            await redis.aclose()

        async def receives(lines: AsyncIterator[str]) -> bool:
            async for line in lines:
                if line.startswith("data:") and str(event_id) in line:
                    return True
            return False

        # poll_seconds = 30: в пределах 10 с событие приходит только по сигналу приёмника.
        got = await asyncio.wait_for(asyncio.gather(*(receives(lines) for _, lines in streams)), 10)
        assert all(got)
    for client in clients:
        await client.aclose()
    for user in users:
        await user.client.aclose()


async def test_redis_failure_on_slot_accounting_is_503_not_500() -> None:
    class Exhausted:
        async def incr(self, key: str) -> int:
            raise MaxConnectionsError("Too many connections")

    with pytest.raises(ProblemError) as err:
        await events._acquire_slot(Exhausted(), uuid.uuid4())  # type: ignore[arg-type]
    assert err.value.code == "service_unavailable" and err.value.headers == {"Retry-After": "5"}


async def test_hub_fans_out_and_resubscribes_after_redis_restart() -> None:
    proc = RedisProcess()
    proc.start()
    redis = Redis.from_url(proc.url, socket_timeout=1.0, socket_connect_timeout=1.0)
    hub = SignalHub(redis, reconnect_seconds=0.1, read_timeout=0.2)
    try:
        a = await hub.subscribe(("chan:a", "chan:b"))
        b = await hub.subscribe(("chan:a",))
        for _ in range(50):
            if hub.connected:
                break
            await asyncio.sleep(0.05)
        publisher = Redis.from_url(proc.url)
        await publisher.publish("chan:a", "1")
        assert await a.wait(3) == "chan:a" and await b.wait(3) == "chan:a"
        await b.close()
        await publisher.publish("chan:b", "1")
        assert await a.wait(3) == "chan:b"
        await publisher.aclose()

        proc.kill()  # без persistence: подписки на сервере потеряны
        await asyncio.sleep(0.5)
        assert await a.wait(0.3) is None, "без Redis — тишина (поток опрашивает БД)"
        proc.start()
        publisher = Redis.from_url(proc.url)
        delivered = None
        for _ in range(50):  # приёмник переподключается и подписывается заново
            await publisher.publish("chan:a", "1")
            delivered = await a.wait(0.2)
            if delivered:
                break
        assert delivered == "chan:a"
        await publisher.aclose()
        await a.close()
        assert hub.subscriptions == 0
    finally:
        await hub.close()
        await redis.aclose()
        proc.kill()
