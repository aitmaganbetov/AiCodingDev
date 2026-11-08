"""P6-10: оценка соответствия run и трассировка standard → … → verdict (QUALITY_GATES §8.2, ТЗ §13)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace

import pytest

from aicd_platform.ids import uuid7
from aicd_standards.compliance import EvaluationInput, evaluate
from aicd_standards.model import (
    DISCLAIMER,
    Attestation,
    Control,
    EvidenceItem,
    FindingItem,
    GateRun,
    ProjectControl,
    RunFacts,
)
from tests.support.standards import control, seed_controls

COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
OWNER = uuid7()


def _approved(
    *controls: Control, applicability: str = "applicable", rationale: str | None = None
) -> dict[uuid.UUID, ProjectControl]:
    return {
        c.id: ProjectControl(c.id, applicability, rationale, None, OWNER)  # type: ignore[arg-type]
        for c in controls
    }


def _evidence(
    status: str = "VERIFIED", commit: str | None = COMMIT, producer: str = "verification_engine"
) -> EvidenceItem:
    return EvidenceItem(uuid7(), "command_result", producer, status, commit, uuid7())


def _gate(
    gate_type: str, status: str, ev: EvidenceItem | None, *, name: str | None = None, commit: str = COMMIT
) -> GateRun:
    return GateRun(
        uuid7(), gate_type, name or gate_type.lower(), True, status, commit, ev.id if ev else None,
        "no markers" if status == "NOT_APPLICABLE" else None,
    )  # fmt: skip


def _run(
    controls: Sequence[Control],
    gates: Sequence[GateRun] = (),
    evidence: Sequence[EvidenceItem] = (),
    findings: Sequence[FindingItem] = (),
    *,
    project: dict[uuid.UUID, ProjectControl] | None = None,
    attestations: Sequence[Attestation] = (),
    mode: str = "DEVELOP",
    profile_block: str = "high",
    commit: str | None = COMMIT,
) -> dict[str, tuple[str, str]]:
    report = evaluate(
        EvaluationInput(
            facts=RunFacts(uuid7(), mode, "strict", commit, profile_block),
            controls=controls,
            project_controls=_approved(*controls) if project is None else project,
            gates=gates,
            evidence={e.id: e for e in evidence},
            findings=findings,
            attestations={a.control_id: a for a in attestations},
        )
    )
    return {v.control.control_code: (v.status, v.notes) for v in report.verdicts}


def _status(*args: object, **kwargs: object) -> str:
    result = _run(*args, **kwargs)  # type: ignore[arg-type]
    assert len(result) == 1
    return next(iter(result.values()))[0]


LINT = control("QM-25010-01")
SECRETS = control("SEC-27002-01")
INJ = control("CWE-INJ-01")
MANUAL = control("ARCH-42010-02")
REVIEW = control("ASVS-V2-01")
DOC = control("ARCH-42010-01")


# --- automated_gate -----------------------------------------------------------------------------------


def test_pass_requires_verified_evidence_on_run_commit() -> None:
    ev = _evidence()
    assert _status([LINT], [_gate("LINT", "PASS", ev)], [ev]) == "PASS"
    stale = _evidence(commit=OLD_COMMIT)
    assert _status([LINT], [_gate("LINT", "PASS", stale, commit=OLD_COMMIT)], [stale]) == "UNVERIFIED"
    pending = _evidence(status="PENDING")
    assert _status([LINT], [_gate("LINT", "PASS", pending)], [pending]) == "UNVERIFIED"
    assert _status([LINT], [_gate("LINT", "PASS", ev)], []) == "UNVERIFIED"  # evidence не найдено
    mismatched = _evidence(commit=OLD_COMMIT)  # отчёт другого SHA (FP-08)
    assert _status([LINT], [_gate("LINT", "PASS", mismatched)], [mismatched]) == "UNVERIFIED"


@pytest.mark.parametrize("status", ["ERROR", "SKIPPED", "PENDING", "RUNNING"])
def test_error_and_skipped_are_not_pass(status: str) -> None:
    ev = _evidence()
    gates = [_gate("LINT", "PASS", ev, name="ruff"), _gate("LINT", status, None, name="eslint")]
    assert _status([LINT], gates, [ev]) == "UNVERIFIED"


def test_failed_gate_fails_control_with_evidence() -> None:
    ok, bad = _evidence(), _evidence()
    gates = [_gate("LINT", "PASS", ok, name="ruff"), _gate("LINT", "FAIL", bad, name="eslint")]
    result = _run([LINT], gates, [ok, bad])
    assert result["QM-25010-01"][0] == "FAIL" and "eslint" in result["QM-25010-01"][1]


def test_missing_gate_and_not_applicable_gate() -> None:
    assert _status([SECRETS]) == "UNVERIFIED"
    assert _status([LINT], [_gate("LINT", "NOT_APPLICABLE", None)]) == "NOT_APPLICABLE"
    ev = _evidence()
    mixed = [_gate("LINT", "PASS", ev, name="ruff"), _gate("LINT", "NOT_APPLICABLE", None, name="eslint")]
    assert _status([LINT], mixed, [ev]) == "PASS"


def _finding(
    severity: str, *, gate: GateRun | None = None, cwe: str | None = None, status: str = "OPEN"
) -> FindingItem:
    return FindingItem(
        uuid7(), severity, status, "rule.x", cwe, gate.gate_type if gate else None, gate.id if gate else None, None
    )


def test_open_findings_above_threshold_fail_control() -> None:
    ev = _evidence()
    gate = _gate("SECRETS", "PASS", ev)
    assert _status([SECRETS], [gate], [ev], [_finding("medium", gate=gate)]) == "PASS"  # ниже порога strict
    assert _status([SECRETS], [gate], [ev], [_finding("high", gate=gate)]) == "FAIL"
    assert _status([SECRETS], [gate], [ev], [_finding("high", gate=gate, status="FIXED")]) == "FAIL"
    for closed in ("VERIFIED_FIXED", "FALSE_POSITIVE", "ACCEPTED_RISK"):
        assert _status([SECRETS], [gate], [ev], [_finding("critical", gate=gate, status=closed)]) == "PASS"
    assert _status([SECRETS], [gate], [ev], [_finding("medium", gate=gate)], profile_block="medium") == "FAIL"
    # Находка без evidence (ни своего, ни у запуска gate) не даёт вердикта FAIL — UNVERIFIED, не PASS.
    orphan = FindingItem(uuid7(), "critical", "OPEN", "r", None, "SECRETS", None, None)
    assert _status([SECRETS], [gate], [ev], [orphan]) == "UNVERIFIED"


def test_cwe_control_uses_cwe_findings_only() -> None:
    ev = _evidence()
    sast = _gate("SAST", "FAIL", ev, name="semgrep")  # gate упал из-за находки другой категории
    other = _finding("critical", gate=sast, cwe="CWE-327")
    assert _status([INJ], [sast], [ev], [other]) == "PASS"
    assert _status([INJ], [sast], [ev], [other, _finding("high", gate=sast, cwe="CWE-89")]) == "FAIL"
    assert _status([INJ], [sast], [ev], [_finding("medium", gate=sast, cwe="CWE-78")]) == "PASS"  # порог high
    assert _status([INJ], [_gate("SAST", "ERROR", None)], [], []) == "UNVERIFIED"


# --- применимость и режим ------------------------------------------------------------------------------


def test_unapproved_or_not_applicable_or_mode() -> None:
    ev = _evidence()
    gates, evidence = [_gate("LINT", "PASS", ev)], [ev]
    draft = {LINT.id: ProjectControl(LINT.id, "applicable", None, None, None)}
    assert _status([LINT], gates, evidence, project=draft) == "UNVERIFIED"
    assert _status([LINT], gates, evidence, project={}) == "UNVERIFIED"
    na = _approved(LINT, applicability="not_applicable", rationale="только документация")
    result = _run([LINT], gates, evidence, project=na)
    assert result["QM-25010-01"] == ("NOT_APPLICABLE", "Неприменим (утверждено): только документация")
    perf = control("QM-25010-05")
    assert _status([perf], mode="DEVELOP") == "NOT_APPLICABLE"
    assert _status([perf], mode="OPTIMIZE") == "UNVERIFIED"


# --- agent_review / manual / document_check ---------------------------------------------------------


def test_agent_review_is_at_most_manual_review() -> None:
    ev = _evidence()
    gate = _gate("REVIEW", "PASS", ev)
    assert _status([REVIEW], [gate], [ev]) == "MANUAL_REVIEW"
    human = _evidence(producer="user")
    att = Attestation(REVIEW.id, "PASS", human.id, OWNER, "проверено вручную")
    assert _status([REVIEW], [gate], [ev, human], attestations=[att]) == "PASS"


def test_manual_and_document_checks_need_human() -> None:
    create = {"mode": "CREATE"}  # ARCH-42010-02 относится к режимам CREATE/MODERNIZE
    assert _status([MANUAL], **create) == "MANUAL_REVIEW"
    assert _status([DOC]) == "MANUAL_REVIEW"
    unverified = _evidence(status="UNVERIFIED", producer="agent")
    weak = Attestation(MANUAL.id, "PASS", unverified.id, OWNER)
    assert _status([MANUAL], evidence=[unverified], attestations=[weak], **create) == "MANUAL_REVIEW"
    human = _evidence(producer="user")
    assert _status([MANUAL], evidence=[human], attestations=[replace(weak, evidence_id=human.id)], **create) == "PASS"
    fail = Attestation(MANUAL.id, "FAIL", human.id, OWNER)
    assert _status([MANUAL], evidence=[human], attestations=[fail], **create) == "FAIL"
    ev = _evidence()
    assert _status([DOC], [_gate("POLICY", "FAIL", ev)], [ev]) == "FAIL"  # документная проверка выполнена gate


def test_attestation_does_not_override_automated_gate() -> None:
    ev, human = _evidence(), _evidence(producer="user")
    att = Attestation(LINT.id, "PASS", human.id, OWNER)
    assert _status([LINT], [_gate("LINT", "FAIL", ev)], [ev, human], attestations=[att]) == "FAIL"


# --- трассировка и отчёт --------------------------------------------------------------------------------


def test_traceability_chain_standard_to_verdict() -> None:
    ev = _evidence()
    gate = _gate("SCOPE", "PASS", ev, name="scope")
    scope = control("SEC-27001-01")
    report = evaluate(
        EvaluationInput(
            facts=RunFacts(uuid7(), "DEVELOP", "strict", COMMIT, "high"),
            controls=list(seed_controls()),
            project_controls=_approved(*seed_controls()),
            gates=[gate],
            evidence={ev.id: ev},
            findings=[],
        )
    )
    assert len(report.verdicts) == len(seed_controls())
    verdict = next(v for v in report.verdicts if v.control.id == scope.id)
    trace = verdict.trace()
    assert trace["standard"] == {"code": "ISO/IEC 27001", "edition": "2022"}
    assert trace["control"]["code"] == "SEC-27001-01" and trace["control"]["clause_ref"] == "Annex A 8.4"
    assert trace["applicability"] == {"value": "applicable", "approved_by": str(OWNER)}
    assert trace["agent_role"] == "security"
    assert trace["check"] == {"method": "automated_gate", "gate_type": "SCOPE", "gate_execution_ids": [str(gate.id)]}
    assert trace["evidence"] == {"primary": str(ev.id), "all": [str(ev.id)]}
    assert trace["verdict"]["status"] == "PASS"
    line = verdict.trace_line()
    for part in ("ISO/IEC 27001 2022", "Annex A 8.4 / SEC-27001-01", "applicable", "security", "automated_gate:SCOPE",
                 str(ev.id), "PASS"):  # fmt: skip
        assert part in line
    summary = report.summary()
    assert sum(summary.values()) == len(seed_controls()) and summary["PASS"] == 1
    assert report.disclaimer == DISCLAIMER and "не является сертификацией" in report.render_text()
    assert len(report.trace()) == len(seed_controls())
    # Каждый PASS/FAIL опирается на evidence (CHECK §13.4).
    assert all(v.evidence_id is not None for v in report.verdicts if v.status in ("PASS", "FAIL"))
