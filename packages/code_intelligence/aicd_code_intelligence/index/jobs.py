"""Жизненный цикл `indexing_jobs` (P5-02; DATABASE_DESIGN §10.9, §18.3; DATA_FLOW §9).

FSM (триггер `trg_indexing_jobs_transition`, миграция 0007): создание → QUEUED; QUEUED → RUNNING |
CANCELLED | FAILED; RUNNING → SUCCEEDED | FAILED | CANCELLED | QUEUED. `run_indexing_job` — точка входа
для actor `worker-index` (Dramatiq), идемпотентна при повторной доставке:

1. транзакция «захвата»: `SELECT … FOR UPDATE`; терминальное задание — no-op; QUEUED → RUNNING
   (`attempt_count + 1`, `started_at`); RUNNING (повторная доставка после потери аренды) — продолжается.
   Если READY-снимок `(repository, commit, INDEX_VERSION)` уже есть — сразу SUCCEEDED с ним.
   База инкрементального построения — последний READY-снимок репозитория; без базы `kind` становится
   `full` (`ck_indexing_jobs_kind`);
2. построение снимка без БД (`builder.build_snapshot`, в отдельном потоке: git + tree-sitter);
3. одна транзакция: весь снимок (`store.persist_snapshot`) + задание SUCCEEDED. Ошибка откатывает снимок
   целиком (наполовину построенных снимков не бывает, поэтому и помечать FAILED нечего), задание → FAILED
   с безопасным сообщением в отдельной транзакции. Повтор для того же commit возможен новым заданием.

Путь репозитория — `aicd_security.paths.resolve_ref(repositories.local_root_ref, repository_roots)`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_code_intelligence.index import INDEX_VERSION
from aicd_code_intelligence.index.builder import IndexingError, IndexStats, PreviousFile, build_snapshot
from aicd_code_intelligence.index.gitsource import GitSourceError, validate_commit_sha
from aicd_code_intelligence.index.policy import IndexPolicy
from aicd_code_intelligence.index.store import latest_ready_snapshot, load_previous, persist_snapshot
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.paths import UnsafePathError, resolve_ref

log = logging.getLogger("aicd.code_intelligence.index")

_ERROR_MAX = 4000


@dataclass
class IndexingResult:
    job_id: uuid.UUID
    status: str
    snapshot_id: uuid.UUID | None = None
    kind: str | None = None
    base_snapshot_id: uuid.UUID | None = None
    error: str | None = None
    stats: IndexStats | None = None
    persisted: dict[str, Any] = field(default_factory=dict)


async def enqueue_indexing_job(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    repository_id: uuid.UUID,
    commit_sha: str,
    kind: str = "incremental",
) -> tuple[uuid.UUID, bool]:
    """Ставит задание в очередь; при активном задании того же commit возвращает его (`created=False`)."""
    validate_commit_sha(commit_sha)
    if kind not in ("full", "incremental"):
        raise ValueError("kind должен быть full или incremental")
    job_id = uuid7()
    row = (
        await conn.execute(
            text(
                "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha, kind)"
                " VALUES (:id, :ws, :repo, :sha, :kind)"
                " ON CONFLICT (repository_id, commit_sha) WHERE status IN ('QUEUED','RUNNING') DO NOTHING"
                " RETURNING id"
            ),
            {"id": job_id, "ws": workspace_id, "repo": repository_id, "sha": commit_sha, "kind": kind},
        )
    ).first()
    if row is not None:
        return row[0], True
    existing = (
        await conn.execute(
            text(
                "SELECT id FROM indexing_jobs WHERE repository_id = :repo AND commit_sha = :sha"
                " AND status IN ('QUEUED','RUNNING')"
            ),
            {"repo": repository_id, "sha": commit_sha},
        )
    ).first()
    if existing is None:  # завершилось между INSERT и SELECT — повторяем
        return await enqueue_indexing_job(
            conn, workspace_id=workspace_id, repository_id=repository_id, commit_sha=commit_sha, kind=kind
        )
    return existing[0], False


@dataclass
class _Claim:
    repository_id: uuid.UUID
    project_id: uuid.UUID
    local_root_ref: str
    commit_sha: str
    kind: str
    base_snapshot_id: uuid.UUID | None
    previous: dict[str, PreviousFile]


async def _finish(
    engine: AsyncEngine, ctx: DbContext, job_id: uuid.UUID, status: str, *, error: str | None = None
) -> None:
    async with transaction(engine, ctx) as conn:
        await conn.execute(
            text(
                "UPDATE indexing_jobs SET status = :status, finished_at = now(), error = :error"
                " WHERE id = :id AND status = 'RUNNING'"
            ),
            {"id": job_id, "status": status, "error": (error or "")[:_ERROR_MAX] or None},
        )


async def run_indexing_job(
    engine: AsyncEngine,
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    repository_roots: Mapping[str, Path],
    policy: IndexPolicy | None = None,
) -> IndexingResult:
    ctx = DbContext(workspace_id=workspace_id, actor_type="system")
    async with transaction(engine, ctx) as conn:
        job = (
            await conn.execute(
                text(
                    "SELECT j.status, j.repository_id, j.commit_sha, j.kind, r.project_id, r.local_root_ref"
                    "  FROM indexing_jobs j JOIN repositories r ON r.workspace_id = j.workspace_id AND r.id = j.repository_id"
                    " WHERE j.id = :id FOR UPDATE OF j"
                ),
                {"id": job_id},
            )
        ).first()
        if job is None:
            raise LookupError("indexing job не найден в workspace")
        status, repository_id, commit_sha, kind, project_id, root_ref = job
        if status not in ("QUEUED", "RUNNING"):
            log.info("index.job_already_finished", extra={"job_id": str(job_id), "status": status})
            return IndexingResult(job_id=job_id, status=status)
        existing = (
            await conn.execute(
                text(
                    "SELECT id, status FROM code_index_snapshots"
                    " WHERE repository_id = :repo AND commit_sha = :sha AND index_version = :ver"
                ),
                {"repo": repository_id, "sha": commit_sha, "ver": INDEX_VERSION},
            )
        ).first()
        base = None if kind == "full" or existing is not None else await latest_ready_snapshot(
            conn, repository_id=repository_id
        )
        effective_kind = "incremental" if base is not None else "full"
        await conn.execute(
            text(
                "UPDATE indexing_jobs SET status = 'RUNNING', kind = :kind, base_snapshot_id = :base,"
                " attempt_count = attempt_count + 1, started_at = COALESCE(started_at, now()), error = NULL"
                " WHERE id = :id"
            ),
            {"id": job_id, "kind": effective_kind, "base": base},
        )
        if existing is not None:
            snap_id, snap_status = existing
            if snap_status == "READY":
                await conn.execute(
                    text(
                        "UPDATE indexing_jobs SET status = 'SUCCEEDED', snapshot_id = :snap, finished_at = now()"
                        " WHERE id = :id"
                    ),
                    {"id": job_id, "snap": snap_id},
                )
                return IndexingResult(job_id=job_id, status="SUCCEEDED", snapshot_id=snap_id, kind=effective_kind)
            claim = None
            fail = f"снимок commit уже существует в статусе {snap_status}"
        else:
            previous = await load_previous(conn, snapshot_id=base) if base is not None else {}
            claim = _Claim(repository_id, project_id, root_ref, commit_sha, effective_kind, base, previous)
            fail = None
    if claim is None:
        await _finish(engine, ctx, job_id, "FAILED", error=fail)
        return IndexingResult(job_id=job_id, status="FAILED", error=fail)

    result = IndexingResult(job_id=job_id, status="RUNNING", kind=claim.kind, base_snapshot_id=claim.base_snapshot_id)
    try:
        repo_path = resolve_ref(claim.local_root_ref, dict(repository_roots))
        build = await asyncio.to_thread(
            build_snapshot, repo_path, claim.commit_sha, policy=policy, previous=claim.previous
        )
        result.stats = build.stats
        async with transaction(engine, ctx) as conn:
            persisted = await persist_snapshot(
                conn,
                workspace_id=workspace_id,
                project_id=claim.project_id,
                repository_id=claim.repository_id,
                commit_sha=claim.commit_sha,
                parent_snapshot_id=claim.base_snapshot_id,
                build=build,
            )
            await conn.execute(
                text(
                    "UPDATE indexing_jobs SET status = 'SUCCEEDED', snapshot_id = :snap, finished_at = now()"
                    " WHERE id = :id"
                ),
                {"id": job_id, "snap": persisted.snapshot_id},
            )
        result.status = "SUCCEEDED"
        result.snapshot_id = persisted.snapshot_id
        result.persisted = {
            "file_count": persisted.file_count,
            "symbol_count": persisted.symbol_count,
            "chunk_count": persisted.chunk_count,
            "edge_count": persisted.edge_count,
            "copied_symbols": persisted.copied_symbols,
            "copied_chunks": persisted.copied_chunks,
        }
        log.info(
            "index.job_succeeded",
            extra={"job_id": str(job_id), "snapshot_id": str(persisted.snapshot_id), **build.stats.as_dict()},
        )
        return result
    except (IndexingError, GitSourceError, UnsafePathError) as exc:
        message = str(exc) if not isinstance(exc, UnsafePathError) else "путь репозитория вне разрешённых корней"
    except IntegrityError:
        # Параллельная сборка того же commit успела раньше (uq_code_index_snapshots_commit).
        message = "снимок этого commit уже построен параллельным заданием"
    except Exception:  # граница задания: любая ошибка → FAILED, детали только в журнале
        log.exception("index.job_failed", extra={"job_id": str(job_id)})
        message = "внутренняя ошибка индексации"
    await _finish(engine, ctx, job_id, "FAILED", error=message)
    result.status = "FAILED"
    result.error = message
    return result
