"""Heartbeat процессов воркеров (`worker_hosts`): запись под ролью aicd_worker, OFFLINE при остановке, метрика
`aicd_worker_heartbeat_age_seconds`, классификация живости в `GET /system/health` (свежий → ok, устаревший → degraded
→ down, ожидаемый вид без процессов → down)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.routers import system
from aicd_platform import metrics, worker_hosts
from tests.conftest import UserSession
from tests.support import db as testdb
from tests.support.contract import validate_response
from tests.support.orchestration import connect_admin

pytestmark = pytest.mark.db


@pytest.fixture
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


def _hostname() -> str:
    return f"hb-{uuid.uuid4().hex[:10]}"


def _row(admin: psycopg.Connection, host_id: uuid.UUID) -> tuple[Any, ...]:
    row = admin.execute(
        "SELECT status, last_heartbeat_at, labels->>'heartbeat_interval_seconds', labels ? 'version'"
        " FROM worker_hosts WHERE id = %s",
        (host_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _age(admin: psycopg.Connection, host_id: uuid.UUID, seconds: float) -> None:
    admin.execute(
        "UPDATE worker_hosts SET last_heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
        (seconds, host_id),
    )


async def test_heartbeat_advances_under_worker_role_and_marks_offline(
    worker_engine: AsyncEngine, admin: psycopg.Connection
) -> None:
    hb = worker_hosts.Heartbeat(worker_engine, "verify", 0.2, hostname=_hostname())
    stop = asyncio.Event()
    task = asyncio.create_task(hb.run(stop))
    try:
        for _ in range(100):
            if hb.host_id is not None:
                break
            await asyncio.sleep(0.05)
        assert hb.host_id is not None
        status, first, interval, has_version = _row(admin, hb.host_id)
        assert status == "ONLINE" and float(interval) == 0.2 and has_version
        await asyncio.sleep(0.7)
        _, second, _, _ = _row(admin, hb.host_id)
        assert second > first, "heartbeat обновляет last_heartbeat_at"
        # Строку удалили (очистка): следующий такт регистрирует процесс заново.
        old_id = hb.host_id
        admin.execute("DELETE FROM worker_hosts WHERE id = %s", (old_id,))
        for _ in range(100):
            if hb.host_id != old_id:
                break
            await asyncio.sleep(0.05)
        assert hb.host_id != old_id
    finally:
        stop.set()
        await task
    try:
        assert _row(admin, hb.host_id)[0] == "OFFLINE", "штатная остановка — OFFLINE"
    finally:
        admin.execute("DELETE FROM worker_hosts WHERE id = %s", (hb.host_id,))


async def test_heartbeat_gauges_and_liveness_from_db(worker_engine: AsyncEngine, admin: psycopg.Connection) -> None:
    hb = worker_hosts.Heartbeat(worker_engine, "api", 10.0, hostname=_hostname())
    await hb.tick()
    assert hb.host_id is not None
    try:
        await worker_hosts.sample_heartbeat_gauges(worker_engine)
        age = metrics.REGISTRY.get_sample_value("aicd_worker_heartbeat_age_seconds", {"kind": "api"})
        interval = metrics.REGISTRY.get_sample_value("aicd_worker_heartbeat_interval_seconds", {"kind": "api"})
        assert age is not None and age < 5 and interval == 10.0
        _age(admin, hb.host_id, 200)
        await worker_hosts.sample_heartbeat_gauges(worker_engine)
        stale = metrics.REGISTRY.get_sample_value("aicd_worker_heartbeat_age_seconds", {"kind": "api"})
        assert stale is not None and stale >= 200
        admin.execute("UPDATE worker_hosts SET status = 'OFFLINE' WHERE id = %s", (hb.host_id,))
        await worker_hosts.sample_heartbeat_gauges(worker_engine)
        assert metrics.REGISTRY.get_sample_value("aicd_worker_heartbeat_age_seconds", {"kind": "api"}) is None
    finally:
        admin.execute("DELETE FROM worker_hosts WHERE id = %s", (hb.host_id,))


async def test_system_health_classifies_worker_liveness(
    app: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, admin: psycopg.Connection, user: UserSession
) -> None:
    ws = (await user.create_workspace())["id"]
    hb = worker_hosts.Heartbeat(worker_engine, "verify", 1.0, hostname=_hostname())
    await hb.tick()
    assert hb.host_id is not None

    async def kind_state() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        app.state.aicd_system_health = None  # без кэша
        r = await user.client.get("/api/v1/system/health", headers={"X-Workspace-Id": ws})
        validate_response("getSystemHealth", r)
        assert r.status_code == 200, r.text
        body = r.json()
        kind = next(k for k in body["worker_kinds"] if k["kind"] == "verify")
        host = next(h for h in body["worker_hosts"] if h["id"] == str(hb.host_id))
        workers = next(c for c in body["components"] if c["name"] == "workers")
        return kind, host, workers

    try:
        kind, host, _ = await kind_state()
        assert kind["status"] == "ok" and host["liveness"] == "ok" and host["heartbeat_interval_seconds"] == 1.0
        assert kind["expected"] is False and kind["active_hosts"] == 1
        _age(admin, hb.host_id, 5)  # 5 интервалов: 3 < 5 ≤ 10
        kind, host, workers = await kind_state()
        assert kind["status"] == "degraded" and host["liveness"] == "degraded"
        assert workers["status"] in ("degraded", "down") and "verify: degraded" in (workers["detail"] or "")
        _age(admin, hb.host_id, 30)  # > 10 интервалов
        kind, host, workers = await kind_state()
        assert kind["status"] == "down" and host["liveness"] == "down" and workers["status"] == "down"
        admin.execute("UPDATE worker_hosts SET status = 'OFFLINE' WHERE id = %s", (hb.host_id,))
        kind, host, _ = await kind_state()
        assert host["liveness"] == "unknown" and kind["active_hosts"] == 0
        # Вид verify в тестовой БД пишут только эти тесты: процессов не осталось, вид не ожидается — unknown.
        assert kind["status"] == "unknown", "остановленный штатно и не ожидаемый вид — не отказ"
    finally:
        admin.execute("DELETE FROM worker_hosts WHERE id = %s", (hb.host_id,))
        app.state.aicd_system_health = None


async def test_expected_kind_without_processes_is_down(app_engine: AsyncEngine) -> None:
    state: Any = SimpleNamespace(engine=app_engine, settings=SimpleNamespace(expected_worker_kinds=["api"]))
    component, _, kinds = await system.check_workers(state)
    assert kinds is not None
    api = next(k for k in kinds if k["kind"] == "api")
    # Вид api в тестовой БД регистрирует только test_heartbeat_gauges_and_liveness_from_db (и удаляет строку).
    assert api["active_hosts"] == 0 and api["status"] == "down" and api["expected"] is True
    assert component.status == "down"
