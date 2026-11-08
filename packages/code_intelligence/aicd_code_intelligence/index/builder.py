"""Построение снимка индекса без БД (P5-02): дерево commit → файлы → символы/фрагменты → граф.

Инкрементальность (DATA_FLOW §9, критерий P5-02 «повторная индексация меняет только изменённые файлы»):
файл переиспользуется из предыдущего снимка того же репозитория (та же `index_version`), если совпадают
путь, blob sha (git-хэш содержимого ⇒ тот же sha256 содержимого), язык и — для Python — имя модуля
(зависит от `__init__.py` вокруг). Переиспользованный файл **не читается и не разбирается**: его строки
(символы, фрагменты) копирует `store`, а импорты берутся из рёбер предыдущего снимка и заново
разрешаются против нового набора файлов. Счётчики работы — `IndexStats`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from aicd_code_intelligence.index.chunks import Chunk, build_chunks
from aicd_code_intelligence.index.gitsource import GitSourceError, TreeEntry, list_tree, read_blobs
from aicd_code_intelligence.index.graph import Edge, GraphFile, build_edges, python_module_names
from aicd_code_intelligence.index.languages import SYMBOL_LANGUAGES, detect_language
from aicd_code_intelligence.index.policy import (
    IndexPolicy,
    SkipReason,
    has_generated_marker,
    is_binary,
    is_test_path,
)
from aicd_code_intelligence.index.symbols import (
    ExtractedSymbol,
    FileAnalysis,
    analyze_file,
    default_module_name,
    sha256_hex,
)

log = logging.getLogger("aicd.code_intelligence.index")

PHP_DECLARED_KINDS = frozenset({"class", "interface", "enum", "function"})


class IndexingError(RuntimeError):
    """Ошибка построения снимка; сообщение безопасно для `indexing_jobs.error`."""


@dataclass(frozen=True)
class PreviousFile:
    """Файл предыдущего снимка (база инкрементального построения)."""

    file_id: uuid.UUID | None
    path: str
    blob_sha: str
    content_hash: str
    size_bytes: int
    language: str | None
    is_test: bool
    is_generated: bool
    is_binary: bool
    module_name: str | None = None  # qualified_name символа `module`
    imports: tuple[str, ...] = ()  # target_ref рёбер `imports`
    declared: tuple[str, ...] = ()  # PHP: FQCN объявлений (для разрешения `use`)


@dataclass
class FileRecord:
    path: str
    language: str | None
    blob_sha: str
    content_hash: str
    size_bytes: int
    is_test: bool
    is_generated: bool
    is_binary: bool
    reused_from: PreviousFile | None = None
    analysis: FileAnalysis | None = None
    chunks: list[Chunk] = field(default_factory=list)
    imports: tuple[str, ...] = ()
    declared: tuple[str, ...] = ()
    has_module_symbol: bool = False


@dataclass
class IndexStats:
    tree_entries: int = 0
    unsafe_paths: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    blobs_read: int = 0
    files_parsed: int = 0
    files_reused: int = 0
    parse_failures: int = 0
    symbols_extracted: int = 0
    chunks_built: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "tree_entries": self.tree_entries,
            "unsafe_paths": self.unsafe_paths,
            "skipped": dict(sorted(self.skipped.items())),
            "blobs_read": self.blobs_read,
            "files_parsed": self.files_parsed,
            "files_reused": self.files_reused,
            "parse_failures": self.parse_failures,
            "symbols_extracted": self.symbols_extracted,
            "chunks_built": self.chunks_built,
        }


@dataclass
class SnapshotBuild:
    commit_sha: str
    files: list[FileRecord]
    edges: list[Edge]
    stats: IndexStats


def _analyze(path: str, language: str, content: bytes, module: str, is_test: bool, stats: IndexStats) -> FileAnalysis:
    try:
        return analyze_file(path, language, content, module_name=module, is_test=is_test)
    except (ValueError, RecursionError, UnicodeError) as exc:
        # Сбой разбора одного файла не валит снимок: файл остаётся с одним символом module.
        stats.parse_failures += 1
        log.warning("index.parse_failed", extra={"path": path, "error": type(exc).__name__})
        return analyze_file(path, "", content, module_name=module, is_test=is_test)


def build_snapshot(
    repo: Path,
    commit_sha: str,
    *,
    policy: IndexPolicy | None = None,
    previous: Mapping[str, PreviousFile] | None = None,
) -> SnapshotBuild:
    policy = policy or IndexPolicy()
    previous = previous or {}
    stats = IndexStats()
    entries, unsafe = list_tree(repo, commit_sha)
    stats.tree_entries = len(entries) + len(unsafe)
    stats.unsafe_paths = len(unsafe)
    if len(entries) > policy.max_files:
        raise IndexingError(f"слишком много файлов в commit ({len(entries)} > {policy.max_files})")

    kept: list[TreeEntry] = []
    for entry in entries:
        reason = policy.skip_reason(entry.path, entry.mode, entry.size)
        if reason is None and entry.object_type != "blob":
            reason = SkipReason.EXCLUDED
        if reason is not None:
            stats.skipped[reason.value] = stats.skipped.get(reason.value, 0) + 1
            continue
        kept.append(entry)

    module_names = python_module_names(e.path for e in kept)
    records: list[FileRecord] = []
    to_read: list[tuple[TreeEntry, str | None, bool, str]] = []
    for entry in kept:
        language = detect_language(entry.path)
        is_test = is_test_path(entry.path)
        module = module_names.get(entry.path) or default_module_name(entry.path, language or "")
        prev = previous.get(entry.path)
        if (
            prev is not None
            and prev.blob_sha == entry.blob_sha
            and prev.language == language
            and (language != "python" or prev.module_name in (None, module))
        ):
            records.append(
                FileRecord(
                    path=entry.path,
                    language=language,
                    blob_sha=entry.blob_sha,
                    content_hash=prev.content_hash,
                    size_bytes=prev.size_bytes,
                    is_test=prev.is_test,
                    is_generated=prev.is_generated,
                    is_binary=prev.is_binary,
                    reused_from=prev,
                    imports=prev.imports,
                    declared=prev.declared,
                    has_module_symbol=prev.module_name is not None,
                )
            )
            stats.files_reused += 1
            continue
        to_read.append((entry, language, is_test, module))

    try:
        blobs = read_blobs(repo, [e for e, _, _, _ in to_read])
    except GitSourceError as exc:
        raise IndexingError(str(exc)) from exc
    stats.blobs_read = len(blobs)

    for entry, language, is_test, module in to_read:
        content = blobs[entry.blob_sha]
        binary = is_binary(content)
        generated = not binary and (policy.is_generated_name(entry.path) or has_generated_marker(content))
        record = FileRecord(
            path=entry.path,
            language=language,
            blob_sha=entry.blob_sha,
            content_hash=sha256_hex(content),
            size_bytes=len(content),
            is_test=is_test,
            is_generated=generated,
            is_binary=binary,
        )
        if not binary and not generated:
            symbols: tuple[ExtractedSymbol, ...] = ()
            if language in SYMBOL_LANGUAGES:
                analysis = _analyze(entry.path, language, content, module, is_test, stats)
                stats.files_parsed += 1
                stats.symbols_extracted += len(analysis.symbols)
                record.analysis = analysis
                record.has_module_symbol = True
                record.imports = tuple(ref.target for ref in analysis.imports)
                if language == "php":
                    record.declared = tuple(
                        s.qualified_name for s in analysis.symbols if s.kind in PHP_DECLARED_KINDS and s.qualified_name
                    )
                symbols = analysis.symbols
            record.chunks = build_chunks(
                path=entry.path,
                language=language,
                content=content,
                symbols=symbols,
                chunk_lines=policy.chunk_lines,
                max_symbol_chunk_lines=policy.max_symbol_chunk_lines,
            )
            stats.chunks_built += len(record.chunks)
        records.append(record)

    records.sort(key=lambda r: r.path)
    graph_files = {
        r.path: GraphFile(path=r.path, language=r.language, is_test=r.is_test, imports=r.imports, declared=r.declared)
        for r in records
        if r.has_module_symbol
    }
    edges = build_edges(graph_files)
    return SnapshotBuild(commit_sha=commit_sha, files=records, edges=edges, stats=stats)
