"""Определения gates v0 и детектор применимости (QUALITY_GATES §2–§3, §5; ТЗ §14).

v0 (P3-14) покрывает:
* `scope`       (SCOPE, обязателен всегда) — diff base..commit не выходит за worktree, не трогает `.git/`,
  symlink'и не указывают наружу, в добавленных строках нет секретов. Diff и метаданные готовит
  платформа на хосте по git-объектам (`aicd_security.git.run_git`), сам сканер исполняется в
  verification-контейнере (`scope_scan.py`, только stdlib);
* `ruff-lint`   (LINT)   — `ruff check`, если в репозитории есть Python и pyproject.toml/ruff.toml;
* `ruff-format` (FORMAT) — `ruff format --check` по изменённым *.py, если в конфигурации ruff есть секция format;
* `pytest-unit` (UNIT)   — pytest, если в репозитории есть тестовые файлы Python;
* `npm-unit`    (UNIT)   — `npm test`, если в package.json есть непустой скрипт `test`.

Отсутствие инструмента в verification-образе даёт ERROR (никогда PASS; SKIPPED ≠ PASS).
Модуль чистый, кроме `collect_repo_facts` (git по объектам коммита, без исполнения кода репозитория).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aicd_platform import redaction
from aicd_security.git import GitRepositoryError, run_git

GateStatus = Literal["PASS", "FAIL", "ERROR", "SKIPPED", "NOT_APPLICABLE"]
ErrorCode = Literal[
    "TOOL_UNAVAILABLE", "TIMEOUT", "INFRA", "SHA_MISMATCH", "REPORT_UNPARSABLE", "EMPTY_SUITE", "WORKTREE_DIRTY",
    "NO_BASE_COMMIT", "UNSUPPORTED_GATE",
]

SCANNER_SOURCE = (Path(__file__).with_name("scope_scan.py")).read_text(encoding="utf-8")
SCANNER_SHA256 = hashlib.sha256(SCANNER_SOURCE.encode()).hexdigest()
CONTAINER_WORKDIR = "/workspace"
CONTAINER_INPUT_DIR = "/aicd-input"
SCOPE_INPUT_FILE = "scope.json"
SCOPE_DIFF_FILE = "diff.patch"
DEFAULT_TIMEOUT_SECONDS = 600
MAX_ROOT_FILE_BYTES = 256 * 1024

# --- Секреты: паттерны redaction платформы + gitleaks-style правила ------------------------------

_GITLEAKS_RULES: tuple[tuple[str, str], ...] = (
    ("aws-access-key-id", r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),
    ("aws-secret-access-key", r"(?i)aws.{0,20}(?:secret|private).{0,20}['\"][A-Za-z0-9/+=]{40}['\"]"),
    ("private-key", r"-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----"),
    ("github-token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    ("github-fine-grained-pat", r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    ("gitlab-pat", r"\bglpat-[A-Za-z0-9_-]{20}\b"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9+/]{40,}"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("stripe-live-key", r"\b(?:sk|rk)_live_[0-9a-zA-Z]{24,}\b"),
    ("npm-token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("anthropic-openai-key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"),
    (
        "generic-secret-assignment",
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret)\b"
        r"[\"']?\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']",
    ),
)
ALLOW_SUBSTRINGS: tuple[str, ...] = ("EXAMPLE", "example", "${", "{{", "<", "XXXX", "xxxx", "changeme", "placeholder")
_RE_FLAGS_MASK = re.IGNORECASE | re.DOTALL | re.MULTILINE | re.VERBOSE


def secret_rules() -> list[dict[str, Any]]:
    """Правила для сканера. Из redaction берутся «токенные» паттерны; прозаические (`basic …`,
    `password=` без кавычек) дали бы ложные срабатывания на обычном коде и заменены gitleaks-правилом
    `generic-secret-assignment` (значение в кавычках, ≥ 16 символов)."""
    rules: list[dict[str, Any]] = []
    for index, pattern in enumerate(redaction._PATTERNS):
        if pattern.pattern.startswith("(?i)") or pattern.groups >= 3:
            continue
        rules.append({"id": f"platform-redaction-{index}", "pattern": pattern.pattern,
                      "flags": int(pattern.flags & _RE_FLAGS_MASK)})
    rules.extend({"id": rid, "pattern": pat, "flags": 0} for rid, pat in _GITLEAKS_RULES)
    return rules


# --- Факты о репозитории на коммите ---------------------------------------------------------------


@dataclass(frozen=True)
class DiffEntry:
    path: str
    status: str
    old_mode: str
    new_mode: str
    new_blob: str
    link_target: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "old_mode": self.old_mode, "new_mode": self.new_mode,
                "link_target": self.link_target}


@dataclass(frozen=True)
class RepoFacts:
    """Содержимое репозитория на commit: список файлов, корневые конфиги, изменения относительно base."""

    files: frozenset[str]
    root_texts: Mapping[str, str] = field(default_factory=dict)  # pyproject.toml, ruff.toml, package.json …
    changes: tuple[DiffEntry, ...] = ()

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.changes if e.status != "D")


def parse_raw_diff(raw: str) -> list[DiffEntry]:
    """Разбор `git diff --raw -z --no-renames --no-abbrev`: `:om nm osha nsha S\\0path\\0`."""
    tokens = raw.split("\0")
    entries: list[DiffEntry] = []
    i = 0
    while i < len(tokens):
        head = tokens[i]
        if not head:
            i += 1
            continue
        if not head.startswith(":") or i + 1 >= len(tokens):
            raise ValueError("unexpected git diff --raw output")
        parts = head[1:].split(" ")
        if len(parts) != 5:
            raise ValueError("unexpected git diff --raw header")
        old_mode, new_mode, _old_sha, new_sha, status = parts
        entries.append(DiffEntry(path=tokens[i + 1], status=status[:1], old_mode=old_mode, new_mode=new_mode,
                                 new_blob=new_sha))
        i += 2
    return entries


ROOT_CONFIGS = ("pyproject.toml", "ruff.toml", ".ruff.toml", "package.json")


def collect_repo_facts(repo: Path, commit_sha: str, base_sha: str | None) -> RepoFacts:
    """Факты по git-объектам (`run_git`: без хуков/внешних программ, чистое окружение)."""
    listing = run_git(repo, "ls-tree", "-r", "-z", "--name-only", commit_sha)
    files = frozenset(p for p in listing.split("\0") if p)
    texts: dict[str, str] = {}
    for name in ROOT_CONFIGS:
        if name not in files:
            continue
        try:
            size = int(run_git(repo, "cat-file", "-s", f"{commit_sha}:{name}"))
            if size <= MAX_ROOT_FILE_BYTES:
                texts[name] = run_git(repo, "cat-file", "blob", f"{commit_sha}:{name}")
        except (GitRepositoryError, ValueError):
            continue  # не-UTF-8 или нечитаемый конфиг — считаем отсутствующим
    changes: list[DiffEntry] = []
    if base_sha is not None:
        raw = run_git(repo, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", "--no-ext-diff", base_sha,
                      commit_sha, timeout=120.0)
        for entry in parse_raw_diff(raw):
            if entry.new_mode == "120000":
                try:
                    target: str | None = run_git(repo, "cat-file", "blob", entry.new_blob)
                except (GitRepositoryError, ValueError):
                    target = None
                entry = DiffEntry(entry.path, entry.status, entry.old_mode, entry.new_mode, entry.new_blob, target)
            changes.append(entry)
    return RepoFacts(files=files, root_texts=texts, changes=tuple(changes))


def write_scope_input(repo: Path, commit_sha: str, base_sha: str, facts: RepoFacts, target_dir: Path) -> None:
    """scope.json + diff.patch для сканера. diff пишет сам git (`--output`), байты не декодируются."""
    run_git(repo, "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "--no-textconv",
            "--no-renames", "-U0", f"--output={target_dir / SCOPE_DIFF_FILE}", base_sha, commit_sha, timeout=300.0)
    spec = {
        "entries": [e.as_json() for e in facts.changes],
        "diff_file": SCOPE_DIFF_FILE,
        "rules": secret_rules(),
        "allow_substrings": list(ALLOW_SUBSTRINGS),
    }
    (target_dir / SCOPE_INPUT_FILE).write_text(json.dumps(spec), encoding="utf-8")


# --- Применимость ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Applicability:
    applicable: bool
    reason: str
    markers: tuple[str, ...] = ()


_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py)$")
_NPM_DEFAULT_TEST = "no test specified"


def _has_python(facts: RepoFacts) -> bool:
    return any(p.endswith(".py") for p in facts.files)


def _ruff_configured(facts: RepoFacts) -> tuple[bool, tuple[str, ...]]:
    markers = tuple(n for n in ("pyproject.toml", "ruff.toml", ".ruff.toml") if n in facts.files)
    return bool(markers), markers


def _ruff_format_configured(facts: RepoFacts) -> bool:
    pyproject = facts.root_texts.get("pyproject.toml", "")
    if re.search(r"(?m)^\s*\[tool\.ruff\.format\]", pyproject):
        return True
    return any(re.search(r"(?m)^\s*\[format\]", facts.root_texts.get(n, "")) for n in ("ruff.toml", ".ruff.toml"))


def npm_test_script(facts: RepoFacts) -> str | None:
    text = facts.root_texts.get("package.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(script, str) or not script.strip() or _NPM_DEFAULT_TEST in script:
        return None
    return script


def changed_python_files(facts: RepoFacts) -> tuple[str, ...]:
    return tuple(e.path for e in facts.changes if e.status != "D" and e.path.endswith(".py")
                 and e.new_mode in ("100644", "100755"))


def detect_applicability(facts: RepoFacts) -> dict[str, Applicability]:
    """Детерминированный детектор: имя gate v0 → применимость с перечнем проверенных маркеров."""
    result: dict[str, Applicability] = {"scope": Applicability(True, "SCOPE is always applicable", ("diff",))}
    has_py = _has_python(facts)
    ruff_ok, ruff_markers = _ruff_configured(facts)
    if has_py and ruff_ok:
        result["ruff-lint"] = Applicability(True, "python sources and ruff/pyproject config", ("*.py", *ruff_markers))
    else:
        result["ruff-lint"] = Applicability(
            False, "no python sources" if not has_py else "no pyproject.toml/ruff.toml at repository root",
            ("*.py", "pyproject.toml", "ruff.toml", ".ruff.toml"))
    changed_py = changed_python_files(facts)
    if has_py and ruff_ok and _ruff_format_configured(facts) and changed_py:
        result["ruff-format"] = Applicability(True, f"ruff format configured, {len(changed_py)} changed python files",
                                              ("[tool.ruff.format]", *ruff_markers))
    else:
        result["ruff-format"] = Applicability(
            False, "ruff format is not configured or no python files changed",
            ("[tool.ruff.format]", "ruff.toml [format]", "changed *.py"))
    tests = sorted(p for p in facts.files if _TEST_FILE_RE.search(p))
    if tests:
        result["pytest-unit"] = Applicability(True, f"{len(tests)} python test files", ("test_*.py", "*_test.py"))
    else:
        result["pytest-unit"] = Applicability(False, "no python test files", ("test_*.py", "*_test.py"))
    if npm_test_script(facts) is not None:
        result["npm-unit"] = Applicability(True, "package.json defines scripts.test", ("package.json",))
    else:
        result["npm-unit"] = Applicability(False, "no package.json with a non-default scripts.test",
                                           ("package.json",))
    return result


# --- Определения gates и интерпретация результата -------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    status: GateStatus
    error_code: ErrorCode | None = None
    message: str = ""


@dataclass(frozen=True)
class GateContext:
    """Пути, видимые процессу gate (в контейнере или на хосте для локального исполнения)."""

    workdir: str
    input_dir: str
    changed_python: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateSpec:
    name: str
    gate_type: str
    tool: str
    probe_argv: tuple[str, ...]
    evidence_kind: str  # evidence.kind
    artifact_kind: str  # artifacts.kind
    build_argv: Callable[[GateContext], tuple[str, ...]]
    display_argv: Callable[[GateContext], tuple[str, ...]]
    interpret: Callable[[int, str], Verdict]
    needs_scope_input: bool = False
    executes_repository: bool = True  # исполняет/читает рабочее дерево (нужен чистый worktree на commit)


def _simple_exit(code: int, _output: str) -> Verdict:
    if code == 0:
        return Verdict("PASS")
    if code == 1:
        return Verdict("FAIL", message="tool reported violations (exit 1)")
    return Verdict("ERROR", "INFRA", f"tool error (exit {code})")


def interpret_scope(code: int, output: str) -> Verdict:
    try:
        report = json.loads(output.strip().splitlines()[-1]) if output.strip() else None
    except ValueError:
        report = None
    if not isinstance(report, dict) or "violations_total" not in report:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"scope scanner report missing (exit {code})")
    total = int(report["violations_total"])
    if code == 0 and total == 0:
        return Verdict("PASS")
    if code == 1 and total > 0:
        return Verdict("FAIL", message=f"{total} scope violation(s)")
    return Verdict("ERROR", "REPORT_UNPARSABLE", f"exit code {code} contradicts report ({total} violations)")


_PYTEST_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")


def pytest_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in output.splitlines()[-20:]:
        for num, kind in _PYTEST_COUNT_RE.findall(line):
            key = "errors" if kind.startswith("error") else kind
            counts[key] = int(num)
    return counts


def interpret_pytest(code: int, output: str) -> Verdict:
    counts = pytest_counts(output)
    bad = counts.get("failed", 0) + counts.get("errors", 0)
    if code == 0:
        if bad:
            return Verdict("FAIL", message="exit 0 but summary reports failures")  # FP-03
        if counts.get("passed", 0) == 0:
            return Verdict("ERROR", "REPORT_UNPARSABLE", "exit 0 without a 'passed' summary")
        return Verdict("PASS")
    if code == 1:
        return Verdict("FAIL", message=f"{bad} failed/errored test(s)")
    if code == 5:
        return Verdict("FAIL", "EMPTY_SUITE", "no tests collected")  # FP-05
    return Verdict("ERROR", "INFRA", f"pytest error (exit {code})")


def interpret_npm(code: int, _output: str) -> Verdict:
    if code == 0:
        return Verdict("PASS")
    if code > 0:
        return Verdict("FAIL", message=f"npm test failed (exit {code})")
    return Verdict("ERROR", "INFRA", f"npm test terminated (exit {code})")


def _scope_argv(ctx: GateContext) -> tuple[str, ...]:
    return ("python3", "-I", "-S", "-c", SCANNER_SOURCE, f"{ctx.input_dir}/{SCOPE_INPUT_FILE}")


def _scope_display(ctx: GateContext) -> tuple[str, ...]:
    return ("python3", "-I", "-S", "-c", f"<aicd-scope-scanner sha256:{SCANNER_SHA256}>",
            f"{ctx.input_dir}/{SCOPE_INPUT_FILE}")


def _ruff_lint_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("ruff", "check", "--no-cache", ".")


def _ruff_format_argv(ctx: GateContext) -> tuple[str, ...]:
    return ("ruff", "format", "--check", "--no-cache", "--", *ctx.changed_python)


def _pytest_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no")


def _npm_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("npm", "test", "--no-update-notifier")


GATE_SPECS: dict[str, GateSpec] = {
    spec.name: spec
    for spec in (
        GateSpec("scope", "SCOPE", "aicd-scope", ("python3", "--version"), "scan_report", "scan_report",
                 _scope_argv, _scope_display, interpret_scope, needs_scope_input=True, executes_repository=False),
        GateSpec("ruff-lint", "LINT", "ruff", ("ruff", "--version"), "command_result", "report",
                 _ruff_lint_argv, _ruff_lint_argv, _simple_exit),
        GateSpec("ruff-format", "FORMAT", "ruff", ("ruff", "--version"), "command_result", "report",
                 _ruff_format_argv, _ruff_format_argv, _simple_exit),
        GateSpec("pytest-unit", "UNIT", "pytest", ("python3", "-m", "pytest", "--version"), "test_report",
                 "test_report", _pytest_argv, _pytest_argv, interpret_pytest),
        GateSpec("npm-unit", "UNIT", "npm", ("npm", "--version"), "test_report", "test_report",
                 _npm_argv, _npm_argv, interpret_npm),
    )
}

# Предлагаемый seed `quality_gates` для глобальных профилей basic/strict/critical v1 (миграция 0003 его
# не содержит — см. отчёт P3-14). Поля: name, gate_type, required, timeout_seconds.
V0_GATE_DEFINITIONS: tuple[tuple[str, str, bool, int], ...] = (
    ("scope", "SCOPE", True, DEFAULT_TIMEOUT_SECONDS),
    ("ruff-lint", "LINT", True, DEFAULT_TIMEOUT_SECONDS),
    ("ruff-format", "FORMAT", True, DEFAULT_TIMEOUT_SECONDS),
    ("pytest-unit", "UNIT", True, DEFAULT_TIMEOUT_SECONDS),
    ("npm-unit", "UNIT", True, DEFAULT_TIMEOUT_SECONDS),
)


def finding_counts(gate_name: str, status: str, output: str) -> dict[str, int]:
    """Агрегат находок для `gate.completed.findings` (EVENT_SCHEMAS severity_counts)."""
    counts = {"info": 0, "low": 0, "medium": 0, "high": 0, "critical": 0}
    if gate_name != "scope" or status != "FAIL":
        return counts
    try:
        report = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return counts
    for violation in report.get("violations", []) if isinstance(report, dict) else []:
        severity = violation.get("severity") if isinstance(violation, dict) else None
        if severity in counts:
            counts[severity] += 1
    return counts
