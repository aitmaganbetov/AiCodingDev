"""Правила вердикта и evidence (P6-08) и guard завершения run (P6-09); QUALITY_GATES §4.2, §5, ТЗ §6/§14,
AC-11/AC-14.

Чистые правила (unit-тесты без БД):
* `evidence_problems` — полнота evidence процесса (AC-14): commit, команда, exit code, timestamps,
  окружение (runner + версии инструментов), artifact sha256; `VERIFIED` и producer `verification_engine`;
  совпадение sha256 при повторном чтении artifact;
* `gate_problems` — SKIPPED ≠ PASS, ERROR ≠ PASS, NOT_APPLICABLE только с причиной, PASS только с
  полным evidence на том же commit; результат на устаревшем commit задачи не засчитывается;
* `evaluate_completion` — run `COMPLETED` только если каждый обязательный gate профиля имеет
  выполнение, все актуальные обязательные выполнения PASS/NOT_APPLICABLE на финальном commit своей
  задачи, есть хотя бы один PASS, нет открытых блокирующих findings (severity ≥ порога профиля;
  ACCEPTED_RISK выше допустимого профилем уровня тоже блокирует) и нет PENDING approvals.

`run_completion_verdict(conn, run_id, store=…)` загружает данные run и применяет правила; оркестратор
вызывает его в блоке VERIFYING → COMPLETED (engine.tick). DB-guard `trg_runs_completion_guard` (§18.4)
остаётся вторым рубежом.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform.artifacts import ArtifactIntegrityError, ArtifactNotFoundError, ArtifactStore
from aicd_verification.findings import SEVERITY_RANK, is_blocking
from aicd_verification.profiles import level, thresholds

OK_STATUSES = frozenset({"PASS", "NOT_APPLICABLE"})
PROCESS_EVIDENCE_KINDS = frozenset({"command_result", "test_report", "scan_report", "benchmark"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class EvidenceRecord:
    kind: str
    producer: str
    verification_status: str
    commit_sha: str | None
    command: str | None
    exit_code: int | None
    started_at: datetime | None
    finished_at: datetime | None
    environment: Mapping[str, Any]
    artifact_sha256: str | None
    storage_ref: str | None = None


def evidence_problems(
    ev: EvidenceRecord | None, *, expected_commit: str, reread_sha256: str | None = None
) -> list[str]:
    """Почему evidence не может подтверждать PASS (пустой список — evidence полон и VERIFIED)."""
    if ev is None:
        return ["evidence is missing"]
    problems: list[str] = []
    if ev.verification_status != "VERIFIED":
        problems.append(f"evidence is {ev.verification_status}, not VERIFIED")
    if ev.producer != "verification_engine":
        problems.append(f"evidence producer is {ev.producer}, not verification_engine")
    if not ev.commit_sha:
        problems.append("evidence has no commit_sha")
    elif ev.commit_sha.lower() != expected_commit.lower():
        problems.append("evidence commit differs from the gate commit")
    if not (ev.command and ev.command.strip()):
        problems.append("evidence has no command")
    if ev.exit_code is None:
        problems.append("evidence has no exit code")
    if ev.started_at is None or ev.finished_at is None:
        problems.append("evidence has no start/finish timestamps")
    elif ev.finished_at < ev.started_at:
        problems.append("evidence finished before it started")
    env = ev.environment or {}
    if not env.get("runner") or not env.get("tool_versions"):
        problems.append("evidence environment lacks runner/tool versions")
    if env.get("runner") == "docker" and not env.get("image"):
        problems.append("evidence environment lacks the container image")
    if not (ev.artifact_sha256 and _SHA256_RE.match(ev.artifact_sha256)):
        problems.append("evidence artifact has no sha256")
    elif reread_sha256 is not None and reread_sha256 != ev.artifact_sha256:
        problems.append("evidence artifact sha256 mismatch on re-read")
    return problems


@dataclass(frozen=True)
class GateRecord:
    execution_id: uuid.UUID
    task_id: uuid.UUID | None
    gate_name: str
    gate_type: str
    required: bool
    status: str
    commit_sha: str
    not_applicable_reason: str | None = None
    error_message: str | None = None
    evidence: EvidenceRecord | None = None
    created_at: datetime | None = None


def gate_problems(gate: GateRecord, *, final_commit: str | None, reread_sha256: str | None = None) -> list[str]:
    """Нарушения правил вердикта для одного актуального выполнения gate."""
    label = f"{gate.gate_name}[{gate.gate_type}]"
    if gate.status not in OK_STATUSES:
        detail = f": {gate.error_message[:160]}" if gate.error_message else ""
        return [f"{label} is {gate.status}{detail}"]  # FAIL/ERROR/SKIPPED/PENDING/RUNNING ≠ PASS
    problems: list[str] = []
    if final_commit is not None and gate.commit_sha.lower() != final_commit.lower():
        problems.append(
            f"{label} result is for commit {gate.commit_sha[:12]}, not the final commit {final_commit[:12]}"
        )
    if gate.status == "NOT_APPLICABLE":
        if not (gate.not_applicable_reason and gate.not_applicable_reason.strip()):
            problems.append(f"{label} is NOT_APPLICABLE without a reason")
        return problems
    problems.extend(
        f"{label}: {p}"
        for p in evidence_problems(gate.evidence, expected_commit=gate.commit_sha, reread_sha256=reread_sha256)
    )
    return problems


@dataclass(frozen=True)
class FindingRecord:
    severity: str
    status: str
    rule_id: str
    gate_name: str | None = None


@dataclass(frozen=True)
class CompletionVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()
    checked_gates: int = 0

    def summary(self) -> str:
        return "ok" if self.ok else "; ".join(self.reasons)[:2000]


def final_commits(gates: Iterable[GateRecord]) -> dict[uuid.UUID | None, str]:
    """Финальный commit задачи — commit её последнего (по времени создания) актуального выполнения."""
    latest: dict[uuid.UUID | None, GateRecord] = {}
    for g in gates:
        cur = latest.get(g.task_id)
        if cur is None or (g.created_at, str(g.execution_id)) > (cur.created_at, str(cur.execution_id)):
            latest[g.task_id] = g
    return {task: g.commit_sha for task, g in latest.items()}


def evaluate_completion(
    *,
    profile: str,
    required_gate_names: Iterable[str],
    gates: Sequence[GateRecord],
    findings: Iterable[FindingRecord] = (),
    pending_approvals: int = 0,
    reread: Mapping[uuid.UUID, str | None] | None = None,
    profile_definition: Mapping[str, Any] | None = None,
) -> CompletionVerdict:
    reasons: list[str] = []
    finals = final_commits(gates)
    present = {g.gate_name for g in gates}
    for name in sorted(set(required_gate_names) - present):
        reasons.append(f"required gate {name} has no execution in this run")
    for g in gates:
        if not g.required:
            continue
        reasons.extend(
            gate_problems(g, final_commit=finals.get(g.task_id), reread_sha256=(reread or {}).get(g.execution_id))
        )
    if not any(g.status == "PASS" for g in gates):
        reasons.append("no gate execution has PASS (empty verification)")
    limits = thresholds(profile, profile_definition)
    for f in findings:
        if is_blocking(f.status, f.severity, limits.finding_block_severity):
            reasons.append(f"open {f.severity} finding {f.rule_id} ({f.status})")
        elif (
            f.status == "ACCEPTED_RISK"
            and SEVERITY_RANK.get(f.severity, 0) > SEVERITY_RANK[limits.accept_risk_max_severity]
        ):
            reasons.append(f"{f.severity} finding {f.rule_id} cannot be accepted as risk in profile {profile}")
    if pending_approvals:
        reasons.append(f"{pending_approvals} approval(s) still PENDING")
    return CompletionVerdict(not reasons, tuple(reasons), len(gates))


# --- Загрузка из БД ------------------------------------------------------------------------------------


@dataclass
class _Loaded:
    profile: str
    definition: Mapping[str, Any]
    required: list[str] = field(default_factory=list)
    gates: list[GateRecord] = field(default_factory=list)
    findings: list[FindingRecord] = field(default_factory=list)
    pending: int = 0


async def _load(conn: AsyncConnection, run_id: uuid.UUID) -> _Loaded | None:
    run = (
        (
            await conn.execute(
                text(
                    "SELECT r.quality_profile, r.quality_profile_id, p.definition FROM runs r"
                    " LEFT JOIN quality_profiles p ON p.id = r.quality_profile_id WHERE r.id = :r"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .first()
    )
    if run is None:
        return None
    data = _Loaded(str(run["quality_profile"]), dict(run["definition"] or {}))
    for row in (
        (
            await conn.execute(
                text("SELECT name, gate_type, required FROM quality_gates WHERE profile_id = :p ORDER BY name"),
                {"p": run["quality_profile_id"]},
            )
        )
        .mappings()
        .all()
    ):
        # Та же эффективная обязательность, что в плане (profiles.plan_gates): R/R* или snapshot строки.
        if row["gate_type"] == "SCOPE" or row["required"] or level(data.profile, row["gate_type"]) in ("R", "R*"):
            data.required.append(str(row["name"]))
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT g.id, g.task_id, q.name, g.gate_type, g.required, g.status, g.commit_sha,"
                    " g.not_applicable_reason, g.error_message, g.created_at,"
                    " e.id AS ev_id, e.kind, e.producer, e.verification_status, e.commit_sha AS ev_commit, e.command,"
                    " e.exit_code, e.started_at, e.finished_at, e.environment, a.sha256, a.storage_ref"
                    " FROM gate_executions g JOIN quality_gates q ON q.id = g.gate_id"
                    " LEFT JOIN tasks t ON t.workspace_id = g.workspace_id AND t.id = g.task_id"
                    " LEFT JOIN evidence e ON e.workspace_id = g.workspace_id AND e.id = g.evidence_id"
                    " LEFT JOIN artifacts a ON a.workspace_id = e.workspace_id AND a.id = e.artifact_id"
                    " WHERE g.run_id = :r AND g.superseded_at IS NULL"
                    " AND (g.task_id IS NULL OR t.superseded_at IS NULL) ORDER BY g.created_at, g.id"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .all()
    )
    for r in rows:
        ev = None
        if r["ev_id"] is not None:
            ev = EvidenceRecord(
                r["kind"],
                r["producer"],
                r["verification_status"],
                r["ev_commit"],
                r["command"],
                r["exit_code"],
                r["started_at"],
                r["finished_at"],
                dict(r["environment"] or {}),
                r["sha256"],
                r["storage_ref"],
            )
        data.gates.append(
            GateRecord(
                r["id"],
                r["task_id"],
                str(r["name"]),
                str(r["gate_type"]),
                bool(r["required"]),
                str(r["status"]),
                str(r["commit_sha"]),
                r["not_applicable_reason"],
                r["error_message"],
                ev,
                r["created_at"],
            )
        )
    for f in (
        (
            await conn.execute(
                text(
                    "SELECT f.severity, f.status, f.rule_id, q.name FROM findings f"
                    " LEFT JOIN quality_gates q ON q.id = f.source_gate_id"
                    " WHERE f.run_id = :r AND f.status IN ('OPEN','ACKNOWLEDGED','FIXED','ACCEPTED_RISK')"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .all()
    ):
        data.findings.append(FindingRecord(str(f["severity"]), str(f["status"]), str(f["rule_id"]), f["name"]))
    data.pending = int(
        (
            await conn.execute(
                text("SELECT count(*) FROM approvals WHERE run_id = :r AND status = 'PENDING'"), {"r": run_id}
            )
        ).scalar_one()
    )
    return data


async def _reread(store: ArtifactStore, gates: Sequence[GateRecord]) -> dict[uuid.UUID, str | None]:
    """sha256 артефактов evidence при повторном чтении (FP-14); ошибка чтения → «нет хеша»."""

    def digest(ref: str) -> str | None:
        h = hashlib.sha256()
        try:
            for chunk in store.open(ref):
                h.update(chunk)
        except (ArtifactIntegrityError, ArtifactNotFoundError, OSError):
            return "unreadable"
        return h.hexdigest()

    out: dict[uuid.UUID, str | None] = {}
    for g in gates:
        if g.status == "PASS" and g.required and g.evidence is not None and g.evidence.storage_ref:
            out[g.execution_id] = await asyncio.to_thread(digest, g.evidence.storage_ref)
    return out


async def run_completion_verdict(
    conn: AsyncConnection, run_id: uuid.UUID, *, store: ArtifactStore | None = None
) -> CompletionVerdict:
    """Можно ли перевести run в COMPLETED (§5.3). `store` — повторная сверка sha256 артефактов evidence."""
    data = await _load(conn, run_id)
    if data is None:
        return CompletionVerdict(False, ("run not found",))
    reread = await _reread(store, data.gates) if store is not None else None
    return evaluate_completion(
        profile=data.profile,
        required_gate_names=data.required,
        gates=data.gates,
        findings=data.findings,
        pending_approvals=data.pending,
        reread=reread,
        profile_definition=data.definition,
    )
