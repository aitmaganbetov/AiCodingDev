"""P6-03/P6-08: интерпретация отчётов gate runners (без инструментов): PASS только при согласии кода
выхода и отчёта; exit 0 с ошибками → FAIL; нет/битый отчёт → ERROR; пустой набор → FAIL(EMPTY_SUITE)."""

from __future__ import annotations

import json
import re

import pytest

from aicd_verification import migrations, runners
from aicd_verification.gates import DiffEntry, GateContext, RepoFacts, evaluate_ruff_lint
from aicd_verification.images import (
    DEFAULT_GATE_IMAGES,
    ImagePolicyError,
    default_gate_images,
    image_key_for,
    validate_image_map,
)


def ctx(profile: str = "strict", **kw: object) -> GateContext:
    return GateContext("/workspace", "/aicd-input", profile=profile, **kw)  # type: ignore[arg-type]


def eslint_report(errors: int, warnings: int = 0) -> str:
    msgs = [{"ruleId": "no-undef", "severity": 2, "message": "x is not defined", "line": 3}] * errors
    msgs += [{"ruleId": "no-console", "severity": 1, "message": "console", "line": 4}] * warnings
    return json.dumps(
        [{"filePath": "/workspace/src/a.js", "messages": msgs, "errorCount": errors, "warningCount": warnings}]
    )


@pytest.mark.parametrize(
    ("code", "output", "status", "error"),
    [
        (0, eslint_report(0), "PASS", None),
        (1, eslint_report(2), "FAIL", None),
        (0, eslint_report(1), "FAIL", None),  # FP-03: exit 0, но в отчёте ошибки
        (1, eslint_report(0), "ERROR", "REPORT_UNPARSABLE"),  # код противоречит отчёту
        (2, "Oops! Something went wrong", "ERROR", "INFRA"),  # инструмент упал
        (0, "", "ERROR", "REPORT_UNPARSABLE"),
        (0, "not json at all", "ERROR", "REPORT_UNPARSABLE"),
    ],
)
def test_eslint(code: int, output: str, status: str, error: str | None) -> None:
    v = runners.evaluate_eslint(code, output, ctx())
    assert (v.status, v.error_code) == (status, error)


def test_eslint_findings_are_normalized_and_warnings_block_only_in_critical() -> None:
    v = runners.evaluate_eslint(0, "log line\n" + eslint_report(0, warnings=1), ctx("strict"))
    assert v.status == "PASS"
    (f,) = v.findings
    assert (f.rule_id, f.severity, f.file_path, f.line) == ("no-console", "medium", "src/a.js", 4)
    assert re.fullmatch(r"[0-9a-f]{64}", f.fingerprint)
    assert runners.evaluate_eslint(0, eslint_report(0, warnings=1), ctx("critical")).status == "FAIL"


@pytest.mark.parametrize(
    ("code", "output", "status"),
    [
        (0, "Success: no issues found in 3 source files", "PASS"),
        (1, "pkg/a.py:2: error: Incompatible return value type  [return-value]\nFound 1 error in 1 file", "FAIL"),
        (0, "pkg/a.py:2: error: boom  [misc]", "FAIL"),
        (0, "", "ERROR"),  # exit 0 без итоговой строки
        (2, "mypy: error: unrecognized arguments", "ERROR"),
    ],
)
def test_mypy(code: int, output: str, status: str) -> None:
    assert runners.evaluate_mypy(code, output, ctx()).status == status


@pytest.mark.parametrize(
    ("code", "output", "status"),
    [
        (0, "", "PASS"),
        (2, "src/a.ts(2,3): error TS2322: Type 'string' is not assignable to type 'number'.", "FAIL"),
        (1, "error TS5058: The specified path does not exist: 'tsconfig.json'.", "FAIL"),
        (0, "src/a.ts(1,1): error TS1005: ';' expected.", "FAIL"),
        (1, "", "ERROR"),
        (134, "FATAL ERROR: heap out of memory", "ERROR"),
    ],
)
def test_tsc(code: int, output: str, status: str) -> None:
    assert runners.evaluate_tsc(code, output, ctx()).status == status


