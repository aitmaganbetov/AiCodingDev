"""Нормализация находок gates и их жизненный цикл (QUALITY_GATES §5, DATABASE_DESIGN §12.6; P6-08).

* `Finding` — нормализованная находка: severity (`finding_severity_t`), `rule_id`, относительный
  `file_path` + `line`, `title`, `fingerprint` (sha256: gate + rule + путь + нормализованный фрагмент —
  без номера строки, чтобы сдвиг кода не плодил «новые» находки). Пути с `..`/абсолютные отбрасываются
  до относительного вида (CHECK `findings.file_path`).
* Жизненный цикл (`finding_status_t`): OPEN → ACKNOWLEDGED/FIXED/ACCEPTED_RISK/FALSE_POSITIVE;
  FIXED → VERIFIED_FIXED только с evidence повторного прогона на commit исправления;
  FALSE_POSITIVE/ACCEPTED_RISK — только человек (`resolved_by` + `resolution_reason`);
  в профиле `critical` находку уровня `critical` нельзя принять как риск.
* Блокировка завершения: открытые (OPEN/ACKNOWLEDGED/FIXED без VERIFIED_FIXED) находки с severity ≥
  порога профиля (`basic`: critical, `strict`: high, `critical`: medium).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal

Severity = Literal["info", "low", "medium", "high", "critical"]
FindingStatus = Literal["OPEN", "ACKNOWLEDGED", "FIXED", "VERIFIED_FIXED", "FALSE_POSITIVE", "ACCEPTED_RISK"]

SEVERITIES: Final[tuple[Severity, ...]] = ("info", "low", "medium", "high", "critical")
SEVERITY_RANK: Final[Mapping[str, int]] = {s: i for i, s in enumerate(SEVERITIES)}
OPEN_STATUSES: Final = frozenset({"OPEN", "ACKNOWLEDGED", "FIXED"})
HUMAN_ONLY: Final = frozenset({"FALSE_POSITIVE", "ACCEPTED_RISK"})

TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "OPEN": frozenset({"ACKNOWLEDGED", "FIXED", "ACCEPTED_RISK", "FALSE_POSITIVE"}),
    "ACKNOWLEDGED": frozenset({"FIXED", "ACCEPTED_RISK", "FALSE_POSITIVE"}),
    "FIXED": frozenset({"VERIFIED_FIXED", "OPEN"}),  # повторный прогон нашёл снова → OPEN
    "VERIFIED_FIXED": frozenset(),
    "FALSE_POSITIVE": frozenset({"OPEN"}),  # пересмотр человеком
    "ACCEPTED_RISK": frozenset({"OPEN"}),
}

_SEVERITY_ALIASES: Final[Mapping[str, Severity]] = {
    "info": "info",
    "information": "info",
    "note": "info",
    "none": "info",
    "low": "low",
    "minor": "low",
    "warning": "medium",
    "warn": "medium",
    "medium": "medium",
    "moderate": "medium",
    "error": "high",
    "high": "high",
    "major": "high",
    "critical": "critical",
    "blocker": "critical",
    "fatal": "critical",
}


def normalize_severity(raw: str | int | None, default: Severity = "medium") -> Severity:
    """Строка инструмента (ESLint 1/2, SARIF level, PHPStan, Semgrep …) → `finding_severity_t`."""
    if isinstance(raw, int):
        by_level: dict[int, Severity] = {0: "info", 1: "medium", 2: "high"}  # ESLint: 1 warn, 2 error
        return by_level.get(raw, default)
    if raw is None:
        return default
    return _SEVERITY_ALIASES.get(str(raw).strip().lower(), default)


def normalize_path(path: str | None, *, root: str | None = None) -> str | None:
    """Относительный POSIX-путь внутри репозитория; путь вне корня → None (не сохраняем)."""
    if not path:
        return None
    p = path.replace("\\", "/")
    for prefix in filter(None, (root, "/workspace")):
        prefix = prefix.rstrip("/") + "/"
        if p.startswith(prefix):
            p = p[len(prefix) :]
            break
    p = re.sub(r"^(\./)+", "", p)
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or p.startswith("/") or ".." in parts:
        return None
    return "/".join(parts)[:1024]


def _normalize_snippet(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:500]


def fingerprint(gate: str, rule_id: str, file_path: str | None, snippet: str) -> str:
    """Отпечаток для дедупликации (`uq_findings_run_fingerprint`): без номера строки."""
    material = "\x1f".join((gate, rule_id, file_path or "", _normalize_snippet(snippet)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Finding:
    gate: str
    rule_id: str
    severity: Severity
    title: str
    file_path: str | None = None
    line: int | None = None
    fingerprint: str = ""
    cwe_id: str | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "title": self.title,
            "file_path": self.file_path,
            "line": self.line,
            "fingerprint": self.fingerprint,
            "cwe_id": self.cwe_id,
        }


_CWE_RE = re.compile(r"^CWE-[0-9]{1,5}$")


def make_finding(
    gate: str,
    rule_id: str | None,
    severity: str | int | None,
    title: str,
    *,
    file_path: str | None = None,
    line: int | None = None,
    snippet: str | None = None,
    root: str | None = None,
    cwe_id: str | None = None,
    default_severity: Severity = "medium",
) -> Finding:
    rule = (rule_id or "unknown").strip()[:255] or "unknown"
    path = normalize_path(file_path, root=root)
    line_no = line if (path is not None and isinstance(line, int) and line >= 1) else None
    title_n = _normalize_snippet(title)[:1000] or rule
    return Finding(
        gate=gate,
        rule_id=rule,
        severity=normalize_severity(severity, default_severity),
        title=title_n,
        file_path=path,
        line=line_no,
        fingerprint=fingerprint(gate, rule, path, snippet if snippet is not None else title_n),
        cwe_id=cwe_id if cwe_id and _CWE_RE.match(cwe_id) else None,
    )


def dedupe(findings: Iterable[Finding]) -> list[Finding]:
    """Одна находка на отпечаток; при коллизии остаётся наиболее строгая severity."""
    best: dict[str, Finding] = {}
    for f in findings:
        cur = best.get(f.fingerprint)
        if cur is None or SEVERITY_RANK[f.severity] > SEVERITY_RANK[cur.severity]:
            best[f.fingerprint] = f
    return sorted(best.values(), key=lambda f: (-SEVERITY_RANK[f.severity], f.file_path or "", f.line or 0, f.rule_id))


def severity_counts(findings: Iterable[Finding]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(SEVERITIES, 0)
    for f in findings:
        counts[f.severity] += 1
    return counts


def at_or_above(severity: str, threshold: str) -> bool:
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK[threshold]


def with_severity_floor(finding: Finding, floor: Severity) -> Finding:
    return finding if at_or_above(finding.severity, floor) else replace(finding, severity=floor)


# --- Жизненный цикл ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionCheck:
    allowed: bool
    reason: str = ""


def check_transition(
    current: str,
    target: str,
    *,
    actor_type: Literal["user", "agent", "system"],
    severity: str,
    profile: str,
    resolution_reason: str | None = None,
    evidence_verified: bool = False,
    fixed_in_commit_sha: str | None = None,
) -> TransitionCheck:
    """Правила переходов статуса находки (БД дублирует CHECK'и ck_findings_*)."""
    if target not in TRANSITIONS.get(current, frozenset()):
        return TransitionCheck(False, f"transition {current} -> {target} is not allowed")
    if target in HUMAN_ONLY or current in HUMAN_ONLY:
        if actor_type != "user":
            return TransitionCheck(False, f"{target} can be set only by a human")
        if target in HUMAN_ONLY and not (resolution_reason and resolution_reason.strip()):
            return TransitionCheck(False, "resolution_reason is required")
    if target == "ACCEPTED_RISK" and profile == "critical" and severity == "critical":
        return TransitionCheck(False, "critical findings cannot be accepted as risk in profile critical")
    if target == "VERIFIED_FIXED" and not (evidence_verified and fixed_in_commit_sha):
        return TransitionCheck(False, "VERIFIED_FIXED requires VERIFIED evidence of a re-run on the fix commit")
    if target == "FIXED" and actor_type == "agent" and not fixed_in_commit_sha:
        return TransitionCheck(False, "FIXED requires the commit with the fix")
    return TransitionCheck(True)


def is_blocking(status: str, severity: str, threshold: str) -> bool:
    """Открытая находка с severity ≥ порога блокирует завершение run (§5.3 п.3)."""
    return status in OPEN_STATUSES and at_or_above(severity, threshold)
