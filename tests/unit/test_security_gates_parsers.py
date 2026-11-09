"""Unit (P6-04/P6-07): разбор отчётов инструментов по сохранённым выводам реальных образов
(`tests/fixtures/security/samples`) и негативные случаи (невалидный/противоречивый отчёт → problem)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aicd_verification.findings import Finding
from aicd_verification.tools import parsers

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "security" / "samples"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def by_rule(report: parsers.ParsedReport) -> dict[str, Finding]:
    return {f.rule_id: f for f in report.findings}


def test_semgrep_sample() -> None:
    report = parsers.parse_semgrep(sample("semgrep.json"), rule_prefix="aicd-input.")
    assert report.problem is None
    rules = by_rule(report)
    shell = rules["aicd.python.subprocess-shell-true"]
    assert (shell.severity, shell.cwe_id, shell.file_path, shell.line) == ("high", "CWE-78", "vuln_handlers.py", 11)
    assert rules["aicd.js.inner-html"].severity == "medium"
    assert rules["aicd.python.os-system"].line == 15
    assert report.summary["scanned_files"] == 2
    assert all(len(f.fingerprint) == 64 for f in report.findings)


def test_semgrep_fingerprint_is_line_independent_but_distinguishes_occurrences() -> None:
    def doc(lines: list[int]) -> str:
        results = [
            {
                "check_id": "r",
                "path": "/workspace/a.py",
                "start": {"line": n},
                "extra": {"message": "m", "severity": "ERROR"},
            }
            for n in lines
        ]
        return json.dumps({"results": results, "errors": [], "paths": {"scanned": ["/workspace/a.py"]}})

    first = parsers.parse_semgrep(doc([3, 9]))
    shifted = parsers.parse_semgrep(doc([5, 11]))
    assert len({f.fingerprint for f in first.findings}) == 2
    assert [f.fingerprint for f in first.findings] == [f.fingerprint for f in shifted.findings]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "not valid JSON"),
        ("{not json", "not valid JSON"),
        ('{"results": "x"}', "no 'results'"),
        (
            json.dumps(
                {"results": [], "errors": [{"level": "error", "message": "invalid configuration"}], "paths": {}}
            ),
            "1 error(s)",
        ),
        (json.dumps({"results": [], "errors": [], "paths": {"scanned": []}}), "scanned no files"),
    ],
)
def test_semgrep_problems(text: str, fragment: str) -> None:
    problem = parsers.parse_semgrep(text).problem
    assert problem is not None and fragment in problem


def test_gitleaks_sample_has_no_secret_values() -> None:
    report = parsers.parse_gitleaks(sample("gitleaks.json"))
    assert report.problem is None
    assert set(by_rule(report)) == {"slack-bot-token", "github-pat"}
    for f in report.findings:
        assert (f.severity, f.cwe_id, f.file_path) == ("high", "CWE-798", "settings.env")
    assert parsers.parse_gitleaks("[]").findings == ()
    assert parsers.parse_gitleaks("{}").problem is not None


def test_trivy_config_sample() -> None:
    report = parsers.parse_trivy(sample("trivy-config.json"), gate="trivy-config")
    assert report.problem is None
    rules = by_rule(report)
    assert rules["DS-0002"].severity == "high" and rules["DS-0002"].line == 2
    assert rules["DS-0002"].file_path == "Dockerfile"
    assert any(r.startswith("KSV") and f.file_path == "k8s/deploy.yaml" for r, f in rules.items())
    assert report.summary["passed_checks"] == 0  # PASS-проверки в отчёт не включаются (только FAIL)


def test_trivy_vuln_sample() -> None:
    report = parsers.parse_trivy(sample("trivy-vuln.json"), gate="trivy-vuln")
    rules = by_rule(report)
    cve = rules["CVE-2020-14343"]
    assert (cve.severity, cve.cwe_id, cve.file_path) == ("critical", "CWE-20", "requirements.txt")
    assert "pyyaml@5.3.1" in cve.title and "fixed in 5.4" in cve.title


def test_trivy_unknown_severity_is_not_downgraded() -> None:
    doc = {
        "SchemaVersion": 2,
        "Results": [{"Target": "go.sum", "Vulnerabilities": [{"VulnerabilityID": "X-1", "Severity": "UNKNOWN"}]}],
    }
    (finding,) = parsers.parse_trivy(json.dumps(doc), gate="trivy-vuln").findings
    assert finding.severity == "medium"
    assert parsers.parse_trivy('{"Results": []}', gate="trivy-vuln").problem is not None


def test_osv_sample() -> None:
    report = parsers.parse_osv(sample("osv-scanner.json"))
    (finding,) = report.findings
    assert (finding.rule_id, finding.severity, finding.cwe_id, finding.file_path) == (
        "AICD-TEST-2026-0001",
        "critical",
        "CWE-20",
        "requirements.txt",
    )
    assert "CVE-2099-0001" in finding.title


def test_osv_severity_fallbacks() -> None:
    def doc(group: dict[str, object], vuln: dict[str, object]) -> str:
        pkg = {
            "package": {"name": "p", "version": "1", "ecosystem": "npm"},
            "groups": [group],
            "vulnerabilities": [vuln],
        }
        return json.dumps({"results": [{"source": {"path": "/workspace/package-lock.json"}, "packages": [pkg]}]})

    word = parsers.parse_osv(doc({"ids": ["G-1"]}, {"id": "G-1", "database_specific": {"severity": "MODERATE"}}))
    assert word.findings[0].severity == "medium"
    score = parsers.parse_osv(doc({"ids": ["G-2"], "max_severity": "7.5"}, {"id": "G-2"}))
    assert score.findings[0].severity == "high"
    unknown = parsers.parse_osv(doc({"ids": ["G-3"]}, {"id": "G-3"}))
    assert unknown.findings[0].severity == "medium"
    assert parsers.severity_from_cvss_score("9.8") == "critical"
    assert parsers.severity_from_cvss_score("n/a") is None


def test_checkov_sample_and_severity_map() -> None:
    report = parsers.parse_checkov(sample("checkov.json"))
    assert report.problem is None
    rules = by_rule(report)
    assert rules["CKV_AWS_57"].severity == "critical"
    assert rules["CKV_DOCKER_8"].severity == "high"
    assert rules["CKV_DOCKER_2"].severity == "low"
    assert rules["CKV2_AWS_62"].severity == "medium"  # нет в карте платформы → medium
    assert rules["CKV_AWS_24"].file_path == "main.tf" and rules["CKV_AWS_24"].line == 10
    assert report.summary["failed"] == len(report.findings) == 14
    assert set(report.summary["frameworks"]) == {"dockerfile", "terraform"}


def test_checkov_empty_and_parse_errors() -> None:
    empty = parsers.parse_checkov(
        '{"passed": 0, "failed": 0, "skipped": 0, "parsing_errors": 0, "checkov_version": "3"}'
    )
    assert empty.problem is None and empty.findings == ()
    broken = {
        "check_type": "terraform",
        "results": {"failed_checks": []},
        "summary": {"passed": 0, "failed": 0, "parsing_errors": 2},
    }
    assert "could not parse" in (parsers.parse_checkov(json.dumps(broken)).problem or "")


def test_conftest_sample() -> None:
    report = parsers.parse_conftest(sample("conftest.json"))
    severities = sorted(f.severity for f in report.findings)
    assert severities == ["high", "low"]
    deny = next(f for f in report.findings if f.severity == "high")
    assert (deny.rule_id, deny.file_path) == ("data.main.deny", "deploy/app.yaml")
    assert report.summary["failures"] == 1 and report.summary["warnings"] == 1
    assert parsers.parse_conftest("").problem is not None


def test_cyclonedx_sample() -> None:
    report = parsers.parse_cyclonedx(sample("sbom.cdx.json"))
    assert report.problem is None
    assert report.summary["spec_version"] == "1.7"
    assert report.summary["components"] >= 2
    assert any(g.startswith("syft ") for g in report.summary["generator"])
    assert parsers.parse_cyclonedx('{"bomFormat": "SPDX"}').problem is not None


@pytest.mark.parametrize(
    ("code", "stderr", "status"),
    [
        (0, "Verified OK", "PASS"),
        (1, "Error: failed to verify signature: invalid signature when validating ASN.1", "FAIL"),
        (1, "Error: no signatures found", "FAIL"),
        (1, "Error: must verify either an image index or image", "ERROR"),
        (1, "Error: open /aicd-input/cosign.pub: no such file or directory", "ERROR"),
        (1, None, "FAIL"),
    ],
)
def test_classify_cosign(code: int, stderr: str | None, status: str) -> None:
    assert parsers.classify_cosign(code, stderr)[0] == status


def test_redact_report_strips_trivy_secret_matches() -> None:
    leaked = "xoxb-" + "1" * 12 + "-" + "2" * 12 + "-" + "a" * 24
    doc = {
        "SchemaVersion": 2,
        "Results": [
            {"Target": "s.py", "Secrets": [{"RuleID": "slack", "Match": f"T = '{leaked}'", "Code": {"Lines": []}}]}
        ],
    }
    redacted = parsers.redact_report("trivy", json.dumps(doc))
    assert leaked not in redacted
    assert json.loads(redacted)["Results"][0]["Secrets"][0]["Match"] == "[REDACTED]"
    assert "ghp_" + "x" * 36 not in parsers.redact_report("gitleaks", "token ghp_" + "x" * 36)