def phpstan_report(errors: int) -> str:
    files = {"/workspace/src/a.php": {"errors": errors, "messages": [{"message": "bad", "line": 7}] * errors}}
    return json.dumps({"totals": {"errors": 0, "file_errors": errors}, "files": files if errors else [], "errors": []})


@pytest.mark.parametrize(
    ("code", "output", "status"),
    [
        (0, phpstan_report(0), "PASS"),
        (1, phpstan_report(2), "FAIL"),
        (0, phpstan_report(1), "FAIL"),
        (255, "", "ERROR"),
    ],
)
def test_phpstan(code: int, output: str, status: str) -> None:
    v = runners.evaluate_phpstan(code, output, ctx())
    assert v.status == status
    if status == "FAIL":
        assert v.findings[0].file_path == "src/a.php" and v.findings[0].line == 7


def pyright_report(errors: int) -> str:
    diags = [{"file": "/workspace/a.py", "severity": "error", "message": "bad", "rule": "reportX",
              "range": {"start": {"line": 4}}}] * errors  # fmt: skip
    return json.dumps({"generalDiagnostics": diags, "summary": {"errorCount": errors}})


@pytest.mark.parametrize(
    ("code", "output", "status"),
    [(0, pyright_report(0), "PASS"), (1, pyright_report(1), "FAIL"), (0, pyright_report(1), "FAIL"), (3, "x", "ERROR")],
)
def test_pyright(code: int, output: str, status: str) -> None:
    v = runners.evaluate_pyright(code, output, ctx())
    assert v.status == status
    if status == "FAIL":
        assert v.findings[0].line == 5 and v.findings[0].file_path == "a.py"


def vitest_report(passed: int, failed: int = 0, skipped: int = 0) -> str:
    return json.dumps(
        {
            "numTotalTests": passed + failed + skipped,
            "numPassedTests": passed,
            "numFailedTests": failed,
            "numPendingTests": skipped,
            "numTodoTests": 0,
            "numFailedTestSuites": 1 if failed else 0,
            "testResults": [
                {"name": "/workspace/a.test.js", "assertionResults": [{"status": "failed", "fullTitle": "t"}] * failed}
            ],
        }
    )


@pytest.mark.parametrize(
    ("code", "output", "status", "error"),
    [
        (0, vitest_report(3), "PASS", None),
        (1, vitest_report(2, failed=1), "FAIL", None),
        (0, vitest_report(2, failed=1), "FAIL", None),  # exit 0 с падениями
        (1, vitest_report(0), "FAIL", "EMPTY_SUITE"),
        (0, vitest_report(0, skipped=3), "FAIL", "EMPTY_SUITE"),  # все пропущены
        (0, vitest_report(2, skipped=3), "FAIL", None),  # skipped > 20% в strict
        (1, vitest_report(3), "ERROR", "REPORT_UNPARSABLE"),  # exit ≠ 0 при зелёном отчёте
        (1, "Error: Cannot find module", "ERROR", "REPORT_UNPARSABLE"),
    ],
)
def test_vitest(code: int, output: str, status: str, error: str | None) -> None:
    v = runners.evaluate_vitest(code, output, ctx())
    assert (v.status, v.error_code) == (status, error)


def test_skipped_share_only_finding_in_basic() -> None:
    v = runners.evaluate_vitest(0, vitest_report(2, skipped=3), ctx("basic"))
    assert v.status == "PASS" and v.findings[0].rule_id == "aicd/skipped-tests-share"


def pw_report(expected: int, unexpected: int = 0, flaky: int = 0, errors: int = 0) -> str:
    return json.dumps(
        {
            "stats": {"expected": expected, "unexpected": unexpected, "flaky": flaky, "skipped": 0},
            "errors": [{"message": "x"}] * errors,
        }
    )


