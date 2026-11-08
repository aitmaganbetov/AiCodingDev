"""P1-04 / P1-14: CI-гейты сами проверены негативными тестами."""

from pathlib import Path

import pytest

from scripts import check_no_ai_sdk as gate
from scripts import traceability

ROOT = Path(__file__).resolve().parents[2]


def test_real_lockfiles_are_parsed() -> None:
    py = gate.python_packages((ROOT / "uv.lock").read_text())
    js = gate.js_packages((ROOT / "apps" / "web" / "pnpm-lock.yaml").read_text())
    assert {"fastapi", "sqlalchemy", "psycopg"} <= py
    assert {"react", "vite", "@tanstack/react-query"} <= js


def test_gate_detects_forbidden_sdks() -> None:
    py_lock = (
        '[[package]]\nname = "fastapi"\n\n[[package]]\nname = "anthropic"\n\n[[package]]\nname = "langchain-core"\n'
    )
    js_lock = "packages:\n\n  '@anthropic-ai/sdk@0.30.0':\n    resolution: {}\n\n  react@18.3.1:\n    resolution: {}\n"
    assert gate.violations(py_lock, js_lock) == ["python: anthropic", "python: langchain-core", "js: @anthropic-ai/sdk"]


def test_current_dependencies_pass_gate() -> None:
    assert gate.main() == 0


def test_traceability_parsing_and_current_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reqs = traceability.requirements("| FR-X-01 | a | §1 | P1 | P2 | — |\n| NFR-Y-02 | b | §2 | x |\n")
    assert reqs == {"FR-X-01": "P1", "NFR-Y-02": "P0"}
    backlog = traceability.tasks("| P1-01 | t | FR-X-01, NFR-Y-02 | — | P0 | dod | TODO |\n")
    assert backlog["P1-01"]["reqs"] == {"FR-X-01", "NFR-Y-02"}
    report = tmp_path / "trace.md"
    monkeypatch.setattr("sys.argv", ["traceability", "--report", str(report)])
    assert traceability.main() == 0
    assert "| FR-IAM-01 |" in report.read_text()
