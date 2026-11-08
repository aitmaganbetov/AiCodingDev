"""Ранжирование и бюджет Context Pack (DATA_FLOW §9, AGENT_ROLES §1.1, P5-06).

Порядок источников: точное совпадение (ripgrep/символ) > зависимость (граф) > релевантность задаче (FTS/trgm)
> свежесть изменения. Внутри класса — по оценке источника. Фрагменты другого commit, чем base_commit задачи,
исключаются или маркируются `stale=true` (ТЗ §12) — по политике. Бюджет: максимум байтов и файлов; фрагменты,
не вошедшие в бюджет, перечисляются в manifest (`omitted`), чтобы агент знал о неполноте контекста.
Хэш содержимого = sha256(commit + policy_version + упорядоченные входы) — ключ кэша `context_packs`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

SourceKind = Literal["map", "exact", "dependency", "task_relevance", "recency", "docs"]
Section = Literal["code", "symbols_graph", "tests", "repo_map", "docs"]
StalePolicy = Literal["exclude", "mark"]

# `map` — сжатая карта репозитория (repo_map): мала и нужна всегда, поэтому идёт раньше ранжируемых классов.
KIND_RANK: dict[str, int] = {"map": -1, "exact": 0, "dependency": 1, "task_relevance": 2, "recency": 3, "docs": 4}


@dataclass(frozen=True)
class Fragment:
    section: Section
    kind: SourceKind
    path: str
    commit_sha: str
    text: str
    score: float = 0.0  # оценка внутри класса источника (выше — важнее)
    start_line: int | None = None
    end_line: int | None = None
    source_ref: str | None = None  # chunk/symbol/doc section id

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def key(self) -> tuple[str, str, int | None, int | None]:
        return (self.section, self.path, self.start_line, self.end_line)


@dataclass(frozen=True)
class Budget:
    max_bytes: int
    max_files: int
    max_fragment_bytes: int = 32 * 1024

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.max_files <= 0 or self.max_fragment_bytes <= 0:
            raise ValueError("бюджет Context Pack должен быть положительным")


@dataclass
class PackSelection:
    fragments: list[Fragment] = field(default_factory=list)
    stale: list[Fragment] = field(default_factory=list)  # вошедшие, но помеченные stale
    omitted: list[dict[str, Any]] = field(default_factory=list)
    size_bytes: int = 0

    def manifest(self, *, base_commit: str, policy_version: int, stale_policy: StalePolicy) -> dict[str, Any]:
        stale_keys = {f.key() for f in self.stale}
        return {
            "schema": 1,
            "base_commit": base_commit,
            "policy_version": policy_version,
            "stale_policy": stale_policy,
            "size_bytes": self.size_bytes,
            "fragments": [
                {
                    "section": f.section,
                    "kind": f.kind,
                    "path": f.path,
                    "lines": [f.start_line, f.end_line] if f.start_line is not None else None,
                    "commit": f.commit_sha,
                    "sha256": f.sha256,
                    "bytes": f.size,
                    "source_ref": f.source_ref,
                    "stale": f.key() in stale_keys,
                }
                for f in self.fragments
            ],
            "omitted": self.omitted,
        }


def rank(fragments: Iterable[Fragment]) -> list[Fragment]:
    """Детерминированный порядок: класс источника, затем оценка, затем путь и строки (стабильно для кэша)."""
    return sorted(
        fragments,
        key=lambda f: (KIND_RANK[f.kind], -f.score, f.path, f.start_line or 0, f.end_line or 0, f.section),
    )


def select(
    fragments: Iterable[Fragment],
    *,
    base_commit: str,
    budget: Budget,
    stale_policy: StalePolicy = "exclude",
) -> PackSelection:
    out = PackSelection()
    seen: set[tuple[str, str, int | None, int | None]] = set()
    files: set[str] = set()
    for f in rank(fragments):
        if f.key() in seen:
            continue  # один и тот же фрагмент из нескольких источников — берётся с лучшим рангом
        seen.add(f.key())
        is_stale = f.commit_sha != base_commit
        if is_stale and stale_policy == "exclude":
            out.omitted.append(_omit(f, "stale_commit"))
            continue
        if f.size > budget.max_fragment_bytes:
            out.omitted.append(_omit(f, "fragment_too_large"))
            continue
        new_file = f.path not in files
        if new_file and len(files) >= budget.max_files:
            out.omitted.append(_omit(f, "max_files"))
            continue
        if out.size_bytes + f.size > budget.max_bytes:
            out.omitted.append(_omit(f, "max_bytes"))
            continue
        files.add(f.path)
        out.fragments.append(f)
        out.size_bytes += f.size
        if is_stale:
            out.stale.append(f)
    return out


def _omit(f: Fragment, reason: str) -> dict[str, Any]:
    return {"section": f.section, "path": f.path, "kind": f.kind, "commit": f.commit_sha, "reason": reason}


def content_hash(
    *, commit: str, policy_version: int, inputs: Sequence[Fragment], extra: dict[str, Any] | None = None
) -> str:
    """Ключ кэша Context Pack: одинаковые commit, политика и входы ⇒ тот же pack (DATABASE_DESIGN §10.8)."""
    payload = {
        "commit": commit,
        "policy_version": policy_version,
        "inputs": [[f.section, f.kind, f.path, f.start_line, f.end_line, f.commit_sha, f.sha256] for f in inputs],
        "extra": extra or {},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
