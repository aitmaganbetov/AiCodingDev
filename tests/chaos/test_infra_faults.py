"""P9-03 (NFR-REL-01/02): сетевые сбои инфраструктуры через `FaultProxy` и рассинхрон часов.

Общие контейнеры PostgreSQL/S3 не перезапускаются: клиенты приложения подключаются через локальный
TCP-прокси, который рвёт соединения (RST) или «гасит» сервис на время (рестарт/failover с точки зрения клиента).

* PostgreSQL: обрыв соединения посреди транзакции команды — ни строки команды, ни события outbox (атомарность);
  пул восстанавливается сам (`pool_pre_ping`); outbox-диспетчер переживает серию обрывов и простой БД и
  доставляет все события ровно с одной проекцией.
* S3: недоступность при записи артефакта — явная ошибка, строка `artifacts` не создаётся, частичного объекта
  нет; обрыв посреди загрузки — объект не появляется; после восстановления запись/чтение работают,
  ранее записанные объекты целы.
* Часы: аренды и fencing решаются только по `now()` PostgreSQL — сдвиг локальных часов воркера на ±2 ч не
  меняет захват/heartbeat/истечение; воркер, считающий аренду живой по своим часам, получает отказ.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform import clock, leases, outbox
from aicd_platform.artifacts import S3ArtifactStore
from aicd_platform.db import DbContext, create_engine, transaction
from aicd_platform.ids import uuid7
from aicd_worker.broker import build_broker, register_actors
from aicd_worker.main import run_outbox
from aicd_worker.settings import WorkerSettings
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.faultproxy import FaultProxy
from tests.support.processes import RedisProcess

pytestmark = pytest.mark.db


@pytest.fixture
def pg_proxy(database: testdb.TestDatabase) -> Iterator[FaultProxy]:
    parts = urlsplit(database.url_for("aicd_worker"))
    proxy = FaultProxy(parts.hostname or "127.0.0.1", parts.port or 5432)
    yield proxy
    proxy.close()


@pytest.fixture
async def proxied_engine(database: testdb.TestDatabase, pg_proxy: FaultProxy) -> AsyncIterator[AsyncEngine]:
    url = re.sub(r"@[^/]+/", f"@127.0.0.1:{pg_proxy.port}/", database.url_for("aicd_worker"), count=1)
    engine = create_engine(url, pool_size=4)
    yield engine
    await engine.dispose()


async def _emit(engine: AsyncEngine, ws: uuid.UUID, run: uuid.UUID, n: int) -> uuid.UUID:
    async with transaction(engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        return await outbox.emit(conn, event_type="run.state_changed", aggregate_type="run", aggregate_id=run,
                                 workspace_id=ws, run_id=run, payload={"from": "CREATED", "to": "DISCOVERY", "n": n})  # fmt: skip


async def test_pg_connection_cut_mid_transaction_commits_nothing_and_pool_recovers(
    database: testdb.TestDatabase, proxied_engine: AsyncEngine, pg_proxy: FaultProxy
) -> None:
    with orch.connect_admin(database) as admin:
        tenant = orch.seed_tenant(admin)
        run = orch.new_run(admin, tenant)
    ws = tenant.workspace_id
    marker = f"cut-{secrets.token_hex(4)}"
    with pytest.raises(DBAPIError):
        async with transaction(proxied_engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            await outbox.emit(conn, event_type="run.state_changed", aggregate_type="run", aggregate_id=run,
                              workspace_id=ws, run_id=run, payload={"from": "CREATED", "to": "DISCOVERY", "m": marker})  # fmt: skip
            assert pg_proxy.cut() >= 2  # клиент↔прокси и прокси↔PostgreSQL
            await conn.execute(text("SELECT 1"))
    with orch.connect_admin(database) as admin:
        assert admin.execute("SELECT count(*) FROM outbox_events WHERE payload->>'m' = %s", (marker,)).fetchone() == (
            0,
        ), "транзакция с обрывом соединения не оставила следов"
    # Пул отбрасывает мёртвые соединения (pre-ping): следующая транзакция проходит без перезапуска процесса.
    event = await _emit(proxied_engine, ws, run, 1)
    with orch.connect_admin(database) as admin:
        assert admin.execute("SELECT count(*) FROM outbox_events WHERE id = %s", (event,)).fetchone() == (1,)


async def test_outbox_dispatcher_survives_pg_cuts_and_outage(
    database: testdb.TestDatabase, worker_engine: AsyncEngine, proxied_engine: AsyncEngine, pg_proxy: FaultProxy
) -> None:
    redis = RedisProcess()
    redis.start()
    with orch.connect_admin(database) as admin:
        tenant = orch.seed_tenant(admin)
        runs = [orch.new_run(admin, tenant) for _ in range(3)]
    settings = WorkerSettings(environment="test", redis_url=redis.url, outbox_poll_seconds=0.05, outbox_batch_size=5)
    actors = register_actors(build_broker(redis.url))
    stop = asyncio.Event()
    loop_task = asyncio.create_task(run_outbox(settings, proxied_engine, stop, actors=actors))
    ids: list[uuid.UUID] = []
    try:
        for i in range(150):
            ids.append(await _emit(worker_engine, tenant.workspace_id, runs[i % 3], i))
            if i % 25 == 10:
                pg_proxy.cut()  # обрыв соединений диспетчера посреди пачки
            if i == 90:
                with pg_proxy.outage():  # «рестарт» PostgreSQL для диспетчера: 2 с недоступности
                    await asyncio.sleep(2.0)
                    assert not loop_task.done(), "диспетчер переживает недоступность БД"
        deadline = time.monotonic() + 60
        while True:
            with orch.connect_admin(database) as admin:
                row = admin.execute(
                    "SELECT count(*) FROM outbox_events WHERE id = ANY(%s) AND published_at IS NOT NULL", (ids,)
                ).fetchone()
            if row == (len(ids),):
                break
            assert time.monotonic() < deadline, f"опубликовано {row} из {len(ids)}"
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.wait_for(loop_task, 15)
        redis.kill()
    assert pg_proxy.accepted >= 2, "диспетчер переподключался после обрывов"
    with orch.connect_admin(database) as admin:
        rows = admin.execute("SELECT event_id FROM run_event_log WHERE event_id = ANY(%s)", (ids,)).fetchall()
    assert len(rows) == len(ids) and {r[0] for r in rows} == set(ids), "проекция ровно одна на событие"


# --------------------------------------------------------------------------------------------- S3
S3_ENDPOINT = os.environ.get("AICD_TEST_S3_ENDPOINT")


@pytest.fixture
def s3_proxy() -> Iterator[FaultProxy]:
    if not S3_ENDPOINT:
        pytest.skip("AICD_TEST_S3_ENDPOINT не задан")
    parts = urlsplit(S3_ENDPOINT)
    proxy = FaultProxy(parts.hostname or "127.0.0.1", parts.port or 80)
    yield proxy
    proxy.close()


def _s3(endpoint: str, bucket: str) -> S3ArtifactStore:
    store = S3ArtifactStore(endpoint_url=endpoint, bucket=bucket, access_key=os.environ["AICD_TEST_S3_ACCESS_KEY"],
                            secret_key=os.environ["AICD_TEST_S3_SECRET_KEY"])  # fmt: skip
    # Без повторов botocore: сбой должен проявиться сразу и явно (иначе повтор после восстановления маскирует его).
    import boto3
    from botocore.config import Config

    store._client = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=os.environ["AICD_TEST_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["AICD_TEST_S3_SECRET_KEY"], region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=2, read_timeout=10),
    )  # fmt: skip
    return store


@pytest.mark.s3
async def test_s3_outage_fails_explicitly_without_partial_state(
    s3_proxy: FaultProxy, worker_engine: AsyncEngine, database: testdb.TestDatabase
) -> None:
    assert S3_ENDPOINT is not None
    bucket = f"aicd-chaos-{secrets.token_hex(3)}"
    direct = _s3(S3_ENDPOINT, bucket)
    direct.ensure_bucket()
    store = _s3(f"http://127.0.0.1:{s3_proxy.port}", bucket)
    ws = uuid7()
    before = store.put(ws, uuid7(), io.BytesIO(b"written before outage"))

    # 1. Недоступность: явная ошибка записи и проверки готовности, без частичного объекта.
    artifact_id = uuid7()
    with s3_proxy.outage():
        with pytest.raises(Exception, match=r"(?i)connect|endpoint|closed") as err:
            store.put(ws, artifact_id, io.BytesIO(b"x" * 1024))
        assert "Connect" in type(err.value).__name__ or "Connection" in str(err.value) or "Endpoint" in str(err.value)
        with pytest.raises(Exception):  # noqa: B017, PT011 — botocore: ошибка соединения
            store.healthy()
    prefix = f"{ws}/"
    listed = direct._client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    assert [o["Key"] for o in listed] == [before.storage_ref.removeprefix(f"s3://{bucket}/")]

    # 2. Обрыв посреди загрузки крупного объекта: объект не появляется (put атомарен, sha256 проверяет сервер).
    s3_proxy.latency = 0.01
    payload = secrets.token_bytes(8 * 1024 * 1024)
    upload = asyncio.create_task(asyncio.to_thread(store.put, ws, uuid7(), io.BytesIO(payload)))
    while not upload.done():  # рвём соединения, пока идёт загрузка (первый обрыв может попасть в HEAD)
        await asyncio.sleep(0.15)
        s3_proxy.cut()
    with pytest.raises(Exception) as cut_err:  # noqa: PT011 — botocore: соединение разорвано
        await upload
    assert "Connection" in type(cut_err.value).__name__ or "Connection" in str(cut_err.value)
    s3_proxy.latency = 0.0
    listed = direct._client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    assert len(listed) == 1, "частичный объект не создан"

    # 3. Восстановление: запись и чтение работают, ранее записанное цело (sha256 сверяется при чтении).
    after = store.put(ws, uuid7(), io.BytesIO(b"after outage"))
    assert b"".join(store.open(before.storage_ref, expected_sha256=before.sha256)) == b"written before outage"
    assert b"".join(store.open(after.storage_ref, expected_sha256=after.sha256)) == b"after outage"
    assert store.healthy()


# ------------------------------------------------------------------------------------------- часы
async def test_lease_decisions_ignore_worker_clock_skew(
    database: testdb.TestDatabase, worker_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with orch.connect_admin(database) as admin:
        tenant = orch.seed_tenant(admin)
    ws = tenant.workspace_id
    resource = uuid7()
    real_now = clock.utcnow
    for skew in (timedelta(hours=2), -timedelta(hours=2)):
        monkeypatch.setattr(clock, "utcnow", lambda s=skew: real_now() + s)
        monkeypatch.setattr(time, "time", lambda s=skew, t=time.time: t() + s.total_seconds())
        async with transaction(worker_engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            lease = await leases.acquire(conn, resource_type="task", resource_id=resource, worker_id=f"skew:{int(skew.total_seconds())}",
                                         workspace_id=ws, ttl_seconds=30)  # fmt: skip
            assert lease is not None, f"захват при сдвиге часов {skew}"
            db_now = (await conn.execute(text("SELECT now()"))).scalar_one()
            assert abs((lease.lease_until - db_now).total_seconds() - 30) < 5, "срок — по часам БД, не воркера"
            assert await leases.heartbeat(conn, lease, 30)
            assert await leases.is_current(conn, "task", resource, lease.fencing_token)
            # Второй воркер (с теми же «сбитыми» часами) не перехватывает живую аренду.
            assert await leases.acquire(conn, resource_type="task", resource_id=resource, worker_id="other",
                                        workspace_id=ws, ttl_seconds=30) is None  # fmt: skip
            await leases.release(conn, lease)
    monkeypatch.undo()

    # Аренда истекла по часам БД, хотя локально воркер «видит» lease_until в будущем (часы отстают):
    # heartbeat и запись результата отклоняются, новая аренда получает больший fencing token.
    async with transaction(worker_engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        lease = await leases.acquire(conn, resource_type="task", resource_id=resource, worker_id="slow-clock",
                                     workspace_id=ws, ttl_seconds=30)  # fmt: skip
        assert lease is not None
    with orch.connect_admin(database) as admin:
        admin.execute(
            "UPDATE worker_leases SET acquired_at = now() - interval '2 minutes', lease_until = now() - interval '1 second'"
            " WHERE resource_id = %s",
            (resource,),
        )
    believed = leases.Lease(lease.resource_type, resource, lease.worker_id, lease.fencing_token,
                            datetime.now(UTC) + timedelta(hours=1))  # fmt: skip
    async with transaction(worker_engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        assert not await leases.heartbeat(conn, believed, 30)
        assert not await leases.is_current(conn, "task", resource, lease.fencing_token)
        newer = await leases.acquire(conn, resource_type="task", resource_id=resource, worker_id="next",
                                     workspace_id=ws, ttl_seconds=30)  # fmt: skip
        assert newer is not None and newer.fencing_token > lease.fencing_token
        assert not await leases.is_current(conn, "task", resource, lease.fencing_token)
