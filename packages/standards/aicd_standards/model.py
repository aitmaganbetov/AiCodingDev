"""Модель реестра стандартов и входов оценки соответствия (DATABASE_DESIGN §13, QUALITY_GATES §8)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

ComplianceStatus = Literal["PASS", "FAIL", "NOT_APPLICABLE", "UNVERIFIED", "MANUAL_REVIEW"]
VerificationMethod = Literal["automated_gate", "agent_review", "manual", "document_check"]
Applicability = Literal["applicable", "not_applicable", "tailored"]

SEVERITIES: tuple[str, ...] = ("info", "low", "medium", "high", "critical")
OPEN_FINDING_STATUSES = frozenset({"OPEN", "ACKNOWLEDGED", "FIXED"})  # FIXED без VERIFIED_FIXED не закрыта (§5.3)
# Порог блокирующих находок по профилю (QUALITY_GATES §5.3 п.3), если definition профиля его не задаёт.
PROFILE_BLOCK_SEVERITY: dict[str, str] = {"basic": "critical", "strict": "high", "critical": "medium"}
RUN_MODES = frozenset(
    {
        "CREATE",
        "DEVELOP",
        "DEBUG",
        "AUDIT",
        "MODERNIZE",
        "SECURITY",
        "OPTIMIZE",
        "TEST",
        "MAINTAIN",
        "DOCUMENT",
        "REVIEW",
    }
)

DISCLAIMER = (
    "Отчёт Standards & Compliance — внутренний инструмент трассировки платформы. Он не является "
    "сертификацией, аудитом соответствия или заключением третьей стороны (ТЗ §13). Формулировки "
    "контролей — собственные формулировки платформы со ссылками на разделы стандартов."
)


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else -1


@dataclass(frozen=True)
class Control:
    """Строка `standard_controls` вместе с кодом/редакцией стандарта."""

    id: uuid.UUID
    standard_code: str
    edition: str
    clause_ref: str
    control_code: str
    description: str
    verification_method: VerificationMethod
    default_agent_role: str | None
    default_gate_type: str | None


@dataclass(frozen=True)
class ProjectControl:
    """Строка `project_controls`; учитывается только утверждённая человеком (`approved_by`)."""

    control_id: uuid.UUID
    applicability: Applicability
    rationale: str | None
    agent_role: str | None
    approved_by: uuid.UUID | None


@dataclass(frozen=True)
class ControlRule:
    """Правило платформы для контроля: применимость по тегам стека и режимам run, связь с находками.

    `requires_any` пуст — контроль применим к любому проекту; `modes is None` — к любому режиму.
    `cwe_ids` — контроль оценивается по находкам этих CWE (а не по любой находке gate);
    `min_severity` — порог блокирующих находок вместо порога профиля.
    """

    requires_any: frozenset[str] = frozenset()
    modes: frozenset[str] | None = None
    cwe_ids: frozenset[str] = frozenset()
    min_severity: str | None = None


@dataclass(frozen=True)
class GateRun:
    """Актуальная (`superseded_at IS NULL`) строка `gate_executions` run."""

    id: uuid.UUID
    gate_type: str
    gate_name: str
    required: bool
    status: str
    commit_sha: str
    evidence_id: uuid.UUID | None
    not_applicable_reason: str | None = None
    task_id: uuid.UUID | None = None


@dataclass(frozen=True)
class EvidenceItem:
    id: uuid.UUID
    kind: str
    producer: str
    verification_status: str
    commit_sha: str | None
    artifact_id: uuid.UUID | None = None


@dataclass(frozen=True)
class FindingItem:
    id: uuid.UUID
    severity: str
    status: str
    rule_id: str
    cwe_id: str | None
    gate_type: str | None  # тип gate-источника (None — ревью/ручная/GitHub)
    gate_execution_id: uuid.UUID | None
    evidence_id: uuid.UUID | None
    control_refs: tuple[str, ...] = ()  # findings.control_refs (миграция 0010)


@dataclass(frozen=True)
class Attestation:
    """Вердикт человека по контролю в этом run (актуальная строка `compliance_results` с `reviewed_by`)."""

    control_id: uuid.UUID
    status: str
    evidence_id: uuid.UUID | None
    reviewed_by: uuid.UUID
    notes: str | None = None


@dataclass(frozen=True)
class RunFacts:
    run_id: uuid.UUID
    mode: str
    quality_profile: str
    commit_sha: str | None  # commit, на котором оценивается run (последняя верификация)
    block_severity: str
