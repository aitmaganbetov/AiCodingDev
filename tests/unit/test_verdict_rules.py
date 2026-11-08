"""P6-08/P6-09/P6-12: правила вердикта, evidence (AC-14), жизненный цикл findings и guard завершения run.

Сценарии false-pass (AC-11, QUALITY_GATES §7) — ни один не даёт PASS/COMPLETED; DB-часть того же
набора — tests/integration/test_false_pass_prevention.py.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aicd_verification import findings as fnd
from aicd_verification.gates import GateContext, interpret_pytest
from aicd_verification.runners import evaluate_coverage, evaluate_vitest
from aicd_verification.verdict import (
    EvidenceRecord,
    FindingRecord,
    GateRecord,
    evaluate_completion,
    evidence_problems,
    final_commits,
    gate_problems,
)

SHA1 = "a" * 40
SHA2 = "b" * 40
T0 = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
ART = "c" * 64
TASK = uuid.uuid4()


def evidence(**kw: object) -> EvidenceRecord:
    base = EvidenceRecord(
        kind="test_report",
        producer="verification_engine",
        verification_status="VERIFIED",
        commit_sha=SHA1,
        command="python3 -m pytest -q",
        exit_code=0,
        started_at=T0,
        finished_at=T0 + timedelta(seconds=3),
        environment={"runner": "docker", "image": "python@sha256:" + "d" * 64, "tool_versions": {"pytest": "8"}},
        artifact_sha256=ART,
        storage_ref="file://bucket/x",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def gate(name: str = "pytest-unit", status: str = "PASS", **kw: object) -> GateRecord:
    base = GateRecord(
        execution_id=uuid.uuid4(),
        task_id=TASK,
        gate_name=name,
        gate_type="UNIT",
        required=True,
        status=status,
        commit_sha=SHA1,
        not_applicable_reason="detector: no tests" if status == "NOT_APPLICABLE" else None,
        evidence=evidence() if status in ("PASS", "FAIL") else None,
        created_at=T0,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


# --- Evidence (AC-14) ---------------------------------------------------------------------------------


def test_complete_evidence_has_no_problems() -> None:
    assert evidence_problems(evidence(), expected_commit=SHA1, reread_sha256=ART) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"verification_status": "REJECTED"}, "not VERIFIED"),
        ({"verification_status": "UNVERIFIED", "producer": "agent"}, "producer is agent"),
        ({"commit_sha": SHA2}, "commit differs"),
        ({"commit_sha": None}, "no commit_sha"),
        ({"command": ""}, "no command"),
        ({"exit_code": None}, "no exit code"),
        ({"started_at": None}, "timestamps"),
        ({"finished_at": T0 - timedelta(seconds=1)}, "finished before"),
        ({"environment": {"runner": "local"}}, "runner/tool versions"),
        ({"environment": {"runner": "docker", "tool_versions": {"x": "1"}}}, "container image"),
        ({"artifact_sha256": None}, "no sha256"),
    ],
)
def test_incomplete_evidence(change: dict[str, object], problem: str) -> None:
    problems = evidence_problems(evidence(**change), expected_commit=SHA1)
    assert any(problem in p for p in problems), problems


def test_artifact_sha_mismatch_on_reread() -> None:
    assert "sha256 mismatch" in evidence_problems(evidence(), expected_commit=SHA1, reread_sha256="e" * 64)[0]
    assert evidence_problems(None, expected_commit=SHA1) == ["evidence is missing"]


# --- Статусы gate -------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["FAIL", "ERROR", "SKIPPED", "PENDING", "RUNNING"])
def test_non_pass_statuses_never_count(status: str) -> None:
    assert gate_problems(gate(status=status, error_message="TOOL_UNAVAILABLE: x"), final_commit=SHA1)


def test_not_applicable_requires_reason() -> None:
    assert gate_problems(gate(status="NOT_APPLICABLE"), final_commit=SHA1) == []
    assert (
        "without a reason"
        in gate_problems(gate(status="NOT_APPLICABLE", not_applicable_reason="  "), final_commit=SHA1)[0]
    )


def test_stale_result_on_old_commit() -> None:
    assert "not the final commit" in gate_problems(gate(), final_commit=SHA2)[0]


def test_final_commit_is_latest_execution_per_task() -> None:
    other = uuid.uuid4()
    gates = [
        gate(created_at=T0),
        gate(created_at=T0 + timedelta(minutes=5), commit_sha=SHA2),
        gate(task_id=other, created_at=T0 + timedelta(minutes=9)),
    ]
    assert final_commits(gates) == {TASK: SHA2, other: SHA1}


# --- Guard завершения run --------------------------------------------------------------------------


def completion(gates: list[GateRecord], **kw: object) -> tuple[bool, tuple[str, ...]]:
    params: dict[str, object] = {
        "profile": "strict",
        "required_gate_names": [g.gate_name for g in gates],
        "gates": gates,
    }
    params.update(kw)
    v = evaluate_completion(**params)  # type: ignore[arg-type]
    return v.ok, v.reasons


def test_all_required_pass_completes() -> None:
    ok, reasons = completion([gate("scope"), gate("pytest-unit"), gate("eslint", "NOT_APPLICABLE")])
    assert ok, reasons


def test_advisory_failure_does_not_block_but_required_does() -> None:
    advisory = gate("pytest-coverage", "FAIL", required=False)
    assert completion([gate("scope"), advisory], required_gate_names=["scope"])[0]
    assert not completion([gate("scope"), replace(advisory, required=True)])[0]


@pytest.mark.parametrize(
    ("profile", "severity", "blocks"),
    [
        ("basic", "high", False),
        ("basic", "critical", True),
        ("strict", "medium", False),
        ("strict", "high", True),
        ("critical", "medium", True),
        ("critical", "low", False),
    ],
)
def test_open_findings_threshold_by_profile(profile: str, severity: str, blocks: bool) -> None:
    ok, _ = completion([gate("scope")], profile=profile, findings=[FindingRecord(severity, "OPEN", "r")])
    assert ok is (not blocks)


@pytest.mark.parametrize(
    ("status", "profile", "severity", "blocks"),
    [
        ("ACKNOWLEDGED", "strict", "high", True),
        ("FIXED", "strict", "high", True),  # FIXED без VERIFIED_FIXED не закрывает находку
        ("ACCEPTED_RISK", "strict", "critical", False),
        ("ACCEPTED_RISK", "critical", "critical", True),  # в critical нельзя принять critical как риск
        ("ACCEPTED_RISK", "critical", "high", False),
    ],
)
def test_finding_status_rules_in_completion(status: str, profile: str, severity: str, blocks: bool) -> None:
    ok, _ = completion([gate("scope")], profile=profile, findings=[FindingRecord(severity, status, "r")])
    assert ok is (not blocks)


def test_pending_approvals_block_completion() -> None:
    ok, reasons = completion([gate("scope")], pending_approvals=1)
    assert not ok and "PENDING" in reasons[0]


def test_failed_blocking_controls_block_completion() -> None:
    """§5.3 п.6: FAIL блокирующего контроля (список собирает `_load` по project_controls.blocking)."""
    ok, reasons = completion([gate("scope")], blocking_failed_controls=["SEC-27002-01", "SEC-27002-01"])
    assert not ok and reasons == ("blocking control SEC-27002-01 is FAIL",)
    assert completion([gate("scope")], blocking_failed_controls=[])[0]


# --- P6-12: сценарии ложного прохождения (ни один не даёт PASS / COMPLETED) --------------------------


def test_fp_tool_missing() -> None:
    assert not completion([gate("ruff-lint", "ERROR", error_message="TOOL_UNAVAILABLE: ruff")])[0]


def test_fp_tool_crash_and_timeout() -> None:
    assert interpret_pytest(3, "INTERNALERROR").status == "ERROR"
    assert not completion([gate(status="ERROR", error_message="TIMEOUT: gate exceeded 2s")])[0]


def test_fp_zero_tests_and_all_skipped() -> None:
    assert interpret_pytest(5, "no tests ran").status == "FAIL"
    v = evaluate_vitest(0, '{"numTotalTests": 2, "numPassedTests": 0, "numPendingTests": 2}', GateContext("/w", "/i"))
    assert (v.status, v.error_code) == ("FAIL", "EMPTY_SUITE")


def test_fp_coverage_missing_and_unparsable() -> None:
    ctx = GateContext("/w", "/i", nonce="n")
    assert evaluate_coverage(0, "3 passed\n", ctx).status == "ERROR"
    assert evaluate_coverage(0, "3 passed\nAICD_COVERAGE_REPORT n null\n", ctx).status == "ERROR"
    assert evaluate_coverage(0, "3 passed\nAICD_COVERAGE_REPORT n {broken\n", ctx).status == "ERROR"


def test_fp_exit_zero_with_failures_in_output() -> None:
    assert interpret_pytest(0, "3 passed, 1 failed in 0.2s").status == "FAIL"


def test_fp_gate_on_different_commit_and_stale_after_new_commit() -> None:
    old = gate("pytest-unit", created_at=T0)
    newer = gate("scope", created_at=T0 + timedelta(minutes=1), commit_sha=SHA2)
    ok, reasons = completion([old, newer])
    assert not ok and any("not the final commit" in r for r in reasons)
    forged = gate(evidence=evidence(commit_sha=SHA2))  # evidence от другого SHA
    assert not completion([forged])[0]


def test_fp_evidence_sha_mismatch() -> None:
    g = gate()
    ok, reasons = completion([g], reread={g.execution_id: "f" * 64})
    assert not ok and "sha256 mismatch" in reasons[0]
    assert not completion([g], reread={g.execution_id: "unreadable"})[0]


def test_fp_not_applicable_without_reason() -> None:
    assert not completion([gate("scope"), gate("eslint", "NOT_APPLICABLE", not_applicable_reason=None)])[0]


def test_fp_empty_verification_and_missing_required_gate() -> None:
    ok, reasons = completion([gate("eslint", "NOT_APPLICABLE")])
    assert not ok and "no gate execution has PASS" in reasons[0]
    ok, reasons = completion([gate("scope")], required_gate_names=["scope", "pytest-unit"])
    assert not ok and "pytest-unit has no execution" in reasons[0]
    assert not completion([], required_gate_names=[])[0]


def test_fp_required_skipped() -> None:
    assert not completion([gate("scope"), gate("pytest-unit", "SKIPPED", not_applicable_reason="budget")])[0]


# --- Findings: нормализация и жизненный цикл -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(2, "high"), (1, "medium"), ("error", "high"), ("WARNING", "medium"), ("note", "info"), ("blocker", "critical"),
     (None, "medium"), ("weird", "medium"), (7, "medium")],
)  # fmt: skip
def test_normalize_severity(raw: str | int | None, expected: str) -> None:
    assert fnd.normalize_severity(raw) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/workspace/src/a.py", "src/a.py"), ("./src/a.py", "src/a.py"), ("../etc/passwd", None), ("/etc/passwd", None),
     ("src\\win.py", "src/win.py"), ("", None), ("/repo/x.py", "x.py")],
)  # fmt: skip
def test_normalize_path(path: str, expected: str | None) -> None:
    assert fnd.normalize_path(path, root="/repo") == expected


def test_fingerprint_ignores_line_and_dedupe_keeps_strictest() -> None:
    a = fnd.make_finding("eslint", "no-undef", 2, "x is not defined", file_path="src/a.js", line=3)
    b = fnd.make_finding("eslint", "no-undef", 1, "x  is not   defined", file_path="src/a.js", line=30)
    assert a.fingerprint == b.fingerprint
    assert fnd.dedupe([b, a]) == [a]
    c = fnd.make_finding("eslint", "no-undef", 2, "x is not defined", file_path="src/b.js")
    assert c.fingerprint != a.fingerprint
    assert fnd.make_finding("g", None, None, "", line=5).line is None  # строка без пути не сохраняется
    assert fnd.make_finding("g", "r", "high", "t", cwe_id="CWE-79").cwe_id == "CWE-79"
    assert fnd.make_finding("g", "r", "high", "t", cwe_id="79").cwe_id is None
    assert fnd.severity_counts([a, c]) == {"info": 0, "low": 0, "medium": 0, "high": 2, "critical": 0}


@pytest.mark.parametrize(
    ("current", "target", "actor", "kw", "allowed"),
    [
        ("OPEN", "ACKNOWLEDGED", "user", {}, True),
        ("OPEN", "FALSE_POSITIVE", "user", {"resolution_reason": "test code"}, True),
        ("OPEN", "FALSE_POSITIVE", "agent", {"resolution_reason": "nope"}, False),  # агент не закрывает сам
        ("OPEN", "ACCEPTED_RISK", "system", {"resolution_reason": "x"}, False),
        ("OPEN", "ACCEPTED_RISK", "user", {}, False),  # без причины
        ("OPEN", "FIXED", "agent", {}, False),
        ("OPEN", "FIXED", "agent", {"fixed_in_commit_sha": SHA2}, True),
        ("FIXED", "VERIFIED_FIXED", "system", {}, False),
        ("FIXED", "VERIFIED_FIXED", "system", {"evidence_verified": True, "fixed_in_commit_sha": SHA2}, True),
        ("FIXED", "OPEN", "system", {}, True),
        ("VERIFIED_FIXED", "OPEN", "system", {}, False),
        ("ACCEPTED_RISK", "OPEN", "user", {}, True),
        ("ACKNOWLEDGED", "OPEN", "user", {}, False),
    ],
)
def test_finding_lifecycle(current: str, target: str, actor: str, kw: dict[str, object], allowed: bool) -> None:
    check = fnd.check_transition(current, target, actor_type=actor, severity="high", profile="strict", **kw)  # type: ignore[arg-type]
    assert check.allowed is allowed, check.reason


def test_critical_profile_forbids_accepting_critical_risk() -> None:
    check = fnd.check_transition(
        "OPEN", "ACCEPTED_RISK", actor_type="user", severity="critical", profile="critical", resolution_reason="r"
    )
    assert not check.allowed and "critical" in check.reason
    assert fnd.is_blocking("OPEN", "high", "high") and not fnd.is_blocking("VERIFIED_FIXED", "critical", "low")
    assert fnd.with_severity_floor(fnd.make_finding("g", "r", "low", "t"), "high").severity == "high"