@pytest.mark.parametrize(
    ("code", "output", "status", "error"),
    [
        (0, pw_report(2), "PASS", None),
        (1, pw_report(1, unexpected=1), "FAIL", None),
        (1, pw_report(0, errors=1), "FAIL", None),
        (0, pw_report(0), "FAIL", "EMPTY_SUITE"),
        (1, "Error: No tests found", "FAIL", "EMPTY_SUITE"),
        (1, pw_report(2), "ERROR", "REPORT_UNPARSABLE"),
        (1, "browserType.launch: Executable doesn't exist", "ERROR", "REPORT_UNPARSABLE"),
    ],
)
def test_playwright(code: int, output: str, status: str, error: str | None) -> None:
    v = runners.evaluate_playwright(code, output, ctx())
    assert (v.status, v.error_code) == (status, error)


@pytest.mark.parametrize(("code", "status"), [(0, "PASS"), (1, "FAIL"), (2, "FAIL"), (-9, "ERROR")])
def test_build(code: int, status: str) -> None:
    assert runners.evaluate_build(code, "", ctx()).status == status


def test_build_driver_passes_repository_data_only_as_arguments() -> None:
    spec = runners.SPECS[[s.name for s in runners.SPECS].index("npm-build")]
    argv = spec.build_argv(ctx())
    assert argv[:2] == ("sh", "-c") and argv[2] == runners.BUILD_DRIVER
    assert argv[3:] == ("aicd-build", "/workspace", "npm", "run", "build", "--no-update-notifier")
    assert "sha256:" in " ".join(spec.display_argv(ctx())) and runners.BUILD_DRIVER not in spec.display_argv(ctx())


@pytest.mark.parametrize(
    ("code", "output", "status"),
    [
        (0, "pkg/a.py:1:8: F401 [*] `os` imported but unused\nFound 1 error.", "FAIL"),
        (1, "pkg/a.py:1:8: F401 [*] `os` imported but unused\nFound 1 error.", "FAIL"),
        (0, "All checks passed!", "PASS"),
        (1, "", "ERROR"),
        (2, "error: invalid config", "ERROR"),
    ],
)
def test_ruff_lint(code: int, output: str, status: str) -> None:
    assert evaluate_ruff_lint(code, output, ctx()).status == status


# --- Coverage ----------------------------------------------------------------------------------------


def coverage_output(files: dict[str, list[int]] | None, nonce: str = "n1", summary: str = "4 passed in 0.1s") -> str:
    report = None if files is None else {"files": files, "totals": [0, 0]}
    return f"....\n{summary}\nAICD_COVERAGE_REPORT {nonce} {json.dumps(report)}\n"


@pytest.mark.parametrize(
    ("output", "profile", "status", "error"),
    [
        (coverage_output({"src/app/a.py": [90, 100]}), "strict", "PASS", None),
        (coverage_output({"src/app/a.py": [80, 100]}), "strict", "FAIL", "THRESHOLD"),
        (coverage_output({"src/app/a.py": [10, 100]}), "basic", "PASS", None),  # basic: только отчёт
        (coverage_output(None), "strict", "ERROR", "REPORT_UNPARSABLE"),  # отчёт не создан этим прогоном
        ("4 passed in 0.1s\n", "strict", "ERROR", "REPORT_UNPARSABLE"),  # нет маркера драйвера
        (coverage_output({"src/a.py": [99, 100]}, nonce="forged"), "strict", "ERROR", "REPORT_UNPARSABLE"),
        (coverage_output({"src/a.py": [99, 100]}, summary="1 failed, 3 passed"), "strict", "FAIL", None),
        (coverage_output({"tests/test_a.py": [5, 5]}), "strict", "ERROR", "REPORT_UNPARSABLE"),  # нет core
        ("AICD_COVERAGE_REPORT n1 {not json\n4 passed", "strict", "ERROR", "REPORT_UNPARSABLE"),
    ],
)
def test_coverage(output: str, profile: str, status: str, error: str | None) -> None:
    code = 1 if "failed" in output else 0
    v = runners.evaluate_coverage(code, output, ctx(profile, nonce="n1"))
    assert (v.status, v.error_code) == (status, error), v.message


