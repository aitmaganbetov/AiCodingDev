"""Heartbeat процессов воркеров (`worker_hosts`) и оценка их живости.

Каждый процесс `aicd-worker <kind>` регистрирует строку `(hostname, kind)` и каждые `interval` секунд обновляет
`last_heartbeat_at` (роль `aicd_worker`: `SELECT, INSERT, UPDATE` на `worker_hosts`, миграция 0002 — отдельные права
не нужны). Интервал и версия пакета пишутся в `labels` (`heartbeat_interval_seconds`, `version`), поэтому читатель
(`GET /system/health`, метрика `aicd_worker_heartbeat_age_seconds`) оценивает живость по интервалу самого процесса,
а не по своей настройке. При штатной остановке строка получает `status = 'OFFLINE'` (остановленный процесс не
считается упавшим). Возраст heartbeat вычисляется по часам БД (`now()`), без расхождения часов хостов.

Живость процесса: `ok` — возраст ≤ 3 интервалов, `degraded` — ≤ 10, иначе `down`; интервал неизвестен (строка без
`labels.heartbeat_interval_seconds`, например записанная до появления heartbeat) — `unknown`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_platform import metrics
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7

log = logging.getLogger("aicd.worker.heartbeat")

Liveness = Literal["ok", "degraded", "down", "unknown"]
OK_INTERVALS: Final = 3
DEGRADED_INTERVALS: Final = 10


def package_version() -> str | None:
    try:
        return metadata.version("aicodingdev")
    except metadata.PackageNotFoundError:
        return None


def liveness(age_seconds: float | None, interval_seconds: float | None) -> Liveness:
    if age_seconds is None or interval_seconds is None or interval_seconds <= 0:
        return "unknown"
    if age_seconds <= OK_INTERVALS * interval_seconds:
        return "ok"
    if age_seconds <= DEGRADED_INTERVALS * interval_seconds:
        return "degraded"
    return "down"


def _interval(labels: Any) -> float | None:
    if isinstance(labels, str):
        labels = json.loads(labels)
    value = labels.get("heartbeat_interval_seconds") if isinstance(labels, Mapping) else None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value > 0 else None


def _version(labels: Any) -> str | None:
    if isinstance(labels, str):
        labels = json.loads(labels)
    value = labels.get("version") if isinstance(labels, Mapping) else None
    return value if isinstance(value, str) and len(value) <= 64 else None


async def register(
    conn: AsyncConnection, kind: str, *, interval_seconds: float, hostname: str | None = None
) -> uuid.UUID:
    """Upsert строки `(hostname, kind)`: ONLINE, heartbeat сейчас, интервал и версия — в `labels`."""
    labels = {"heartbeat_interval_seconds": interval_seconds, "version": package_version()}
    row = (
        await conn.execute(
            text(
                "INSERT INTO worker_hosts (id, hostname, kind, labels) VALUES (:id, :h, :k, CAST(:l AS jsonb))"
                " ON CONFLICT ON CONSTRAINT uq_worker_hosts_hostname_kind DO UPDATE SET last_heartbeat_at = now(),"
                " status = 'ONLINE', labels = worker_hosts.labels || EXCLUDED.labels RETURNING id"
            ),
            {"id": uuid7(), "h": (hostname or socket.gethostname())[:253], "k": kind, "l": json.dumps(labels)},
        )
    ).scalar_one()
    return uuid.UUID(str(row))


async def beat(conn: AsyncConnection, host_id: uuid.UUID) -> bool:
    """Heartbeat; `False` — строки нет (удалена), процесс должен зарегистрироваться заново."""
    row = (
        await conn.execute(
            text("UPDATE worker_hosts SET last_heartbeat_at = now(), status = 'ONLINE' WHERE id = :id RETURNING id"),
            {"id": host_id},
        )
    ).first()
    return row is not None


async def mark_offline(conn: AsyncConnection, host_id: uuid.UUID) -> None:
    await conn.execute(text("UPDATE worker_hosts SET status = 'OFFLINE' WHERE id = :id"), {"id": host_id})


@dataclass
class Heartbeat:
    """Цикл heartbeat процесса воркера (фоновая задача рядом с основным циклом)."""

    engine: AsyncEngine
    kind: str
    interval_seconds: float
    hostname: str | None = None
    host_id: uuid.UUID | None = None

    async def _register(self) -> uuid.UUID:
        async with transaction(self.engine, DbContext(actor_type="system")) as conn:
            self.host_id = await register(
                conn, self.kind, interval_seconds=self.interval_seconds, hostname=self.hostname
            )
        return self.host_id

    async def tick(self) -> None:
        if self.host_id is None:
            await self._register()
            return
        async with transaction(self.engine, DbContext(actor_type="system")) as conn:
            alive = await beat(conn, self.host_id)
        if not alive:
            await self._register()

    async def run(self, stop: asyncio.Event) -> None:
        """До `stop`: heartbeat каждые `interval_seconds`; ошибка БД не останавливает процесс (повтор на
        следующем такте, счётчик `aicd_worker_heartbeat_failures_total`). При остановке — `OFFLINE`."""
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    metrics.WORKER_HEARTBEAT_FAILURES.labels(metrics.label(self.kind, metrics.WORKER_KINDS)).inc()
                    log.warning("worker heartbeat failed", extra={"kind": self.kind}, exc_info=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
        finally:
            if self.host_id is not None:
                try:
                    async with transaction(self.engine, DbContext(actor_type="system")) as conn:
                        await mark_offline(conn, self.host_id)
                except Exception:
                    log.warning("worker host offline mark failed", extra={"kind": self.kind}, exc_info=True)


@dataclass(frozen=True)
class HostState:
    id: uuid.UUID
    hostname: str
    kind: str
    status: str
    last_heartbeat_at: Any
    age_seconds: float
    interval_seconds: float | None
    version: str | None

    @property
    def liveness(self) -> Liveness:
        if self.status == "OFFLINE":
            return "unknown"  # остановлен штатно: живость не оценивается
        return liveness(self.age_seconds, self.interval_seconds)


async def read_hosts(conn: AsyncConnection) -> list[HostState]:
    rows = (
        await conn.execute(
            text(
                "SELECT id, hostname, kind, status, last_heartbeat_at, labels,"
                " GREATEST(EXTRACT(EPOCH FROM now() - last_heartbeat_at), 0)::float8 AS age"
                " FROM worker_hosts ORDER BY kind, hostname"
            )
        )
    ).mappings()
    return [
        HostState(
            id=r["id"],
            hostname=r["hostname"],
            kind=r["kind"],
            status=r["status"],
            last_heartbeat_at=r["last_heartbeat_at"],
            age_seconds=float(r["age"]),
            interval_seconds=_interval(r["labels"]),
            version=_version(r["labels"]),
        )
        for r in rows
    ]


def freshest_by_kind(hosts: list[HostState]) -> dict[str, HostState]:
    """Самый свежий heartbeat среди неостановленных процессов каждого вида."""
    best: dict[str, HostState] = {}
    for h in hosts:
        if h.status == "OFFLINE":
            continue
        current = best.get(h.kind)
        if current is None or h.age_seconds < current.age_seconds:
            best[h.kind] = h
    return best


async def sample_heartbeat_gauges(engine: AsyncEngine) -> None:
    """Gauge `aicd_worker_heartbeat_age_seconds{kind}` / `…_interval_seconds{kind}` (worker-outbox)."""
    async with transaction(engine, DbContext(actor_type="system")) as conn:
        hosts = await read_hosts(conn)
    metrics.WORKER_HEARTBEAT_AGE.clear()
    metrics.WORKER_HEARTBEAT_INTERVAL.clear()
    for kind, host in freshest_by_kind(hosts).items():
        if kind not in metrics.WORKER_KINDS or host.interval_seconds is None:
            continue
        metrics.WORKER_HEARTBEAT_AGE.labels(kind).set(host.age_seconds)
        metrics.WORKER_HEARTBEAT_INTERVAL.labels(kind).set(host.interval_seconds)
