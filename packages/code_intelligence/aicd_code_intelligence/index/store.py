"""Запись снимка индекса в PostgreSQL (P5-02; DATABASE_DESIGN §10.1–§10.5, миграция 0007).

Снимок записывается **одной транзакцией** (роль `aicd_worker`, RLS-контекст workspace): строка
`code_index_snapshots` (BUILDING) → `code_files` → `code_symbols` → `knowledge_chunks` →
`code_dependencies` → статус READY. При ошибке транзакция откатывается целиком: частичных снимков нет
(у worker нет DELETE на таблицы снимка — неизменяемость обеспечена правами, §19.4).

Переиспользованные файлы (`FileRecord.reused_from`) не разбираются заново: их символы и фрагменты
копируются из базового снимка с новыми id (`text_ref` ссылается на тот же blob — остаётся верным,
`search_vector` копируется как есть).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_code_intelligence.index import INDEX_VERSION
from aicd_code_intelligence.index.builder import PHP_DECLARED_KINDS, PreviousFile, SnapshotBuild
from aicd_code_intelligence.index.chunks import TS_CONFIG
from aicd_platform.ids import uuid7


@dataclass(frozen=True)
class PersistResult:
    snapshot_id: uuid.UUID
    file_count: int
    symbol_count: int
    chunk_count: int
    edge_count: int
    copied_symbols: int
    copied_chunks: int


async def latest_ready_snapshot(conn: AsyncConnection, *, repository_id: uuid.UUID) -> uuid.UUID | None:
    """Последний READY-снимок репозитория текущей `INDEX_VERSION` — база инкрементального построения."""
    row = (
        await conn.execute(
            text(
                "SELECT id FROM code_index_snapshots"
                " WHERE repository_id = :repo AND index_version = :ver AND status = 'READY'"
                " ORDER BY completed_at DESC, id DESC LIMIT 1"
            ),
            {"repo": repository_id, "ver": INDEX_VERSION},
        )
    ).first()
    return row[0] if row else None


async def load_previous(conn: AsyncConnection, *, snapshot_id: uuid.UUID) -> dict[str, PreviousFile]:
    files = await conn.execute(
        text(
            "SELECT f.id, f.path, f.blob_sha, f.content_hash, f.size_bytes, f.language, f.is_test, f.is_generated,"
            "       f.is_binary, m.qualified_name"
            "  FROM code_files f"
            "  LEFT JOIN code_symbols m ON m.file_id = f.id AND m.snapshot_id = f.snapshot_id AND m.symbol_kind = 'module'"
            " WHERE f.snapshot_id = :snap"
        ),
        {"snap": snapshot_id},
    )
    rows = files.all()
    imports: dict[uuid.UUID, list[str]] = {}
    for file_id, ref in await conn.execute(
        text(
            "SELECT m.file_id, d.target_ref FROM code_dependencies d"
            "  JOIN code_symbols m ON m.id = d.source_symbol_id AND m.snapshot_id = d.snapshot_id"
            " WHERE d.snapshot_id = :snap AND d.edge_type = 'imports' AND d.target_ref IS NOT NULL"
            " ORDER BY d.id"
        ),
        {"snap": snapshot_id},
    ):
        imports.setdefault(file_id, []).append(ref)
    declared: dict[uuid.UUID, list[str]] = {}
    for file_id, qualified in await conn.execute(
        text(
            "SELECT s.file_id, s.qualified_name FROM code_symbols s"
            "  JOIN code_files f ON f.id = s.file_id AND f.snapshot_id = s.snapshot_id"
            " WHERE s.snapshot_id = :snap AND f.language = 'php' AND s.symbol_kind = ANY(:kinds)"
            "   AND s.qualified_name IS NOT NULL"
            " ORDER BY s.file_id, s.start_line, s.id"
        ),
        {"snap": snapshot_id, "kinds": sorted(PHP_DECLARED_KINDS)},
    ):
        declared.setdefault(file_id, []).append(qualified)
    out: dict[str, PreviousFile] = {}
    for r in rows:
        out[r[1]] = PreviousFile(
            file_id=r[0],
            path=r[1],
            blob_sha=r[2],
            content_hash=r[3],
            size_bytes=int(r[4]),
            language=r[5],
            is_test=r[6],
            is_generated=r[7],
            is_binary=r[8],
            module_name=r[9],
            imports=tuple(imports.get(r[0], ())),
            declared=tuple(declared.get(r[0], ())),
        )
    return out


async def _insert_many(conn: AsyncConnection, sql: str, rows: Sequence[dict[str, Any]]) -> None:
    if rows:
        await conn.execute(text(sql), list(rows))


_INSERT_SYMBOL = (
    "INSERT INTO code_symbols (id, workspace_id, snapshot_id, file_id, file_path, symbol_name, qualified_name,"
    " symbol_kind, start_line, end_line, signature, content_hash)"
    " VALUES (:id, :ws, :snap, :file_id, :file_path, :name, :qualified, :kind, :start, :end, :signature, :hash)"
)
_INSERT_CHUNK_NEW = (
    "INSERT INTO knowledge_chunks (id, workspace_id, project_id, source_type, source_id, snapshot_id, chunk_index,"
    " text_ref, content_hash, token_estimate, search_vector, metadata)"
    " VALUES (:id, :ws, :project, :source_type, :source_id, :snap, :chunk_index, :text_ref, :hash, :tokens,"
    " to_tsvector(CAST(:cfg AS regconfig), :fts), CAST(:metadata AS jsonb))"
)
_INSERT_CHUNK_COPY = (
    "INSERT INTO knowledge_chunks (id, workspace_id, project_id, source_type, source_id, snapshot_id, chunk_index,"
    " text_ref, content_hash, token_estimate, search_vector, metadata)"
    " VALUES (:id, :ws, :project, :source_type, :source_id, :snap, :chunk_index, :text_ref, :hash, :tokens,"
    " CAST(:vector AS tsvector), CAST(:metadata AS jsonb))"
)


async def persist_snapshot(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    commit_sha: str,
    parent_snapshot_id: uuid.UUID | None,
    build: SnapshotBuild,
) -> PersistResult:
    snapshot_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO code_index_snapshots (id, workspace_id, repository_id, commit_sha, index_version,"
            " parent_snapshot_id, status) VALUES (:id, :ws, :repo, :sha, :ver, :parent, 'BUILDING')"
        ),
        {
            "id": snapshot_id,
            "ws": workspace_id,
            "repo": repository_id,
            "sha": commit_sha,
            "ver": INDEX_VERSION,
            "parent": parent_snapshot_id,
        },
    )
    base = {"ws": workspace_id, "snap": snapshot_id}

    # 1. Файлы
    file_ids: dict[str, uuid.UUID] = {}
    file_rows = []
    for rec in build.files:
        fid = uuid7()
        file_ids[rec.path] = fid
        file_rows.append(
            {
                **base,
                "id": fid,
                "path": rec.path,
                "language": rec.language,
                "blob": rec.blob_sha,
                "hash": rec.content_hash,
                "size": rec.size_bytes,
                "is_test": rec.is_test,
                "is_generated": rec.is_generated,
                "is_binary": rec.is_binary,
            }
        )
    await _insert_many(
        conn,
        "INSERT INTO code_files (id, workspace_id, snapshot_id, path, language, blob_sha, content_hash, size_bytes,"
        " is_test, is_generated, is_binary) VALUES (:id, :ws, :snap, :path, :language, :blob, :hash, :size,"
        " :is_test, :is_generated, :is_binary)",
        file_rows,
    )

    # 2. Символы: новые (разобранные) и скопированные из базового снимка
    module_symbol: dict[str, uuid.UUID] = {}
    symbol_rows: list[dict[str, Any]] = []
    new_symbol_ids: dict[str, list[uuid.UUID]] = {}
    for rec in build.files:
        if rec.analysis is None:
            continue
        ids = []
        for sym in rec.analysis.symbols:
            sid = uuid7()
            ids.append(sid)
            if sym.kind == "module":
                module_symbol[rec.path] = sid
            symbol_rows.append(
                {
                    **base,
                    "id": sid,
                    "file_id": file_ids[rec.path],
                    "file_path": rec.path,
                    "name": sym.name,
                    "qualified": sym.qualified_name,
                    "kind": sym.kind,
                    "start": sym.start_line,
                    "end": sym.end_line,
                    "signature": sym.signature,
                    "hash": sym.content_hash,
                }
            )
        new_symbol_ids[rec.path] = ids

    reused = {rec.reused_from.file_id: rec.path for rec in build.files if rec.reused_from and rec.reused_from.file_id}
    symbol_map: dict[uuid.UUID, uuid.UUID] = {}
    if reused and parent_snapshot_id is not None:
        result = await conn.execute(
            text(
                "SELECT id, file_id, symbol_name, qualified_name, symbol_kind, start_line, end_line, signature,"
                "       content_hash"
                "  FROM code_symbols WHERE snapshot_id = :parent AND file_id = ANY(:files)"
                " ORDER BY file_id, start_line, id"
            ),
            {"parent": parent_snapshot_id, "files": list(reused)},
        )
        for r in result:
            path = reused[r[1]]
            sid = uuid7()
            symbol_map[r[0]] = sid
            if r[4] == "module":
                module_symbol[path] = sid
            symbol_rows.append(
                {
                    **base,
                    "id": sid,
                    "file_id": file_ids[path],
                    "file_path": path,
                    "name": r[2],
                    "qualified": r[3],
                    "kind": r[4],
                    "start": r[5],
                    "end": r[6],
                    "signature": r[7],
                    "hash": r[8],
                }
            )
    await _insert_many(conn, _INSERT_SYMBOL, symbol_rows)

    # 3. Фрагменты
    chunk_rows: list[dict[str, Any]] = []
    for rec in build.files:
        for chunk in rec.chunks:
            if chunk.source == "code_file":
                source_id = file_ids[rec.path]
            else:
                if chunk.symbol_index is None:  # pragma: no cover - инвариант build_chunks
                    continue
                source_id = new_symbol_ids[rec.path][chunk.symbol_index]
            chunk_rows.append(
                {
                    **base,
                    "id": uuid7(),
                    "project": project_id,
                    "source_type": chunk.source,
                    "source_id": source_id,
                    "chunk_index": chunk.chunk_index,
                    "text_ref": chunk.text_ref(rec.blob_sha),
                    "hash": chunk.content_hash,
                    "tokens": chunk.token_estimate,
                    "cfg": TS_CONFIG,
                    "fts": chunk.fts_text,
                    "metadata": json.dumps({**chunk.metadata, "commit_sha": commit_sha}, ensure_ascii=False),
                }
            )
    await _insert_many(conn, _INSERT_CHUNK_NEW, chunk_rows)

    copied_chunks: list[dict[str, Any]] = []
    if reused and parent_snapshot_id is not None:
        file_map = {old: file_ids[path] for old, path in reused.items()}
        result = await conn.execute(
            text(
                "SELECT source_type, source_id, chunk_index, text_ref, content_hash, token_estimate,"
                "       search_vector::text, metadata"
                "  FROM knowledge_chunks"
                " WHERE snapshot_id = :parent AND ((source_type = 'code_file' AND source_id = ANY(:files))"
                "    OR (source_type = 'code_symbol' AND source_id = ANY(:symbols)))"
                " ORDER BY source_type, source_id, chunk_index"
            ),
            {"parent": parent_snapshot_id, "files": list(file_map), "symbols": list(symbol_map)},
        )
        for r in result:
            new_source = file_map[r[1]] if r[0] == "code_file" else symbol_map[r[1]]
            metadata = dict(r[7])
            metadata["commit_sha"] = commit_sha
            copied_chunks.append(
                {
                    **base,
                    "id": uuid7(),
                    "project": project_id,
                    "source_type": r[0],
                    "source_id": new_source,
                    "chunk_index": r[2],
                    "text_ref": r[3],
                    "hash": r[4],
                    "tokens": r[5],
                    "vector": r[6],
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }
            )
    await _insert_many(conn, _INSERT_CHUNK_COPY, copied_chunks)

    # 4. Рёбра графа (импорты переразрешены для всего снимка, test links)
    edge_rows = []
    for edge in build.edges:
        source = module_symbol.get(edge.source_path)
        if source is None:
            continue
        target = module_symbol.get(edge.target_path) if edge.target_path else None
        if target is None and edge.target_ref is None:
            continue
        edge_rows.append(
            {**base, "id": uuid7(), "source": source, "target": target, "ref": edge.target_ref, "type": edge.edge_type}
        )
    await _insert_many(
        conn,
        "INSERT INTO code_dependencies (id, workspace_id, snapshot_id, source_symbol_id, target_symbol_id,"
        " target_ref, edge_type) VALUES (:id, :ws, :snap, :source, :target, :ref, :type)",
        edge_rows,
    )

    # 5. Готово
    await conn.execute(
        text(
            "UPDATE code_index_snapshots SET status = 'READY', completed_at = now(), file_count = :files,"
            " symbol_count = :symbols WHERE id = :id"
        ),
        {"id": snapshot_id, "files": len(file_rows), "symbols": len(symbol_rows)},
    )
    # Снимки того же commit более старой версии индексатора больше не используются для поиска.
    await conn.execute(
        text(
            "UPDATE code_index_snapshots SET status = 'SUPERSEDED'"
            " WHERE repository_id = :repo AND commit_sha = :sha AND index_version < :ver AND status = 'READY'"
        ),
        {"repo": repository_id, "sha": commit_sha, "ver": INDEX_VERSION},
    )
    return PersistResult(
        snapshot_id=snapshot_id,
        file_count=len(file_rows),
        symbol_count=len(symbol_rows),
        chunk_count=len(chunk_rows) + len(copied_chunks),
        edge_count=len(edge_rows),
        copied_symbols=len(symbol_map),
        copied_chunks=len(copied_chunks),
    )
