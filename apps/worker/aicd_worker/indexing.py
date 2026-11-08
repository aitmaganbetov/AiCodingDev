"""Воркер индексации `aicd-worker index` (P5-02; DATABASE_DESIGN §10.9, DATA_FLOW §9).

Итерация (`index_once`): workspace с заданиями в очереди и «зависшими» RUNNING (дольше
`index_stale_seconds` — аренда потеряна) выбираются SECURITY DEFINER `sys_indexing_workspaces` (межарендаторная
выборка; сами задания читаются уже в RLS-контексте workspace). Задания выполняются последовательно
(`run_job_with_stack`: снимок индекса, затем определение стека commit → `project_technologies`; сбой стека не
делает задание неуспешным). Периодически удаляются старые экспорты снимков для ripgrep (`prune_export_cache`).

Задания ставят: привязка репозитория (`attachRepository`), исполнитель задач (индекс перед исполнением —
`context.snapshot.ensure_snapshot`).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_code_intelligence.context.snapshot import JobOutcome, run_job_with_stack
from aicd_code_intelligence.index.search import prune_export_cache
from aicd_platform.db import DbContext, transaction
from aicd_worker.settings import WorkerSettings

log = logging.getLogger("aicd.worker.indexing")


@dataclass
class IndexLoopState:
    last_prune: float | None = None


@dataclass
class IterationResult:
    outcomes: list[JobOutcome] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)


async def _jobs(engine: AsyncEngine, ws: uuid.UUID, limit: int, stale_seconds: int) -> list[uuid.UUID]:
    async with transaction(engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        rows = await conn.execute(
            text(
                "SELECT id FROM indexing_jobs"
                " WHERE status = 'QUEUED'"
                "    OR (status = 'RUNNING' AND started_at < now() - make_interval(secs => :stale))"
                " ORDER BY created_at, id LIMIT :lim"
            ),
            {"stale": max(stale_seconds, 60), "lim": limit},
        )
        return [r[0] for r in rows]


async def index_once(
    settings: WorkerSettings, engine: AsyncEngine, state: IndexLoopState | None = None
) -> IterationResult:
    state = state or IndexLoopState()
    result = IterationResult()
    async with transaction(engine, DbContext(actor_type="system")) as conn:
        workspaces = [
            r[0]
            for r in await conn.execute(
                text("SELECT workspace_id FROM sys_indexing_workspaces(:n, :stale)"),
                {"n": settings.index_workspace_batch, "stale": settings.index_stale_seconds},
            )
        ]
    for ws in workspaces:
        for job_id in await _jobs(engine, ws, settings.index_jobs_per_workspace, settings.index_stale_seconds):
            outcome = await run_job_with_stack(engine, ws, job_id, repository_roots=settings.repository_roots)
            log.info(
                "index.job_processed",
                extra={
                    "job_id": str(job_id),
                    "status": outcome.result.status,
                    "technologies": outcome.technologies,
                    "stack_error": outcome.stack_error,
                },
            )
            result.outcomes.append(outcome)
    now = time.monotonic()
    if state.last_prune is None or now - state.last_prune >= settings.index_prune_interval_seconds:
        state.last_prune = now
        result.pruned = await asyncio.to_thread(
            prune_export_cache, settings.index_export_cache_root, keep=settings.index_export_keep
        )
    return result
