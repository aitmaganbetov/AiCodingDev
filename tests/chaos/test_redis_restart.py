"""P3-16 (chaos v1): рестарт Redis (брокер Dramatiq + SSE pub/sub) под работающим outbox-диспетчером.

Отдельный `redis-server` на свободном порту (общий Redis тестов не трогаем) убивается SIGKILL без
persistence — всё, что было в очередях, теряется. Гарантия transactional outbox (ADR-0002): событие,
записанное, пока Redis лежит, не теряется и не дублируется — диспетчер получает ошибку публикации,
оставляет событие неопубликованным и доставляет его после возврата Redis; проекция в run_event_log — ровно одна.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform import outbox
from aicd_platform.db import DbContext, transaction
from aicd_worker.broker import ORCHESTRATOR_QUEUE, build_broker, register_actors
from aicd_worker.main import run_outbox
from aicd_worker.settings import WorkerSettings
from tests.support import db as testdb
from tests.support import orchestration as orch

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server не установлен"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class RedisProcess:
    def __init__(self, port: int) -> None:
        self.port = port
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["redis-server", "--port", str(self.port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        raise AssertionError("redis-server не стартовал")

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


@pytest.fixture
def redis_proc() -> Iterator[RedisProcess]:
    proc = RedisProcess(_free_port())
    proc.start()
    yield proc
    proc.kill()


async def _emit(
    engine: AsyncEngine, ws: uuid.UUID, run: uuid.UUID, event_type: str, payload: dict[str, object]
) -> uuid.UUID:
    async with transaction(engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        return await outbox.emit(
            conn,
            event_type=event_type,
            aggregate_type="run",
            aggregate_id=run,
            workspace_id=ws,
            run_id=run,
            payload=payload,
        )


async def _queued(client: Redis, needle: str) -> bool:
    messages = await client.hvals(f"aicd-dramatiq:{ORCHESTRATOR_QUEUE}.msgs")
    return any(needle in (m.decode() if isinstance(m, bytes) else m) for m in messages)


async def test_event_written_during_redis_outage_is_delivered_once_after_restart(
    database: testdb.TestDatabase, app_engine: AsyncEngine, worker_engine: AsyncEngine, redis_proc: RedisProcess
) -> None:
    url = f"redis://127.0.0.1:{redis_proc.port}/0"
    with orch.connect_admin(database) as admin:
        tenant = orch.seed_tenant(admin)
        run_before, run_during = orch.new_run(admin, tenant), orch.new_run(admin, tenant)
    actors = register_actors(build_broker(url))
    settings = WorkerSettings(environment="test", redis_url=url, outbox_poll_seconds=0.05)
    stop = asyncio.Event()
    probe = Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)
    loop_task = asyncio.create_task(run_outbox(settings, worker_engine, stop, actors=actors))

    async def wait_queued(needle: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with contextlib.suppress(RedisError, OSError):  # Redis ещё поднимается
                if await _queued(probe, needle):
                    return
            await asyncio.sleep(0.1)
        pytest.fail(f"событие run {needle} не доставлено за {seconds} с")

    try:
        await _emit(app_engine, tenant.workspace_id, run_before, "run.created", {"mode": "DEVELOP"})
        await wait_queued(str(run_before), 15)

        redis_proc.kill()  # без persistence: очередь потеряна целиком
        # Тип и для очереди оркестратора, и для SSE-проекции (run_event_log).
        during = await _emit(
            app_engine, tenant.workspace_id, run_during, "run.state_changed", {"from": "CREATED", "to": "DISCOVERY"}
        )
        await asyncio.sleep(1.5)  # диспетчер несколько раз упирается в недоступный Redis
        with orch.connect_admin(database) as admin:
            row = admin.execute(
                "SELECT published_at IS NULL, publish_attempts > 0 FROM outbox_events WHERE id = %s", (during,)
            ).fetchone()
        assert row == (True, True), "без Redis событие не помечено опубликованным, ошибка учтена"
        assert not loop_task.done(), "диспетчер переживает падение Redis"

        redis_proc.start()
        await probe.aclose()
        probe = Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)
        await wait_queued(str(run_during), 60)
    finally:
        stop.set()
        await asyncio.wait_for(loop_task, 10)
        await probe.aclose()

    with orch.connect_admin(database) as admin:
        assert admin.execute(
            "SELECT published_at IS NOT NULL FROM outbox_events WHERE id = %s", (during,)
        ).fetchone() == (True,)
        assert admin.execute("SELECT count(*) FROM run_event_log WHERE event_id = %s", (during,)).fetchone() == (1,), (
            "проекция для SSE — ровно одна, несмотря на повторы публикации"
        )
