"""Retrieval и сборка Context Pack для попытки задачи (P5-06; DATA_FLOW §9, AGENT_ROLES §1.1, ADR-0005).

Вход — снимок индекса commit задачи `(repository, base_commit, INDEX_VERSION)`, текст задачи (title, objective,
instructions, acceptance criteria), роль, режим документации и «семена» (тексты inbox: FIX_REQUEST,
REVIEW_REQUEST и т. п.). Источники в порядке DATA_FLOW §9:

1. **exact** — термины задачи (идентификаторы camelCase/snake_case/qualified, `в обратных кавычках`, "в кавычках",
   пути файлов) → ripgrep (`index.search.ripgrep_search`, безопасный argv) по экспорту снимка; слова задачи,
   совпадающие с именами символов снимка (`code_symbols`), — определения символов; пути из задачи и inbox —
   файлы целиком (голова файла). Строка совпадения расширяется до наименьшего охватывающего символа
   (иначе окно ±10 строк);
2. **dependency** — соседи по графу (`code_dependencies`, `imports` в обе стороны) файлов-«фокусов» (лучшие
   exact-файлы), test links (`tests`) → раздел `tests`, контур символов и рёбер → раздел `symbols_graph`;
3. **task_relevance** — FTS (`knowledge_chunks.search_vector`) по словам задачи и pg_trgm по терминам;
4. **recency** — файлы, изменённые относительно базового (родительского) снимка;
5. **docs** — только при `documentation_mode` SMART/STRICT: разделы READY-снимков включённых и утверждённых
   источников (`documentation_sections`), текст — из artifact; в режиме OFF не запрашиваются вовсе (AC-10);
6. **repo_map** — сжатое дерево файлов и стек (`project_technologies`, только чтение таблиц).

Текст фрагментов читается из git blob снимка (`git cat-file --batch`, без checkout). Ранжирование, бюджет,
stale-политика и ключ кэша — `context.ranking`. Снимок commit задачи отсутствует ⇒ используется последний
READY-снимок репозитория, все его фрагменты устаревшие (`commit ≠ base`): по умолчанию исключаются
(`stale_policy="exclude"`, перечислены в `omitted`), при `mark` — включаются с `stale=true`.
Ошибки отдельных источников (ripgrep, git, artifact) не прерывают сборку — попадают в `warnings` manifest.

Модуль не знает об оркестрации (import-linter): запись в БД — `context.store`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_code_intelligence.context.ranking import (
    Budget,
    Fragment,
    PackSelection,
    Section,
    SourceKind,
    StalePolicy,
    content_hash,
    select,
)
from aicd_code_intelligence.index import INDEX_VERSION
from aicd_code_intelligence.index.gitsource import GitSourceError, TreeEntry, read_blobs
from aicd_code_intelligence.index.search import (
    ChunkHit,
    SearchError,
    SearchLimits,
    SearchQueryError,
    fts_search,
    materialize_export,
    ripgrep_search,
    trigram_search,
)
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_security.paths import UnsafePathError, resolve_ref

log = logging.getLogger("aicd.code_intelligence.context")

CONTEXT_POLICY_VERSION: Final = 1
"""Версия политики сборки (`context_packs.policy_version`): меняется при изменении источников/бюджетов/формата."""

KIB: Final = 1024
# Бюджеты по ролям (байты текста фрагментов, число файлов). Пишущие роли получают больше кода; reviewer/security —
# компактный контекст вокруг diff: их главный вход — REVIEW_REQUEST/diff в inbox (раздел inbox вне этого бюджета).
ROLE_BUDGETS: Final[Mapping[str, Budget]] = {
    "developer": Budget(max_bytes=192 * KIB, max_files=40),
    "maintenance": Budget(max_bytes=192 * KIB, max_files=40),
    "database": Budget(max_bytes=160 * KIB, max_files=32),
    "performance": Budget(max_bytes=160 * KIB, max_files=32),
    "devops": Budget(max_bytes=128 * KIB, max_files=32),
    "qa": Budget(max_bytes=128 * KIB, max_files=32),
    "architect": Budget(max_bytes=128 * KIB, max_files=40),
    "requirements": Budget(max_bytes=96 * KIB, max_files=30),
    "planner": Budget(max_bytes=96 * KIB, max_files=30),
    "documentation": Budget(max_bytes=96 * KIB, max_files=30),
    "reviewer": Budget(max_bytes=64 * KIB, max_files=16, max_fragment_bytes=16 * KIB),
    "security": Budget(max_bytes=64 * KIB, max_files=16, max_fragment_bytes=16 * KIB),
}
DEFAULT_BUDGET: Final = Budget(max_bytes=96 * KIB, max_files=30)

MAX_TERMS: Final = 12
MAX_FOCUS_FILES: Final = 6
MAX_RANGES_PER_FILE: Final = 4
HIT_CONTEXT_LINES: Final = 10
MAX_SYMBOL_LINES: Final = 150
HEAD_LINES: Final[Mapping[str, int]] = {"exact": 150, "dependency": 80, "tests": 150, "recency": 60}
REPO_MAP_MAX_CHARS: Final = 4000
DOC_SECTION_MAX_BYTES: Final = 12 * KIB
DOCS_LIMIT: Final = 5

_TERM_LIMITS: Final = SearchLimits(max_matches=60, max_matches_per_file=5, timeout_seconds=5.0)
_BACKTICK = re.compile(r"`([^`\n]{2,200})`")
_QUOTED = re.compile(r'"([^"\n]{3,120})"|«([^»\n]{3,120})»')
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::|\\)[A-Za-z_][A-Za-z0-9_]*)*")
_PATHLIKE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}(?![\w/])|(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+")
_WORD = re.compile(r"\w{3,}", re.UNICODE)
_STOP: Final = frozenset(
    {"the", "and", "for", "with", "that", "this", "from", "into", "must", "should", "file", "files", "test", "tests"}
)


def budget_for_role(role: str) -> Budget:
    return ROLE_BUDGETS.get(role, DEFAULT_BUDGET)


# --- Термины задачи --------------------------------------------------------------------------------------


def _strong_identifier(token: str) -> bool:
    """Идентификатор кода, а не слово естественного языка: `_`, внутренняя заглавная, цифра, квалификация."""
    if len(token) < 3 or len(token) > 200:
        return False
    if any(sep in token for sep in (".", "::", "\\")):
        parts = re.split(r"\.|::|\\", token)
        return len(parts) > 1 and all(parts) and not token.replace(".", "").isdigit()
    if "_" in token.strip("_"):
        return True
    return bool(re.search(r"[a-z][A-Z]|[A-Z][a-z]+[A-Z]", token)) or (
        bool(re.search(r"[A-Za-z]", token)) and bool(re.search(r"\d", token)) and token[0].isalpha()
    )


def _clean(term: str) -> str | None:
    term = term.strip().strip(".,;:!?()[]{}'")
    if not 2 <= len(term) <= 200 or any(ord(ch) < 0x20 or ch == "\x7f" for ch in term):
        return None
    return term


def extract_terms(texts: Iterable[str], *, limit: int = MAX_TERMS) -> list[str]:
    """Точные термины: `в кавычках` → идентификаторы кода → пути. Порядок стабилен, дубликаты убраны."""
    quoted: list[str] = []
    idents: list[str] = []
    for raw in texts:
        for m in _BACKTICK.finditer(raw):
            quoted.append(m.group(1))
        for m in _QUOTED.finditer(raw):
            quoted.append(m.group(1) or m.group(2))
        for m in _PATHLIKE.finditer(raw):
            if "/" in m.group(0) or re.search(r"\.[A-Za-z]{1,5}$", m.group(0)):
                idents.append(m.group(0))
        for m in _IDENT.finditer(raw):
            if _strong_identifier(m.group(0)):
                idents.append(m.group(0))
    out: list[str] = []
    seen: set[str] = set()
    for term in (*quoted, *idents):
        cleaned = _clean(term)
        if cleaned is None or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def extract_words(texts: Iterable[str], *, limit: int = 30) -> list[str]:
    """Слова задачи для FTS и сопоставления с именами символов (в нижнем регистре кроме исходного написания)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in texts:
        for m in _WORD.finditer(raw):
            word = m.group(0)
            if word.lower() in _STOP or word.isdigit() or word in seen:
                continue
            seen.add(word)
            out.append(word)
            if len(out) >= limit:
                return out
    return out


