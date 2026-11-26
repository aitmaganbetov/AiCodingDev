"""`scripts/cli_matrix.py`: строка SUPPORTED — только из полного evidence набора real-cli-smoke@1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aicd_cli_adapters import compat
from scripts import cli_matrix

HEADER = "# Compatibility matrix\n# строки — только по evidence\n"


def facts(version: str = "1.2.3", **checks: dict[str, Any]) -> dict[str, Any]:
    passed = {name: {"status": "passed"} for name in cli_matrix.REQUIRED_CHECKS}
    passed["minimal_task"] |= {"usage_source": "cli_reported", "tool_events": 3}
    passed.update(checks)
    return {
        "detected_version": version,
        "adapter_version": "1.0.0",
        "detected_capabilities": {"noninteractive_mode": True, "machine_readable_output": "jsonl_stream"},
        "checks": passed,
    }


def run(
    tmp_path: Path, providers: dict[str, Any], existing: str = "schema_version: 1\nentries: []\n"
) -> tuple[int, Path]:
    evidence = tmp_path / "smoke.json"
    evidence.write_text(
        json.dumps({"suite": cli_matrix.SUITE, "finished_at": "2026-11-11T06:00:00Z", "providers": providers})
    )
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text(HEADER + existing)
    return cli_matrix.main([str(evidence), "--matrix", str(matrix)]), matrix


def test_full_evidence_becomes_a_supported_row_for_exactly_that_version(tmp_path: Path) -> None:
    code, matrix = run(tmp_path, {"codex": facts("0.167.0"), "claude": facts("2.1.319")})
    assert code == 0
    assert matrix.read_text().startswith(HEADER), "пояснение в начале файла сохранено"
    entries = compat.load_matrix(matrix)
    assert compat.evaluate("codex", "codex-cli 0.167.0", entries)[0] == "SUPPORTED"
    assert compat.evaluate("claude", "2.1.319 (Claude Code)", entries)[0] == "SUPPORTED"
    # Соседняя версия набором не проверялась — подтверждать её нечем.
    assert compat.evaluate("codex", "0.167.1", entries)[0] == "UNTESTED"
    codex = next(e for e in entries if e.provider == "codex")
    assert codex.capabilities["token_usage_exposed"] is True and codex.capabilities["cancel_signal_safe"] is True
    assert codex.capabilities["noninteractive_mode"] is True, "обнаруженное probe не потеряно"
    assert codex.command_profiles == ("codex/agent_task@1",)


@pytest.mark.parametrize("broken", [{"status": "failed"}, {"status": "skipped"}, None])
def test_incomplete_evidence_never_marks_a_version_supported(tmp_path: Path, broken: dict[str, Any] | None) -> None:
    bad = facts("0.167.0")
    if broken is None:
        del bad["checks"]["cancel"]
    else:
        bad["checks"]["cancel"] = broken
    code, matrix = run(tmp_path, {"codex": bad, "claude": facts("2.1.319")})
    assert code == 1, "неполный evidence — ошибка, а не тихий пропуск"
    entries = compat.load_matrix(matrix)
    assert compat.evaluate("codex", "0.167.0", entries)[0] == "UNTESTED"
    assert compat.evaluate("claude", "2.1.319", entries)[0] == "SUPPORTED", "полный evidence другого провайдера учтён"


def test_rerun_replaces_the_row_and_keeps_other_versions(tmp_path: Path) -> None:
    existing = (
        "schema_version: 1\nentries:\n"
        "- {id: codex-0.166.0, provider: codex, version_range: '==0.166.0', status: SUPPORTED}\n"
        "- {id: codex-0.167.0, provider: codex, version_range: '==0.167.0', status: UNSUPPORTED}\n"
    )
    code, matrix = run(tmp_path, {"codex": facts("0.167.0")}, existing)
    assert code == 0
    entries = compat.load_matrix(matrix)
    assert [e.id for e in entries] == ["codex-0.166.0", "codex-0.167.0"]
    assert compat.evaluate("codex", "0.167.0", entries)[0] == "SUPPORTED"
    assert compat.evaluate("codex", "0.166.0", entries)[0] == "SUPPORTED"


def test_evidence_of_another_suite_is_rejected(tmp_path: Path) -> None:
    evidence = tmp_path / "other.json"
    evidence.write_text(json.dumps({"suite": "something-else@1", "providers": {"codex": facts()}}))
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text(HEADER + "schema_version: 1\nentries: []\n")
    assert cli_matrix.main([str(evidence), "--matrix", str(matrix)]) == 1
    assert compat.load_matrix(matrix) == ()
