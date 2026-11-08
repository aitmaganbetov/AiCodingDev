"""Gate runners Phase 6 (P6-03; QUALITY_GATES §3.1, §5.1): ESLint, PHPStan, typecheck (mypy/pyright/tsc),
build (npm run build / python -m build), Vitest, Playwright, coverage (pytest-cov).

Правила исполнения (ADR-0010, ТЗ §14):
* только фиксированный argv без shell-интерполяции; код репозитория — недоверенный: всё, что его
  исполняет (npm-скрипты, тесты, сборка), запускается в verification-контейнере (`network none`,
  read-only worktree); `LocalProcessRunner` — только dev/тесты (в production запрещён wiring'ом);
* сборка пишет артефакты, а worktree смонтирован read-only: платформенный драйвер `BUILD_DRIVER`
  (фиксированный POSIX sh-скрипт, sha256 в evidence) копирует дерево без `.git` в `$HOME/aicd-build`
  (tmpfs контейнера) и исполняет фиксированную команду — данные репозитория передаются только
  позиционными аргументами (`"$@"`), в текст скрипта не подставляются;
* инструменты из `node_modules` репозитория (tsc, vitest, playwright) вызываются как `node <файл>`;
  отсутствие файла → probe падает → ERROR(TOOL_UNAVAILABLE), никогда PASS;
* Playwright — только при `playwright.config.*`; сеть контейнера `none` (браузинг невозможен).

Интерпретация: PASS только при согласии кода выхода и разобранного отчёта; exit 0 с ошибками в
отчёте → FAIL (FP-03); ненулевой код без отчёта → ERROR (FP-04); пустой набор тестов → FAIL
(EMPTY_SUITE, FP-05); неразбираемый отчёт → ERROR(REPORT_UNPARSABLE).
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET  # отчёт cobertura; expat не загружает внешние сущности
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from aicd_verification.findings import Finding, make_finding
from aicd_verification.gates import (
    Applicability,
    GateContext,
    GateProvider,
    GateSpec,
    RepoFacts,
    StackInfo,
    Verdict,
    apply_test_policy,
    interpret_pytest,
    pytest_counts,
)
from aicd_verification.profiles import thresholds

DEFAULT_TIMEOUT = 900
BUILD_DRIVER = """set -eu
src=$1
shift
dst="${HOME:?}/aicd-build"
rm -rf "$dst"
mkdir -p "$dst"
cp -a "$src"/. "$dst"/
rm -rf "$dst/.git"
cd "$dst"
exec "$@"
"""
BUILD_DRIVER_SHA256 = hashlib.sha256(BUILD_DRIVER.encode()).hexdigest()
COVERAGE_DRIVER = Path(__file__).with_name("coverage_driver.py").read_text(encoding="utf-8")
COVERAGE_DRIVER_SHA256 = hashlib.sha256(COVERAGE_DRIVER.encode()).hexdigest()
COVERAGE_MARKER = "AICD_COVERAGE_REPORT"
DEFAULT_CORE_GLOBS = ("src/**", "app/**", "packages/**", "lib/**")

# --- Детекция стека ------------------------------------------------------------------------------

_JS_EXT = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")
_ESLINT_CONFIGS = tuple(
    [f"eslint.config.{e}" for e in ("js", "mjs", "cjs", "ts", "mts", "cts")]
    + [".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml"]
)
_PLAYWRIGHT_CONFIGS = tuple(f"playwright.config.{e}" for e in ("ts", "js", "mjs", "cjs", "mts", "cts"))
_PHPSTAN_CONFIGS = ("phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon")
_TEST_PY_RE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py)$")


def _source_files(facts: RepoFacts, exts: tuple[str, ...]) -> list[str]:
    return [p for p in facts.files if p.endswith(exts) and "node_modules/" not in p and not p.startswith("vendor/")]


def package_json(facts: RepoFacts) -> dict[str, Any]:
    try:
        data = json.loads(facts.root_texts.get("package.json", ""))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def npm_deps(facts: RepoFacts) -> set[str]:
    data = package_json(facts)
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(str(k) for k in section)
    return deps


def npm_script(facts: RepoFacts, name: str) -> str | None:
    scripts = package_json(facts).get("scripts")
    value = scripts.get(name) if isinstance(scripts, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _toml_section(text: str, section: str) -> bool:
    return bool(re.search(rf"(?m)^\s*\[{re.escape(section)}\]", text))


def _has_js(facts: RepoFacts, stack: StackInfo) -> bool:
    return bool(_source_files(facts, _JS_EXT)) or stack.has_lang("javascript") or stack.has_lang("typescript")


def _has_python(facts: RepoFacts, stack: StackInfo) -> bool:
    return any(p.endswith(".py") for p in facts.files) or stack.has_lang("python")


def mypy_config(facts: RepoFacts) -> str | None:
    if _toml_section(facts.root_texts.get("pyproject.toml", ""), "tool.mypy"):
        return "pyproject.toml [tool.mypy]"
    for name in ("mypy.ini", ".mypy.ini"):
        if name in facts.files:
            return name
    if _toml_section(facts.root_texts.get("setup.cfg", ""), "mypy"):
        return "setup.cfg [mypy]"
    return None


def _mypy_has_files_setting(facts: RepoFacts) -> bool:
    texts = [facts.root_texts.get(n, "") for n in ("pyproject.toml", "mypy.ini", ".mypy.ini", "setup.cfg")]
    return any(re.search(r"(?m)^\s*files\s*=", t) for t in texts)


def detect(facts: RepoFacts, stack: StackInfo) -> dict[str, Applicability]:
    """Применимость gates этого модуля: маркеры commit + `project_technologies`."""
    result: dict[str, Applicability] = {}
    deps = npm_deps(facts)
    has_js = _has_js(facts, stack)
    has_py = _has_python(facts, stack)

    eslint_cfg = [c for c in _ESLINT_CONFIGS if c in facts.files]
    if "eslintConfig" in package_json(facts):
        eslint_cfg.append("package.json#eslintConfig")
    eslint_dep = "eslint" in deps or stack.has("npm", "eslint")
    if has_js and eslint_cfg:
        result["eslint"] = Applicability(True, "JS/TS sources and ESLint configuration", tuple(eslint_cfg))
    else:
        why = "no JS/TS sources" if not has_js else "no ESLint configuration"
        if has_js and eslint_dep:
            why = "eslint dependency without an ESLint configuration file"
        result["eslint"] = Applicability(False, why, ("*.js/*.ts", *_ESLINT_CONFIGS[:2], "npm:eslint"))

    if "tsconfig.json" in facts.files:
        result["tsc"] = Applicability(True, "tsconfig.json present", ("tsconfig.json",))
    else:
        ts = bool(_source_files(facts, (".ts", ".tsx", ".mts", ".cts"))) or stack.has_lang("typescript")
        result["tsc"] = Applicability(
            False,
            "typescript sources without tsconfig.json" if ts else "no tsconfig.json",
            ("tsconfig.json", "*.ts", "lang:typescript"),
        )

    vitest = "vitest" in deps or stack.has("npm", "vitest")
    if vitest and "package.json" in facts.files:
        result["vitest"] = Applicability(True, "vitest dependency in package.json", ("package.json", "npm:vitest"))
    else:
        result["vitest"] = Applicability(False, "no vitest dependency", ("package.json", "npm:vitest"))

    pw_cfg = [c for c in _PLAYWRIGHT_CONFIGS if c in facts.files]
    if pw_cfg:
        result["playwright"] = Applicability(True, "Playwright is configured", tuple(pw_cfg))
    else:
        result["playwright"] = Applicability(False, "no playwright.config.*", _PLAYWRIGHT_CONFIGS[:3])

    if npm_script(facts, "build"):
        result["npm-build"] = Applicability(True, "package.json defines scripts.build", ("package.json#scripts.build",))
    else:
        result["npm-build"] = Applicability(False, "no scripts.build in package.json", ("package.json#scripts.build",))

    phpstan_cfg = [c for c in _PHPSTAN_CONFIGS if c in facts.files]
    has_php = bool(_source_files(facts, (".php",))) or "composer.json" in facts.files or stack.has_lang("php")
    if has_php and phpstan_cfg:
        result["phpstan"] = Applicability(True, "PHP sources and PHPStan configuration", tuple(phpstan_cfg))
    else:
        result["phpstan"] = Applicability(
            False, "no PHP sources" if not has_php else "no phpstan.neon configuration", ("*.php", *_PHPSTAN_CONFIGS)
        )

    cfg = mypy_config(facts)
    if has_py and cfg:
        result["mypy"] = Applicability(True, f"mypy configured ({cfg})", (cfg,))
    else:
        result["mypy"] = Applicability(
            False,
            "no python sources" if not has_py else "mypy is not configured",
            ("[tool.mypy]", "mypy.ini", "setup.cfg [mypy]"),
        )
    pyright_cfg = "pyrightconfig.json" in facts.files or _toml_section(
        facts.root_texts.get("pyproject.toml", ""), "tool.pyright"
    )
    if has_py and pyright_cfg:
        result["pyright"] = Applicability(True, "pyright configured", ("pyrightconfig.json", "[tool.pyright]"))
    else:
        result["pyright"] = Applicability(False, "pyright is not configured", ("pyrightconfig.json", "[tool.pyright]"))

    if _toml_section(facts.root_texts.get("pyproject.toml", ""), "build-system"):
        result["python-build"] = Applicability(True, "pyproject.toml declares [build-system]", ("[build-system]",))
    else:
        result["python-build"] = Applicability(False, "no [build-system] in pyproject.toml", ("[build-system]",))

    tests = [p for p in facts.files if _TEST_PY_RE.search(p)]
    if tests:
        result["pytest-coverage"] = Applicability(
            True, f"{len(tests)} python test files (coverage.py via pytest-cov)", ("test_*.py", "*_test.py")
        )
    else:
        result["pytest-coverage"] = Applicability(False, "no python test files", ("test_*.py", "*_test.py"))
    return result


# --- Разбор отчётов -------------------------------------------------------------------------------


def extract_json(output: str) -> Any:
    """JSON-отчёт из stdout (до него могут быть строки журнала инструмента). None — не найден."""
    text = output.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    attempts = 0
    for match in re.finditer(r"(?m)^[\[{]", text):
        attempts += 1
        if attempts > 50:
            break
        try:
            value, _end = decoder.raw_decode(text[match.start() :])
        except ValueError:
            continue
        if isinstance(value, dict | list):
            return value
    return None


def _changed(ctx: GateContext, path: str | None) -> bool:
    return bool(path) and (not ctx.changed_files or path in ctx.changed_files)


# ESLint ---------------------------------------------------------------------------------------------


def eslint_findings(report: list[Any], root: str | None) -> list[Finding]:
    out: list[Finding] = []
    for entry in report:
        if not isinstance(entry, dict):
            continue
        path = entry.get("filePath")
        for msg in entry.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            out.append(
                make_finding(
                    "eslint",
                    str(msg.get("ruleId") or ("fatal-parse-error" if msg.get("fatal") else "eslint")),
                    "high" if msg.get("fatal") else msg.get("severity"),
                    str(msg.get("message") or ""),
                    file_path=path if isinstance(path, str) else None,
                    line=msg.get("line") if isinstance(msg.get("line"), int) else None,
                    root=root,
                )
            )
    return out


def evaluate_eslint(code: int, output: str, ctx: GateContext) -> Verdict:
    report = extract_json(output)
    if not isinstance(report, list):
        if code == 0:
            return Verdict("ERROR", "REPORT_UNPARSABLE", "exit 0 without an ESLint JSON report")
        return Verdict("ERROR", "INFRA" if code >= 2 else "REPORT_UNPARSABLE", f"eslint failed (exit {code})")
    errors = sum(int(e.get("errorCount", 0)) + int(e.get("fatalErrorCount", 0)) for e in report if isinstance(e, dict))
    findings = tuple(eslint_findings(report, ctx.workdir))
    warnings_changed = [f for f in findings if f.severity == "medium" and _changed(ctx, f.file_path)]
    if code >= 2 or code < 0:
        return Verdict("ERROR", "INFRA", f"eslint crashed (exit {code})", findings=findings)
    if errors:
        return Verdict("FAIL", message=f"{errors} ESLint error(s)", findings=findings)  # в т.ч. exit 0 (FP-03)
    if code == 1:
        return Verdict("ERROR", "REPORT_UNPARSABLE", "exit 1 contradicts a report without errors", findings=findings)
    if ctx.profile == "critical" and warnings_changed:
        return Verdict("FAIL", message=f"{len(warnings_changed)} warning(s) in changed files", findings=findings)
    return Verdict("PASS", findings=findings)


def _eslint_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("eslint", "--format", "json", ".")


# PHPStan --------------------------------------------------------------------------------------------


def evaluate_phpstan(code: int, output: str, ctx: GateContext) -> Verdict:
    report = extract_json(output)
    if not isinstance(report, dict) or not isinstance(report.get("totals"), dict):
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"PHPStan JSON report missing (exit {code})")
    totals = report["totals"]
    count = int(totals.get("errors", 0)) + int(totals.get("file_errors", 0))
    findings: list[Finding] = []
    files = report.get("files")
    for path, info in files.items() if isinstance(files, dict) else []:
        for msg in (info or {}).get("messages") or []:
            if isinstance(msg, dict):
                findings.append(
                    make_finding(
                        "phpstan",
                        str(msg.get("identifier") or "phpstan"),
                        "high",
                        str(msg.get("message") or ""),
                        file_path=path,
                        line=msg.get("line") if isinstance(msg.get("line"), int) else None,
                        root=ctx.workdir,
                    )
                )
    for err in report.get("errors") or []:
        findings.append(make_finding("phpstan", "phpstan/general", "high", str(err)))
    if code == 0 and count == 0:
        return Verdict("PASS")
    if code in (0, 1) and count > 0:
        return Verdict("FAIL", message=f"{count} PHPStan error(s)", findings=tuple(findings))
    return Verdict("ERROR", "INFRA", f"PHPStan error (exit {code}, {count} reported)", findings=tuple(findings))


def _phpstan_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("phpstan", "analyse", "--error-format=json", "--no-progress", "--no-interaction", "--memory-limit=1G")


# mypy -----------------------------------------------------------------------------------------------

_MYPY_LINE_RE = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+):(?:\d+:)? (?P<level>error|note|warning): (?P<msg>.*)$")
_MYPY_CODE_RE = re.compile(r"\s+\[(?P<code>[a-z0-9-]+)\]\s*$")


def evaluate_mypy(code: int, output: str, ctx: GateContext) -> Verdict:
    findings: list[Finding] = []
    for line in output.splitlines():
        m = _MYPY_LINE_RE.match(line)
        if not m or m["level"] != "error":
            continue
        c = _MYPY_CODE_RE.search(m["msg"])
        msg = _MYPY_CODE_RE.sub("", m["msg"])
        findings.append(
            make_finding(
                "mypy", f"mypy/{c['code'] if c else 'error'}", "high", msg, file_path=m["path"], line=int(m["line"])
            )
        )
    success = re.search(r"(?m)^Success: no issues found", output) is not None
    summary_fail = re.search(r"(?m)^Found \d+ errors? in \d+ files?", output) is not None
    if code == 0:
        if findings:
            return Verdict("FAIL", message="exit 0 but mypy reported errors", findings=tuple(findings))
        if not success:
            return Verdict("ERROR", "REPORT_UNPARSABLE", "exit 0 without mypy success summary")
        return Verdict("PASS")
    if code == 1 and (findings or summary_fail):
        return Verdict("FAIL", message=f"{len(findings)} mypy error(s)", findings=tuple(findings))
    return Verdict("ERROR", "INFRA", f"mypy crashed or usage error (exit {code})", findings=tuple(findings))


def _mypy_argv(ctx: GateContext) -> tuple[str, ...]:
    base = ("python3", "-m", "mypy", "--cache-dir=/dev/null", "--no-color-output", "--show-error-codes")
    if ctx.facts is not None and _mypy_has_files_setting(ctx.facts):
        return base
    return (*base, ".")


# pyright --------------------------------------------------------------------------------------------


def evaluate_pyright(code: int, output: str, ctx: GateContext) -> Verdict:
    report = extract_json(output)
    if not isinstance(report, dict) or not isinstance(report.get("summary"), dict):
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"pyright JSON report missing (exit {code})")
    findings = []
    for d in report.get("generalDiagnostics") or []:
        if not isinstance(d, dict) or d.get("severity") != "error":
            continue
        start = ((d.get("range") or {}).get("start") or {}).get("line")
        findings.append(
            make_finding(
                "pyright",
                f"pyright/{d.get('rule') or 'error'}",
                "high",
                str(d.get("message") or ""),
                file_path=d.get("file"),
                line=start + 1 if isinstance(start, int) else None,
                root=ctx.workdir,
            )
        )
    errors = int(report["summary"].get("errorCount", 0))
    if code == 0 and errors == 0 and not findings:
        return Verdict("PASS")
    if code in (0, 1) and (errors or findings):
        return Verdict("FAIL", message=f"{errors} pyright error(s)", findings=tuple(findings))
    return Verdict("ERROR", "INFRA", f"pyright error (exit {code})")


# tsc ------------------------------------------------------------------------------------------------

_TSC_RE = re.compile(r"^(?P<path>[^()\n]+)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<msg>.*)$")
_TSC_GLOBAL_RE = re.compile(r"^error (?P<code>TS\d+): (?P<msg>.*)$")
TSC_BIN = "node_modules/typescript/bin/tsc"


def evaluate_tsc(code: int, output: str, ctx: GateContext) -> Verdict:
    findings: list[Finding] = []
    for line in output.splitlines():
        m = _TSC_RE.match(line)
        if m:
            findings.append(
                make_finding(
                    "tsc", m["code"], "high", m["msg"], file_path=m["path"], line=int(m["line"]), root=ctx.workdir
                )
            )
            continue
        g = _TSC_GLOBAL_RE.match(line)
        if g:
            findings.append(make_finding("tsc", g["code"], "high", g["msg"]))
    if code == 0:
        if findings:
            return Verdict("FAIL", message="exit 0 but tsc reported errors", findings=tuple(findings))
        return Verdict("PASS")
    if code in (1, 2) and findings:
        return Verdict("FAIL", message=f"{len(findings)} TypeScript error(s)", findings=tuple(findings))
    return Verdict("ERROR", "INFRA" if code not in (1, 2) else "REPORT_UNPARSABLE", f"tsc failed (exit {code})")


def _tsc_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("node", TSC_BIN, "--noEmit", "--pretty", "false", "-p", "tsconfig.json")


# build ----------------------------------------------------------------------------------------------


def evaluate_build(code: int, _output: str, _ctx: GateContext) -> Verdict:
    if code == 0:
        return Verdict("PASS")
    if code > 0:
        return Verdict("FAIL", message=f"build failed (exit {code})")
    return Verdict("ERROR", "INFRA", f"build terminated (exit {code})")


def _build_argv(*command: str) -> Any:
    def argv(ctx: GateContext) -> tuple[str, ...]:
        return ("sh", "-c", BUILD_DRIVER, "aicd-build", ctx.workdir, *command)

    def display(ctx: GateContext) -> tuple[str, ...]:
        return ("sh", "-c", f"<aicd-build-driver sha256:{BUILD_DRIVER_SHA256}>", "aicd-build", ctx.workdir, *command)

    return argv, display


_NPM_BUILD = _build_argv("npm", "run", "build", "--no-update-notifier")
_PY_BUILD = _build_argv("python3", "-m", "build", "--no-isolation", "--wheel", "--outdir", "dist")


# Vitest ---------------------------------------------------------------------------------------------

VITEST_BIN = "node_modules/vitest/vitest.mjs"


def evaluate_vitest(code: int, output: str, ctx: GateContext) -> Verdict:
    report = extract_json(output)
    if not isinstance(report, dict) or "numTotalTests" not in report:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"vitest JSON report missing (exit {code})")
    tests = {
        "passed": int(report.get("numPassedTests", 0)),
        "failed": int(report.get("numFailedTests", 0)),
        "skipped": int(report.get("numPendingTests", 0)) + int(report.get("numTodoTests", 0)),
        "errors": int(report.get("numRuntimeErrorTestSuites", 0) or 0),
    }
    findings = []
    for suite in report.get("testResults") or []:
        if not isinstance(suite, dict):
            continue
        for case in suite.get("assertionResults") or []:
            if isinstance(case, dict) and case.get("status") == "failed":
                findings.append(
                    make_finding(
                        "vitest",
                        "test-failed",
                        "high",
                        str(case.get("fullTitle") or case.get("title") or "failed test"),
                        file_path=suite.get("name"),
                        root=ctx.workdir,
                    )
                )
    bad = tests["failed"] + tests["errors"] + int(report.get("numFailedTestSuites", 0))
    if int(report.get("numTotalTests", 0)) == 0:
        return Verdict("FAIL", "EMPTY_SUITE", "no tests collected", tests=tests)
    if bad:
        return Verdict("FAIL", message=f"{bad} failed test(s)/suite(s)", findings=tuple(findings), tests=tests)
    if code != 0:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"exit {code} contradicts a green report", tests=tests)
    return apply_test_policy(Verdict("PASS", tests=tests), "vitest", tests, ctx.profile)


def _vitest_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("node", VITEST_BIN, "run", "--reporter=json")


# Playwright -----------------------------------------------------------------------------------------


def _playwright_bin(ctx: GateContext) -> str:
    deps = npm_deps(ctx.facts) if ctx.facts is not None else set()
    return "node_modules/@playwright/test/cli.js" if "@playwright/test" in deps else "node_modules/playwright/cli.js"


def _playwright_argv(ctx: GateContext) -> tuple[str, ...]:
    return ("node", _playwright_bin(ctx), "test", "--reporter=json", "--output=/tmp/aicd-playwright-results")


def _playwright_probe(ctx: GateContext) -> tuple[str, ...]:
    return ("node", _playwright_bin(ctx), "--version")


def evaluate_playwright(code: int, output: str, ctx: GateContext) -> Verdict:
    report = extract_json(output)
    stats = report.get("stats") if isinstance(report, dict) else None
    if not isinstance(stats, dict):
        if code != 0 and "No tests found" in output:
            return Verdict("FAIL", "EMPTY_SUITE", "no tests found")
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"Playwright JSON report missing (exit {code})")
    assert isinstance(report, dict)  # noqa: S101 — сужение типа для mypy
    tests = {
        "passed": int(stats.get("expected", 0)),
        "failed": int(stats.get("unexpected", 0)),
        "skipped": int(stats.get("skipped", 0)),
        "errors": len(report.get("errors") or []),
    }
    flaky = int(stats.get("flaky", 0))
    if sum(tests.values()) + flaky == 0:
        return Verdict("FAIL", "EMPTY_SUITE", "no tests ran", tests=tests)
    if tests["failed"] or tests["errors"]:
        return Verdict("FAIL", message=f"{tests['failed']} failed, {tests['errors']} error(s)", tests=tests)
    if code != 0:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"exit {code} contradicts a green report", tests=tests)
    findings: tuple[Finding, ...] = ()
    if flaky:
        findings = (make_finding("playwright", "aicd/flaky-tests", "medium", f"{flaky} flaky test(s)"),)
    verdict = apply_test_policy(Verdict("PASS", findings=findings, tests=tests), "playwright", tests, ctx.profile)
    return verdict


# Coverage -------------------------------------------------------------------------------------------


def parse_coverage_json(data: Any) -> dict[str, tuple[int, int]] | None:
    """pytest-cov/coverage.py JSON (`files.<path>.summary`) → {path: (covered, statements)}."""
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    out: dict[str, tuple[int, int]] = {}
    for path, info in data["files"].items():
        summary = info.get("summary") if isinstance(info, dict) else None
        if isinstance(summary, dict):
            out[str(path)] = (int(summary.get("covered_lines", 0)), int(summary.get("num_statements", 0)))
    return out


def parse_cobertura(xml_text: str) -> dict[str, tuple[int, int]] | None:
    """coverage.xml (Cobertura): строки `<line hits=…>` каждого `<class filename=…>`."""
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        return None  # отчёт инструмента не содержит DTD; DTD — признак подделки/атаки
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 — без DTD (проверено выше)
    except ET.ParseError:
        return None
    if root.tag != "coverage":
        return None
    out: dict[str, list[int]] = {}
    for cls in root.iter("class"):
        name = cls.get("filename")
        if not name:
            continue
        acc = out.setdefault(name, [0, 0])
        for line in cls.iter("line"):
            acc[1] += 1
            if int(line.get("hits", "0") or 0) > 0:
                acc[0] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def _excluded(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        _TEST_PY_RE.search(path) is not None
        or "tests" in parts[:-1]
        or "migrations" in parts[:-1]
        or "vendor" in parts[:-1]
        or name == "conftest.py"
    )


def core_coverage(
    files: Mapping[str, tuple[int, int]], globs: tuple[str, ...] = DEFAULT_CORE_GLOBS
) -> tuple[float | None, int, str]:
    """Line coverage core (QUALITY_GATES §6): (процент | None, число файлов, область)."""
    norm = {p.replace("\\", "/").removeprefix("./"): v for p, v in files.items()}
    candidates = {p: v for p, v in norm.items() if not _excluded(p) and v[1] > 0}
    core = {p: v for p, v in candidates.items() if any(fnmatchcase(p, g) for g in globs)}
    scope = "core"
    if not core:
        core, scope = candidates, "all-non-test"  # нет файлов под core_globs: меряем весь не-тестовый код
    statements = sum(v[1] for v in core.values())
    if statements == 0:
        return None, 0, scope
    return round(100.0 * sum(v[0] for v in core.values()) / statements, 2), len(core), scope


def evaluate_coverage(code: int, output: str, ctx: GateContext) -> Verdict:
    marker = None
    for line in reversed(output.splitlines()):
        if line.startswith(COVERAGE_MARKER + " "):
            marker = line
            break
    test_verdict = interpret_pytest(code, output.replace(marker or "\0", ""))
    counts = pytest_counts(output)
    tests = {k: counts.get(k, 0) for k in ("passed", "failed", "skipped", "errors")} if counts else None
    if test_verdict.status != "PASS":
        return Verdict(test_verdict.status, test_verdict.error_code, test_verdict.message, tests=tests)
    if marker is None:
        return Verdict("ERROR", "REPORT_UNPARSABLE", "coverage driver output missing", tests=tests)
    parts = marker.split(" ", 2)
    if len(parts) != 3 or (ctx.nonce and parts[1] != ctx.nonce):
        return Verdict("ERROR", "REPORT_UNPARSABLE", "coverage report marker mismatch", tests=tests)
    try:
        summary = json.loads(parts[2])
    except ValueError:
        summary = None
    if not isinstance(summary, dict) or not isinstance(summary.get("files"), dict):
        return Verdict("ERROR", "REPORT_UNPARSABLE", "coverage report missing or not produced by this run", tests=tests)
    files = {str(p): (int(v[0]), int(v[1])) for p, v in summary["files"].items() if isinstance(v, list) and len(v) == 2}
    globs_cfg = ctx.config.get("core_globs")
    globs = tuple(str(g) for g in globs_cfg) if isinstance(globs_cfg, list) and globs_cfg else DEFAULT_CORE_GLOBS
    percent, nfiles, scope = core_coverage(files, globs)
    if percent is None:
        return Verdict("ERROR", "REPORT_UNPARSABLE", "coverage report has no measurable core files", tests=tests)
    minimum = thresholds(ctx.profile).coverage_min_percent
    cfg_min = ctx.config.get("min_percent")
    if isinstance(cfg_min, int | float) and not isinstance(cfg_min, bool):
        minimum = max(float(cfg_min), minimum or 0.0)
    note = f"{scope} coverage {percent}% over {nfiles} file(s)"
    if minimum is not None and percent < minimum:
        finding = make_finding("pytest-coverage", "aicd/coverage-below-threshold", "high", f"{note} < {minimum}%")
        return Verdict(
            "FAIL", "THRESHOLD", f"{note} < {minimum}%", findings=(finding,), tests=tests, coverage_percent=percent
        )
    return Verdict("PASS", message=note, tests=tests, coverage_percent=percent)


def _coverage_argv(ctx: GateContext) -> tuple[str, ...]:
    return ("python3", "-I", "-S", "-c", COVERAGE_DRIVER, ctx.nonce or "-")


def _coverage_display(ctx: GateContext) -> tuple[str, ...]:
    return (
        "python3",
        "-I",
        "-S",
        "-c",
        f"<aicd-coverage-driver sha256:{COVERAGE_DRIVER_SHA256}>",
        ctx.nonce or "-",
    )


def _unused(code: int, _output: str) -> Verdict:  # interpret v0-формы: вызывается только через evaluate
    return Verdict("ERROR", "INFRA", f"no interpreter (exit {code})")


SPECS: tuple[GateSpec, ...] = (
    GateSpec(
        "eslint",
        "LINT",
        "eslint",
        ("eslint", "--version"),
        "command_result",
        "report",
        _eslint_argv,
        _eslint_argv,
        _unused,
        evaluate=evaluate_eslint,
    ),
    GateSpec(
        "phpstan",
        "TYPECHECK",
        "phpstan",
        ("phpstan", "--version"),
        "command_result",
        "report",
        _phpstan_argv,
        _phpstan_argv,
        _unused,
        evaluate=evaluate_phpstan,
    ),
    GateSpec(
        "mypy",
        "TYPECHECK",
        "mypy",
        ("python3", "-m", "mypy", "--version"),
        "command_result",
        "report",
        _mypy_argv,
        _mypy_argv,
        _unused,
        evaluate=evaluate_mypy,
    ),
    GateSpec(
        "pyright",
        "TYPECHECK",
        "pyright",
        ("pyright", "--version"),
        "command_result",
        "report",
        lambda _c: ("pyright", "--outputjson"),
        lambda _c: ("pyright", "--outputjson"),
        _unused,
        evaluate=evaluate_pyright,
    ),
    GateSpec(
        "tsc",
        "TYPECHECK",
        "tsc",
        ("node", TSC_BIN, "--version"),
        "command_result",
        "report",
        _tsc_argv,
        _tsc_argv,
        _unused,
        evaluate=evaluate_tsc,
    ),
    GateSpec(
        "npm-build",
        "BUILD",
        "npm",
        ("npm", "--version"),
        "command_result",
        "report",
        _NPM_BUILD[0],
        _NPM_BUILD[1],
        _unused,
        evaluate=evaluate_build,
    ),
    GateSpec(
        "python-build",
        "BUILD",
        "python-build",
        ("python3", "-m", "build", "--version"),
        "command_result",
        "report",
        _PY_BUILD[0],
        _PY_BUILD[1],
        _unused,
        evaluate=evaluate_build,
    ),
    GateSpec(
        "vitest",
        "UNIT",
        "vitest",
        ("node", VITEST_BIN, "--version"),
        "test_report",
        "test_report",
        _vitest_argv,
        _vitest_argv,
        _unused,
        evaluate=evaluate_vitest,
    ),
    GateSpec(
        "playwright",
        "E2E",
        "playwright",
        ("node", "node_modules/playwright/cli.js", "--version"),
        "test_report",
        "test_report",
        _playwright_argv,
        _playwright_argv,
        _unused,
        evaluate=evaluate_playwright,
        probe_for=_playwright_probe,
    ),
    GateSpec(
        "pytest-coverage",
        "COVERAGE",
        "coverage",
        ("python3", "-c", "import pytest, pytest_cov, coverage; print('coverage', coverage.__version__)"),
        "test_report",
        "coverage_report",
        _coverage_argv,
        _coverage_display,
        _unused,
        evaluate=evaluate_coverage,
    ),
)

DEFINITIONS: tuple[tuple[str, str, int, str], ...] = (
    ("eslint", "LINT", DEFAULT_TIMEOUT, "ESLint (JS/TS), если есть конфигурация ESLint"),
    ("phpstan", "TYPECHECK", DEFAULT_TIMEOUT, "PHPStan, если есть phpstan.neon"),
    ("mypy", "TYPECHECK", DEFAULT_TIMEOUT, "mypy, если mypy сконфигурирован"),
    ("pyright", "TYPECHECK", DEFAULT_TIMEOUT, "pyright, если pyright сконфигурирован"),
    ("tsc", "TYPECHECK", DEFAULT_TIMEOUT, "tsc --noEmit, если есть tsconfig.json"),
    ("npm-build", "BUILD", 1800, "npm run build (в контейнере, копия дерева)"),
    ("python-build", "BUILD", 1800, "python -m build --no-isolation (в контейнере, копия дерева)"),
    ("vitest", "UNIT", DEFAULT_TIMEOUT, "Vitest, если в package.json есть зависимость vitest"),
    ("playwright", "E2E", 1800, "Playwright, только при playwright.config.*; сеть none"),
    ("pytest-coverage", "COVERAGE", 1800, "pytest-cov: line coverage core ≥ порога профиля"),
)

PROVIDER = GateProvider("quality-runners", SPECS, detect, DEFINITIONS)