def test_core_coverage_globs_and_exclusions() -> None:
    files = {
        "src/a.py": (50, 100),
        "src/tests/test_a.py": (10, 10),
        "lib/migrations/0001.py": (0, 50),
        "scripts/tool.py": (0, 100),
        "src/empty.py": (0, 0),
    }
    assert runners.core_coverage(files) == (50.0, 1, "core")
    assert runners.core_coverage({"tool.py": (3, 4)}) == (75.0, 1, "all-non-test")
    assert runners.core_coverage({}, ("x/**",)) == (None, 0, "all-non-test")


def test_coverage_config_threshold_only_tightens() -> None:
    out = coverage_output({"src/a.py": [88, 100]})
    assert runners.evaluate_coverage(0, out, ctx("strict", nonce="n1", config={"min_percent": 90})).status == "FAIL"
    assert runners.evaluate_coverage(0, out, ctx("strict", nonce="n1", config={"min_percent": 10})).status == "PASS"


def test_parse_coverage_reports() -> None:
    data = {"files": {"src/a.py": {"summary": {"covered_lines": 3, "num_statements": 4}}}}
    assert runners.parse_coverage_json(data) == {"src/a.py": (3, 4)}
    assert runners.parse_coverage_json({"nope": 1}) is None
    xml = (
        '<?xml version="1.0" ?><coverage line-rate="0.5"><packages><package><classes>'
        '<class filename="src/a.py"><lines><line number="1" hits="1"/><line number="2" hits="0"/></lines></class>'
        "</classes></package></packages></coverage>"
    )
    assert runners.parse_cobertura(xml) == {"src/a.py": (1, 2)}
    assert runners.parse_cobertura("<coverage") is None
    assert runners.parse_cobertura('<!DOCTYPE x [<!ENTITY a "b">]><coverage/>') is None
    assert runners.parse_cobertura("<other/>") is None


def test_extract_json_skips_log_lines() -> None:
    assert runners.extract_json('npm notice\n{"a": 1}\n') == {"a": 1}
    assert runners.extract_json("[1, 2]") == [1, 2]
    assert runners.extract_json("no json") is None
    assert runners.extract_json("") is None


# --- Образы ------------------------------------------------------------------------------------------


def test_default_images_are_pinned_and_policy_rejects_latest() -> None:
    assert validate_image_map(DEFAULT_GATE_IMAGES, require_digest=True) == []
    assert default_gate_images()["postgres"].startswith("postgres:17-alpine@sha256:")
    errors = validate_image_map({"a": "node:latest", "b": "node", "c": "node:22", "d": " "}, require_digest=True)
    assert len(errors) == 6  # latest/implicit ×2, без digest ×3, пустая ссылка
    assert validate_image_map({"c": "node:22"}, require_digest=False) == []
    with pytest.raises(ImagePolicyError):
        default_gate_images({"ruff": "pipelinecomponents/ruff:latest"})
    assert (
        image_key_for("vitest") == "node" and image_key_for("x", "y") == "y" and image_key_for("semgrep") == "semgrep"
    )


# --- Миграции: статический анализ ---------------------------------------------------------------------


