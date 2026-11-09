"""Read-модель индекса кода для API (B3, UI_REDESIGN_PLAN §3.6, G-06…G-09): поиск символов, граф зависимостей,
очередь индексации. Только чтение существующих таблиц миграции 0007 под RLS-контекстом workspace вызывающего.

* **Снимок.** Запросы адресуют один READY-снимок `(repository, commit, INDEX_VERSION)`: без `commit` — последний
  READY-снимок репозитория (`store.latest_ready_snapshot`), с `commit` — снимок, чей SHA начинается с переданного
  hex-префикса (≥ 7 символов). Неоднозначный префикс — `AmbiguousCommitError`.
* **Поиск символов** — ранжированное объединение (лучшая оценка на символ): точное имя (1.0) > имя без учёта
  регистра (0.95) > префикс имени (0.8) > подстрока имени/qualified name (0.6) > pg_trgm-сходство имени
  (`%`, ×0.5, индекс `ix_code_symbols_name_trgm`) > FTS по фрагментам символа (`knowledge_chunks.search_vector`,
  ≤ 0.45, разбиение идентификаторов — как у `search.fts_search`). Все значения — bind-параметры; `%`/`_` в запросе
  экранируются для ILIKE. Порядок детерминирован (оценка, имя, путь, строка, id), пагинация — смещение внутри
  неизменяемого снимка.
* **Граф** — узлы-файлы (рёбра хранятся между символами `module`, см. `graph`), внешние ссылки (`target_ref` без
  разрешённого файла) — отдельные узлы-листья. Обход в ширину от корней (`path` — файл или каталог, `symbol` —
  файл символа) на глубину ≤ 3 в направлении out/in/both с жёсткими пределами узлов/рёбер (`truncated`).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_code_intelligence.index import INDEX_VERSION
from aicd_code_intelligence.index.chunks import TS_CONFIG, split_identifiers
from aicd_code_intelligence.index.search import validate_query

COMMIT_PREFIX_RE = re.compile(r"^[0-9a-f]{7,64}$")
SYMBOL_KINDS = (
    "module", "class", "interface", "function", "method", "variable", "constant", "type", "enum", "test", "route",
    "other",
)  # fmt: skip
EDGE_TYPES = ("imports", "calls", "inherits", "implements", "references", "tests")
MAX_SYMBOL_RESULTS = 1000  # глубина пагинации поиска символов (смещение внутри снимка)
MAX_GRAPH_DEPTH = 3
MAX_GRAPH_ROOTS = 50
MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 500

Direction = Literal["out", "in", "both"]


class AmbiguousCommitError(ValueError):
    """Префикс SHA совпадает с несколькими проиндексированными commit."""


@dataclass(frozen=True)
class SnapshotRef:
    id: uuid.UUID
    commit_sha: str
    status: str
    index_version: int
    file_count: int | None
    symbol_count: int | None
    completed_at: datetime | None

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "commit_sha": self.commit_sha,
            "status": self.status,
            "index_version": self.index_version,
            "file_count": self.file_count,
            "symbol_count": self.symbol_count,
            "completed_at": self.completed_at,
        }


_SNAPSHOT_COLUMNS = "id, commit_sha, status, index_version, file_count, symbol_count, completed_at"


def _snapshot(row: Any) -> SnapshotRef:
    return SnapshotRef(row[0], row[1], row[2], int(row[3]), row[4], row[5], row[6])


async def resolve_snapshot(
    conn: AsyncConnection, *, repository_id: uuid.UUID, commit: str | None
) -> SnapshotRef | None:
    """READY-снимок текущей версии индекса: последний или по hex-префиксу SHA. Нет снимка — `None`."""
    if commit is None:
        row = (
            await conn.execute(
                text(
                    f"SELECT {_SNAPSHOT_COLUMNS} FROM code_index_snapshots"  # noqa: S608 — подставляются только константы модуля
                    " WHERE repository_id = :repo AND index_version = :ver AND status = 'READY'"
                    " ORDER BY completed_at DESC, id DESC LIMIT 1"
                ),
                {"repo": repository_id, "ver": INDEX_VERSION},
            )
        ).first()
        return _snapshot(row) if row else None
    if not COMMIT_PREFIX_RE.match(commit):
        raise ValueError("commit должен быть hex SHA (7–64 символа)")
    rows = (
        await conn.execute(
            text(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM code_index_snapshots"  # noqa: S608 — подставляются только константы модуля
                " WHERE repository_id = :repo AND index_version = :ver AND status = 'READY'"
                "   AND starts_with(commit_sha::text, :prefix)"
                " ORDER BY completed_at DESC, id DESC LIMIT 2"
            ),
            {"repo": repository_id, "ver": INDEX_VERSION, "prefix": commit},
        )
    ).all()
    if len(rows) > 1:
        raise AmbiguousCommitError("префикс commit неоднозначен")
    return _snapshot(rows[0]) if rows else None


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def path_filter_params(path: str) -> dict[str, str]:
    """Файл или каталог: точное совпадение пути либо префикс `<path>/` (LIKE с экранированием)."""
    return {"path": path, "path_prefix": _like_escape(path) + "/%"}


# --- Поиск символов ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolHit:
    id: uuid.UUID
    name: str
    qualified_name: str | None
    kind: str
    path: str
    start_line: int
    end_line: int
    signature: str | None
    score: float
    match: str  # name | text


async def search_symbols(
    conn: AsyncConnection,
    *,
    snapshot_id: uuid.UUID,
    query: str,
    kind: str | None = None,
    path: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> list[SymbolHit]:
    """Ранжированный поиск символов снимка (см. docstring модуля). Возвращает до `limit` результатов."""
    validate_query(query)
    if kind is not None and kind not in SYMBOL_KINDS:
        raise ValueError("неизвестный вид символа")
    escaped = _like_escape(query)
    params: dict[str, Any] = {
        "snap": snapshot_id,
        "q": query,
        "prefix": escaped + "%",
        "contains": "%" + escaped + "%",
        "cfg": TS_CONFIG,
        "parts": split_identifiers(query),
        "kind": kind,
        "off": max(0, offset),
        "lim": max(1, min(limit, 200)),
    }
    path_sql = ""
    if path:
        params.update(path_filter_params(path))
        path_sql = " AND (s.file_path = :path OR s.file_path LIKE :path_prefix ESCAPE '\\')"
    rows = await conn.execute(
        text(
            "WITH q AS (SELECT websearch_to_tsquery(CAST(:cfg AS regconfig), :q) AS full_q,"  # noqa: S608 — подставляются только константы модуля
            "                  websearch_to_tsquery(CAST(:cfg AS regconfig), :parts) AS parts_q),"
            " cand AS ("
            "  SELECT s.id, CASE"
            "           WHEN s.symbol_name = :q THEN 1.0"
            "           WHEN lower(s.symbol_name) = lower(:q) THEN 0.95"
            "           WHEN s.symbol_name ILIKE :prefix ESCAPE '\\' THEN 0.8"
            "           WHEN s.symbol_name ILIKE :contains ESCAPE '\\' OR s.qualified_name ILIKE :contains ESCAPE '\\'"
            "             THEN 0.6"
            "           ELSE 0.5 * similarity(s.symbol_name, :q) END AS score, 'name' AS match"
            "    FROM code_symbols s"
            "   WHERE s.snapshot_id = :snap"
            "     AND (s.symbol_name ILIKE :contains ESCAPE '\\' OR s.symbol_name % :q"
            "          OR s.qualified_name ILIKE :contains ESCAPE '\\')"
            "  UNION ALL"
            "  SELECT c.source_id, LEAST(0.45, 0.2 + ts_rank_cd(c.search_vector, q.full_q)"
            "                                  + 0.5 * ts_rank_cd(c.search_vector, q.parts_q)), 'text'"
            "    FROM knowledge_chunks c, q"
            "   WHERE c.snapshot_id = :snap AND c.source_type = 'code_symbol'"
            "     AND (c.search_vector @@ q.full_q OR (:parts <> '' AND c.search_vector @@ q.parts_q))"
            " ), best AS ("
            "  SELECT DISTINCT ON (id) id, score, match FROM cand ORDER BY id, score DESC"
            " )"
            " SELECT s.id, s.symbol_name, s.qualified_name, s.symbol_kind, s.file_path, s.start_line, s.end_line,"
            "        s.signature, b.score, b.match"
            "   FROM best b JOIN code_symbols s ON s.id = b.id AND s.snapshot_id = :snap"
            "  WHERE (CAST(:kind AS text) IS NULL OR s.symbol_kind = :kind)"
            + path_sql
            + "  ORDER BY b.score DESC, s.symbol_name, s.file_path, s.start_line, s.id"
            "  OFFSET :off LIMIT :lim"
        ),
        params,
    )
    return [
        SymbolHit(r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6]), r[7], round(float(r[8]), 4), r[9]) for r in rows
    ]


# --- Граф зависимостей ------------------------------------------------------------------------------------


class GraphRootNotFoundError(LookupError):
    """Корень графа (путь/символ) отсутствует в снимке."""


@dataclass
class DependencyGraph:
    roots: list[str] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class _FileNode:
    id: uuid.UUID
    path: str
    language: str | None
    is_test: bool


async def _root_files(
    conn: AsyncConnection, snapshot_id: uuid.UUID, *, path: str | None, symbol_id: uuid.UUID | None
) -> tuple[list[_FileNode], bool]:
    if symbol_id is not None:
        rows = (
            await conn.execute(
                text(
                    "SELECT f.id, f.path, f.language, f.is_test FROM code_symbols s"
                    "  JOIN code_files f ON f.id = s.file_id AND f.snapshot_id = s.snapshot_id"
                    " WHERE s.snapshot_id = :snap AND s.id = :sym"
                ),
                {"snap": snapshot_id, "sym": symbol_id},
            )
        ).all()
    else:
        if path is None:
            raise ValueError("нужен path или symbol")
        rows = (
            await conn.execute(
                text(
                    "SELECT id, path, language, is_test FROM code_files"
                    " WHERE snapshot_id = :snap AND (path = :path OR path LIKE :path_prefix ESCAPE '\\')"
                    " ORDER BY path LIMIT :lim"
                ),
                {"snap": snapshot_id, **path_filter_params(path), "lim": MAX_GRAPH_ROOTS + 1},
            )
        ).all()
    if not rows:
        raise GraphRootNotFoundError("корень графа отсутствует в снимке")
    files = [_FileNode(r[0], r[1], r[2], bool(r[3])) for r in rows]
    return files[:MAX_GRAPH_ROOTS], len(files) > MAX_GRAPH_ROOTS


_EDGE_SELECT = (
    "SELECT d.id, sf.id, sf.path, sf.language, sf.is_test, tf.id, tf.path, tf.language, tf.is_test,"
    "       d.target_ref, d.edge_type"
)
_EDGE_JOINS = (
    "  JOIN code_symbols s ON s.id = d.source_symbol_id AND s.snapshot_id = d.snapshot_id"
    "  JOIN code_files sf ON sf.id = s.file_id AND sf.snapshot_id = d.snapshot_id"
    "  LEFT JOIN code_symbols t ON t.id = d.target_symbol_id AND t.snapshot_id = d.snapshot_id"
    "  LEFT JOIN code_files tf ON tf.id = t.file_id AND tf.snapshot_id = d.snapshot_id"
)
_EDGE_ORDER = " ORDER BY d.edge_type, sf.path, tf.path NULLS LAST, d.target_ref NULLS LAST, d.id LIMIT :lim"


async def _level_edges(
    conn: AsyncConnection, snapshot_id: uuid.UUID, file_ids: list[uuid.UUID], direction: str, limit: int
) -> tuple[list[Any], bool]:
    """Рёбра уровня BFS; второй элемент — выборка какого-либо направления упёрлась в `limit` (граф неполон)."""
    rows: list[Any] = []
    hit_limit = False
    if direction in ("out", "both"):
        rows += (
            await conn.execute(
                text(
                    _EDGE_SELECT + " FROM code_symbols src"
                    "  JOIN code_dependencies d ON d.snapshot_id = src.snapshot_id AND d.source_symbol_id = src.id"
                    + _EDGE_JOINS
                    + " WHERE src.snapshot_id = :snap AND src.file_id = ANY(:files)"
                    + _EDGE_ORDER
                ),
                {"snap": snapshot_id, "files": file_ids, "lim": limit},
            )
        ).all()
        hit_limit = len(rows) >= limit
    if direction in ("in", "both"):
        incoming = (
            await conn.execute(
                text(
                    _EDGE_SELECT + " FROM code_symbols dst"
                    "  JOIN code_dependencies d ON d.snapshot_id = dst.snapshot_id AND d.target_symbol_id = dst.id"
                    + _EDGE_JOINS
                    + " WHERE dst.snapshot_id = :snap AND dst.file_id = ANY(:files)"
                    + _EDGE_ORDER
                ),
                {"snap": snapshot_id, "files": file_ids, "lim": limit},
            )
        ).all()
        hit_limit = hit_limit or len(incoming) >= limit
        rows += incoming
    return rows, hit_limit


def external_node_id(ref: str) -> str:
    return "external:" + ref


async def dependency_graph(
    conn: AsyncConnection,
    *,
    snapshot_id: uuid.UUID,
    path: str | None = None,
    symbol_id: uuid.UUID | None = None,
    depth: int = 1,
    direction: Direction = "both",
    max_nodes: int = MAX_GRAPH_NODES,
    max_edges: int = MAX_GRAPH_EDGES,
) -> DependencyGraph:
    """Ограниченный подграф зависимостей файлов вокруг корней (BFS). Узел файла — `id = path`."""
    if (path is None) == (symbol_id is None):
        raise ValueError("нужен ровно один корень: path или symbol")
    if not 1 <= depth <= MAX_GRAPH_DEPTH:
        raise ValueError("depth вне диапазона")
    if direction not in ("out", "in", "both"):
        raise ValueError("direction: out | in | both")
    roots, truncated = await _root_files(conn, snapshot_id, path=path, symbol_id=symbol_id)
    graph = DependencyGraph(roots=[f.path for f in roots], truncated=truncated)
    nodes: dict[str, dict[str, Any]] = {}
    file_ids: dict[str, uuid.UUID] = {}

    def add_file(fid: uuid.UUID, fpath: str, language: str | None, is_test: bool, level: int) -> bool:
        if fpath in nodes:
            return True
        if len(nodes) >= max_nodes:
            graph.truncated = True
            return False
        nodes[fpath] = {
            "id": fpath,
            "kind": "file",
            "path": fpath,
            "ref": None,
            "language": language,
            "is_test": is_test,
            "depth": level,
        }
        file_ids[fpath] = fid
        return True

    for f in roots:
        add_file(f.id, f.path, f.language, f.is_test, 0)
    seen_edges: set[tuple[str, str, str]] = set()
    frontier = [f.path for f in roots if f.path in nodes]
    for level in range(1, depth + 1):
        if not frontier or (graph.truncated and len(nodes) >= max_nodes):
            break
        fetch_limit = max_edges + 1
        rows, hit_limit = await _level_edges(conn, snapshot_id, [file_ids[p] for p in frontier], direction, fetch_limit)
        if hit_limit:
            graph.truncated = True
        next_frontier: list[str] = []
        for r in rows:
            _eid, sid, spath, slang, stest, tid, tpath, tlang, ttest, ref, edge_type = r
            if tpath is not None and tpath == spath:
                continue
            target_key = tpath if tpath is not None else external_node_id(ref or "")
            key = (spath, target_key, edge_type)
            if key in seen_edges:
                continue
            if len(graph.edges) >= max_edges:
                graph.truncated = True
                break
            ok = add_file(sid, spath, slang, bool(stest), level)
            if ok and spath not in frontier and spath not in next_frontier and nodes[spath]["depth"] == level:
                next_frontier.append(spath)
            if tpath is not None:
                ok = add_file(tid, tpath, tlang, bool(ttest), level) and ok
                if ok and tpath not in frontier and tpath not in next_frontier and nodes[tpath]["depth"] == level:
                    next_frontier.append(tpath)
            elif ref:
                if target_key not in nodes:
                    if len(nodes) >= max_nodes:
                        graph.truncated = True
                        ok = False
                    else:
                        nodes[target_key] = {
                            "id": target_key,
                            "kind": "external",
                            "path": None,
                            "ref": ref,
                            "language": None,
                            "is_test": False,
                            "depth": level,
                        }
            else:
                continue
            if not ok:
                continue
            seen_edges.add(key)
            graph.edges.append({"source": spath, "target": target_key, "edge_type": edge_type})
        frontier = next_frontier
    graph.nodes = sorted(nodes.values(), key=lambda n: (n["depth"], n["kind"], n["id"]))
    graph.edges.sort(key=lambda e: (e["edge_type"], e["source"], e["target"]))
    return graph


# --- Очередь индексации -----------------------------------------------------------------------------------

JOB_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")


async def list_indexing_jobs(
    conn: AsyncConnection,
    *,
    repository_id: uuid.UUID,
    status: str | None = None,
    before: tuple[datetime, uuid.UUID] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Задания индексации репозитория, новые первыми (keyset `(created_at, id)` desc)."""
    if status is not None and status not in JOB_STATUSES:
        raise ValueError("неизвестный статус задания")
    rows = await conn.execute(
        text(
            "SELECT j.id, j.repository_id, j.commit_sha, j.kind, j.status, j.attempt_count, j.base_snapshot_id,"
            "       j.snapshot_id, j.created_at, j.started_at, j.finished_at, j.error,"
            "       s.file_count, s.symbol_count"
            "  FROM indexing_jobs j"
            "  LEFT JOIN code_index_snapshots s ON s.id = j.snapshot_id AND s.workspace_id = j.workspace_id"
            " WHERE j.repository_id = :repo"
            "   AND (CAST(:status AS text) IS NULL OR j.status = :status)"
            "   AND (CAST(:bt AS timestamptz) IS NULL OR (j.created_at, j.id) < (CAST(:bt AS timestamptz), CAST(:bi AS uuid)))"
            " ORDER BY j.created_at DESC, j.id DESC LIMIT :lim"
        ),
        {
            "repo": repository_id,
            "status": status,
            "bt": before[0] if before else None,
            "bi": before[1] if before else None,
            "lim": max(1, limit),
        },
    )
    return [dict(r) for r in rows.mappings()]