# --- Запрос и результат -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalRequest:
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    repository_id: uuid.UUID
    base_commit: str
    role: str
    task_text: str
    documentation_mode: str = "SMART"
    seed_texts: tuple[str, ...] = ()  # inbox и т. п.: пути и идентификаторы — дополнительные семена
    budget: Budget | None = None
    stale_policy: StalePolicy = "exclude"

    @property
    def effective_budget(self) -> Budget:
        return self.budget or budget_for_role(self.role)


@dataclass
class QueryLog:
    """Один поисковый запрос (`retrieval_queries`) и его результаты, сопоставленные фрагментам индекса."""

    strategy: str  # exact|symbol|graph|fts|trigram|docs (CHECK retrieval_queries.strategy)
    query_text: str
    hits: list[tuple[uuid.UUID, float, tuple[str, str, int | None, int | None]]] = field(default_factory=list)


@dataclass
class ContextPack:
    request: RetrievalRequest
    snapshot_id: uuid.UUID | None
    snapshot_commit: str | None
    selection: PackSelection
    content_hash: str
    manifest: dict[str, Any]
    queries: list[QueryLog] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    policy_version: int = CONTEXT_POLICY_VERSION

    @property
    def fragments(self) -> list[Fragment]:
        return self.selection.fragments

    def is_stale(self, frag: Fragment) -> bool:
        return frag.commit_sha != self.request.base_commit

    def body(self) -> bytes:
        """Содержимое artifact Context Pack: детерминировано по входам (одинаковые входы ⇒ тот же sha256)."""
        payload = {
            "schema": 1,
            "policy_version": self.policy_version,
            "base_commit": self.request.base_commit,
            "snapshot_commit": self.snapshot_commit,
            "stale_policy": self.request.stale_policy,
            "fragments": [
                {
                    "section": f.section,
                    "kind": f.kind,
                    "path": f.path,
                    "lines": [f.start_line, f.end_line] if f.start_line is not None else None,
                    "commit": f.commit_sha,
                    "stale": self.is_stale(f),
                    "sha256": f.sha256,
                    "text": f.text,
                }
                for f in self.fragments
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def stats(self) -> dict[str, Any]:
        by_section: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for f in self.fragments:
            by_section[f.section] = by_section.get(f.section, 0) + 1
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        return {
            "size_bytes": self.selection.size_bytes,
            "fragment_count": len(self.fragments),
            "file_count": len({f.path for f in self.fragments}),
            "stale_count": len(self.selection.stale),
            "omitted_count": len(self.selection.omitted),
            "by_section": by_section,
            "by_kind": by_kind,
        }


# --- Внутренние структуры ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _File:
    path: str
    blob_sha: str
    size: int
    is_test: bool
    language: str | None


@dataclass(frozen=True)
class _Symbol:
    path: str
    name: str
    kind: str
    start: int
    end: int
    signature: str | None


@dataclass
class _Candidate:
    """Будущий фрагмент: текст подставляется после чтения blob."""

    kind: SourceKind
    section: Section
    path: str
    start: int | None
    end: int | None
    score: float
    strategy: str | None = None  # для журнала retrieval_queries
    chunk_id: uuid.UUID | None = None
    text: str | None = None  # готовый текст (repo_map, symbols_graph, docs)
    source_ref: str | None = None


@dataclass
class _Snapshot:
    id: uuid.UUID
    commit: str
    parent_id: uuid.UUID | None


def _merge_ranges(ranges: Sequence[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    out: list[tuple[int, int, float]] = []
    for a, b, s in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            pa, pb, ps = out[-1]
            out[-1] = (pa, max(pb, b), ps + s)
        else:
            out.append((a, b, s))
    return out


def _enclosing(symbols: Sequence[_Symbol], line: int) -> tuple[int, int] | None:
    best: _Symbol | None = None
    for sym in symbols:
        if sym.start <= line <= sym.end and sym.end - sym.start <= MAX_SYMBOL_LINES:
            if best is None or sym.end - sym.start < best.end - best.start:
                best = sym
    return (best.start, best.end) if best else None


def _lines(content: bytes) -> list[str]:
    return content.decode("utf-8", "replace").split("\n")


def _section_for(file: _File | None, default: Section = "code") -> Section:
    return "tests" if file is not None and file.is_test else default


# --- Чтение БД --------------------------------------------------------------------------------------------


async def _resolve_snapshot(conn: AsyncConnection, repository_id: uuid.UUID, commit: str) -> _Snapshot | None:
    row = (
        await conn.execute(
            text(
                "SELECT id, commit_sha, parent_snapshot_id FROM code_index_snapshots"
                " WHERE repository_id = :repo AND index_version = :ver AND status = 'READY'"
                " ORDER BY (commit_sha = :sha) DESC, completed_at DESC, id DESC LIMIT 1"
            ),
            {"repo": repository_id, "ver": INDEX_VERSION, "sha": commit},
        )
    ).first()
    return _Snapshot(row[0], row[1], row[2]) if row else None


async def _load_files(conn: AsyncConnection, snapshot_id: uuid.UUID) -> dict[str, _File]:
    rows = await conn.execute(
        text(
            "SELECT path, blob_sha, size_bytes, is_test, language FROM code_files"
            " WHERE snapshot_id = :s AND NOT is_binary AND NOT is_generated ORDER BY path"
        ),
        {"s": snapshot_id},
    )
    return {r[0]: _File(r[0], r[1], int(r[2]), bool(r[3]), r[4]) for r in rows}


async def _load_symbols(conn: AsyncConnection, snapshot_id: uuid.UUID, paths: Sequence[str]) -> dict[str, list[_Symbol]]:
    out: dict[str, list[_Symbol]] = {}
    if not paths:
        return out
    rows = await conn.execute(
        text(
            "SELECT file_path, symbol_name, symbol_kind, start_line, end_line, signature FROM code_symbols"
            " WHERE snapshot_id = :s AND file_path = ANY(:paths) AND symbol_kind <> 'module'"
            " ORDER BY file_path, start_line, id"
        ),
        {"s": snapshot_id, "paths": list(paths)},
    )
    for r in rows:
        out.setdefault(r[0], []).append(_Symbol(r[0], r[1], r[2], int(r[3]), int(r[4]), r[5]))
    return out


async def _symbol_definitions(conn: AsyncConnection, snapshot_id: uuid.UUID, names: Sequence[str]) -> list[_Symbol]:
    if not names:
        return []
    rows = await conn.execute(
        text(
            "SELECT file_path, symbol_name, symbol_kind, start_line, end_line, signature FROM code_symbols"
            " WHERE snapshot_id = :s AND symbol_name = ANY(:names) AND symbol_kind <> 'module'"
            " ORDER BY file_path, start_line, id LIMIT 100"
        ),
        {"s": snapshot_id, "names": list(names)},
    )
    return [_Symbol(r[0], r[1], r[2], int(r[3]), int(r[4]), r[5]) for r in rows]


async def _edges(
    conn: AsyncConnection, snapshot_id: uuid.UUID, paths: Sequence[str]
) -> list[tuple[str, str, str]]:
    if not paths:
        return []
    rows = await conn.execute(
        text(
            "SELECT s.file_path, t.file_path, d.edge_type FROM code_dependencies d"
            "  JOIN code_symbols s ON s.id = d.source_symbol_id AND s.snapshot_id = d.snapshot_id"
            "  JOIN code_symbols t ON t.id = d.target_symbol_id AND t.snapshot_id = d.snapshot_id"
            " WHERE d.snapshot_id = :s AND d.edge_type IN ('imports', 'tests')"
            "   AND (s.file_path = ANY(:paths) OR t.file_path = ANY(:paths))"
            " ORDER BY d.edge_type, s.file_path, t.file_path LIMIT 500"
        ),
        {"s": snapshot_id, "paths": list(paths)},
    )
    return [(r[0], r[1], r[2]) for r in rows if r[0] != r[1]]


async def _file_chunks(
    conn: AsyncConnection, snapshot_id: uuid.UUID, paths: Sequence[str]
) -> dict[str, list[tuple[int, int, uuid.UUID]]]:
    out: dict[str, list[tuple[int, int, uuid.UUID]]] = {}
    if not paths:
        return out
    rows = await conn.execute(
        text(
            "SELECT metadata ->> 'path', (metadata ->> 'start_line')::int, (metadata ->> 'end_line')::int, id"
            "  FROM knowledge_chunks"
            " WHERE snapshot_id = :s AND source_type = 'code_file' AND metadata ->> 'path' = ANY(:paths)"
            " ORDER BY 1, 2"
        ),
        {"s": snapshot_id, "paths": list(paths)},
    )
    for r in rows:
        if r[1] is not None and r[2] is not None:
            out.setdefault(r[0], []).append((int(r[1]), int(r[2]), r[3]))
    return out


async def _changed_paths(conn: AsyncConnection, snap: _Snapshot, limit: int = 8) -> list[str]:
    if snap.parent_id is None:
        return []
    rows = await conn.execute(
        text(
            "SELECT f.path FROM code_files f"
            "  LEFT JOIN code_files p ON p.snapshot_id = :parent AND p.path = f.path"
            " WHERE f.snapshot_id = :s AND NOT f.is_binary AND NOT f.is_generated"
            "   AND (p.id IS NULL OR p.blob_sha <> f.blob_sha)"
            " ORDER BY f.path LIMIT :lim"
        ),
        {"s": snap.id, "parent": snap.parent_id, "lim": limit},
    )
    return [r[0] for r in rows]


async def _stack(conn: AsyncConnection, project_id: uuid.UUID) -> list[str]:
    rows = await conn.execute(
        text(
            "SELECT t.ecosystem, t.name, v.version_label, pt.detected_from, pt.confirmed_at IS NOT NULL"
            "  FROM project_technologies pt"
            "  JOIN technology_versions v ON v.id = pt.technology_version_id"
            "  JOIN technologies t ON t.id = v.technology_id"
            " WHERE pt.project_id = :p ORDER BY t.ecosystem, t.name, v.version_label LIMIT 60"
        ),
        {"p": project_id},
    )
    return [
        f"{r[0]}: {r[1]} {r[2]} ({r[3]}{', подтверждено' if r[4] else ''})" for r in rows
    ]


async def _doc_sections(conn: AsyncConnection, words: Sequence[str]) -> list[dict[str, Any]]:
    """Разделы утверждённых документаций: READY-снимок включённого источника, approval (если есть) APPROVED."""
    rows = (
        await conn.execute(
            text(
                "SELECT ds.id, ds.section_path, ds.heading, ds.body_ref, ds.content_hash, a.storage_ref, a.sha256,"
                "       src.uri_or_file_ref"
                "  FROM documentation_sections ds"
                "  JOIN documentation_snapshots sn ON sn.id = ds.snapshot_id AND sn.status = 'READY'"
                "  JOIN documentation_sources src ON src.id = sn.source_id AND src.enabled"
                "  LEFT JOIN approvals ap ON ap.id = src.approval_id"
                "  JOIN artifacts a ON a.id = sn.storage_ref"
                " WHERE src.approval_id IS NULL OR ap.status = 'APPROVED'"
                " ORDER BY ds.id LIMIT 500"
            )
        )
    ).mappings()
    lowered = [w.lower() for w in words]
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        hay = f"{r['section_path']} {r['heading'] or ''}".lower()
        score = sum(1 for w in lowered if w in hay)
        if score:
            scored.append((score, dict(r)))
    scored.sort(key=lambda x: (-x[0], x[1]["section_path"]))
    return [dict(r, score=s) for s, r in scored[:DOCS_LIMIT]]


# --- Сборка -----------------------------------------------------------------------------------------------


def _read_texts(repo: Path, files: Sequence[_File]) -> dict[str, list[str]]:
    entries = [TreeEntry(path=f.path, mode="100644", object_type="blob", blob_sha=f.blob_sha, size=f.size) for f in files]
    blobs = read_blobs(repo, entries)
    return {f.path: _lines(blobs[f.blob_sha]) for f in files if f.blob_sha in blobs}


def _rg_terms(export: Path, terms: Sequence[str]) -> tuple[dict[str, dict[str, list[int]]], list[str]]:
    """term → path → строки совпадений. Ошибка одного термина — предупреждение, не сбой сборки."""
    out: dict[str, dict[str, list[int]]] = {}
    warnings: list[str] = []
    for term in terms:
        try:
            result = ripgrep_search(export, term, limits=_TERM_LIMITS)
        except (SearchQueryError, SearchError) as exc:
            warnings.append(f"exact_search_failed:{type(exc).__name__}")
            continue
        for m in result.matches:
            out.setdefault(term, {}).setdefault(m.path, []).append(m.line)
    return out, warnings


def _read_doc(store: ArtifactStore, storage_ref: str, sha256: str, body_ref: str) -> str:
    m = re.fullmatch(r"artifact:[0-9a-f-]{36}#bytes=([0-9]+)-([0-9]+)", body_ref)
    if m is None:
        raise ValueError("недопустимый body_ref")
    data = b"".join(store.open(storage_ref, expected_sha256=sha256))
    start, end = int(m.group(1)), int(m.group(2))
    chunk = data[start:end][:DOC_SECTION_MAX_BYTES]
    return chunk.decode("utf-8", "replace")


def _repo_map(files: Mapping[str, _File], stack: Sequence[str]) -> str:
    dirs: dict[str, list[str]] = {}
    for path in files:
        parent = str(PurePosixPath(path).parent)
        parts = [] if parent == "." else parent.split("/")
        key = "/".join(parts[:2]) + "/" if parts else "./"
        dirs.setdefault(key, []).append(path)
    lines = [f"Файлов (текстовых, проиндексированных): {len(files)}"]
    if stack:
        lines.append("Стек (project_technologies):")
        lines.extend(f"- {s}" for s in stack)
    lines.append("Дерево (каталог: число файлов; расширения):")
    for key in sorted(dirs):
        paths = dirs[key]
        exts = sorted({PurePosixPath(p).suffix or PurePosixPath(p).name for p in paths})
        shown = ", ".join(exts[:6]) + (" …" if len(exts) > 6 else "")
        lines.append(f"{key}: {len(paths)} ({shown})")
    body = "\n".join(lines)
    if len(body) > REPO_MAP_MAX_CHARS:
        body = body[: REPO_MAP_MAX_CHARS - 40] + "\n[карта сокращена по бюджету]"
    return body


async def build_context_pack(
    engine: AsyncEngine,
    req: RetrievalRequest,
    *,
    repository_roots: Mapping[str, Path],
    export_cache_root: Path,
    artifacts: ArtifactStore | None = None,
    warnings: Sequence[str] = (),
) -> ContextPack:
    """Retrieval → ранжирование → бюджет → manifest. Чтение — роль воркера с RLS-контекстом workspace."""
    ctx = DbContext(workspace_id=req.workspace_id, actor_type="system")
    budget = req.effective_budget
    warn: list[str] = list(warnings)
    task_texts = [req.task_text]
    terms = extract_terms([*task_texts, *req.seed_texts])
    words = extract_words(task_texts)
    candidates: list[_Candidate] = []
    queries: list[QueryLog] = []
    docs_allowed = req.documentation_mode in ("SMART", "STRICT")

    async with transaction(engine, ctx) as conn:
        snap = await _resolve_snapshot(conn, req.repository_id, req.base_commit)
        root_ref = (
            await conn.execute(text("SELECT local_root_ref FROM repositories WHERE id = :id"), {"id": req.repository_id})
        ).scalar()
        files: dict[str, _File] = {}
        defs: list[_Symbol] = []
        fts_hits: list[ChunkHit] = []
        trgm_hits: list[ChunkHit] = []
        changed: list[str] = []
        stack = await _stack(conn, req.project_id)
        if snap is not None:
            files = await _load_files(conn, snap.id)
            symbol_words = [w for w in {*words, *terms} if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", w)]
            defs = await _symbol_definitions(conn, snap.id, sorted(symbol_words))
            fts_query = " or ".join(words[:20])[:1000]
            if fts_query:
                fts_hits = await fts_search(conn, snapshot_id=snap.id, query=fts_query, limit=20)
            for term in terms[:6]:
                try:
                    trgm_hits.extend(await trigram_search(conn, snapshot_id=snap.id, query=term, limit=5))
                except SearchQueryError:
                    continue
            changed = await _changed_paths(conn, snap)
        docs: list[dict[str, Any]] = await _doc_sections(conn, [*words, *terms]) if docs_allowed else []

    if snap is None:
        warn.append("no_index_snapshot")
    elif snap.commit != req.base_commit:
        warn.append("snapshot_commit_differs_from_base")

    repo_path: Path | None = None
    if root_ref:
        try:
            repo_path = resolve_ref(root_ref, dict(repository_roots))
        except UnsafePathError:
            warn.append("repository_path_unavailable")

    # 1. exact: пути, определения символов, ripgrep
    exact_ranges: dict[str, list[tuple[int, int, float]]] = {}
    path_seeds: list[str] = []
    for term in terms:
        for path in files:
            if path == term or path.endswith("/" + term.lstrip("./")):
                path_seeds.append(path)
    for raw in req.seed_texts:
        for m in _PATHLIKE.finditer(raw):
            if m.group(0) in files:
                path_seeds.append(m.group(0))
    path_seeds = list(dict.fromkeys(path_seeds))[:MAX_FOCUS_FILES]
    for sym in defs:
        if sym.path in files:
            end = sym.end if sym.end - sym.start <= MAX_SYMBOL_LINES * 2 else sym.start + 80
            exact_ranges.setdefault(sym.path, []).append((sym.start, end, 5.0))
    rg_hits: dict[str, dict[str, list[int]]] = {}
    if snap is not None and repo_path is not None and terms:
        try:
            export = await asyncio.to_thread(
                materialize_export, repo_path, snap.commit, [(f.path, f.blob_sha) for f in files.values()], export_cache_root
            )
            rg_hits, rg_warn = await asyncio.to_thread(_rg_terms, export, terms)
            warn.extend(rg_warn)
        except (GitSourceError, OSError) as exc:
            warn.append(f"exact_search_unavailable:{type(exc).__name__}")
    hit_files: dict[str, float] = {}
    for term_hits in rg_hits.values():
        for path in term_hits:
            if path in files:
                hit_files[path] = hit_files.get(path, 0.0) + 1.0
    for sym in defs:
        if sym.path in files:
            hit_files[sym.path] = hit_files.get(sym.path, 0.0) + 2.0
    for path in path_seeds:
        hit_files[path] = hit_files.get(path, 0.0) + 3.0
    focus = sorted(hit_files, key=lambda p: (-hit_files[p], p))[:MAX_FOCUS_FILES]

    async with transaction(engine, ctx) as conn:
        edges = await _edges(conn, snap.id, focus) if snap is not None else []
        neighbour_paths = {p for s, t, _ in edges for p in (s, t)}
        symbols = (
            await _load_symbols(conn, snap.id, sorted({*hit_files, *neighbour_paths})) if snap is not None else {}
        )
        chunk_paths = sorted({*hit_files, *neighbour_paths, *changed})
        chunks = await _file_chunks(conn, snap.id, chunk_paths) if snap is not None else {}

    for term_hits in rg_hits.values():
        for path, hit_lines in term_hits.items():
            if path not in files:
                continue
            for line in hit_lines:
                rng = _enclosing(symbols.get(path, []), line) or (max(1, line - HIT_CONTEXT_LINES), line + HIT_CONTEXT_LINES)
                exact_ranges.setdefault(path, []).append((rng[0], rng[1], 1.0))
    for path, ranges in exact_ranges.items():
        merged = sorted(_merge_ranges(ranges), key=lambda r: (-r[2], r[0]))[:MAX_RANGES_PER_FILE]
        for a, b, s in merged:
            candidates.append(
                _Candidate("exact", _section_for(files.get(path)), path, a, b, hit_files.get(path, 0.0) * 10 + s, "exact")
            )
    for path in path_seeds:
        candidates.append(
            _Candidate("exact", _section_for(files.get(path)), path, 1, HEAD_LINES["exact"], 100.0, "exact")
        )
    exact_query = " | ".join(terms)[:4000]
    if exact_query:
        queries.append(QueryLog("exact", exact_query))
    if defs:
        queries.append(QueryLog("symbol", " | ".join(sorted({d.name for d in defs}))[:4000]))

    # 2. dependency: соседи по импортам, test links, контур символов
    if focus:
        queries.append(QueryLog("graph", " | ".join(focus)[:4000]))
    focus_set = set(focus)
    neighbour_score: dict[str, float] = {}
    tests_for: dict[str, float] = {}
    for src, dst, edge_type in edges:
        if edge_type == "tests":
            if dst in focus_set and src in files:
                tests_for[src] = tests_for.get(src, 0.0) + 1.0
            continue
        other = dst if src in focus_set else src
        if other not in focus_set and other in files:
            neighbour_score[other] = neighbour_score.get(other, 0.0) + 1.0
    for path, score in neighbour_score.items():
        candidates.append(_Candidate("dependency", _section_for(files.get(path)), path, 1, HEAD_LINES["dependency"], score, "graph"))
    for path, score in tests_for.items():
        candidates.append(_Candidate("dependency", "tests", path, 1, HEAD_LINES["tests"], score, "graph"))
    for path in focus:
        outline = [f"{s.kind} {s.name} L{s.start}-L{s.end}" + (f": {s.signature}" if s.signature else "") for s in symbols.get(path, [])]
        imports = sorted({t for s, t, e in edges if s == path and e == "imports"})
        importers = sorted({s for s, t, e in edges if t == path and e == "imports"})
        tests = sorted({s for s, t, e in edges if t == path and e == "tests"})
        body = "\n".join(
            [
                f"file: {path}",
                "symbols:",
                *(f"  {o}" for o in outline[:80]),
                f"imports: {', '.join(imports) or '—'}",
                f"imported_by: {', '.join(importers) or '—'}",
                f"tests: {', '.join(tests) or '—'}",
            ]
        )
        candidates.append(_Candidate("dependency", "symbols_graph", path, None, None, hit_files.get(path, 0.0), "graph", text=body))

    # 3. task_relevance: FTS и pg_trgm
    if fts_hits:
        queries.append(QueryLog("fts", " or ".join(words[:20])[:4000]))
    if trgm_hits:
        queries.append(QueryLog("trigram", " | ".join(terms[:6])[:4000]))
    for strategy, hits in (("fts", fts_hits), ("trigram", trgm_hits)):
        for hit in hits:
            path = hit.metadata.get("path")
            start, end = hit.metadata.get("start_line"), hit.metadata.get("end_line")
            if not isinstance(path, str) or path not in files or not isinstance(start, int) or not isinstance(end, int):
                continue
            end = min(end, start + 119)
            candidates.append(
                _Candidate("task_relevance", _section_for(files.get(path)), path, start, end, hit.score, strategy, chunk_id=hit.chunk_id)
            )

    # 4. recency
    for path in changed:
        candidates.append(_Candidate("recency", _section_for(files.get(path)), path, 1, HEAD_LINES["recency"], 0.0, None))

    # 5. docs (только SMART/STRICT; OFF — запрос не выполняется и не журналируется)
    if docs_allowed:
        queries.append(QueryLog("docs", (" ".join(words[:20]) or "docs")[:4000]))
        if docs and artifacts is None:
            warn.append("docs_skipped_no_artifact_store")
        elif docs and artifacts is not None:
            for d in docs:
                try:
                    body = await asyncio.to_thread(_read_doc, artifacts, d["storage_ref"], d["sha256"], d["body_ref"])
                except Exception as exc:  # noqa: BLE001 — недоступный раздел документации не прерывает сборку
                    warn.append(f"doc_section_unreadable:{type(exc).__name__}")
                    continue
                heading = d["heading"] or d["section_path"]
                candidates.append(
                    _Candidate(
                        "docs", "docs", f"doc:{d['section_path']}", None, None, float(d["score"]), "docs",
                        text=f"# {heading}\nsource: {d['uri_or_file_ref']}\n\n{body}", source_ref=f"documentation_section:{d['id']}",
                    )
                )

    # 6. repo_map
    if snap is not None:
        candidates.append(_Candidate("map", "repo_map", "<repo_map>", None, None, 0.0, None, text=_repo_map(files, stack)))

    # Текст из git blob снимка
    need = sorted({c.path for c in candidates if c.text is None and c.path in files})
    texts: dict[str, list[str]] = {}
    if need and repo_path is not None and snap is not None:
        try:
            texts = await asyncio.to_thread(_read_texts, repo_path, [files[p] for p in need])
        except GitSourceError as exc:
            warn.append(f"git_read_failed:{str(exc)[:120]}")
    elif need:
        warn.append("repository_unavailable_code_skipped")

    fragments: list[Fragment] = []
    origin: dict[tuple[str, str, int | None, int | None], _Candidate] = {}
    commit = snap.commit if snap is not None else req.base_commit
    for c in candidates:
        if c.text is not None:
            body_text, start, end = c.text, c.start, c.end
        else:
            lines = texts.get(c.path)
            if lines is None or c.start is None:
                continue
            start = max(1, c.start)
            end = min(len(lines), c.end or len(lines))
            if end < start:
                continue
            body_text = "\n".join(lines[start - 1 : end])
        if c.chunk_id is None and c.start is not None:
            for a, b, cid in chunks.get(c.path, []):
                if a <= c.start <= b:
                    c.chunk_id = cid
                    break
        frag = Fragment(
            section=c.section,
            kind=c.kind,
            path=c.path,
            commit_sha=req.base_commit if c.kind == "docs" else commit,
            text=body_text,
            score=c.score,
            start_line=start,
            end_line=end,
            source_ref=c.source_ref or (f"chunk:{c.chunk_id}" if c.chunk_id else None),
        )
        fragments.append(frag)
        origin.setdefault(frag.key(), c)

    selection = select(fragments, base_commit=req.base_commit, budget=budget, stale_policy=req.stale_policy)

    # Журнал retrieval: фрагменты индекса (chunk) по стратегиям в порядке ранга
    by_strategy = {q.strategy: q for q in queries}
    for frag in fragments:
        c = origin.get(frag.key())
        if c is None or c.chunk_id is None or c.strategy is None:
            continue
        q = by_strategy.get(c.strategy)
        if q is not None and all(cid != c.chunk_id for cid, _, _ in q.hits):
            q.hits.append((c.chunk_id, float(frag.score), frag.key()))

    extra = {
        "role": req.role,
        "documentation_mode": req.documentation_mode,
        "stale_policy": req.stale_policy,
        "budget": [budget.max_bytes, budget.max_files, budget.max_fragment_bytes],
    }
    digest = content_hash(commit=req.base_commit, policy_version=CONTEXT_POLICY_VERSION, inputs=selection.fragments, extra=extra)
    manifest = selection.manifest(base_commit=req.base_commit, policy_version=CONTEXT_POLICY_VERSION, stale_policy=req.stale_policy)
    pack = ContextPack(
        request=req,
        snapshot_id=snap.id if snap else None,
        snapshot_commit=snap.commit if snap else None,
        selection=selection,
        content_hash=digest,
        manifest=manifest,
        queries=queries,
        warnings=sorted(set(warn)),
    )
    manifest.update(
        {
            "snapshot_id": str(snap.id) if snap else None,
            "snapshot_commit": snap.commit if snap else None,
            "index_version": INDEX_VERSION,
            "role": req.role,
            "documentation_mode": req.documentation_mode,
            "docs": "allowed" if docs_allowed else "off",
            "budget": {"max_bytes": budget.max_bytes, "max_files": budget.max_files, "max_fragment_bytes": budget.max_fragment_bytes},
            "terms": terms,
            "content_hash": digest,
            "stats": pack.stats(),
            "warnings": pack.warnings,
        }
    )
    log.info("context.pack_built", extra={"snapshot_id": manifest["snapshot_id"], **pack.stats()})
    return pack
