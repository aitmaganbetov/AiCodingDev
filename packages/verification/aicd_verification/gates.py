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

Phase 6 (P6-02/P6-03): здесь — базовые типы (`GateSpec`, `GateContext`, `Verdict`, `Applicability`,
`RepoFacts`, `StackInfo`) и ядро v0 (`CORE_PROVIDER`). Остальные gates живут в отдельных модулях и
подключаются через реестр `aicd_verification.registry` (кортеж провайдеров `GateProvider`); полный
словарь `GATE_SPECS` по-прежнему доступен как `aicd_verification.gates.GATE_SPECS` (ленивая ссылка на
реестр) — см. docstring `registry.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from aicd_platform import redaction
from aicd_security.git import GitRepositoryError, run_git

if TYPE_CHECKING:
    from aicd_verification.findings import Finding

GateStatus = Literal["PASS", "FAIL", "ERROR", "SKIPPED", "NOT_APPLICABLE"]
ErrorCode = Literal[
    "TOOL_UNAVAILABLE",
    "TIMEOUT",
    "INFRA",
    "SHA_MISMATCH",
    "REPORT_UNPARSABLE",
    "EMPTY_SUITE",
    "WORKTREE_DIRTY",
    "NO_BASE_COMMIT",
    "UNSUPPORTED_GATE",
    "APPROVAL_REQUIRED",
    "THRESHOLD",
    "EVIDENCE_REJECTED",
    "STALE_EVIDENCE",
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
    # gitleaks-правила первыми: сканер сообщает одно (первое) правило на строку.
    rules: list[dict[str, Any]] = [{"id": rid, "pattern": pat, "flags": 0} for rid, pat in _GITLEAKS_RULES]
    for index, pattern in enumerate(redaction._PATTERNS):
        if pattern.pattern.startswith("(?i)") or pattern.groups >= 3:
            continue
        rules.append(
            {
                "id": f"platform-redaction-{index}",
                "pattern": pattern.pattern,
                "flags": int(pattern.flags & _RE_FLAGS_MASK),
            }
        )
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
        return {
            "path": self.path,
            "status": self.status,
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "link_target": self.link_target,
        }


@dataclass(frozen=True)
class RepoFacts:
    """Содержимое репозитория на commit: список файлов, корневые конфиги, изменения относительно base."""

    files: frozenset[str]
    root_texts: Mapping[str, str] = field(default_factory=dict)  # pyproject.toml, ruff.toml, package.json …
    changes: tuple[DiffEntry, ...] = ()
    # Тексты выбранных изменённых файлов (миграции для анализа деструктивных операций, P6-05).
    changed_texts: Mapping[str, str] = field(default_factory=dict)

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
        entries.append(
            DiffEntry(path=tokens[i + 1], status=status[:1], old_mode=old_mode, new_mode=new_mode, new_blob=new_sha)
        )
        i += 2
    return entries


ROOT_CONFIGS = (
    "pyproject.toml",
    "ruff.toml",
    ".ruff.toml",
    "package.json",
    "setup.cfg",
    "mypy.ini",
    ".mypy.ini",
    "composer.json",
    "alembic.ini",
)
MAX_CHANGED_TEXTS = 200


def _want_changed_text(path: str) -> bool:
    from aicd_verification.migrations import is_migration_path  # локально: migrations импортирует gates

    return is_migration_path(path)


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
        raw = run_git(
            repo,
            "diff",
            "--raw",
            "-z",
            "--no-renames",
            "--no-abbrev",
            "--no-ext-diff",
            base_sha,
            commit_sha,
            timeout=120.0,
        )
        for entry in parse_raw_diff(raw):
            if entry.new_mode == "120000":
                try:
                    target: str | None = run_git(repo, "cat-file", "blob", entry.new_blob)
                except (GitRepositoryError, ValueError):
                    target = None
                entry = DiffEntry(entry.path, entry.status, entry.old_mode, entry.new_mode, entry.new_blob, target)
            changes.append(entry)
    changed_texts: dict[str, str] = {}
    for entry in changes:
        if len(changed_texts) >= MAX_CHANGED_TEXTS:
            break
        if entry.status == "D" or entry.new_mode not in ("100644", "100755") or not _want_changed_text(entry.path):
            continue
        try:
            size = int(run_git(repo, "cat-file", "-s", entry.new_blob))
            if size <= MAX_ROOT_FILE_BYTES:
                changed_texts[entry.path] = run_git(repo, "cat-file", "blob", entry.new_blob)
        except (GitRepositoryError, ValueError):
            continue
    return RepoFacts(files=files, root_texts=texts, changes=tuple(changes), changed_texts=changed_texts)


def write_scope_input(repo: Path, commit_sha: str, base_sha: str, facts: RepoFacts, target_dir: Path) -> None:
    """scope.json + diff.patch для сканера. diff пишет сам git (`--output`), байты не декодируются."""
    run_git(
        repo,
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "-U0",
        f"--output={target_dir / SCOPE_DIFF_FILE}",
        base_sha,
        commit_sha,
        timeout=300.0,
    )
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


@dataclass(frozen=True)
class StackInfo:
    """Стек проекта из `project_technologies` (Phase 5, P5-07): пары (ecosystem, name) в нижнем регистре.

    Дополняет маркеры репозитория: например, `npm:vitest`, `python:mypy`, `lang:typescript`.
    Пустой стек (индексация ещё не прошла) — применимость решают только факты commit."""

    technologies: frozenset[tuple[str, str]] = frozenset()

    @classmethod
    def from_rows(cls, rows: Any) -> StackInfo:
        pairs = set()
        for row in rows:
            eco, name = row["ecosystem"], row["name"]
            if isinstance(eco, str) and isinstance(name, str):
                pairs.add((eco.lower(), name.lower()))
        return cls(frozenset(pairs))

    def has(self, ecosystem: str, name: str) -> bool:
        return (ecosystem.lower(), name.lower()) in self.technologies

    def has_lang(self, name: str) -> bool:
        return self.has("lang", name)


EMPTY_STACK = StackInfo()


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
    return tuple(
        e.path
        for e in facts.changes
        if e.status != "D" and e.path.endswith(".py") and e.new_mode in ("100644", "100755")
    )


def detect_applicability(facts: RepoFacts, stack: StackInfo = EMPTY_STACK) -> dict[str, Applicability]:
    """Детерминированный детектор ядра v0: имя gate → применимость с перечнем проверенных маркеров.
    Полный детектор (все провайдеры) — `aicd_verification.registry.detect_all`."""
    del stack  # ядро v0 решает только по файлам commit
    result: dict[str, Applicability] = {"scope": Applicability(True, "SCOPE is always applicable", ("diff",))}
    has_py = _has_python(facts)
    ruff_ok, ruff_markers = _ruff_configured(facts)
    if has_py and ruff_ok:
        result["ruff-lint"] = Applicability(True, "python sources and ruff/pyproject config", ("*.py", *ruff_markers))
    else:
        result["ruff-lint"] = Applicability(
            False,
            "no python sources" if not has_py else "no pyproject.toml/ruff.toml at repository root",
            ("*.py", "pyproject.toml", "ruff.toml", ".ruff.toml"),
        )
    changed_py = changed_python_files(facts)
    if has_py and ruff_ok and _ruff_format_configured(facts) and changed_py:
        result["ruff-format"] = Applicability(
            True,
            f"ruff format configured, {len(changed_py)} changed python files",
            ("[tool.ruff.format]", *ruff_markers),
        )
    else:
        result["ruff-format"] = Applicability(
            False,
            "ruff format is not configured or no python files changed",
            ("[tool.ruff.format]", "ruff.toml [format]", "changed *.py"),
        )
    tests = sorted(p for p in facts.files if _TEST_FILE_RE.search(p))
    if tests:
        result["pytest-unit"] = Applicability(True, f"{len(tests)} python test files", ("test_*.py", "*_test.py"))
    else:
        result["pytest-unit"] = Applicability(False, "no python test files", ("test_*.py", "*_test.py"))
    if npm_test_script(facts) is not None:
        result["npm-unit"] = Applicability(True, "package.json defines scripts.test", ("package.json",))
    else:
        result["npm-unit"] = Applicability(False, "no package.json with a non-default scripts.test", ("package.json",))
    return result


# --- Определения gates и интерпретация результата -------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    status: GateStatus
    error_code: ErrorCode | None = None
    message: str = ""
    findings: tuple[Finding, ...] = ()
    tests: Mapping[str, int] | None = None  # разобранный отчёт тестов: passed/failed/skipped/errors
    coverage_percent: float | None = None
    # Предмет approval (например, DESTRUCTIVE_MIGRATION): движок не даёт PASS до APPROVED.
    approval: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class GateContext:
    """Пути, видимые процессу gate (в контейнере или на хосте для локального исполнения)."""

    workdir: str
    input_dir: str
    changed_python: tuple[str, ...] = ()
    profile: str = "strict"
    config: Mapping[str, Any] = field(default_factory=dict)  # quality_gates.config
    facts: RepoFacts | None = None
    changed_files: tuple[str, ...] = ()
    nonce: str = ""  # одноразовая метка прогона (маркер отчёта драйвера; подложенный вывод не совпадёт)


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
    # --- Phase 6 (все поля необязательны; позиционная форма v0 сохраняется) ---
    image_key: str | None = None  # ключ таблицы образов (images.py); None → по `tool`
    # Полная интерпретация с контекстом (профиль, config); при наличии заменяет `interpret`.
    evaluate: Callable[[int, str, GateContext], Verdict] | None = None
    probe_for: Callable[[GateContext], tuple[str, ...]] | None = None  # probe, зависящий от репозитория
    container_user: str | None = None  # uid:gid в контейнере (по умолчанию — ContainerSpec.user)
    needs_input_dir: bool = False  # смонтировать staging (CONTAINER_INPUT_DIR) не только для SCOPE

    def run_evaluate(self, code: int, output: str, ctx: GateContext) -> Verdict:
        return self.evaluate(code, output, ctx) if self.evaluate is not None else self.interpret(code, output)

    def probe(self, ctx: GateContext) -> tuple[str, ...]:
        return self.probe_for(ctx) if self.probe_for is not None else self.probe_argv


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


def evaluate_scope(code: int, output: str, _ctx: GateContext) -> Verdict:
    from aicd_verification.findings import make_finding

    verdict = interpret_scope(code, output)
    if verdict.status != "FAIL":
        return verdict
    try:
        report = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return verdict
    findings = tuple(
        make_finding(
            "scope",
            str(v.get("rule")),
            str(v.get("severity")),
            f"scope violation: {v.get('rule')}",
            file_path=v.get("path") if isinstance(v.get("path"), str) else None,
            line=v.get("line") if isinstance(v.get("line"), int) else None,
            snippet=f"{v.get('rule')}@{v.get('path')}:{v.get('line')}",
        )
        for v in (report.get("violations", []) if isinstance(report, dict) else [])
        if isinstance(v, dict)
    )
    return Verdict(verdict.status, verdict.error_code, verdict.message, findings=findings)


MAX_SKIPPED_SHARE = 0.2


def apply_test_policy(verdict: Verdict, gate: str, tests: Mapping[str, int] | None, profile: str) -> Verdict:
    """Общие правила тестовых gates (QUALITY_GATES §5.1): все тесты пропущены → FAIL (EMPTY_SUITE);
    доля skipped > 20% → finding high и FAIL в strict/critical (в basic — только finding)."""
    if tests is None or verdict.status != "PASS":
        return verdict
    from aicd_verification.findings import make_finding

    passed, skipped = tests.get("passed", 0), tests.get("skipped", 0)
    if passed == 0:
        return Verdict("FAIL", "EMPTY_SUITE", "no test passed (all skipped or none ran)", tests=tests)
    if skipped / (passed + skipped) > MAX_SKIPPED_SHARE:
        finding = make_finding(
            gate, "aicd/skipped-tests-share", "high", f"{skipped} of {passed + skipped} tests skipped (> 20%)"
        )
        status: GateStatus = "FAIL" if profile in ("strict", "critical") else "PASS"
        return Verdict(status, None, finding.title, findings=(finding,), tests=tests)
    return Verdict(verdict.status, verdict.error_code, verdict.message, verdict.findings, tests)


def evaluate_pytest(code: int, output: str, ctx: GateContext) -> Verdict:
    verdict = interpret_pytest(code, output)
    counts = pytest_counts(output)
    tests = {k: counts.get(k, 0) for k in ("passed", "failed", "skipped", "errors")} if counts else None
    if code == 0 and counts and counts.get("passed", 0) == 0 and counts.get("skipped", 0) > 0:
        return Verdict("FAIL", "EMPTY_SUITE", "all collected tests were skipped", tests=tests)
    verdict = Verdict(verdict.status, verdict.error_code, verdict.message, tests=tests)
    return apply_test_policy(verdict, "pytest-unit", tests, ctx.profile)


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
    return (
        "python3",
        "-I",
        "-S",
        "-c",
        f"<aicd-scope-scanner sha256:{SCANNER_SHA256}>",
        f"{ctx.input_dir}/{SCOPE_INPUT_FILE}",
    )


def _ruff_lint_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("ruff", "check", "--no-cache", "--output-format=concise", ".")


_RUFF_LINE_RE = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<col>\d+): (?P<rule>[A-Z]+[0-9]*) (?P<msg>.*)$")


def evaluate_ruff_lint(code: int, output: str, _ctx: GateContext) -> Verdict:
    from aicd_verification.findings import make_finding

    findings = tuple(
        make_finding("ruff-lint", m["rule"], "high", m["msg"], file_path=m["path"], line=int(m["line"]))
        for m in (_RUFF_LINE_RE.match(line) for line in output.splitlines())
        if m
    )
    if code == 0:
        if findings:
            return Verdict("FAIL", message="exit 0 but report lists violations", findings=findings)
        return Verdict("PASS")
    if code == 1:
        if not findings and "Found" not in output:
            return Verdict("ERROR", "REPORT_UNPARSABLE", "exit 1 without violations in the report")
        return Verdict("FAIL", message=f"{len(findings)} ruff violation(s)", findings=findings)
    return Verdict("ERROR", "INFRA", f"tool error (exit {code})")


def _ruff_format_argv(ctx: GateContext) -> tuple[str, ...]:
    return ("ruff", "format", "--check", "--no-cache", "--", *ctx.changed_python)


def _pytest_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no")


def _npm_argv(_ctx: GateContext) -> tuple[str, ...]:
    return ("npm", "test", "--no-update-notifier")


CORE_GATE_SPECS: dict[str, GateSpec] = {
    spec.name: spec
    for spec in (
        GateSpec(
            "scope",
            "SCOPE",
            "aicd-scope",
            ("python3", "--version"),
            "scan_report",
            "scan_report",
            _scope_argv,
            _scope_display,
            interpret_scope,
            needs_scope_input=True,
            executes_repository=False,
            evaluate=evaluate_scope,
        ),
        GateSpec(
            "ruff-lint",
            "LINT",
            "ruff",
            ("ruff", "--version"),
            "command_result",
            "report",
            _ruff_lint_argv,
            _ruff_lint_argv,
            _simple_exit,
            evaluate=evaluate_ruff_lint,
        ),
        GateSpec(
            "ruff-format",
            "FORMAT",
            "ruff",
            ("ruff", "--version"),
            "command_result",
            "report",
            _ruff_format_argv,
            _ruff_format_argv,
            _simple_exit,
        ),
        GateSpec(
            "pytest-unit",
            "UNIT",
            "pytest",
            ("python3", "-m", "pytest", "--version"),
            "test_report",
            "test_report",
            _pytest_argv,
            _pytest_argv,
            interpret_pytest,
            evaluate=evaluate_pytest,
        ),
        GateSpec(
            "npm-unit",
            "UNIT",
            "npm",
            ("npm", "--version"),
            "test_report",
            "test_report",
            _npm_argv,
            _npm_argv,
            interpret_npm,
        ),
    )
}

# --- Провайдеры gates (точка расширения реестра) ----------------------------------------------------


@dataclass(frozen=True)
class GateProvider:
    """Вклад модуля в реестр gates: определения + детектор применимости + seed-описание.

    * `specs` — `GateSpec` с уникальными `name` (совпадают с `quality_gates.name`);
    * `detect(facts, stack)` — применимость **только своих** gates (`Applicability` с причиной и
      маркерами: NOT_APPLICABLE без причины недопустим);
    * `definitions` — (name, gate_type, timeout_seconds, description) для seed `quality_gates`
      (обязательность по профилю берётся из `profiles.PROFILE_MATRIX`).
    """

    name: str
    specs: tuple[GateSpec, ...]
    detect: Callable[[RepoFacts, StackInfo], Mapping[str, Applicability]]
    definitions: tuple[tuple[str, str, int, str], ...] = ()


CORE_PROVIDER = GateProvider(
    "core",
    tuple(CORE_GATE_SPECS.values()),
    detect_applicability,
    (
        (
            "scope",
            "SCOPE",
            DEFAULT_TIMEOUT_SECONDS,
            "Изменения только внутри worktree, без .git/symlink-побегов и секретов",
        ),
        (
            "ruff-lint",
            "LINT",
            DEFAULT_TIMEOUT_SECONDS,
            "ruff check (Python), если в репозитории есть конфигурация ruff",
        ),
        ("ruff-format", "FORMAT", DEFAULT_TIMEOUT_SECONDS, "ruff format --check (Python)"),
        ("pytest-unit", "UNIT", DEFAULT_TIMEOUT_SECONDS, "pytest, если есть тесты Python"),
        ("npm-unit", "UNIT", DEFAULT_TIMEOUT_SECONDS, "npm test, если в package.json есть скрипт test"),
    ),
)


def __getattr__(name: str) -> Any:
    """`gates.GATE_SPECS` — полный реестр (ленивая ссылка: registry импортирует этот модуль)."""
    if name == "GATE_SPECS":
        from aicd_verification.registry import GATE_SPECS

        return GATE_SPECS
    raise AttributeError(name)


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
