"""P3-14: детектор применимости gates v0, интерпретация результатов и SCOPE-сканер (без БД)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aicd_verification import scope_scan
from aicd_verification.gates import (
    GATE_SPECS,
    SCANNER_SOURCE,
    DiffEntry,
    RepoFacts,
    detect_applicability,
    finding_counts,
    interpret_pytest,
    interpret_scope,
    npm_test_script,
    parse_raw_diff,
    secret_rules,
)

FAKE_AWS_KEY = "AKIA" + "Q7ZK4M2XW9TR3BNV"  # не настоящий ключ; собран конкатенацией


def facts(files: set[str], texts: dict[str, str] | None = None, changes: tuple[DiffEntry, ...] = ()) -> RepoFacts:
    return RepoFacts(files=frozenset(files), root_texts=texts or {}, changes=changes)


def change(path: str, status: str = "M", mode: str = "100644", target: str | None = None) -> DiffEntry:
    return DiffEntry(path, status, "100644", mode, "0" * 40, target)


def test_scope_always_applicable_even_for_empty_repo() -> None:
    result = detect_applicability(facts({"README.md"}))
    assert result["scope"].applicable
    assert not result["ruff-lint"].applicable
    assert not result["ruff-format"].applicable
    assert not result["pytest-unit"].applicable
    assert not result["npm-unit"].applicable
    assert "checked" not in result["ruff-lint"].reason  # причина — текст детектора
    assert result["ruff-lint"].markers  # перечень проверенных маркеров для not_applicable_reason


def test_python_with_pyproject_enables_ruff_lint_and_pytest() -> None:
    result = detect_applicability(facts({"pyproject.toml", "pkg/mod.py", "tests/test_mod.py"}))
    assert result["ruff-lint"].applicable
    assert "pyproject.toml" in result["ruff-lint"].markers
    assert result["pytest-unit"].applicable
    assert not result["ruff-format"].applicable  # формат не сконфигурирован


def test_python_without_config_has_no_lint_but_tests_detected() -> None:
    result = detect_applicability(facts({"app.py", "app_test.py"}))
    assert not result["ruff-lint"].applicable
    assert result["pytest-unit"].applicable


def test_ruff_format_requires_section_and_changed_python() -> None:
    texts = {"pyproject.toml": "[project]\nname='x'\n\n[tool.ruff.format]\nquote-style='double'\n"}
    files = {"pyproject.toml", "a.py"}
    assert not detect_applicability(facts(files, texts))["ruff-format"].applicable  # нет изменённых *.py
    result = detect_applicability(facts(files, texts, (change("a.py"),)))
    assert result["ruff-format"].applicable
    deleted_only = detect_applicability(facts(files, texts, (change("a.py", status="D"),)))
    assert not deleted_only["ruff-format"].applicable
    ruff_toml = detect_applicability(facts({"ruff.toml", "a.py"}, {"ruff.toml": "[format]\n"}, (change("a.py"),)))
    assert ruff_toml["ruff-format"].applicable


@pytest.mark.parametrize(
    ("package_json", "expected"),
    [
        ('{"scripts": {"test": "vitest run"}}', True),
        ('{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}', False),
        ('{"scripts": {"build": "tsc"}}', False),
        ('{"scripts": {"test": "  "}}', False),
        ("not json", False),
        ("[]", False),
    ],
)
def test_npm_unit_applicability(package_json: str, expected: bool) -> None:
    f = facts({"package.json", "index.js"}, {"package.json": package_json})
    assert (npm_test_script(f) is not None) is expected
    assert detect_applicability(f)["npm-unit"].applicable is expected


def test_parse_raw_diff() -> None:
    raw = (
        f":000000 100644 {'0' * 40} {'a' * 40} A\0new file.py\0"
        f":100644 120000 {'b' * 40} {'c' * 40} T\0link\0"
        f":100644 000000 {'d' * 40} {'0' * 40} D\0gone.txt\0"
    )
    entries = parse_raw_diff(raw)
    assert [(e.path, e.status, e.new_mode) for e in entries] == [
        ("new file.py", "A", "100644"),
        ("link", "T", "120000"),
        ("gone.txt", "D", "000000"),
    ]
    with pytest.raises(ValueError, match="unexpected"):
        parse_raw_diff("garbage\0x\0")


def test_gate_specs_cover_v0_and_scope_runs_scanner_without_shell() -> None:
    assert {"scope", "ruff-lint", "ruff-format", "pytest-unit", "npm-unit"} <= set(GATE_SPECS)
    scope = GATE_SPECS["scope"]
    assert scope.gate_type == "SCOPE" and not scope.executes_repository
    from aicd_verification.gates import GateContext

    argv = scope.build_argv(GateContext("/workspace", "/aicd-input"))
    assert argv[:5] == ("python3", "-I", "-S", "-c", SCANNER_SOURCE)
    display = " ".join(scope.display_argv(GateContext("/workspace", "/aicd-input")))
    assert "sha256:" in display and len(display) < 300  # в evidence.command — хеш сканера, не исходник
    fmt = GATE_SPECS["ruff-format"].build_argv(GateContext("/workspace", "/in", ("-rf.py", "b.py")))
    assert fmt[-3:] == ("--", "-rf.py", "b.py")  # имена файлов не трактуются как опции


@pytest.mark.parametrize(
    ("code", "output", "status", "error"),
    [
        (0, "3 passed in 0.10s", "PASS", None),
        (0, "2 passed, 1 failed in 0.1s", "FAIL", None),  # FP-03: exit 0, но есть падения
        (0, "", "ERROR", "REPORT_UNPARSABLE"),
        (1, "1 failed, 2 passed", "FAIL", None),
        (5, "no tests ran in 0.01s", "FAIL", "EMPTY_SUITE"),  # FP-05
        (2, "interrupted", "ERROR", "INFRA"),
        (4, "usage error", "ERROR", "INFRA"),
    ],
)
def test_interpret_pytest(code: int, output: str, status: str, error: str | None) -> None:
    verdict = interpret_pytest(code, output)
    assert (verdict.status, verdict.error_code) == (status, error)


def test_interpret_scope_requires_consistent_report() -> None:
    clean = json.dumps({"violations_total": 0, "violations": []})
    dirty = json.dumps({"violations_total": 1, "violations": [{"severity": "critical"}]})
    assert interpret_scope(0, clean).status == "PASS"
    assert interpret_scope(1, dirty).status == "FAIL"
    assert interpret_scope(0, dirty).status == "ERROR"  # код выхода противоречит отчёту
    assert interpret_scope(0, "").error_code == "REPORT_UNPARSABLE"
    assert interpret_scope(137, "Killed").status == "ERROR"
    assert finding_counts("scope", "FAIL", dirty)["critical"] == 1


def _scan(entries: list[DiffEntry], diff: str) -> dict[str, object]:
    spec = {"entries": [e.as_json() for e in entries], "rules": secret_rules(), "allow_substrings": ["EXAMPLE", "${"]}
    return scope_scan.scan(spec, diff)


def test_scanner_detects_secret_without_printing_value() -> None:
    diff = (
        "diff --git a/cfg.py b/cfg.py\n--- a/cfg.py\n+++ b/cfg.py\n@@ -1,0 +2,2 @@\n"
        f"+AWS_KEY = '{FAKE_AWS_KEY}'\n+ok = 1\n"
    )
    report = _scan([change("cfg.py")], diff)
    assert report["violations_total"] == 1
    assert report["violations"] == [
        {"rule": "secret-scan/aws-access-key-id", "severity": "critical", "path": "cfg.py", "line": 2}
    ]
    assert FAKE_AWS_KEY not in json.dumps(report)


def test_scanner_ignores_placeholders_and_removed_lines() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n"
        f"-key = '{FAKE_AWS_KEY}'\n+key = 'AKIAIOSFODNN7EXAMPLE'\n"
        "+password: str = get_password()\n+token = os.environ['TOKEN']\n"
    )
    assert _scan([change("a.py")], diff)["violations_total"] == 0


@pytest.mark.parametrize(
    ("entry", "rule"),
    [
        (change(".git/hooks/pre-commit", "A"), "git_internal_path"),
        (change("sub/.GIT/config", "A"), "git_internal_path"),
        (change("../escape.txt", "A"), "path_traversal"),
        (change("/etc/passwd", "A"), "path_absolute"),
        (change("lnk", "A", "120000", "/etc/shadow"), "symlink_outside_worktree"),
        (change("a/lnk", "A", "120000", "../../outside"), "symlink_outside_worktree"),
        (change("lnk", "A", "120000", ".git/config"), "symlink_into_git_dir"),
        (change("lnk", "A", "120000", None), "symlink_unreadable"),
        (change("vendor/lib", "A", "160000"), "submodule_change"),
    ],
)
def test_scanner_path_rules(entry: DiffEntry, rule: str) -> None:
    report = _scan([entry], "")
    assert rule in [v["rule"] for v in report["violations"]]  # type: ignore[attr-defined]


def test_scanner_allows_symlink_inside_worktree() -> None:
    assert _scan([change("docs/lnk", "A", "120000", "../README.md")], "")["violations_total"] == 0


def test_scanner_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "diff.patch").write_text("diff --git a/x b/x\n+++ b/x\n@@ -0,0 +1 @@\n+x = 1\n")
    spec = {"entries": [change("x").as_json()], "diff_file": "diff.patch", "rules": secret_rules()}
    (tmp_path / "scope.json").write_text(json.dumps(spec))
    assert scope_scan.main(["scan", str(tmp_path / "scope.json")]) == 0
    assert json.loads(capsys.readouterr().out)["added_lines_scanned"] == 1
    spec["diff_file"] = "../evil"
    (tmp_path / "scope.json").write_text(json.dumps(spec))
    assert scope_scan.main(["scan", str(tmp_path / "scope.json")]) == 2
    assert scope_scan.main(["scan"]) == 2
