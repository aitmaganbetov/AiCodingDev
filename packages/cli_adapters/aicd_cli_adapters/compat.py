"""Compatibility matrix CLI (CLI_ADAPTER_CONTRACT §5.3): `packages/contracts/cli-compatibility.yaml`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

MATRIX_PATH = Path(__file__).resolve().parents[2] / "contracts" / "cli-compatibility.yaml"
Compatibility = Literal["SUPPORTED", "UNTESTED", "UNSUPPORTED"]
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    m = _VERSION.search(raw)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _satisfies(version: tuple[int, int, int], spec: str) -> bool:
    for clause in filter(None, (c.strip() for c in spec.split(","))):
        m = re.fullmatch(r"(>=|<=|>|<|==)\s*(\d+\.\d+\.\d+)", clause)
        if not m:
            raise ValueError(f"некорректный диапазон версий: {clause}")
        bound = parse_version(m.group(2))
        if bound is None:  # pragma: no cover — регулярное выражение гарантирует формат
            raise ValueError(clause)
        ok = {
            ">=": version >= bound,
            "<=": version <= bound,
            ">": version > bound,
            "<": version < bound,
            "==": version == bound,
        }[m.group(1)]
        if not ok:
            return False
    return True


@dataclass(frozen=True)
class MatrixEntry:
    id: str
    provider: str
    version_range: str
    status: Compatibility
    capabilities: dict[str, Any]
    command_profiles: tuple[str, ...]


@lru_cache(maxsize=4)
def load_matrix(path: Path = MATRIX_PATH) -> tuple[MatrixEntry, ...]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = []
    for raw in data.get("entries") or []:
        entries.append(
            MatrixEntry(
                id=raw["id"],
                provider=raw["provider"],
                version_range=raw["version_range"],
                status=raw["status"],
                capabilities=dict(raw.get("capabilities") or {}),
                command_profiles=tuple(raw.get("command_profiles") or ()),
            )
        )
    return tuple(entries)


def evaluate(
    provider: str, version_raw: str | None, entries: tuple[MatrixEntry, ...] | None = None
) -> tuple[Compatibility, MatrixEntry | None]:
    """Неизвестная/непарсимая версия — UNTESTED (политика решает, допустимо ли исполнение)."""
    version = parse_version(version_raw)
    if version is None:
        return "UNTESTED", None
    for entry in entries if entries is not None else load_matrix():
        if entry.provider == provider and _satisfies(version, entry.version_range):
            return entry.status, entry
    return "UNTESTED", None
