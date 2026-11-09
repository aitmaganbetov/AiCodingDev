"""Точки входа воркеров: `aicd-worker outbox|orchestrator|exec|index`.

* `outbox` — опрос outbox (0,5 с без LISTEN), проекция в run_event_log, публикация в Dramatiq, сигнал SSE;
* `orchestrator` — Dramatiq-консьюмер очередей `orchestrator` и `docs` (загрузка документации через
  `security.ssrf`, `aicd_worker.docs`; шаги поручений `directive.state_changed` → `directives.process`)
  + периодический tick/повторная постановка READY;
* `exec` — консьюмер очереди `exec` (исполнение задач через CLI Gateway; fake-адаптер запрещён в production)
  + reaper аренд и recovery reconciler своего хоста;
* `index` — опрос очереди `indexing_jobs` (`sys_indexing_workspaces`), индексация, определение стека,
  очистка кэша экспортов снимков (`aicd_worker.indexing`).

Каждый процесс пишет heartbeat в `worker_hosts` (`AICD_WORKER_HEARTBEAT_SECONDS`, по умолчанию 15 с;
`aicd_platform.worker_hosts`); при штатной остановке строка помечается OFFLINE.

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

from aicd_cli_adapters.registry import register_host
from aicd_orchestration import directives
from aicd_platform import sse, worker_hosts
from aicd_platform.artifacts import build_artifact_store
from aicd_platform.db import DbContext, TcpKeepalive, create_engine, transaction
from aicd_platform.logging import configure_logging
from aicd_platform.outbox_dispatcher import dispatch_batch
from aicd_worker import docs, github, indexing, maintenance, observability, runtime
from aicd_worker.broker import (
    DOCS_QUEUE,
    EXEC_QUEUE,
    GITHUB_QUEUE,
    ORCHESTRATOR_QUEUE,
    Actors,
    build_broker,
    register_actors,
)
from aicd_worker.settings import WorkerSettings
from aicd_worker.wiring import (
    ConfigurationError,
    build_orchestrator,
    build_provider_prober,
    build_reconciler,
    check_exec_adapter,
)

log = logging.getLogger("aicd.worker")

Kind = Literal["outbox", "orchestrator", "exec", "index"]
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

    async def notify_workspace(workspace_id: uuid.UUID) -> None:
        await redis.publish(sse.workspace_channel(workspace_id), str(workspace_id))

    sampler = observability.Sampler(engine, redis, backup_status_file=settings.backup_status_file)
    try:
        while not stop.is_set():
            await sampler.maybe_sample()  # gauge-метрики P8-06 (ошибки сбора не прерывают доставку)
            try:
                published = await dispatch_batch(
                    engine, actors.publish, notify, settings.outbox_batch_size, notify_workspace=notify_workspace
                )
            except Exception:
                log.exception("outbox dispatch failed")
                await _wait(stop, ERROR_BACKOFF_SECONDS)
                continue
            if published < settings.outbox_batch_size:
                await _wait(stop, settings.outbox_poll_seconds)
    finally:
        if own_redis:
            await redis.aclose()


async def run_indexer(settings: WorkerSettings, engine: AsyncEngine, stop: asyncio.Event) -> None:
    state = indexing.IndexLoopState()
    while not stop.is_set():
        try:
            iteration = await indexing.index_once(settings, engine, state)
        except Exception:
            log.exception("indexing iteration failed")
            await _wait(stop, ERROR_BACKOFF_SECONDS)
            continue
        if not iteration.outcomes:
            await _wait(stop, settings.index_poll_seconds)


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
    store = build_artifact_store(settings)
    host_id: uuid.UUID | None = None
    if kind == "exec":
        async with transaction(engine, DbContext(actor_type="system")) as conn:
            host_id = await register_host(conn, "exec")
    rt = runtime.Runtime(
        engine=engine,
        orchestrator=build_orchestrator(settings, engine, store=store, worker_host_id=host_id),
        loop=asyncio.get_running_loop(),
        dispatch_execution=actors.dispatch_execution,
        stale_ready_seconds=settings.stale_ready_seconds,
        reconciler=build_reconciler(settings, engine, host_id, store) if host_id is not None else None,
        docs_importer=docs.build_importer(settings, engine, store) if kind == "orchestrator" else None,
        provider_prober=build_provider_prober(settings, engine, store) if kind == "exec" else None,
        worker_host_id=host_id,
    )
    if kind == "orchestrator":

        async def process_directive(ws: uuid.UUID, directive_id: uuid.UUID) -> None:
            await directives.process(engine, ws, directive_id, artifacts=store)

        rt.process_directive = process_directive
        rt.github = github.build_workflow(settings, engine)  # None — GitHub App не настроен
    runtime.install(rt)
    scheduler = maintenance.Scheduler()  # exec: reconcile при старте, далее reap 15 с / reconcile 60 с
    github_recovery = maintenance.GitHubRecoverySchedule()  # orchestrator: сверка GitHub-действий (OQ-47 (7))
    queues = {ORCHESTRATOR_QUEUE, DOCS_QUEUE, GITHUB_QUEUE} if kind == "orchestrator" else {EXEC_QUEUE}
    worker = dramatiq.Worker(actors.broker, queues=queues, worker_threads=settings.worker_threads)
    worker.start()
    try:
        while not stop.is_set():
            try:
                if kind == "orchestrator":
                    await maintenance.sweep(rt)
                    await github_recovery.run_due(rt)
                else:
                    await scheduler.run_due(rt)
            except Exception:
                log.exception("%s maintenance failed", kind)
            await _wait(stop, settings.tick_interval_seconds)
    finally:
        await asyncio.to_thread(worker.stop)
        runtime.install(None)
        if rt.github is not None:
            await rt.github.client.aclose()


async def serve(kind: Kind, settings: WorkerSettings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    engine = create_engine(
        settings.require_database_url(),
        pool_size=settings.worker_threads + 2,
        keepalive=TcpKeepalive.from_settings(settings),
    )
    metrics_server = observability.start(settings, kind)
    # Heartbeat процесса (worker_hosts, GET /system/health и aicd_worker_heartbeat_age_seconds): отдельная задача,
    # останавливается тем же событием stop и помечает строку OFFLINE.
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        worker_hosts.Heartbeat(engine, kind, settings.worker_heartbeat_seconds).run(heartbeat_stop)
    )
    try:
        if kind == "outbox":
            await run_outbox(settings, engine, stop)
        elif kind == "index":
            await run_indexer(settings, engine, stop)
        else:
            await run_consumers(kind, settings, engine, stop)
    finally:
        heartbeat_stop.set()
        await heartbeat
        if metrics_server is not None:
            await asyncio.to_thread(metrics_server.shutdown)
            metrics_server.server_close()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicd-worker", description="Фоновые воркеры AiCodingDev")
    parser.add_argument("kind", choices=["outbox", "orchestrator", "exec", "index"])
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
