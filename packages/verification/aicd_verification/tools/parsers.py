"""Разбор отчётов security/supply-chain инструментов в нормализованные находки (P6-04, P6-07).

Каждый парсер получает текст отчёта (JSON со stdout инструмента) и возвращает `ParsedReport`:
находки (`aicd_verification.findings.Finding`), сводку для evidence и `problem` — причину, по
которой отчёт нельзя считать достоверным (невалидный JSON, ошибка конфигурации инструмента,
пустой охват). `problem` всегда ведёт к ERROR (никогда к PASS).

Секреты: значения секретов в находки не попадают (Gitleaks запускается с `--redact`, у Trivy поля
`Match`/`Code` не читаются); `redact_report` дополнительно вычищает их из сохраняемого отчёта.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aicd_platform.redaction import redact_text
from aicd_verification.findings import Finding, make_finding, normalize_severity

SECRET_CWE = "CWE-798"  # Use of Hard-coded Credentials
_CWE_RE = re.compile(r"CWE-(\d{1,5})", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedReport:
    findings: tuple[Finding, ...] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)
    problem: str | None = None  # не None → отчёт недостоверен → ERROR


def _load_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty report")
    return json.loads(stripped)


def _cwe(values: Any) -> str | None:
    items = values if isinstance(values, list | tuple) else [values]
    for item in items:
        match = _CWE_RE.search(str(item or ""))
        if match:
            return f"CWE-{int(match.group(1))}"
        if isinstance(item, int | str) and str(item).isdigit():
            return f"CWE-{int(item)}"
    return None


class _Occurrences:
    """k-е вхождение правила в файле: различитель fingerprint без номера строки (сдвиг кода не
    делает находку «новой», две одинаковые находки в одном файле не сливаются)."""

    def __init__(self) -> None:
        self._seen: Counter[tuple[str, str, str]] = Counter()

    def anchor(self, rule: str, path: str, text: str) -> str:
        key = (rule, path, text)
        self._seen[key] += 1
        return f"{text}#{self._seen[key]}"


def _rel(path: Any) -> str:
    """Путь инструмента → относительный путь репозитория: `/workspace/a.py`, `/a.py` (Checkov
    указывает пути от корня `-d`) → `a.py`."""
    text = str(path or "")
    if text.startswith("/workspace/"):
        text = text[len("/workspace/") :]
    return text.lstrip("/")


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


# --- Semgrep (SAST) -------------------------------------------------------------------------------


def parse_semgrep(text: str, *, gate: str = "semgrep-sast", rule_prefix: str = "") -> ParsedReport:
    """`semgrep scan --json`. Ошибки уровня `error` (конфигурация, правила) и пустой охват → problem."""
    try:
        data = _load_json(text)
    except ValueError as exc:
        return ParsedReport(problem=f"semgrep report is not valid JSON ({exc})")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return ParsedReport(problem="semgrep report has no 'results' list")
    errors = [e for e in data.get("errors") or [] if isinstance(e, dict)]
    fatal = [e for e in errors if str(e.get("level", "")).lower() == "error"]
    scanned = (data.get("paths") or {}).get("scanned") or []
    summary: dict[str, Any] = {
        "tool_version": data.get("version"),
        "scanned_files": len(scanned) if isinstance(scanned, list) else None,
        "errors": len(fatal),
        "warnings": len(errors) - len(fatal),
    }
    if fatal:
        first = redact_text(str(fatal[0].get("message") or fatal[0].get("type") or "error"))[:300]
        return ParsedReport(summary=summary, problem=f"semgrep reported {len(fatal)} error(s): {first}")
    occ = _Occurrences()
    findings: list[Finding] = []
    for item in data["results"]:
        if not isinstance(item, dict):
            return ParsedReport(summary=summary, problem="semgrep result is not an object")
        rule = str(item.get("check_id") or "unknown")
        if rule_prefix and rule.startswith(rule_prefix):
            rule = rule[len(rule_prefix) :]
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        meta = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
        severity = meta.get("aicd-severity") or extra.get("severity")
        message = str(extra.get("message") or rule)
        path = _rel(item.get("path"))
        findings.append(
            make_finding(
                gate,
                rule,
                severity,
                message,
                file_path=path,
                line=_int((item.get("start") or {}).get("line")),
                snippet=occ.anchor(rule, path, message),
                cwe_id=_cwe(meta.get("cwe")),
            )
        )
    if summary["scanned_files"] == 0:
        return ParsedReport(tuple(findings), summary, "semgrep scanned no files (all targets ignored?)")
    return ParsedReport(tuple(findings), summary)


# --- Gitleaks (SECRETS) ---------------------------------------------------------------------------


def parse_gitleaks(text: str, *, gate: str = "gitleaks-secrets") -> ParsedReport:
    """`gitleaks dir --report-format json --redact`: JSON-массив находок (пустой — `[]`)."""
    try:
        data = _load_json(text)
    except ValueError as exc:
        return ParsedReport(problem=f"gitleaks report is not valid JSON ({exc})")
    if not isinstance(data, list):
        return ParsedReport(problem="gitleaks report is not a JSON array")
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            return ParsedReport(problem="gitleaks finding is not an object")
        rule = str(item.get("RuleID") or "secret")
        line = _int(item.get("StartLine"))
        path = _rel(item.get("File"))
        findings.append(
            make_finding(
                gate,
                rule,
                "high",
                f"Secret detected ({rule}): {item.get('Description') or 'hard-coded credential'}",
                file_path=path,
                line=line,
                # Значение секрета в отпечаток не входит (gitleaks уже отдаёт REDACTED).
                snippet=f"{rule}@{path}:{line}",
                cwe_id=SECRET_CWE,
            )
        )
    return ParsedReport(tuple(findings), {"leaks": len(findings)})


# --- Trivy fs (SCA / IAC / secrets) ---------------------------------------------------------------


def parse_trivy(text: str, *, gate: str) -> ParsedReport:
    """`trivy fs --format json`: Vulnerabilities, Misconfigurations (Status=FAIL), Secrets."""
    try:
        data = _load_json(text)
    except ValueError as exc:
        return ParsedReport(problem=f"trivy report is not valid JSON ({exc})")
    if not isinstance(data, dict) or "SchemaVersion" not in data:
        return ParsedReport(problem="trivy report has no SchemaVersion")
    results = data.get("Results") or []
    if not isinstance(results, list):
        return ParsedReport(problem="trivy 'Results' is not a list")
    findings: list[Finding] = []
    occ = _Occurrences()
    targets: list[str] = []
    passed_checks = 0
    for result in results:
        if not isinstance(result, dict):
            return ParsedReport(problem="trivy result is not an object")
        target = _rel(result.get("Target"))
        targets.append(target)
        for vuln in result.get("Vulnerabilities") or []:
            vid = str(vuln.get("VulnerabilityID") or "unknown")
            pkg = f"{vuln.get('PkgName')}@{vuln.get('InstalledVersion')}"
            fixed = vuln.get("FixedVersion")
            title = vuln.get("Title") or vid
            findings.append(
                make_finding(
                    gate,
                    vid,
                    normalize_severity(vuln.get("Severity"), "medium"),  # UNKNOWN → medium (fail-safe)
                    f"{pkg}: {title}" + (f" (fixed in {fixed})" if fixed else " (no fixed version)"),
                    file_path=target,
                    snippet=f"{vid}|{pkg}",
                    cwe_id=_cwe(vuln.get("CweIDs") or []),
                )
            )
        for mis in result.get("Misconfigurations") or []:
            if str(mis.get("Status", "FAIL")).upper() != "FAIL":
                passed_checks += 1
                continue
            rule = str(mis.get("ID") or mis.get("AVDID") or "misconfig")
            title = str(mis.get("Title") or mis.get("Message") or rule)
            findings.append(
                make_finding(
                    gate,
                    rule,
                    normalize_severity(mis.get("Severity"), "medium"),
                    f"{title}: {mis.get('Message') or ''}".strip(": "),
                    file_path=target,
                    line=_int((mis.get("CauseMetadata") or {}).get("StartLine")),
                    snippet=occ.anchor(rule, target, title),
                )
            )
        for sec in result.get("Secrets") or []:
            rule = str(sec.get("RuleID") or "secret")
            line = _int(sec.get("StartLine"))
            findings.append(
                make_finding(
                    gate,
                    rule,
                    "high",
                    f"Secret detected ({rule}): {sec.get('Title') or 'hard-coded credential'}",
                    file_path=target,
                    line=line,
                    snippet=f"{rule}@{target}:{line}",
                    cwe_id=SECRET_CWE,
                )
            )
    summary = {
        "tool_version": (data.get("Trivy") or {}).get("Version") if isinstance(data.get("Trivy"), dict) else None,
        "targets": len(targets),
        "passed_checks": passed_checks,
    }
    return ParsedReport(tuple(findings), summary)


# --- OSV-Scanner (SCA) ----------------------------------------------------------------------------

_OSV_SEVERITY_WORDS = {"CRITICAL", "HIGH", "MODERATE", "MEDIUM", "LOW"}


def severity_from_cvss_score(score: Any) -> str | None:
    """Базовая оценка CVSS (OSV `max_severity`) → severity по шкале CVSS v3; не число → None."""
    try:
        value = float(str(score))
    except (TypeError, ValueError):
        return None
    if value >= 9.0:
        return "critical"
    if value >= 7.0:
        return "high"
    if value >= 4.0:
        return "medium"
    return "low" if value > 0.0 else "info"


def _osv_vuln_severity(vuln: Mapping[str, Any], group_score: Any) -> str:
    by_score = severity_from_cvss_score(group_score)
    if by_score is not None:
        return by_score
    for holder in (vuln.get("database_specific"), *[a.get("database_specific") for a in vuln.get("affected") or []]):
        if isinstance(holder, dict) and str(holder.get("severity", "")).upper() in _OSV_SEVERITY_WORDS:
            return normalize_severity(str(holder["severity"]))
    return "medium"  # severity неизвестна — не занижаем


def _osv_cwe(vuln: Mapping[str, Any]) -> str | None:
    for holder in (vuln.get("database_specific"), *[a.get("database_specific") for a in vuln.get("affected") or []]):
        if isinstance(holder, dict) and holder.get("cwe_ids"):
            return _cwe(holder["cwe_ids"])
    return None


def parse_osv(text: str, *, gate: str = "osv-scanner-sca") -> ParsedReport:
    """`osv-scanner scan source --format json`: results[].packages[].{groups, vulnerabilities}."""
    try:
        data = _load_json(text)
    except ValueError as exc:
        return ParsedReport(problem=f"osv-scanner report is not valid JSON ({exc})")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return ParsedReport(problem="osv-scanner report has no 'results' list")
    findings: list[Finding] = []
    sources: list[str] = []
    packages = 0
    for result in data["results"]:
        source = _rel(((result or {}).get("source") or {}).get("path"))
        sources.append(source)
        for pkg in result.get("packages") or []:
            packages += 1
            info = pkg.get("package") or {}
            name = f"{info.get('ecosystem')}:{info.get('name')}@{info.get('version')}"
            vulns = {str(v.get("id")): v for v in pkg.get("vulnerabilities") or [] if isinstance(v, dict)}
            for group in pkg.get("groups") or [{"ids": [vid]} for vid in vulns]:
                ids = [str(i) for i in group.get("ids") or []]
                if not ids:
                    continue
                vuln = next((vulns[i] for i in ids if i in vulns), {})
                aliases = sorted(set(group.get("aliases") or ids) - {ids[0]})
                summary_text = vuln.get("summary") or vuln.get("details") or ids[0]
                findings.append(
                    make_finding(
                        gate,
                        ids[0],
                        _osv_vuln_severity(vuln, group.get("max_severity")),
                        f"{name}: {summary_text}" + (f" (aliases: {', '.join(aliases[:5])})" if aliases else ""),
                        file_path=source,
                        snippet=f"{ids[0]}|{name}",
                        cwe_id=_osv_cwe(vuln),
                    )
                )
    return ParsedReport(tuple(findings), {"sources": len(sources), "vulnerable_packages": packages})


# --- Checkov (IAC) --------------------------------------------------------------------------------

# Open-source Checkov не отдаёт severity (null без API-ключа платформы Prisma). Платформа задаёт
# severity для известных опасных проверок; остальные проваленные проверки — `medium`
# (блокируют в профиле critical; в basic/strict IAC блокирует high+).
CHECKOV_SEVERITY: Mapping[str, str] = {
    "CKV_AWS_20": "high",  # S3 ACL public READ
    "CKV_AWS_57": "critical",  # S3 ACL public WRITE
    "CKV_AWS_24": "high",  # SG ingress 0.0.0.0/0 → 22
    "CKV_AWS_25": "high",  # SG ingress 0.0.0.0/0 → 3389
    "CKV_AWS_260": "high",  # SG ingress 0.0.0.0/0 → 80
    "CKV_AWS_40": "high",  # IAM policy attached to users
    "CKV_AWS_1": "high",  # IAM "*:*" admin policy
    "CKV_AWS_62": "high",
    "CKV_AWS_63": "high",
    "CKV_AWS_17": "high",  # RDS publicly accessible
    "CKV_AWS_16": "high",  # RDS unencrypted
    "CKV_AWS_19": "medium",
    "CKV_AWS_41": "critical",  # hard-coded AWS credentials in provider
    "CKV_AZURE_41": "high",
    "CKV_GCP_2": "high",
    "CKV_DOCKER_1": "high",  # port 22 exposed
    "CKV_DOCKER_8": "high",  # last USER is root
    "CKV_DOCKER_3": "medium",  # no USER
    "CKV_DOCKER_2": "low",  # no HEALTHCHECK
    "CKV_DOCKER_4": "low",  # ADD instead of COPY
    "CKV_DOCKER_7": "medium",  # latest tag
    "CKV_K8S_16": "critical",  # privileged container
    "CKV_K8S_17": "high",  # hostPID
    "CKV_K8S_18": "high",  # hostIPC
    "CKV_K8S_19": "high",  # hostNetwork
    "CKV_K8S_20": "high",  # allowPrivilegeEscalation
    "CKV_K8S_23": "medium",  # runAsNonRoot
    "CKV_K8S_27": "critical",  # docker.sock mount
    "CKV_K8S_39": "high",  # CAP_SYS_ADMIN
    "CKV_SECRET_2": "high",
    "CKV_SECRET_6": "high",
}


def parse_checkov(text: str, *, gate: str = "checkov-iac") -> ParsedReport:
    """`checkov -o json`: объект (один фреймворк) или массив по фреймворкам; пустой охват — `{"passed":0…}`."""
    try:
        data = _load_json(text)
    except ValueError as exc:
        return ParsedReport(problem=f"checkov report is not valid JSON ({exc})")
    reports = data if isinstance(data, list) else [data]
    findings: list[Finding] = []
    occ = _Occurrences()
    totals = {"passed": 0, "failed": 0, "skipped": 0, "parsing_errors": 0, "resource_count": 0}
    frameworks: list[str] = []
    for report in reports:
        if not isinstance(report, dict):
            return ParsedReport(problem="checkov report entry is not an object")
        if "results" not in report:
            # Нет файлов поддерживаемых фреймворков: {"passed": 0, "failed": 0, …, "checkov_version": …}.
            if "checkov_version" in report or "passed" in report:
                continue
            return ParsedReport(problem="checkov report has no 'results'")
        frameworks.append(str(report.get("check_type")))
        summary = report.get("summary") or {}
        for key in totals:
            totals[key] += int(summary.get(key) or 0)
        for check in (report.get("results") or {}).get("failed_checks") or []:
            rule = str(check.get("check_id") or "checkov")
            severity = check.get("severity") or CHECKOV_SEVERITY.get(rule, "medium")
            path = _rel(check.get("file_path"))
            lines = check.get("file_line_range") or []
            resource = str(check.get("resource") or "")
            findings.append(
                make_finding(
                    gate,
                    rule,
                    str(severity),
                    f"{check.get('check_name') or rule} ({resource})",
                    file_path=path,
                    line=_int(lines[0]) if lines else None,
                    snippet=occ.anchor(rule, path, resource),
                )
            )
    result_summary: dict[str, Any] = {"frameworks": frameworks, **totals}
    if totals["parsing_errors"] and not findings:
        return ParsedReport((), result_summary, f"checkov could not parse {totals['parsing_errors']} file(s)")
    return ParsedReport(tuple(findings), result_summary)


# --- conftest / OPA (POLICY) ----------------------------------------------------------------------


def parse_conftest(text: str, *, gate: str = "conftest-policy") -> ParsedReport:
    """`conftest test --output json`: failures → high, warnings → low, exceptions учитываются в сводке."""
    try:
        data = _load_json(text)
    except ValueError as exc:
        return ParsedReport(problem=f"conftest report is not valid JSON ({exc})")
    if not isinstance(data, list):
        return ParsedReport(problem="conftest report is not a JSON array")
    findings: list[Finding] = []
    occ = _Occurrences()
    totals = {"files": 0, "successes": 0, "failures": 0, "warnings": 0, "exceptions": 0}
    for item in data:
        if not isinstance(item, dict):
            return ParsedReport(problem="conftest result is not an object")
        totals["files"] += 1
        totals["successes"] += int(item.get("successes") or 0)
        totals["exceptions"] += len(item.get("exceptions") or [])
        path = _rel(item.get("filename"))
        namespace = str(item.get("namespace") or "main")
        for kind, severity in (("failures", "high"), ("warnings", "low")):
            for entry in item.get(kind) or []:
                totals[kind] += 1
                msg = str((entry or {}).get("msg") or kind)
                query = str(((entry or {}).get("metadata") or {}).get("query") or f"data.{namespace}")
                findings.append(
                    make_finding(gate, query, severity, msg, file_path=path, snippet=occ.anchor(query, path, msg))
                )
    return ParsedReport(tuple(findings), totals)


# --- Syft (SBOM, CycloneDX JSON) ------------------------------------------------------------------


def parse_cyclonedx(text: str) -> ParsedReport:
    """Валидность SBOM: `bomFormat=CycloneDX`, `specVersion`, список `components`; находок нет."""
    try:
        data = _load_json(text)
    except ValueError as exc:
        return ParsedReport(problem=f"SBOM is not valid JSON ({exc})")
    if not isinstance(data, dict) or data.get("bomFormat") != "CycloneDX" or not data.get("specVersion"):
        return ParsedReport(problem="SBOM is not a CycloneDX document")
    components = data.get("components") or []
    if not isinstance(components, list):
        return ParsedReport(problem="SBOM 'components' is not a list")
    types = Counter(str(c.get("type")) for c in components if isinstance(c, dict))
    purls = sum(1 for c in components if isinstance(c, dict) and c.get("purl"))
    tools = ((data.get("metadata") or {}).get("tools") or {}).get("components") or []
    return ParsedReport(
        (),
        {
            "spec_version": data.get("specVersion"),
            "serial_number": data.get("serialNumber"),
            "components": len(components),
            "components_with_purl": purls,
            "component_types": dict(sorted(types.items())),
            "generator": [f"{t.get('name')} {t.get('version')}" for t in tools if isinstance(t, dict)],
        },
    )


# --- Cosign (SIGN) --------------------------------------------------------------------------------

_COSIGN_FAIL_MARKERS = (
    "no signatures found",
    "no matching signatures",
    "invalid signature",
    "failed to verify signature",
    "signature mismatch",
    "error verifying bundle",
    "none of the expected identities matched",
    "image tag not found",  # подпись (тег .sig) отсутствует в OCI layout
)


def classify_cosign(exit_code: int, stderr: str | None) -> tuple[str, str]:
    """(`PASS`|`FAIL`|`ERROR`, сообщение) по коду и stderr cosign.

    Без stderr (engine v0 передаёт только stdout) ненулевой код трактуется как FAIL: подпись не
    подтверждена; это никогда не PASS."""
    if exit_code == 0:
        return "PASS", "signature verified"
    if stderr is None:
        return "FAIL", f"signature not verified (exit {exit_code})"
    low = stderr.lower()
    reason = next((m for m in _COSIGN_FAIL_MARKERS if m in low), None)
    tail = redact_text(" ".join(stderr.strip().splitlines()[-1:]))[:300]
    if reason:
        return "FAIL", f"signature verification failed: {reason} ({tail})"
    return "ERROR", f"cosign error (exit {exit_code}): {tail}"


# --- Redaction сохраняемого отчёта ----------------------------------------------------------------

_TRIVY_SECRET_FIELDS = ("Match", "Code")


def redact_report(tool: str, text: str) -> str:
    """Отчёт для artifact: у секретов Trivy удаляются `Match`/`Code` (частично маскированы
    инструментом), затем общий `redact_text` платформы по всему тексту."""
    if tool == "trivy":
        try:
            data = json.loads(text)
        except ValueError:
            return redact_text(text)
        if isinstance(data, dict):
            for result in data.get("Results") or []:
                for sec in (result or {}).get("Secrets") or []:
                    if isinstance(sec, dict):
                        for key in _TRIVY_SECRET_FIELDS:
                            if key in sec:
                                sec[key] = "[REDACTED]"
            text = json.dumps(data, ensure_ascii=False, indent=1)
    return redact_text(text)
