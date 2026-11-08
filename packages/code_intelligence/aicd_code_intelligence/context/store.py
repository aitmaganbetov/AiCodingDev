"""Запись Context Pack и журнала retrieval (P5-06; DATABASE_DESIGN §10.6–§10.8, миграция 0007).

* `retrieval_queries` — по строке на выполненную стратегию (exact/symbol/graph/fts/trigram/docs) попытки;
  `retrieval_results` — найденные фрагменты индекса (`knowledge_chunks`) с рангом, `selected` (вошёл в pack
  по бюджету) и `is_stale` (commit фрагмента ≠ base_commit задачи). Разделы документации не являются
  `knowledge_chunks` (импортёр их не создаёт) — для `docs` пишется только запрос, ссылка на раздел — в manifest.
* `context_packs` — строка на попытку (`uq_context_packs_attempt`). **Кэш:** при совпадении
  `content_hash + source_commit + policy_version` (`ix_context_packs_cache`) новая строка ссылается на artifact
  ранее собранного pack (FK `fk_context_packs_artifact` это допускает — artifact только workspace-уровня
  ссылочно), новый artifact не создаётся; `manifest.cache` фиксирует попадание. Artifact кэша проверяется:
  не истёк (`expires_at`), объект присутствует в хранилище; иначе собирается заново.
* Без снимка индекса (`snapshot_id NOT NULL`) строка `context_packs` невозможна — `store_context_pack`
  возвращает `None`, исполнение продолжается без кода (решение вызывающего, предупреждение — в manifest промпта).

Повторная попытка той же `task_attempt_id` (повторная доставка исполнения) не создаёт дубль: возвращается
существующая строка.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_code_intelligence.context.retrieval import ContextPack
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.ids import uuid7


@dataclass(frozen=True)
class StoredPack:
    context_pack_id: uuid.UUID
    artifact_id: uuid.UUID
    sha256: str
    size_bytes: int
    cache_hit: bool


async def record_retrieval(
    conn: AsyncConnection,
    pack: ContextPack,
    *,
    run_id: uuid.UUID,
    task_id: uuid.UUID,
    task_attempt_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Журнал поиска попытки. Возвращает id записанных `retrieval_queries`."""
    ws = pack.request.workspace_id
    selected = {f.key() for f in pack.fragments}
    ids: list[uuid.UUID] = []
    for q in pack.queries:
        if not q.query_text:
            continue
        qid = uuid7()
        await conn.execute(
            text(
                "INSERT INTO retrieval_queries (id, workspace_id, run_id, task_id, task_attempt_id, query_text, strategy,"
                " snapshot_id, documentation_mode) VALUES (:id, :ws, :run, :task, :att, :q, :strategy, :snap, :mode)"
            ),
            {
                "id": qid,
                "ws": ws,
                "run": run_id,
                "task": task_id,
                "att": task_attempt_id,
                "q": q.query_text[:4000],
                "strategy": q.strategy,
                "snap": pack.snapshot_id,
                "mode": pack.request.documentation_mode,
            },
        )
        rows = [
            {
                "ws": ws,
                "q": qid,
                "chunk": chunk_id,
                "rank": rank,
                "score": score,
                "selected": key in selected,
                "stale": pack.snapshot_commit is not None and pack.snapshot_commit != pack.request.base_commit,
            }
            for rank, (chunk_id, score, key) in enumerate(q.hits, start=1)
        ]
        if rows:
            await conn.execute(
                text(
                    "INSERT INTO retrieval_results (workspace_id, query_id, chunk_id, rank, score, selected, is_stale)"
                    " VALUES (:ws, :q, :chunk, :rank, :score, :selected, :stale)"
                ),
                rows,
            )
        ids.append(qid)
    return ids


