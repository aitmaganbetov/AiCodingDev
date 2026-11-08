"""Оценка соответствия run (Compliance Engine; ТЗ §13, QUALITY_GATES §8.2, DATABASE_DESIGN §13.4).

Чистые функции: по реестру контролей, утверждённой применимости проекта и результатам верификации run
(gate executions, evidence, findings, вердикты людей) — вердикт по каждому контролю с трассировкой
`standard → edition → clause/control → applicability → agent role → check → evidence → verdict`.

Правила (QUALITY_GATES §8.2):
* применимость не утверждена человеком → `UNVERIFIED`; `not_applicable` (утверждено) → `NOT_APPLICABLE`;
  контроль не относится к режиму run → `NOT_APPLICABLE` (с пояснением);
* `automated_gate`: `PASS` только при `PASS` gate связанного типа на commit run с `VERIFIED` evidence и без
  блокирующих находок; `FAIL` — gate `FAIL` или открытые находки не ниже порога; `ERROR`/`SKIPPED`/
  незавершённый/отсутствующий gate → `UNVERIFIED` (ERROR ≠ PASS, SKIPPED ≠ PASS);
* контроль с CWE (`ControlRule.cwe_ids`) оценивается по находкам этих CWE: прогон анализатора на commit
  run с `VERIFIED` evidence и без таких находок → `PASS`;
* `agent_review`: анализ агента максимум `MANUAL_REVIEW` до подтверждения человеком;
* `manual`: `MANUAL_REVIEW`, пока человек не записал вердикт; `document_check`: по gate своего типа,
  если он выполнялся, иначе `MANUAL_REVIEW`; вердикт человека (`Attestation`) учитывается для
  `manual`/`document_check`/`agent_review`, но не перекрывает автоматический gate.
`PASS` и `FAIL` всегда опираются на evidence (CHECK §13.4); без evidence вердикт не выносится.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aicd_standards.applicability import relevant_for_mode
from aicd_standards.catalog import rule_for
from aicd_standards.model import (
    DISCLAIMER,
    OPEN_FINDING_STATUSES,
    Attestation,
    ComplianceStatus,
    Control,
    ControlRule,
    EvidenceItem,
    FindingItem,
    GateRun,
    ProjectControl,
    RunFacts,
    severity_rank,
)

_UNFINISHED = frozenset({"PENDING", "RUNNING"})
_INCONCLUSIVE = frozenset({"ERROR", "SKIPPED"}) | _UNFINISHED


@dataclass(frozen=True)
class ControlVerdict:
    """Вердикт по контролю и цепочка трассировки (ТЗ §13)."""

    control: Control
    applicability: str  # applicable / not_applicable / tailored / undecided (черновик или нет строки)
    applicability_approved_by: uuid.UUID | None
    agent_role: str | None
    check: str  # метод проверки и тип gate, например "automated_gate:SAST"
    status: ComplianceStatus
    notes: str
    evidence_id: uuid.UUID | None = None
    gate_execution_id: uuid.UUID | None = None
    reviewed_by: uuid.UUID | None = None
    gate_execution_ids: tuple[uuid.UUID, ...] = ()
    evidence_ids: tuple[uuid.UUID, ...] = ()
    finding_ids: tuple[uuid.UUID, ...] = ()

    def trace(self) -> dict[str, Any]:
        """Цепочка standard → … → verdict для отчёта (`GET /runs/{id}/compliance`, `/report`)."""
        c = self.control
        return {
            "standard": {"code": c.standard_code, "edition": c.edition},
            "control": {
                "id": str(c.id),
                "code": c.control_code,
                "clause_ref": c.clause_ref,
                "description": c.description,
            },
            "applicability": {
                "value": self.applicability,
                "approved_by": str(self.applicability_approved_by) if self.applicability_approved_by else None,
            },
            "agent_role": self.agent_role,
            "check": {
                "method": c.verification_method,
                "gate_type": c.default_gate_type,
                "gate_execution_ids": [str(i) for i in self.gate_execution_ids],
            },
            "evidence": {
                "primary": str(self.evidence_id) if self.evidence_id else None,
                "all": [str(i) for i in self.evidence_ids],
            },
            "findings": [str(i) for i in self.finding_ids],
            "verdict": {
                "status": self.status,
                "notes": self.notes,
                "reviewed_by": str(self.reviewed_by) if self.reviewed_by else None,
            },
        }

    def trace_line(self) -> str:
        c = self.control
        evidence = str(self.evidence_id) if self.evidence_id else "—"
        return (
            f"{c.standard_code} {c.edition} → {c.clause_ref} / {c.control_code} → {self.applicability}"
            f" → {self.agent_role or '—'} → {self.check} → evidence {evidence} → {self.status}"
        )


@dataclass(frozen=True)
class ComplianceReport:
    run_id: uuid.UUID
    commit_sha: str | None
    verdicts: tuple[ControlVerdict, ...]
    disclaimer: str = field(default=DISCLAIMER)

    def summary(self) -> dict[str, int]:
        counts = Counter(v.status for v in self.verdicts)
        return {s: counts.get(s, 0) for s in ("PASS", "FAIL", "NOT_APPLICABLE", "UNVERIFIED", "MANUAL_REVIEW")}

    def trace(self) -> list[dict[str, Any]]:
        return [v.trace() for v in self.verdicts]

    def render_text(self) -> str:
        lines = [f"Compliance run {self.run_id} @ {self.commit_sha or 'нет commit верификации'}", self.disclaimer]
        lines.extend(v.trace_line() for v in self.verdicts)
        return "\n".join(lines)


@dataclass(frozen=True)
class EvaluationInput:
    facts: RunFacts
    controls: Sequence[Control]
    project_controls: Mapping[uuid.UUID, ProjectControl]
    gates: Sequence[GateRun]
    evidence: Mapping[uuid.UUID, EvidenceItem]
    findings: Sequence[FindingItem]
    attestations: Mapping[uuid.UUID, Attestation] = field(default_factory=dict)


@dataclass(frozen=True)
class _Outcome:
    status: ComplianceStatus
    notes: str
    evidence_id: uuid.UUID | None = None
    gate_execution_id: uuid.UUID | None = None
    reviewed_by: uuid.UUID | None = None
    gate_ids: tuple[uuid.UUID, ...] = ()
    finding_ids: tuple[uuid.UUID, ...] = ()


def evaluate(inp: EvaluationInput) -> ComplianceReport:
    verdicts = tuple(
        _evaluate_control(control, inp)
        for control in sorted(inp.controls, key=lambda c: (c.standard_code, c.control_code))
    )
    return ComplianceReport(inp.facts.run_id, inp.facts.commit_sha, verdicts)


def _check_label(control: Control) -> str:
    gate = f":{control.default_gate_type}" if control.default_gate_type else ""
    return f"{control.verification_method}{gate}"


def _evaluate_control(control: Control, inp: EvaluationInput) -> ControlVerdict:
    pc = inp.project_controls.get(control.id)
    approved = pc is not None and pc.approved_by is not None
    applicability = pc.applicability if pc is not None and approved else "undecided"
    role = (pc.agent_role if pc is not None and pc.agent_role else None) or control.default_agent_role
    outcome = _decide(control, pc if approved else None, inp)
    evidence_ids = tuple(
        dict.fromkeys(
            [*([outcome.evidence_id] if outcome.evidence_id else [])]
            + [g.evidence_id for g in inp.gates if g.id in outcome.gate_ids and g.evidence_id]
        )
    )
    return ControlVerdict(
        control=control,
        applicability=applicability,
        applicability_approved_by=pc.approved_by if pc is not None and approved else None,
        agent_role=role,
        check=_check_label(control),
        status=outcome.status,
        notes=outcome.notes[:4000],
        evidence_id=outcome.evidence_id,
        gate_execution_id=outcome.gate_execution_id,
        reviewed_by=outcome.reviewed_by,
        gate_execution_ids=outcome.gate_ids,
        evidence_ids=evidence_ids,
        finding_ids=outcome.finding_ids,
    )


def _decide(control: Control, pc: ProjectControl | None, inp: EvaluationInput) -> _Outcome:
    facts = inp.facts
    if pc is None:
        return _Outcome("UNVERIFIED", "Применимость контроля для проекта не утверждена человеком.")
    if pc.applicability == "not_applicable":
        return _Outcome("NOT_APPLICABLE", f"Неприменим (утверждено): {pc.rationale or 'без обоснования'}")
    if not relevant_for_mode(control.control_code, facts.mode):
        return _Outcome("NOT_APPLICABLE", f"Контроль не относится к режиму run {facts.mode}.")
    method = control.verification_method
    rule = rule_for(control.control_code)
    if method != "automated_gate":
        attested = _from_attestation(inp.attestations.get(control.id), inp.evidence)
        if attested is not None:
            return attested
    if method == "manual":
        return _Outcome("MANUAL_REVIEW", "Требуется проверка и вердикт человека.")
    if method == "agent_review":
        gates = _gates_of(control, inp.gates)
        done = next((g for g in gates if g.evidence_id and g.status in ("PASS", "FAIL")), None)
        return _Outcome(
            "MANUAL_REVIEW",
            "Анализ агента не даёт PASS без подтверждения человеком.",
            evidence_id=done.evidence_id if done else None,
            gate_execution_id=done.id if done else None,
            gate_ids=tuple(g.id for g in gates),
        )
    if method == "document_check" and not _gates_of(control, inp.gates):
        return _Outcome("MANUAL_REVIEW", "Проверка документа не выполнялась автоматически — нужен вердикт человека.")
    return _from_gates(control, rule, inp)


def _from_attestation(att: Attestation | None, evidence: Mapping[uuid.UUID, EvidenceItem]) -> _Outcome | None:
    if att is None or att.status not in ("PASS", "FAIL") or att.evidence_id is None:
        return None
    ev = evidence.get(att.evidence_id)
    if att.status == "PASS" and (ev is None or ev.verification_status != "VERIFIED"):
        return None
    return _Outcome(
        att.status,  # type: ignore[arg-type]  # PASS/FAIL — проверено выше
        att.notes or "Вердикт человека.",
        evidence_id=att.evidence_id,
        reviewed_by=att.reviewed_by,
    )


def _gates_of(control: Control, gates: Sequence[GateRun]) -> list[GateRun]:
    return sorted((g for g in gates if g.gate_type == control.default_gate_type), key=lambda g: (g.gate_name, g.id))


def _blocking_findings(control: Control, rule: ControlRule, inp: EvaluationInput) -> list[FindingItem]:
    threshold = severity_rank(rule.min_severity or inp.facts.block_severity)
    result: list[FindingItem] = []
    for f in inp.findings:
        if f.status not in OPEN_FINDING_STATUSES or severity_rank(f.severity) < threshold:
            continue
        related = (f.cwe_id in rule.cwe_ids) if rule.cwe_ids else (f.gate_type == control.default_gate_type)
        if related:
            result.append(f)
    return sorted(result, key=lambda f: (-severity_rank(f.severity), str(f.id)))


def _verified_on_commit(g: GateRun, inp: EvaluationInput) -> bool:
    ev = inp.evidence.get(g.evidence_id) if g.evidence_id else None
    if ev is None or ev.verification_status != "VERIFIED":
        return False
    if ev.commit_sha is not None and ev.commit_sha != g.commit_sha:
        return False
    return inp.facts.commit_sha is None or g.commit_sha == inp.facts.commit_sha


def _from_gates(control: Control, rule: ControlRule, inp: EvaluationInput) -> _Outcome:
    gate_type = control.default_gate_type
    gates = _gates_of(control, inp.gates)
    gate_ids = tuple(g.id for g in gates)
    blocking = _blocking_findings(control, rule, inp)
    finding_ids = tuple(f.id for f in blocking)
    if blocking:
        by_id = {g.id: g for g in inp.gates}
        for f in blocking:
            source = by_id.get(f.gate_execution_id) if f.gate_execution_id else None
            evidence_id = f.evidence_id or (source.evidence_id if source else None)
            if evidence_id is not None:
                worst = blocking[0]
                return _Outcome(
                    "FAIL",
                    f"Открытые находки не ниже порога: {len(blocking)} (худшая {worst.severity}: {worst.rule_id}).",
                    evidence_id=evidence_id,
                    gate_execution_id=source.id if source else None,
                    gate_ids=gate_ids,
                    finding_ids=finding_ids,
                )
        return _Outcome(
            "UNVERIFIED",
            f"Открытые находки не ниже порога ({len(blocking)}) без evidence — нужен повторный прогон.",
            gate_ids=gate_ids,
            finding_ids=finding_ids,
        )
    if not gates:
        return _Outcome(
            "UNVERIFIED", f"Gate {gate_type} в run не выполнялся.", gate_ids=gate_ids, finding_ids=finding_ids
        )
    if not rule.cwe_ids:
        failed = next((g for g in gates if g.status == "FAIL" and g.evidence_id is not None), None)
        if failed is not None:
            return _Outcome(
                "FAIL",
                f"Gate {failed.gate_name} ({gate_type}) — FAIL.",
                evidence_id=failed.evidence_id,
                gate_execution_id=failed.id,
                gate_ids=gate_ids,
                finding_ids=finding_ids,
            )
    inconclusive = [g for g in gates if g.status in _INCONCLUSIVE]
    if inconclusive:
        listed = ", ".join(f"{g.gate_name}={g.status}" for g in inconclusive)
        return _Outcome(
            "UNVERIFIED", f"Проверка не завершена успешно: {listed}.", gate_ids=gate_ids, finding_ids=finding_ids
        )
    if all(g.status == "NOT_APPLICABLE" for g in gates):
        reasons = "; ".join(f"{g.gate_name}: {g.not_applicable_reason or 'неприменим'}" for g in gates)
        return _Outcome("NOT_APPLICABLE", f"Gate {gate_type} неприменим к коду run ({reasons}).", gate_ids=gate_ids)
    completed = [g for g in gates if g.status in ("PASS", "FAIL") and _verified_on_commit(g, inp)]
    if rule.cwe_ids:
        if completed:
            return _Outcome(
                "PASS",
                f"Анализатор {completed[0].gate_name} выполнен на commit run; находок "
                f"{', '.join(sorted(rule.cwe_ids))} не ниже порога нет.",
                evidence_id=completed[0].evidence_id,
                gate_execution_id=completed[0].id,
                gate_ids=gate_ids,
            )
    else:
        passed = [g for g in completed if g.status == "PASS"]
        if passed and len(passed) == sum(1 for g in gates if g.status != "NOT_APPLICABLE"):
            return _Outcome(
                "PASS",
                f"Gate {', '.join(g.gate_name for g in passed)} ({gate_type}) — PASS на commit run.",
                evidence_id=passed[0].evidence_id,
                gate_execution_id=passed[0].id,
                gate_ids=gate_ids,
            )
    return _Outcome(
        "UNVERIFIED",
        f"Нет результата {gate_type} с VERIFIED evidence на commit run {inp.facts.commit_sha or '—'}.",
        gate_ids=gate_ids,
        finding_ids=finding_ids,
    )