def test_destructive_sql_operations_in_upgrade_only() -> None:
    texts = {
        "db/migrations/0002_x.up.sql": (
            "-- DROP TABLE commented_out;\n"
            "ALTER TABLE a DROP COLUMN email;\n"
            "ALTER TABLE a DROP CONSTRAINT ck_a;\n"
            "TRUNCATE b;\n"
            "DELETE FROM c;\n"
            "DELETE FROM d WHERE id = 1;\n"
            "ALTER TABLE a ALTER COLUMN name TYPE varchar(10);\n"
            "/* DROP SCHEMA x; */\n"
            "DROP TABLE legacy;\n"
        ),
        "db/migrations/0002_x.down.sql": "DROP TABLE everything;\n",
        "src/app.sql": "DROP TABLE not_a_migration;\n",
    }
    ops = migrations.destructive_operations(texts)
    assert sorted((o.kind, o.line) for o in ops) == [
        ("delete_all", 5),
        ("drop_column", 2),
        ("drop_table", 9),
        ("truncate", 4),
        ("type_change", 7),
    ]
    assert all(o.path == "db/migrations/0002_x.up.sql" for o in ops)


def test_destructive_alembic_operations() -> None:
    body = (
        "def upgrade():\n    op.drop_table('t')\n    op.alter_column('a', 'b', type_=sa.String(5))\n"
        "    op.add_column('a', sa.Column('c'))\n\n\ndef downgrade():\n    op.drop_column('a', 'c')\n"
    )
    ops = migrations.destructive_operations({"alembic/versions/0002.py": body})
    assert sorted((o.kind, o.line) for o in ops) == [("drop_table", 2), ("type_change", 3)]
    assert (
        migrations.destructive_operations({"alembic/versions/0003.py": "def downgrade():\n    op.drop_table('t')\n"})
        == []
    )


def test_approval_subject_is_commit_agnostic_and_matches_orchestration_hash() -> None:
    from aicd_orchestration.approvals import subject_hash as orchestration_hash

    ops = migrations.destructive_operations({"db/migrations/1.up.sql": "DROP TABLE a;\n"})
    subject = migrations.approval_subject(ops)
    assert subject["kind"] == "destructive_migration" and "commit" not in json.dumps(subject)
    assert migrations.subject_hash(subject) == orchestration_hash(subject)
    (finding,) = migrations.destructive_findings(ops)
    assert finding.severity == "high" and finding.rule_id == "migration/destructive-drop-table"


def _facts(files: dict[str, str], texts: dict[str, str] | None = None) -> RepoFacts:
    changes = tuple(DiffEntry(p, "A", "000000", "100644", "0" * 40) for p in files)
    return RepoFacts(frozenset(files), texts or {}, changes, files)


def test_layout_detection_and_driver_argv() -> None:
    updown = _facts(
        {
            "db/migrations/0001_a.up.sql": "CREATE TABLE a(id int);",
            "db/migrations/0001_a.down.sql": "DROP TABLE a;",
            "db/migrations/0002_b.up.sql": "ALTER TABLE a ADD COLUMN b int;",
            "db/migrations/0002_b.down.sql": "ALTER TABLE a DROP COLUMN b;",
        }
    )
    layout = migrations.detect_layout(updown)
    assert layout is not None and layout.kind == "sql-updown"
    assert layout.down_files == ("db/migrations/0002_b.down.sql", "db/migrations/0001_a.down.sql")
    argv = migrations.build_argv(GateContext("/workspace", "/in", facts=updown))
    assert argv[:5] == ("sh", "-c", migrations.MIGRATION_DRIVER, "aicd-migrate", "sql") and argv[5] == "2"
    assert argv[6:] == (
        "./db/migrations/0001_a.up.sql",
        "./db/migrations/0002_b.up.sql",
        "./db/migrations/0002_b.down.sql",
        "./db/migrations/0001_a.down.sql",
    )
    display = migrations.display_argv(GateContext("/workspace", "/in", facts=updown))
    assert display[2].startswith("<aicd-migration-driver sha256:")
    forward = migrations.detect_layout(_facts({"migrations/V1__init.sql": "CREATE TABLE a(id int);"}))
    assert forward is not None and forward.kind == "sql-forward"
    alembic = _facts({"alembic.ini": "", "alembic/env.py": ""}, {"alembic.ini": "script_location = %(here)s/alembic\n"})
    lay = migrations.detect_layout(alembic)
    assert lay is not None and lay.kind == "alembic" and lay.directory == "alembic"
    assert migrations.probe(GateContext("/w", "/i", facts=alembic)) == ("alembic", "--version")
    assert migrations.detect_layout(_facts({"src/a.py": ""})) is None
    forced = migrations.detect_layout(updown, {"migrations": {"kind": "sql-forward", "path": "db/migrations"}})
    assert forced is not None and forced.kind == "sql-forward"


