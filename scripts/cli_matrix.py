#!/usr/bin/env python3
"""Строки матрицы совместимости CLI из evidence набора `real-cli-smoke@1` (CLI_ADAPTER_CONTRACT §5.3, §13.2).

Строка `SUPPORTED` появляется только так: набор пройден на реальной установке целиком, evidence сохранён.
Скрипт читает файл evidence (`AICD_REAL_CLI_EVIDENCE` набора `tests/real_cli`) и для каждого провайдера, у
которого пройдены **все** обязательные проверки, записывает строку ровно на проверенную версию
(`==X.Y.Z`): поведение другой версии набором не подтверждено.

    uv run python scripts/cli_matrix.py reports/real-cli/smoke-2026-11-11.json

Провайдер с непройденной или пропущенной проверкой в матрицу не попадает (код выхода 1).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "packages" / "contracts" / "cli-compatibility.yaml"
SUITE = "real-cli-smoke@1"
#: Проверки набора, без которых версия не считается подтверждённой.
REQUIRED_CHECKS = ("probe_and_auth", "minimal_task", "readonly_profile", "cancel", "model_selection")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class EvidenceError(ValueError):
    pass


def entry_for(provider: str, facts: dict[str, Any], *, evidence_ref: str, verified_at: str) -> dict[str, Any]:
    """Строка матрицы провайдера; `EvidenceError`, если evidence неполон."""
    checks: dict[str, Any] = facts.get("checks") or {}
    missing = [c for c in REQUIRED_CHECKS if (checks.get(c) or {}).get("status") != "passed"]
    if missing:
        raise EvidenceError(f"{provider}: не пройдены проверки {', '.join(missing)}")
    version = str(facts.get("detected_version") or "")
    if not _VERSION.fullmatch(version):
        raise EvidenceError(f"{provider}: в evidence нет распознанной версии CLI")
    capabilities = dict(facts.get("detected_capabilities") or {})
    # Список моделей — не свойство версии: каталог меняется без обновления CLI, его каждый раз читает probe.
    capabilities.pop("available_models", None)
    # Обнаруженное по справке дополняется тем, что подтвердил прогон: usage в результате, остановка по отмене,
    # разобранные события инструментов, работа с выбранной моделью.
    capabilities.update(
        {
            "token_usage_exposed": checks["minimal_task"].get("usage_source") == "cli_reported",
            "tool_events_exposed": int(checks["minimal_task"].get("tool_events") or 0) > 0,
            "cancel_signal_safe": True,
            "model_selection": True,
        }
    )
    return {
        "id": f"{provider}-{version}",
        "provider": provider,
        "version_range": f"=={version}",
        "adapter_version": str(facts.get("adapter_version") or "1.0.0"),
        "status": "SUPPORTED",
        "verified_at": verified_at,
        "verified_by_suite": SUITE,
        "evidence_ref": evidence_ref,
        "capabilities": capabilities,
        "command_profiles": [f"{provider}/agent_task@1"],
    }


def merge(matrix: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Новые строки заменяют строки с тем же id; остальные сохраняются."""
    kept = [e for e in matrix.get("entries") or [] if e.get("id") not in {n["id"] for n in entries}]
    merged = sorted([*kept, *entries], key=lambda e: (e["provider"], e["id"]))
    return {"schema_version": matrix.get("schema_version", 1), "entries": merged}


def render(header: str, matrix: dict[str, Any]) -> str:
    return header + yaml.safe_dump(matrix, allow_unicode=True, sort_keys=False, width=110)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evidence", type=Path, help="файл evidence набора real-cli-smoke@1")
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    args = parser.parse_args(argv)
    data = json.loads(args.evidence.read_text(encoding="utf-8"))
    if data.get("suite") != SUITE:
        print(f"evidence другого набора: {data.get('suite')!r}", file=sys.stderr)
        return 1
    try:
        ref = str(args.evidence.resolve().relative_to(ROOT))
    except ValueError:
        ref = str(args.evidence)
    entries, failed = [], False
    for provider, facts in sorted((data.get("providers") or {}).items()):
        try:
            entries.append(entry_for(provider, facts, evidence_ref=ref, verified_at=str(data.get("finished_at"))))
        except EvidenceError as exc:
            print(f"пропущено: {exc}", file=sys.stderr)
            failed = True
    text = args.matrix.read_text(encoding="utf-8")
    header = "".join(line for line in text.splitlines(keepends=True) if line.startswith("#"))
    matrix = yaml.safe_load(text) or {}
    if entries:
        args.matrix.write_text(render(header, merge(matrix, entries)), encoding="utf-8")
        for e in entries:
            print(f"SUPPORTED: {e['provider']} {e['version_range']} (evidence {ref})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
