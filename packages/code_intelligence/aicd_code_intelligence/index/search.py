"""Поиск по снимку индекса (P5-04; ADR-0005 п.1, DATA_FLOW §9 «rg точный → символы/граф → FTS/trgm»).

**Точный поиск — ripgrep по экспорту снимка.** ripgrep работает с файлами на диске, поэтому снимок
материализуется в кэш-каталог `<cache_root>/<commit>-v<INDEX_VERSION>/` **из blob, прочитанных
`git cat-file --batch`** (не `git archive`/checkout: фильтры, `export-subst` и хуки недоверенного
репозитория не применяются). Экспортируются только проиндексированные текстовые файлы; пути
проверены (`is_safe_tree_path`), symlink не создаются, запись — `O_EXCL|O_NOFOLLOW` в новый временный
каталог, затем атомарный `rename`. Старые экспорты удаляет `prune_export_cache`.

Безопасный argv ripgrep (`build_rg_argv`): фиксированный набор флагов, `--no-config` (нет
RIPGREP_CONFIG_PATH), без `--pre`/`--search-zip` (внешние программы), `--engine=default` (линейный
движок Rust regex, без PCRE2-backtracking), лимиты размера regex/DFA, `--max-count`, `--max-columns`,
`--max-filesize`; по умолчанию `--fixed-strings`; запрос передаётся **после `--`** одним элементом argv,
без shell — `--pre=sh`, `-e`, `$(…)`, обратные кавычки остаются литеральным текстом. Запросы с переводом
строки/NUL/управляющими символами отклоняются (`SearchQueryError`). Процесс — чистое окружение,
таймаут, ограничение объёма вывода и числа совпадений.

**Нечёткий поиск — PostgreSQL:** FTS по `knowledge_chunks.search_vector` (конфигурация `simple`,
`websearch_to_tsquery` — синтаксис запроса не интерпретируется как SQL, всё — bind-параметры) и
`pg_trgm` по именам символов (`code_symbols.symbol_name`) и путям (`code_files.path`) с отображением
на фрагменты (текст кода в `knowledge_chunks` не хранится — trigram по самим фрагментам невозможен).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_code_intelligence.index import INDEX_VERSION
from aicd_code_intelligence.index.chunks import TS_CONFIG, split_identifiers
from aicd_code_intelligence.index.gitsource import (
    GitSourceError,
    TreeEntry,
    is_safe_tree_path,
    read_blobs,
    validate_commit_sha,
)

MAX_QUERY_CHARS = 1000
_EXPORT_MARKER = ".aicd-export-complete"


class SearchQueryError(ValueError):
    """Недопустимый поисковый запрос (сообщение безопасно для API)."""


class SearchError(RuntimeError):
    """Сбой поиска (ripgrep недоступен, ошибка процесса)."""


@dataclass(frozen=True)
class SearchLimits:
    max_matches: int = 200
    max_matches_per_file: int = 20
    max_output_bytes: int = 4 * 1024 * 1024
    max_columns: int = 400
    max_filesize: str = "2M"
    timeout_seconds: float = 10.0
    threads: int = 2


@dataclass(frozen=True)
class TextMatch:
    path: str
    line: int
    text: str
    submatches: tuple[tuple[int, int], ...] = ()


@dataclass
class TextSearchResult:
    matches: list[TextMatch] = field(default_factory=list)
    truncated: bool = False
    timed_out: bool = False


def validate_query(query: str, *, regex: bool = False) -> str:
    if not isinstance(query, str) or not query:
        raise SearchQueryError("пустой запрос")
    if len(query) > MAX_QUERY_CHARS:
        raise SearchQueryError("слишком длинный запрос")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in query):
        raise SearchQueryError("перевод строки, NUL и управляющие символы в запросе недопустимы")
    if regex and query.strip() == "":
        raise SearchQueryError("пустое регулярное выражение")
    return query


def build_rg_argv(
    rg_binary: str,
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = True,
    limits: SearchLimits | None = None,
    search_path: str = ".",
) -> list[str]:
    """argv ripgrep: фиксированные флаги, запрос — единственный позиционный аргумент после `--`."""
    limits = limits or SearchLimits()
    validate_query(query, regex=regex)
    argv = [
        rg_binary,
        "--no-config",
        "--json",
        "--no-pre",
        "--no-search-zip",
        "--no-follow",
        "--hidden",
        "--no-ignore",
        "--no-messages",
        "--engine=default",
        "--regex-size-limit=1M",
        "--dfa-size-limit=10M",
        f"--max-count={limits.max_matches_per_file}",
        f"--max-columns={limits.max_columns}",
        "--max-columns-preview",
        f"--max-filesize={limits.max_filesize}",
        f"--threads={limits.threads}",
        "--sort=path",
        "--case-sensitive" if case_sensitive else "--ignore-case",
    ]
    if not regex:
        argv.append("--fixed-strings")
    argv.extend(["--", query, search_path])
    return argv


def _rg_binary() -> str:
    path = shutil.which("rg", path="/usr/local/bin:/usr/bin:/bin")
    if path is None:
        raise SearchError("ripgrep (rg) не установлен")
    return path


def ripgrep_search(
    root: Path,
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = True,
    limits: SearchLimits | None = None,
    rg_binary: str | None = None,
) -> TextSearchResult:
    """Точный поиск в каталоге экспорта снимка. Пути в результате — относительные пути снимка."""
    limits = limits or SearchLimits()
    argv = build_rg_argv(rg_binary or _rg_binary(), query, regex=regex, case_sensitive=case_sensitive, limits=limits)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    result = TextSearchResult()
    proc = subprocess.Popen(  # noqa: S603 — argv-массив, без shell
        argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    timer = threading.Timer(limits.timeout_seconds, proc.kill)
    timer.start()
    started = time.monotonic()
    read = 0
    try:
        for raw in proc.stdout or ():
            read += len(raw)
            if read > limits.max_output_bytes or len(result.matches) >= limits.max_matches:
                result.truncated = True
                proc.kill()
                break
            match = _parse_rg_line(raw)
            if match is not None:
                result.matches.append(match)
        stderr = proc.stderr.read(4096) if proc.stderr is not None else b""
        code = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    if not result.truncated and code not in (0, 1):
        if time.monotonic() - started >= limits.timeout_seconds:
            result.timed_out = True
            return result
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        raise SearchQueryError(f"ripgrep отклонил запрос: {detail[0][:200] if detail else code}")
    return result


def _parse_rg_line(raw: bytes) -> TextMatch | None:
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if event.get("type") != "match":
        return None
    data = event.get("data", {})
    path = data.get("path", {}).get("text")
    line_text = data.get("lines", {}).get("text")
    line_no = data.get("line_number")
    if not isinstance(path, str) or not isinstance(line_text, str) or not isinstance(line_no, int):
        return None  # не-UTF-8 путь/строка (base64 `bytes`) — пропускается
    path = path.removeprefix("./")
    subs = tuple(
        (int(s["start"]), int(s["end"]))
        for s in data.get("submatches", [])
        if isinstance(s.get("start"), int) and isinstance(s.get("end"), int)
    )
    return TextMatch(path=path, line=line_no, text=line_text.rstrip("\n"), submatches=subs)


# ---------------------------------------------------------------------------------------------
# Экспорт снимка для ripgrep
# ---------------------------------------------------------------------------------------------


def export_dir_name(commit_sha: str) -> str:
    return f"{validate_commit_sha(commit_sha)}-v{INDEX_VERSION}"


def materialize_export(repo: Path, commit_sha: str, files: Iterable[tuple[str, str]], cache_root: Path) -> Path:
    """Каталог с текстовыми файлами снимка (`(path, blob_sha)`), из кэша или построенный заново."""
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = cache_root / export_dir_name(commit_sha)
    if (final / _EXPORT_MARKER).is_file():
        os.utime(final)  # LRU для prune_export_cache
        return final
    entries = []
    for path, blob_sha in files:
        if not is_safe_tree_path(path):
            raise GitSourceError("небезопасный путь в снимке")
        entries.append(TreeEntry(path=path, mode="100644", object_type="blob", blob_sha=blob_sha, size=None))
    blobs = read_blobs(repo, entries)
    tmp = cache_root / f".tmp-{uuid.uuid4().hex}"
    tmp.mkdir(mode=0o700)
    try:
        for entry in entries:
            target = tmp.joinpath(*entry.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(blobs[entry.blob_sha])
        (tmp / _EXPORT_MARKER).write_text(commit_sha)
        try:
            tmp.rename(final)
        except OSError:
            # параллельная сборка успела раньше — используем её
            if not (final / _EXPORT_MARKER).is_file():
                raise
            shutil.rmtree(tmp, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


def prune_export_cache(cache_root: Path, *, keep: int = 8, max_age_seconds: float = 7 * 24 * 3600) -> list[str]:
    """Удаляет экспорты сверх `keep` самых свежих и старше `max_age_seconds`; незавершённые `.tmp-*` > 1 ч."""
    if not cache_root.is_dir():
        return []
    now = time.time()
    removed: list[str] = []
    exports = []
    for child in cache_root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        mtime = child.stat().st_mtime
        if child.name.startswith(".tmp-"):
            if now - mtime > 3600:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child.name)
            continue
        exports.append((mtime, child))
    exports.sort(reverse=True)
    for idx, (mtime, child) in enumerate(exports):
        if idx >= keep or now - mtime > max_age_seconds:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed


# ---------------------------------------------------------------------------------------------
# PostgreSQL: FTS и pg_trgm
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    text_ref: str
    score: float
    metadata: dict[str, Any]


async def fts_search(conn: AsyncConnection, *, snapshot_id: uuid.UUID, query: str, limit: int = 50) -> list[ChunkHit]:
    """FTS по фрагментам снимка (RLS: соединение с контекстом workspace)."""
    validate_query(query)
    parts = split_identifiers(query)
    rows = await conn.execute(
        text(
            "WITH q AS (SELECT websearch_to_tsquery(CAST(:cfg AS regconfig), :q) AS full_q,"
            "                  websearch_to_tsquery(CAST(:cfg AS regconfig), :parts) AS parts_q)"
            " SELECT c.id, c.source_type, c.source_id, c.text_ref, c.metadata,"
            "        ts_rank_cd(c.search_vector, q.full_q) + 0.5 * ts_rank_cd(c.search_vector, q.parts_q) AS score"
            "   FROM knowledge_chunks c, q"
            "  WHERE c.snapshot_id = :snapshot"
            "    AND (c.search_vector @@ q.full_q OR (:parts <> '' AND c.search_vector @@ q.parts_q))"
            "  ORDER BY score DESC, c.id"
            "  LIMIT :limit"
        ),
        {"cfg": TS_CONFIG, "q": query, "parts": parts, "snapshot": snapshot_id, "limit": max(1, min(limit, 500))},
    )
    return [ChunkHit(r[0], r[1], r[2], r[3], float(r[5]), dict(r[4])) for r in rows]


async def trigram_search(
    conn: AsyncConnection, *, snapshot_id: uuid.UUID, query: str, limit: int = 50
) -> list[ChunkHit]:
    """pg_trgm: похожие имена символов и пути файлов → первый фрагмент символа/файла."""
    validate_query(query)
    rows = await conn.execute(
        text(
            "WITH hits AS ("
            "  SELECT 'code_symbol'::text AS source_type, s.id AS source_id, similarity(s.symbol_name, :q) AS score"
            "    FROM code_symbols s"
            "   WHERE s.snapshot_id = :snapshot AND s.symbol_kind <> 'module' AND s.symbol_name % :q"
            "  UNION ALL"
            "  SELECT 'code_file', f.id, similarity(f.path, :q)"
            "    FROM code_files f"
            "   WHERE f.snapshot_id = :snapshot AND f.path % :q"
            ")"
            " SELECT c.id, c.source_type, c.source_id, c.text_ref, c.metadata, h.score"
            "   FROM hits h"
            "   JOIN knowledge_chunks c ON c.source_type = h.source_type AND c.source_id = h.source_id"
            "        AND c.snapshot_id = :snapshot AND c.chunk_index = 0"
            "  ORDER BY h.score DESC, c.id"
            "  LIMIT :limit"
        ),
        {"q": query, "snapshot": snapshot_id, "limit": max(1, min(limit, 500))},
    )
    return [ChunkHit(r[0], r[1], r[2], r[3], float(r[5]), dict(r[4])) for r in rows]


async def snapshot_text_files(conn: AsyncConnection, *, snapshot_id: uuid.UUID) -> list[tuple[str, str]]:
    """(path, blob_sha) текстовых негенерированных файлов снимка — вход `materialize_export`."""
    rows = await conn.execute(
        text(
            "SELECT path, blob_sha FROM code_files"
            " WHERE snapshot_id = :snapshot AND NOT is_binary AND NOT is_generated ORDER BY path"
        ),
        {"snapshot": snapshot_id},
    )
    return [(r[0], r[1]) for r in rows]