SHA = {k: k[0] * 64 for k in ("a", "b", "c", "d")}


def driver_output(result: str, schemas: dict[str, str], steps: str = "") -> str:
    lines = [steps] if steps else []
    lines += [f"AICD_SCHEMA phase={p} sha256={s}" for p, s in schemas.items()]
    lines.append(f"AICD_MIGRATION_RESULT {result}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("code", "output", "status", "error"),
    [
        (0, driver_output("ok", {"base": SHA["a"], "upgrade1": SHA["b"], "downgrade": SHA["a"], "upgrade2": SHA["b"]}),
         "PASS", None),
        (0, driver_output("ok", {"base": SHA["a"], "upgrade1": SHA["b"], "downgrade": SHA["c"], "upgrade2": SHA["b"]}),
         "FAIL", None),
        (0, driver_output("ok", {"base": SHA["a"], "upgrade1": SHA["b"], "downgrade": SHA["a"], "upgrade2": SHA["d"]}),
         "FAIL", None),
        (1, driver_output("fail upgrade1", {"base": SHA["a"]},
                          "AICD_MIGRATION step=upgrade1 status=fail file=./db/migrations/0002.up.sql"), "FAIL", None),
        (3, "AICD_MIGRATION_RESULT fail initdb\n", "ERROR", "INFRA"),
        (0, "garbage", "ERROR", "REPORT_UNPARSABLE"),
        (1, driver_output("ok", {"base": SHA["a"], "upgrade1": SHA["b"]}), "ERROR", "REPORT_UNPARSABLE"),
        (0, driver_output("ok", {"base": SHA["a"]}), "ERROR", "REPORT_UNPARSABLE"),
    ],
)  # fmt: skip
def test_migration_evaluate(code: int, output: str, status: str, error: str | None) -> None:
    facts = _facts(
        {"db/migrations/0001_a.up.sql": "CREATE TABLE a(id int);", "db/migrations/0001_a.down.sql": "DROP TABLE a;"}
    )
    v = migrations.evaluate(code, output, GateContext("/w", "/i", facts=facts))
    assert (v.status, v.error_code) == (status, error)
    assert v.approval is None  # нет деструктивных операций в upgrade


def test_migration_destructive_sets_approval_subject_and_forward_only_rules() -> None:
    facts = _facts(
        {"db/migrations/0002.up.sql": "ALTER TABLE a DROP COLUMN b;", "db/migrations/0002.down.sql": "SELECT 1;"}
    )
    ok = driver_output("ok", {"base": SHA["a"], "upgrade1": SHA["b"], "downgrade": SHA["a"], "upgrade2": SHA["b"]})
    v = migrations.evaluate(0, ok, GateContext("/w", "/i", facts=facts))
    assert v.status == "PASS" and v.approval is not None  # движок не даст PASS без APPROVED approval
    assert any(f.rule_id == "migration/destructive-drop-column" for f in v.findings)
    fwd = _facts({"migrations/V1__init.sql": "CREATE TABLE a(id int);"})
    up_only = driver_output("ok", {"base": SHA["a"], "upgrade1": SHA["b"]})
    assert migrations.evaluate(0, up_only, GateContext("/w", "/i", facts=fwd, profile="strict")).status == "FAIL"
    basic = migrations.evaluate(0, up_only, GateContext("/w", "/i", facts=fwd, profile="basic"))
    assert basic.status == "PASS" and basic.findings[0].rule_id == "migration/irreversible"
