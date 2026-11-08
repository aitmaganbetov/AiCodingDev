"""Точки входа воркеров: `aicd-worker outbox|orchestrator|exec`.

* `outbox` — опрос outbox (0,5 с без LISTEN), проекция в run_event_log, публикация в Dramatiq, сигнал SSE;
* `orchestrator` — Dramatiq-консьюмер очереди `orchestrator` + периодический reaper аренд и tick;
* `exec` — консьюмер очереди `exec` (исполнение задач через CLI Gateway; fake-адаптер запрещён в production).

SIGINT/SIGTERM — штатная остановка: новые сообщения не берутся, текущие дорабатываются.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import uuid
from collections.abc import Sequence
from typing import Literal

import dramatiq
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform import sse
from aicd_platform.db import create_engine
from aicd_platform.logging import configure_logging
from aicd_platform.outbox_dispatcher import dispatch_batch
from aicd_worker import maintenance, runtime
from aicd_worker.broker import EXEC_QUEUE, ORCHESTRATOR_QUEUE, Actors, build_broker, register_actors
from aicd_worker.settings import WorkerSettings
from aicd_worker.wiring import ConfigurationError, build_orchestrator, check_exec_adapter

log = logging.getLogger("aicd.worker")

Kind = Literal["outbox", "orchestrator", "exec"]
ERROR_BACKOFF_SECONDS = 2.0


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run_outbox(
    settings: WorkerSettings,
    engine: AsyncEngine,
    stop: asyncio.Event,
    *,
    actors: Actors | None = None,
    redis: Redis | None = None,
) -> None:
    actors = actors or register_actors(build_broker(settings.redis_url))
    own_redis = redis is None
    redis = redis or Redis.from_url(settings.redis_url, socket_timeout=2.0, socket_connect_timeout=2.0)

    async def notify(run_id: uuid.UUID) -> None:
        await redis.publish(sse.run_channel(run_id), str(run_id))

    try:
        while not stop.is_set():
            try:
                published = await dispatch_batch(engine, actors.publish, notify, settings.outbox_batch_size)
            except Exception:
                log.exception("outbox dispatch failed")
                await _wait(stop, ERROR_BACKOFF_SECONDS)
                continue
            if published < settings.outbox_batch_size:
                await _wait(stop, settings.outbox_poll_seconds)
    finally:
        if own_redis:
            await redis.aclose()


async def run_consumers(
    kind: Literal["orchestrator", "exec"],
    settings: WorkerSettings,
    engine: AsyncEngine,
    stop: asyncio.Event,
    *,
    actors: Actors | None = None,
) -> None:
    if kind == "exec":
        check_exec_adapter(settings)
    actors = actors or register_actors(
        build_broker(settings.redis_url), exec_time_limit_seconds=settings.exec_time_limit_seconds
    )
    rt = runtime.Runtime(
        engine=engine,
        orchestrator=build_orchestrator(settings, engine),
        loop=asyncio.get_running_loop(),
        dispatch_execution=actors.dispatch_execution,
        stale_ready_seconds=settings.stale_ready_seconds,
    )
    runtime.install(rt)
    queue = ORCHESTRATOR_QUEUE if kind == "orchestrator" else EXEC_QUEUE
    worker = dramatiq.Worker(actors.broker, queues={queue}, worker_threads=settings.worker_threads)
    worker.start()
    try:
        while not stop.is_set():
            if kind == "orchestrator":
                try:
                    await maintenance.sweep(rt)
                except Exception:
                    log.exception("orchestrator sweep failed")
            await _wait(stop, settings.tick_interval_seconds)
    finally:
        await asyncio.to_thread(worker.stop)
        runtime.install(None)


async def serve(kind: Kind, settings: WorkerSettings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    engine = create_engine(settings.require_database_url(), pool_size=settings.worker_threads + 2)
    try:
        if kind == "outbox":
            await run_outbox(settings, engine, stop)
        else:
            await run_consumers(kind, settings, engine, stop)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicd-worker", description="Фоновые воркеры AiCodingDev")
    parser.add_argument("kind", choices=["outbox", "orchestrator", "exec"])
    args = parser.parse_args(argv)
    settings = WorkerSettings()
    configure_logging(settings.log_level)
    try:
        if args.kind == "exec":
            check_exec_adapter(settings)
        asyncio.run(serve(args.kind, settings))
    except ConfigurationError as exc:
        print(f"aicd-worker: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