async def _cached_artifact(
    conn: AsyncConnection, store: ArtifactStore, pack: ContextPack
) -> tuple[uuid.UUID, str, int, uuid.UUID] | None:
    row = (
        await conn.execute(
            text(
                "SELECT a.id, a.sha256, a.size_bytes, a.storage_ref, cp.id FROM context_packs cp"
                "  JOIN artifacts a ON a.workspace_id = cp.workspace_id AND a.id = cp.artifact_id"
                " WHERE cp.source_commit = :commit AND cp.policy_version = :ver AND cp.content_hash = :hash"
                "   AND (a.expires_at IS NULL OR a.expires_at > now() + interval '1 hour')"
                " ORDER BY cp.created_at, cp.id LIMIT 1"
            ),
            {"commit": pack.request.base_commit, "ver": pack.policy_version, "hash": pack.content_hash},
        )
    ).first()
    if row is None:
        return None
    if not await asyncio.to_thread(store.exists, row[3]):
        return None
    return row[0], row[1], int(row[2]), row[4]


async def store_context_pack(
    conn: AsyncConnection,
    store: ArtifactStore,
    pack: ContextPack,
    *,
    task_attempt_id: uuid.UUID,
    run_id: uuid.UUID | None,
    extra_manifest: Mapping[str, Any] | None = None,
) -> StoredPack | None:
    """Строка `context_packs` попытки (+ artifact или ссылка на кэшированный). Без снимка — `None`."""
    if pack.snapshot_id is None:
        return None
    existing = (
        await conn.execute(
            text(
                "SELECT cp.id, cp.artifact_id, a.sha256, a.size_bytes FROM context_packs cp"
                "  JOIN artifacts a ON a.workspace_id = cp.workspace_id AND a.id = cp.artifact_id"
                " WHERE cp.task_attempt_id = :att"
            ),
            {"att": task_attempt_id},
        )
    ).first()
    if existing is not None:
        return StoredPack(existing[0], existing[1], existing[2], int(existing[3]), cache_hit=True)
    ws = pack.request.workspace_id
    cached = await _cached_artifact(conn, store, pack)
    cache_info: dict[str, Any]
    if cached is not None:
        artifact_id, sha256, size, source_pack = cached
        cache_info = {"hit": True, "source_context_pack_id": str(source_pack)}
    else:
        artifact_id = uuid7()
        data = pack.body()
        stored = await asyncio.to_thread(store.put, ws, artifact_id, io.BytesIO(data))
        await conn.execute(
            text(
                "INSERT INTO artifacts (id, workspace_id, project_id, run_id, kind, storage_ref, sha256, size_bytes,"
                " mime_type, is_sensitive) VALUES (:id, :ws, :p, :r, 'context_pack', :ref, :sha, :size,"
                " 'application/json', true)"
            ),
            {
                "id": artifact_id,
                "ws": ws,
                "p": pack.request.project_id,
                "r": run_id,
                "ref": stored.storage_ref,
                "sha": stored.sha256,
                "size": stored.size_bytes,
            },
        )
        sha256, size = stored.sha256, stored.size_bytes
        cache_info = {"hit": False}
    manifest = {**pack.manifest, **(extra_manifest or {}), "cache": cache_info, "artifact_sha256": sha256}
    pack_id = uuid7()
    budget = pack.request.effective_budget
    await conn.execute(
        text(
            "INSERT INTO context_packs (id, workspace_id, task_attempt_id, snapshot_id, source_commit, policy_version,"
            " documentation_mode, token_budget, size_bytes, manifest, content_hash, artifact_id)"
            " VALUES (:id, :ws, :att, :snap, :commit, :ver, :mode, :tokens, :size, CAST(:manifest AS jsonb), :hash, :art)"
        ),
        {
            "id": pack_id,
            "ws": ws,
            "att": task_attempt_id,
            "snap": pack.snapshot_id,
            "commit": pack.request.base_commit,
            "ver": pack.policy_version,
            "mode": pack.request.documentation_mode,
            "tokens": max(1, budget.max_bytes // 4),
            "size": pack.selection.size_bytes,
            "manifest": json.dumps(manifest, ensure_ascii=False, default=str),
            "hash": pack.content_hash,
            "art": artifact_id,
        },
    )
    return StoredPack(pack_id, artifact_id, sha256, size, cache_hit=bool(cache_info["hit"]))
