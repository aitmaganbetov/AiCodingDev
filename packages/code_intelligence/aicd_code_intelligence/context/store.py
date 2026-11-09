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
import re
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


# --- Чтение (B3, G-08): manifest Context Pack попытки для API --------------------------------------------------

_SECTIONS = frozenset({"code", "symbols_graph", "tests", "repo_map", "docs"})
_KINDS = frozenset({"map", "exact", "dependency", "task_relevance", "recency", "docs"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WARNING_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_MANIFEST_ENTRIES = 2000


async def load_context_pack(conn: AsyncConnection, *, task_attempt_id: uuid.UUID) -> dict[str, Any] | None:
    """Строка `context_packs` попытки (RLS workspace) и commit её снимка; нет pack — `None`."""
    row = (
        (
            await conn.execute(
                text(
                    "SELECT cp.id, cp.task_attempt_id, cp.snapshot_id, cp.source_commit, cp.policy_version,"
                    "       cp.documentation_mode, cp.token_budget, cp.size_bytes, cp.manifest, cp.content_hash,"
                    "       cp.artifact_id, cp.created_at, s.commit_sha AS snapshot_commit"
                    "  FROM context_packs cp"
                    "  LEFT JOIN code_index_snapshots s ON s.id = cp.snapshot_id AND s.workspace_id = cp.workspace_id"
                    " WHERE cp.task_attempt_id = :att"
                ),
                {"att": task_attempt_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _str(value: Any, limit: int = 4096) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _lines(value: Any) -> list[int] | None:
    if isinstance(value, list) and len(value) == 2 and all(_int(v) is not None and v >= 1 for v in value):
        return [int(value[0]), int(value[1])]
    return None


def _sha(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.match(value) else None


def manifest_view(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Безопасное подмножество manifest: состав pack без текста фрагментов.

    Только перечисленные поля фрагментов (section/kind/path/lines/commit/sha256/bytes/stale/source_ref) и
    пропущенных (section/kind/path/commit/reason); предупреждения — только коды (`git_read_failed:<текст>` →
    `git_read_failed`): текст исключений источников может содержать сведения о хосте. Термины задачи не отдаются.
    """
    fragments: list[dict[str, Any]] = []
    raw_fragments = manifest.get("fragments")
    for f in raw_fragments if isinstance(raw_fragments, list) else []:
        if not isinstance(f, Mapping) or len(fragments) >= MAX_MANIFEST_ENTRIES:
            continue
        section, kind, path = f.get("section"), f.get("kind"), _str(f.get("path"))
        if section not in _SECTIONS or kind not in _KINDS or path is None:
            continue
        fragments.append(
            {
                "section": section,
                "kind": kind,
                "path": path,
                "lines": _lines(f.get("lines")),
                "commit": _sha(f.get("commit"), _SHA_RE),
                "sha256": _sha(f.get("sha256"), _SHA256_RE),
                "bytes": _int(f.get("bytes")),
                "stale": f.get("stale") is True,
                "source_ref": _str(f.get("source_ref"), 255),
            }
        )
    omitted: list[dict[str, Any]] = []
    raw_omitted = manifest.get("omitted")
    for o in raw_omitted if isinstance(raw_omitted, list) else []:
        if not isinstance(o, Mapping) or len(omitted) >= MAX_MANIFEST_ENTRIES:
            continue
        section, kind, path, reason = o.get("section"), o.get("kind"), _str(o.get("path")), _str(o.get("reason"), 64)
        if section not in _SECTIONS or kind not in _KINDS or path is None or reason is None:
            continue
        omitted.append(
            {
                "section": section,
                "kind": kind,
                "path": path,
                "commit": _sha(o.get("commit"), _SHA_RE),
                "reason": reason,
            }
        )
    warnings: list[str] = []
    raw_warnings = manifest.get("warnings")
    for w in raw_warnings if isinstance(raw_warnings, list) else []:
        code = w.split(":", 1)[0] if isinstance(w, str) else ""
        if _WARNING_CODE_RE.match(code) and code not in warnings:
            warnings.append(code)
    budget = manifest.get("budget")
    budget_view = None
    if isinstance(budget, Mapping):
        budget_view = {k: _int(budget.get(k)) for k in ("max_bytes", "max_files", "max_fragment_bytes")}
    cache = manifest.get("cache")
    stale_policy = manifest.get("stale_policy")
    return {
        "schema": _int(manifest.get("schema")),
        "stale_policy": stale_policy if stale_policy in ("exclude", "mark") else None,
        "role": _str(manifest.get("role"), 64),
        "docs": manifest.get("docs") if manifest.get("docs") in ("allowed", "off") else None,
        "budget": budget_view,
        "cache_hit": bool(cache.get("hit")) if isinstance(cache, Mapping) else None,
        "fragments": fragments,
        "omitted": omitted,
        "warnings": warnings,
        "fragments_total": len(raw_fragments) if isinstance(raw_fragments, list) else 0,
        "omitted_total": len(raw_omitted) if isinstance(raw_omitted, list) else 0,
    }
