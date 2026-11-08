"""Индекс до исполнения и определение стека после индексации (P5-06/P5-11; DATA_FLOW §9 «снимок существует?»).

* `run_job_with_stack` — задание индексации (`index.jobs.run_indexing_job`) и, если снимок READY, определение
  стека этого commit (`technologies.detect_at_commit` → `persist_detection`, роль `aicd_worker`). Ошибка
  определения стека не делает задание индексации неуспешным: пишется в журнал и в результат (`stack_error`).
* `ensure_snapshot` — перед исполнением задачи: READY-снимок `(repository, commit, INDEX_VERSION)` есть ⇒ он;
  иначе задание ставится в очередь (или берётся активное) и выполняется **в процессе исполнителя** с
  ограничением времени. Задание, уже RUNNING у другого воркера, не перезапускается — снимок ожидается до того же
  таймаута. Любая неудача (ошибка индексации, таймаут, недоступный репозиторий) **не роняет задачу**:
  возвращается `EnsureResult(snapshot_id=None, warning=…)`, Context Pack собирается без кода (или по
  последнему READY-снимку — его фрагменты устаревшие и по умолчанию исключаются), предупреждение — в manifest.
  По таймауту задание остаётся RUNNING и подхватывается воркером индексации как «зависшее»
  (`sys_indexing_workspaces(…, stale_seconds)`).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_code_intelligence.index import INDEX_VERSION
from aicd_code_intelligence.index.gitsource import GitSourceError
from aicd_code_intelligence.index.jobs import IndexingResult, enqueue_indexing_job, run_indexing_job
from aicd_code_intelligence.technologies.detect import detect_at_commit
from aicd_code_intelligence.technologies.store import persist_detection
from aicd_platform.db import DbContext, transaction
from aicd_security.paths import resolve_ref

log = logging.getLogger("aicd.code_intelligence.context")

DEFAULT_ENSURE_TIMEOUT_SECONDS = 300.0
_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class EnsureResult:
    snapshot_id: uuid.UUID | None
    indexed_now: bool = False
    job_id: uuid.UUID | None = None
    warning: str | None = None


async def detect_stack(
    engine: AsyncEngine, workspace_id: uuid.UUID, job_id: uuid.UUID, *, repository_roots: Mapping[str, Path]
) -> int:
    """Стек commit задания → `project_technologies`. Возвращает число записанных технологий."""
    ctx = DbContext(workspace_id=workspace_id, actor_type="system")
    async with transaction(engine, ctx) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT j.commit_sha, r.project_id, r.local_root_ref FROM indexing_jobs j"
                    "  JOIN repositories r ON r.workspace_id = j.workspace_id AND r.id = j.repository_id"
                    " WHERE j.id = :id"
                ),
                {"id": job_id},
            )
        ).one()
    repo = resolve_ref(row.local_root_ref, dict(repository_roots))
    detected = await asyncio.to_thread(detect_at_commit, repo, row.commit_sha)
    async with transaction(engine, ctx) as conn:
        ids = await persist_detection(
            conn, workspace_id=workspace_id, project_id=row.project_id, commit_sha=row.commit_sha, detected=detected
        )
    return len(ids)


@dataclass
class JobOutcome:
    result: IndexingResult
    technologies: int | None = None
    stack_error: str | None = None


async def run_job_with_stack(
    engine: AsyncEngine, workspace_id: uuid.UUID, job_id: uuid.UUID, *, repository_roots: Mapping[str, Path]
) -> JobOutcome:
    result = await run_indexing_job(engine, workspace_id, job_id, repository_roots=repository_roots)
    outcome = JobOutcome(result)
    if result.status != "SUCCEEDED":
        return outcome
    try:
        outcome.technologies = await detect_stack(engine, workspace_id, job_id, repository_roots=repository_roots)
    except Exception as exc:  # noqa: BLE001 — стек вторичен: задание индексации уже успешно
        log.warning("index.stack_detection_failed", extra={"job_id": str(job_id), "error": type(exc).__name__})
        outcome.stack_error = type(exc).__name__
    return outcome


async def _ready_snapshot(
    engine: AsyncEngine, ctx: DbContext, repository_id: uuid.UUID, commit: str
) -> uuid.UUID | None:
    async with transaction(engine, ctx) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id FROM code_index_snapshots WHERE repository_id = :repo AND commit_sha = :sha"
                    " AND index_version = :ver AND status = 'READY'"
                ),
                {"repo": repository_id, "sha": commit, "ver": INDEX_VERSION},
            )
        ).first()
    return row[0] if row else None


async def ensure_snapshot(
    engine: AsyncEngine,
    *,
    workspace_id: uuid.UUID,
    repository_id: uuid.UUID,
    commit_sha: str,
    repository_roots: Mapping[str, Path],
    timeout_seconds: float = DEFAULT_ENSURE_TIMEOUT_SECONDS,
) -> EnsureResult:
    ctx = DbContext(workspace_id=workspace_id, actor_type="system")
    try:
        existing = await _ready_snapshot(engine, ctx, repository_id, commit_sha)
        if existing is not None:
            return EnsureResult(existing)
        async with transaction(engine, ctx) as conn:
            job_id, _ = await enqueue_indexing_job(
                conn, workspace_id=workspace_id, repository_id=repository_id, commit_sha=commit_sha
            )
            status = (
                await conn.execute(text("SELECT status FROM indexing_jobs WHERE id = :id"), {"id": job_id})
            ).scalar_one()
    except (GitSourceError, ValueError) as exc:
        return EnsureResult(None, warning=f"index_enqueue_failed:{type(exc).__name__}")
    deadline = time.monotonic() + timeout_seconds
    if status == "RUNNING":  # индексирует другой воркер — ждём снимок, не перезапуская задание
        while time.monotonic() < deadline:
            snap = await _ready_snapshot(engine, ctx, repository_id, commit_sha)
            if snap is not None:
                return EnsureResult(snap, job_id=job_id)
            await asyncio.sleep(_POLL_SECONDS)
        return EnsureResult(None, job_id=job_id, warning="index_wait_timeout")
    try:
        outcome = await asyncio.wait_for(
            run_job_with_stack(engine, workspace_id, job_id, repository_roots=repository_roots), timeout=timeout_seconds
        )
    except TimeoutError:
        log.warning("index.ensure_timeout", extra={"job_id": str(job_id)})
        return EnsureResult(None, job_id=job_id, warning="index_timeout")
    except Exception as exc:  # индексация не должна ронять задачу
        log.exception("index.ensure_failed", extra={"job_id": str(job_id)})
        return EnsureResult(None, job_id=job_id, warning=f"index_failed:{type(exc).__name__}")
    if outcome.result.status == "SUCCEEDED" and outcome.result.snapshot_id is not None:
        return EnsureResult(outcome.result.snapshot_id, indexed_now=True, job_id=job_id)
    return EnsureResult(None, job_id=job_id, warning=f"index_failed:{(outcome.result.error or 'unknown')[:200]}")
