"""Наблюдаемость воркеров (P8-06): `/metrics` процесса и периодические gauge-метрики.

* каждый процесс `aicd-worker <kind>` при `AICD_METRICS_PORT > 0` поднимает `/metrics` (bearer-токен из
  `AICD_METRICS_TOKEN_FILE`; в production без токена endpoint выключен);
* worker-outbox раз в `SAMPLE_INTERVAL_SECONDS` собирает gauge: backlog outbox, stuck leases, размер БД
  (`outbox_dispatcher.sample_gauges`), доступность провайдеров (`cli_adapters.registry`), глубину очередей
  Dramatiq в Redis. Ошибка сбора не останавливает доставку (`aicd_metrics_sampler_errors_total`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from wsgiref.simple_server import WSGIServer

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters import registry
from aicd_platform import metrics
from aicd_platform.db import DbContext, transaction
from aicd_platform.outbox_dispatcher import sample_gauges
from aicd_platform.telemetry import configure_tracing
from aicd_worker.broker import DOCS_QUEUE, EXEC_QUEUE, ORCHESTRATOR_QUEUE
from aicd_worker.settings import WorkerSettings

log = logging.getLogger("aicd.worker.metrics")

SAMPLE_INTERVAL_SECONDS = 15.0
DRAMATIQ_NAMESPACE = "aicd-dramatiq"
QUEUES = (ORCHESTRATOR_QUEUE, EXEC_QUEUE, DOCS_QUEUE)


def start(settings: WorkerSettings, kind: str) -> WSGIServer | None:
    """Трассировка процесса и `/metrics` (если задан порт)."""
    configure_tracing(f"aicd-worker-{kind}", settings.otel_exporter)
    if settings.metrics_port <= 0:
        return None
    return metrics.start_worker_server(
        host=settings.metrics_host,
        port=settings.metrics_port,
        token=settings.metrics_token_value(),
        environment=settings.environment,
    )


async def sample_queues(redis: Redis) -> dict[str, int]:
    """Глубина очередей Dramatiq: список готовых + отложенных (`.DQ`), dead letters — `.XQ` (zset)."""
    depths: dict[str, int] = {}
    for queue in QUEUES:
        key = f"{DRAMATIQ_NAMESPACE}:{queue}"
        ready = int(await redis.llen(key))
        delayed = int(await redis.llen(f"{key}.DQ"))
        dead = int(await redis.zcard(f"{key}.XQ"))
        depths[queue] = ready + delayed
        metrics.QUEUE_DEPTH.labels(queue).set(ready + delayed)
        metrics.QUEUE_DEAD_LETTERS.labels(queue).set(dead)
    return depths


@dataclass
class Sampler:
    """Периодический сбор gauge в цикле worker-outbox (не чаще `interval`)."""

    engine: AsyncEngine
    redis: Redis
    interval: float = SAMPLE_INTERVAL_SECONDS
    _last: float = float("-inf")

    async def maybe_sample(self) -> bool:
        now = time.monotonic()
        if now - self._last < self.interval:
            return False
        self._last = now
        await self.sample()
        return True

    async def sample(self) -> None:
        for step in (self._db, self._providers, self._queues):
            try:
                await step()
            except Exception:
                metrics.SAMPLER_ERRORS.inc()
                log.warning("metrics sampling failed", extra={"step": step.__name__}, exc_info=True)

    async def _db(self) -> None:
        await sample_gauges(self.engine)

    async def _providers(self) -> None:
        async with transaction(self.engine, DbContext(actor_type="system")) as conn:
            await registry.sample_availability(conn)

    async def _queues(self) -> None:
        await sample_queues(self.redis)
