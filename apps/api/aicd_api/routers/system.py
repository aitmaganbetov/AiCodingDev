"""Состояние платформы для администратора (UI redesign B2; UI_REDESIGN_PLAN §3.2 «System Health», G-05).

`GET /system/health` — право `system:health` (owner/admin workspace из `X-Workspace-Id`; не член ⇒ 404). Компоненты:

* `api` — этот процесс (ответ получен) и ожидаемая ревизия схемы;
* `database` — `SELECT version_num FROM alembic_version` (задержка, совпадение ревизии с ожидаемой);
* `redis` — `PING` (задержка);
* `object_storage` — проверка хранилища артефактов (`ArtifactStore.healthy`: каталог fs / `HeadBucket` S3);
* `outbox` — backlog доставки (`sys_outbox_backlog()`, миграция 0017: число неопубликованных событий и возраст
  самого старого; пороги — как алерт `AicdOutboxLag`);
* `workers` — heartbeat процессов (`worker_hosts`, `aicd_platform.worker_hosts`): живость каждого процесса и вида по
  его собственному интервалу (ok ≤ 3 интервалов, degraded ≤ 10, иначе down); остановленные штатно (OFFLINE) не
  считаются упавшими; вид без работающих процессов — `down`, только если он перечислен в `expected_worker_kinds`;
* `queues` — глубина очередей Dramatiq в Redis (готовые + отложенные) и dead letters (как `aicd_worker.observability`
  и `aicd_worker.dlq`);
* `backup` — файл статуса утилиты бэкапа (`AICD_BACKUP_STATUS_FILE`, если смонтирован в api) и `pg_stat_archiver`.

Ничего не выдумывается: не измеренное — `unknown` с причиной; текст ошибок зависимостей (хосты, учётные данные) не
отдаётся. Результат кэшируется в процессе на `CACHE_TTL_SECONDS` (одна проверка на всех администраторов) и
ограничен отдельным лимитом `SYSTEM_HEALTH_PER_MINUTE` на пользователя (поверх общего лимита чтения).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy import text
from starlette.responses import Response

from aicd_api import EXPECTED_SCHEMA_REVISION
from aicd_api.deps import (
    _remember_rate,
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    workspace_access,
)
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_api.state import AppState
from aicd_identity import api as identity
from aicd_platform import worker_hosts
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction

router = APIRouter(tags=["system"])
log = logging.getLogger("aicd.system_health")

Status = Literal["ok", "degraded", "down", "unknown"]

CACHE_TTL_SECONDS: Final = 10
SYSTEM_HEALTH_PER_MINUTE: Final = 30
CHECK_TIMEOUT_SECONDS: Final = 2.0
# Очереди Dramatiq (namespace брокера и имена очередей — aicd_worker.broker / aicd_worker.dlq; тест сверяет).
DRAMATIQ_NAMESPACE: Final = "aicd-dramatiq"
QUEUES: Final = ("orchestrator", "exec", "docs", "github", "notifications")
QUEUE_BACKLOG_DEGRADED: Final = 500  # как алерт AicdQueueBacklog
OUTBOX_AGE_DEGRADED_SECONDS: Final = 60.0
OUTBOX_AGE_DOWN_SECONDS: Final = 300.0  # как алерт AicdOutboxLag (critical)
# Свежесть бэкапов — как алерты AicdBaseBackupStale / AicdWalSyncStale / AicdArtifactsBackupStale.
BACKUP_MAX_AGE_SECONDS: Final[Mapping[str, int]] = {
    "postgres_base": 26 * 3600,
    "postgres_wal": 15 * 60,
    "artifacts": 26 * 3600,
}
BACKUP_COMPONENTS: Final = ("postgres_base", "postgres_wal", "postgres_dump", "artifacts", "secrets")
# Отказ этих компонентов делает платформу неработоспособной (как критичные проверки /health/ready).
CRITICAL: Final = frozenset({"database", "object_storage"})
_RANK: Final[dict[str, int]] = {"ok": 0, "unknown": 0, "degraded": 1, "down": 2}


@dataclass
class Component:
    name: str
    status: Status
    checked_at: datetime
    latency_ms: float | None = None
    measurements: dict[str, Any] = field(default_factory=dict)
    detail: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "checked_at": self.checked_at,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
            "measurements": self.measurements,
            "detail": self.detail,
        }


def _worst(statuses: list[Status]) -> Status:
    worst: Status = "ok"
    for s in statuses:
        if _RANK[s] > _RANK[worst]:
            worst = s
    return worst


def overall(components: list[Component]) -> Status:
    """`down` — отказ критичного компонента; `degraded` — любая деградация или отказ некритичного; иначе `ok`.
    `unknown` (не измерено) не ухудшает и не улучшает итог."""
    if any(c.status == "down" and c.name in CRITICAL for c in components):
        return "down"
    if any(c.status in ("down", "degraded") for c in components):
        return "degraded"
    return "ok"


async def _timed(check: Callable[[], Awaitable[Any]]) -> tuple[Any, float]:
    started = time.monotonic()
    result = await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_SECONDS)
    return result, (time.monotonic() - started) * 1000


async def check_database(state: AppState) -> Component:
    async def query() -> Any:
        async with state.engine.connect() as conn:
            return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()

    try:
        revision, latency = await _timed(query)
    except Exception:
        log.warning("system health: database check failed", exc_info=True)
        return Component("database", "down", utcnow(), detail="PostgreSQL недоступен или не ответил вовремя")
    current = revision == EXPECTED_SCHEMA_REVISION
    return Component(
        "database",
        "ok" if current else "degraded",
        utcnow(),
        latency,
        {"schema_revision": revision, "expected_schema_revision": EXPECTED_SCHEMA_REVISION, "schema_current": current},
        None if current else "Ревизия схемы не совпадает с ожидаемой версией API",
    )


async def check_redis(state: AppState) -> Component:
    async def ping() -> Any:
        return await state.redis.ping()

    try:
        pong, latency = await _timed(ping)
    except Exception:  # noqa: BLE001
        return Component("redis", "down", utcnow(), detail="Redis недоступен или не ответил вовремя")
    if not pong:
        return Component("redis", "down", utcnow(), latency, detail="Redis не ответил на PING")
    return Component("redis", "ok", utcnow(), latency)


async def check_object_storage(state: AppState) -> Component:
    backend = state.settings.artifact_backend

    async def probe() -> Any:
        return await asyncio.to_thread(state.artifacts.healthy)

    try:
        healthy, latency = await _timed(probe)
    except Exception:  # noqa: BLE001
        return Component(
            "object_storage",
            "down",
            utcnow(),
            measurements={"backend": backend},
            detail="Хранилище артефактов недоступно",
        )
    if not healthy:
        return Component(
            "object_storage",
            "down",
            utcnow(),
            latency,
            {"backend": backend},
            "Хранилище артефактов недоступно для записи",
        )
    return Component("object_storage", "ok", utcnow(), latency, {"backend": backend})


async def check_outbox(state: AppState) -> Component:
    async def query() -> Any:
        async with transaction(state.engine, DbContext()) as conn:
            return (await conn.execute(text("SELECT pending, oldest_created_at FROM sys_outbox_backlog()"))).one()

    try:
        (pending, oldest), latency = await _timed(query)
    except Exception:
        log.warning("system health: outbox check failed", exc_info=True)
        return Component("outbox", "unknown", utcnow(), detail="Backlog outbox не измерен (БД недоступна)")
    now = utcnow()
    age = max((now - oldest).total_seconds(), 0.0) if oldest is not None else 0.0
    status: Status = "ok"
    if age > OUTBOX_AGE_DOWN_SECONDS:
        status = "down"
    elif age > OUTBOX_AGE_DEGRADED_SECONDS:
        status = "degraded"
    return Component(
        "outbox",
        status,
        now,
        latency,
        {"pending_events": int(pending), "oldest_pending_age_seconds": round(age, 1)},
        None if status == "ok" else "События outbox не доставляются вовремя",
    )


def classify_kinds(hosts: list[worker_hosts.HostState], expected: Collection[str]) -> list[dict[str, Any]]:
    """Живость по видам: по самому свежему heartbeat неостановленных процессов вида (ok ≤ 3 интервалов, degraded
    ≤ 10, иначе down). Нет работающих процессов: `down`, если вид ожидается настройкой `expected_worker_kinds`,
    иначе `unknown` (ожидания не заданы — отсутствие не выдаётся за отказ)."""
    freshest = worker_hosts.freshest_by_kind(hosts)
    out = []
    for kind in sorted({h.kind for h in hosts} | set(expected)):
        of_kind = [h for h in hosts if h.kind == kind]
        best = freshest.get(kind)
        status: Status = best.liveness if best is not None else ("down" if kind in expected else "unknown")
        out.append(
            {
                "kind": kind,
                "status": status,
                "expected": kind in expected,
                "hosts": len(of_kind),
                "active_hosts": sum(1 for h in of_kind if h.status != "OFFLINE"),
                "freshest_heartbeat_age_seconds": round(best.age_seconds, 1) if best is not None else None,
                "heartbeat_interval_seconds": best.interval_seconds if best is not None else None,
            }
        )
    return out


async def check_workers(
    state: AppState,
) -> tuple[Component, list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    async def query() -> Any:
        async with transaction(state.engine, DbContext()) as conn:
            return await worker_hosts.read_hosts(conn)

    try:
        rows, latency = await _timed(query)
    except Exception:
        log.warning("system health: workers check failed", exc_info=True)
        component = Component("workers", "unknown", utcnow(), detail="worker_hosts не прочитаны (БД недоступна)")
        return component, None, None
    now = utcnow()
    hosts = [
        {
            "id": h.id,
            "hostname": h.hostname,
            "kind": h.kind,
            "status": h.status,
            "last_heartbeat_at": h.last_heartbeat_at,
            "heartbeat_age_seconds": round(h.age_seconds, 1),
            "heartbeat_interval_seconds": h.interval_seconds,
            "liveness": h.liveness,
            "version": h.version,
        }
        for h in rows
    ]
    kinds = classify_kinds(rows, state.settings.expected_worker_kinds)
    known: list[Status] = [k["status"] for k in kinds if k["status"] != "unknown"]
    measurements = {
        "hosts": len(hosts),
        "active_hosts": sum(1 for h in rows if h.status != "OFFLINE"),
        "kinds_ok": sum(1 for k in kinds if k["status"] == "ok"),
        "kinds_not_ok": sum(1 for k in kinds if k["status"] in ("degraded", "down")),
    }
    if not known:
        unknown = (
            "Нет процессов воркеров с heartbeat" if not hosts else "Живость воркеров не определена (нет heartbeat)"
        )
        return Component("workers", "unknown", now, latency, measurements, unknown), hosts, kinds
    status = _worst(known)
    bad = [f"{k['kind']}: {k['status']}" for k in kinds if k["status"] in ("degraded", "down")]
    detail = ("Heartbeat: " + ", ".join(bad)) if bad else None
    return Component("workers", status, now, latency, measurements, detail), hosts, kinds


async def sample_queues(redis: Redis) -> list[dict[str, Any]]:
    """Готовые + отложенные (`.DQ`) сообщения и dead letters (`.XQ`, zset) каждой очереди Dramatiq."""
    out = []
    for queue in QUEUES:
        key = f"{DRAMATIQ_NAMESPACE}:{queue}"
        ready = int(await redis.llen(key))
        delayed = int(await redis.llen(f"{key}.DQ"))
        dead = int(await redis.zcard(f"{key}.XQ"))
        out.append({"queue": queue, "ready": ready, "delayed": delayed, "dead_letters": dead})
    return out


async def check_queues(state: AppState) -> tuple[Component, list[dict[str, Any]] | None]:
    async def sample() -> Any:
        return await sample_queues(state.redis)

    try:
        queues, latency = await _timed(sample)
    except Exception:  # noqa: BLE001
        return Component("queues", "unknown", utcnow(), detail="Очереди не измерены (Redis недоступен)"), None
    depth = sum(q["ready"] + q["delayed"] for q in queues)
    dead = sum(q["dead_letters"] for q in queues)
    backlog = any(q["ready"] + q["delayed"] > QUEUE_BACKLOG_DEGRADED for q in queues)
    status: Status = "degraded" if dead or backlog else "ok"
    detail = None
    if dead:
        detail = "Есть сообщения в dead-letter очередях (python -m aicd_worker.dlq list)"
    elif backlog:
        detail = f"Очередь длиннее {QUEUE_BACKLOG_DEGRADED} сообщений"
    return Component("queues", status, utcnow(), latency, {"depth": depth, "dead_letters": dead}, detail), queues


def _read_backup_status(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, Mapping) else None


def backup_entries(status: Mapping[str, Any] | None, now: datetime) -> list[dict[str, Any]]:
    """Компоненты файла статуса бэкапа (`components.<имя>.last_status/last_success_unixtime`) — только известные."""
    components = status.get("components") if status else None
    if not isinstance(components, Mapping):
        return []
    out = []
    for name in BACKUP_COMPONENTS:
        entry = components.get(name)
        if not isinstance(entry, Mapping):
            continue
        success = entry.get("last_success_unixtime")
        success_at = datetime.fromtimestamp(success, UTC) if isinstance(success, int | float) else None
        last_status = entry.get("last_status")
        max_age = BACKUP_MAX_AGE_SECONDS.get(name)
        stale = bool(max_age and (success_at is None or (now - success_at).total_seconds() > max_age))
        out.append(
            {
                "component": name,
                "last_status": last_status if last_status in ("success", "failure") else "unknown",
                "last_success_at": success_at,
                "stale": stale,
            }
        )
    return out


async def check_backup(state: AppState) -> tuple[Component, list[dict[str, Any]] | None]:
    now = utcnow()
    entries: list[dict[str, Any]] | None = None
    measurements: dict[str, Any] = {}
    reasons: list[str] = []
    path = state.settings.backup_status_file
    if path is not None:
        try:
            entries = backup_entries(await asyncio.to_thread(_read_backup_status, path), now)
        except (OSError, ValueError):
            reasons.append("файл статуса бэкапа не прочитан")
    archiver = None
    try:

        async def query() -> Any:
            async with state.engine.connect() as conn:
                return (
                    await conn.execute(
                        text(
                            "SELECT archived_count, failed_count, last_archived_time, last_failed_time"
                            " FROM pg_stat_archiver"
                        )
                    )
                ).one()

        archiver, _ = await _timed(query)
    except Exception:  # noqa: BLE001
        reasons.append("pg_stat_archiver не прочитан")
    wal_failing = False
    wal_configured = False
    if archiver is not None:
        archived, failed, last_archived, last_failed = archiver
        wal_configured = bool(archived or failed)
        wal_failing = last_failed is not None and (last_archived is None or last_failed > last_archived)
        measurements.update(
            {
                "wal_archived_count": int(archived),
                "wal_failed_count": int(failed),
                "wal_last_archived_at": last_archived.isoformat() if last_archived else None,
                "wal_last_failed_at": last_failed.isoformat() if last_failed else None,
            }
        )
    if not entries and not wal_configured:
        unknown = "Статус бэкапов недоступен API: файл статуса не смонтирован, архивирование WAL не выполнялось"
        if reasons:
            unknown += " (" + "; ".join(reasons) + ")"
        return Component("backup", "unknown", now, None, measurements, unknown), entries
    problems = [e["component"] for e in entries or [] if e["last_status"] != "success" or e["stale"]]
    if wal_failing:
        problems.append("wal_archiving")
    status: Status = "degraded" if problems else "ok"
    notes = [f"Проблемы: {', '.join(problems)}"] if problems else []
    if entries is None and path is None:
        notes.append("файл статуса бэкапа не смонтирован в api — только архив WAL")
    return Component("backup", status, now, None, measurements, "; ".join(notes) or None), entries


async def collect(state: AppState) -> dict[str, Any]:
    started = utcnow()
    database, redis, storage, outbox = await asyncio.gather(
        check_database(state), check_redis(state), check_object_storage(state), check_outbox(state)
    )
    (workers, hosts, kinds), (queues, queue_list), (backup, backups) = await asyncio.gather(
        check_workers(state), check_queues(state), check_backup(state)
    )
    api = Component("api", "ok", started, measurements={"expected_schema_revision": EXPECTED_SCHEMA_REVISION})
    components = [api, database, redis, storage, outbox, workers, queues, backup]
    return {
        "status": overall(components),
        "checked_at": started,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "components": [c.view() for c in components],
        "worker_hosts": hosts,
        "worker_kinds": kinds,
        "queues": queue_list,
        "backups": backups,
    }


@dataclass
class _Cache:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    expires: float = float("-inf")
    body: dict[str, Any] | None = None


def _cache(request: Request) -> _Cache:
    cache: _Cache | None = getattr(request.app.state, "aicd_system_health", None)
    if cache is None:
        cache = _Cache()
        request.app.state.aicd_system_health = cache
    return cache


@router.get("/system/health", **op("getSystemHealth", "system:health"))
async def get_system_health(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "system:health")
    state = app_state(request)
    _remember_rate(
        request, await state.limiter.hit(f"system_health:user:{principal.user_id}", SYSTEM_HEALTH_PER_MINUTE)
    )
    cache = _cache(request)
    async with cache.lock:
        if cache.body is None or time.monotonic() >= cache.expires:
            cache.body = await collect(state)
            cache.expires = time.monotonic() + CACHE_TTL_SECONDS
        body = cache.body
    return json_response(body, headers={"Cache-Control": "private, max-age=0, no-store"})
