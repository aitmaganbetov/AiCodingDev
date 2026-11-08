"""Доступ к БД модуля standards (миграция 0008, DATABASE_DESIGN §13; RLS по workspace).

* `standards`/`standard_controls` — G-справочник (только чтение);
* `project_controls` — черновики применимости пишет система (`propose_project_controls`, роль worker
  или app), утверждает человек (`approve_project_control`, роль app с `app.user_id`) — триггер БД
  не даёт системе утвердить или изменить утверждённую строку;
* `compliance_results` — новая строка на изменившийся вердикт, прежняя получает `superseded_at`;
  evidence вердикта связывается с ним через `evidence_links` (трассировка ТЗ §13).
Результаты верификации (`runs`, `gate_executions`, `evidence`, `findings`, `quality_*`) только читаются.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform.ids import uuid7
from aicd_standards.applicability import ApplicabilityProposal, StackFacts, propose
from aicd_standards.compliance import ComplianceReport, ControlVerdict, EvaluationInput, evaluate
from aicd_standards.model import (
    PROFILE_BLOCK_SEVERITY,
    SEVERITIES,
    Attestation,
    Control,
    EvidenceItem,
    FindingItem,
    GateRun,
    ProjectControl,
    RunFacts,
)


class RunNotFoundError(LookupError):
    """Run не найден в текущем workspace (RLS)."""


async def load_controls(conn: AsyncConnection) -> list[Control]:
    rows = (
        await conn.execute(
            text(
                "SELECT c.id, s.code, s.edition, c.clause_ref, c.control_code, c.description, c.verification_method,"
                " c.default_agent_role, c.default_gate_type"
                " FROM standard_controls c JOIN standards s ON s.id = c.standard_id"
                " WHERE c.active AND s.active ORDER BY s.code, c.control_code"
            )
        )
    ).all()
    return [Control(*r) for r in rows]


async def load_project_controls(conn: AsyncConnection, project_id: uuid.UUID) -> dict[uuid.UUID, ProjectControl]:
    rows = (
        await conn.execute(
            text(
                "SELECT control_id, applicability, rationale, agent_role, approved_by FROM project_controls"
                " WHERE project_id = :p"
            ),
            {"p": project_id},
        )
    ).all()
    return {r[0]: ProjectControl(*r) for r in rows}


async def load_stack(conn: AsyncConnection, project_id: uuid.UUID) -> StackFacts:
    rows = (
        await conn.execute(
            text(
                "SELECT t.ecosystem, t.name FROM project_technologies pt"
                " JOIN technology_versions tv ON tv.id = pt.technology_version_id"
                " JOIN technologies t ON t.id = tv.technology_id WHERE pt.project_id = :p"
            ),
            {"p": project_id},
        )
    ).all()
    return StackFacts.of((r[0], r[1]) for r in rows)


async def propose_project_controls(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> list[ApplicabilityProposal]:
    """Черновики применимости по стеку проекта; утверждённые человеком строки не меняются."""
    proposals = propose(await load_controls(conn), await load_stack(conn, project_id))
    for p in proposals:
        await conn.execute(
            text(
                "INSERT INTO project_controls (workspace_id, project_id, control_id, applicability, rationale,"
                " agent_role) VALUES (:ws, :p, :c, :a, :r, :role)"
                " ON CONFLICT ON CONSTRAINT pk_project_controls DO UPDATE"
                " SET applicability = EXCLUDED.applicability, rationale = EXCLUDED.rationale,"
                " agent_role = EXCLUDED.agent_role"
                " WHERE project_controls.approved_by IS NULL"
            ),
            {
                "ws": workspace_id,
                "p": project_id,
                "c": p.control_id,
                "a": p.applicability,
                "r": p.rationale,
                "role": p.agent_role,
            },
        )
    return proposals


async def approve_project_control(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    control_id: uuid.UUID,
    user_id: uuid.UUID,
    applicability: str,
    rationale: str | None,
    agent_role: str | None = None,
) -> None:
    """Утверждение применимости человеком (RBAC admin проверяет API; БД требует `app.user_id = user_id`)."""
    await conn.execute(
        text(
            "INSERT INTO project_controls (workspace_id, project_id, control_id, applicability, rationale, agent_role,"
            " approved_by, approved_at) VALUES (:ws, :p, :c, :a, :r, :role, :u, now())"
            " ON CONFLICT ON CONSTRAINT pk_project_controls DO UPDATE"
            " SET applicability = EXCLUDED.applicability, rationale = EXCLUDED.rationale,"
            " agent_role = coalesce(EXCLUDED.agent_role, project_controls.agent_role),"
            " approved_by = EXCLUDED.approved_by, approved_at = EXCLUDED.approved_at"
        ),
        {
            "ws": workspace_id,
            "p": project_id,
            "c": control_id,
            "a": applicability,
            "r": rationale,
            "role": agent_role,
            "u": user_id,
        },
    )


async def load_run_input(
    conn: AsyncConnection, run_id: uuid.UUID, *, commit_sha: str | None = None
) -> tuple[uuid.UUID, uuid.UUID, EvaluationInput]:
    """Входы оценки run: (workspace_id, project_id, EvaluationInput). Commit по умолчанию — последний
    проверенный (самая поздняя актуальная строка gate_executions run)."""
    run = (
        (
            await conn.execute(
                text(
                    "SELECT r.workspace_id, r.project_id, r.mode, r.quality_profile,"
                    " qp.definition->>'finding_block_severity' AS block_severity"
                    " FROM runs r LEFT JOIN quality_profiles qp ON qp.id = r.quality_profile_id WHERE r.id = :id"
                ),
                {"id": run_id},
            )
        )
        .mappings()
        .first()
    )
    if run is None:
        raise RunNotFoundError(str(run_id))
    gate_rows = (
        await conn.execute(
            text(
                "SELECT g.id, g.gate_type, coalesce(q.name, g.gate_type) AS name, g.required, g.status, g.commit_sha,"
                " g.evidence_id, g.not_applicable_reason, g.task_id, g.created_at"
                " FROM gate_executions g LEFT JOIN quality_gates q ON q.id = g.gate_id"
                " WHERE g.run_id = :r AND g.superseded_at IS NULL ORDER BY g.created_at, g.id"
            ),
            {"r": run_id},
        )
    ).all()
    gates = [GateRun(*r[:9]) for r in gate_rows]
    if commit_sha is None and gate_rows:
        commit_sha = gate_rows[-1][5]
    evidence = {
        r[0]: EvidenceItem(*r)
        for r in (
            await conn.execute(
                text(
                    "SELECT id, kind, producer, verification_status, commit_sha, artifact_id FROM evidence"
                    " WHERE run_id = :r"
                ),
                {"r": run_id},
            )
        ).all()
    }
    findings = [
        FindingItem(*r)
        for r in (
            await conn.execute(
                text(
                    "SELECT f.id, f.severity, f.status, f.rule_id, f.cwe_id, coalesce(g.gate_type, q.gate_type),"
                    " f.gate_execution_id, f.evidence_id"
                    " FROM findings f LEFT JOIN gate_executions g ON g.id = f.gate_execution_id"
                    " LEFT JOIN quality_gates q ON q.id = f.source_gate_id WHERE f.run_id = :r"
                ),
                {"r": run_id},
            )
        ).all()
    ]
    attestations = {
        r[0]: Attestation(*r)
        for r in (
            await conn.execute(
                text(
                    "SELECT control_id, status, evidence_id, reviewed_by, notes FROM compliance_results"
                    " WHERE run_id = :r AND superseded_at IS NULL AND reviewed_by IS NOT NULL"
                ),
                {"r": run_id},
            )
        ).all()
    }
    block = run["block_severity"]
    if block not in SEVERITIES:
        block = PROFILE_BLOCK_SEVERITY.get(run["quality_profile"], "high")
    facts = RunFacts(run_id, run["mode"], run["quality_profile"], commit_sha, block)
    inp = EvaluationInput(
        facts=facts,
        controls=await load_controls(conn),
        project_controls=await load_project_controls(conn, run["project_id"]),
        gates=gates,
        evidence=evidence,
        findings=findings,
        attestations=attestations,
    )
    return run["workspace_id"], run["project_id"], inp


def _same(row: Any, v: ControlVerdict) -> bool:
    return (row.status, row.evidence_id, row.gate_execution_id, row.reviewed_by, row.notes) == (
        v.status,
        v.evidence_id,
        v.gate_execution_id,
        v.reviewed_by,
        v.notes,
    )


_RELATION = {"PASS": "supports", "FAIL": "refutes"}


async def persist_report(conn: AsyncConnection, workspace_id: uuid.UUID, report: ComplianceReport) -> dict[str, int]:
    """Записывает вердикты (роль worker). Неизменившийся вердикт сохраняет строку; изменившийся —
    новая строка, прежняя получает `superseded_at`. Возвращает {'inserted': n, 'unchanged': m}."""
    current = {
        r.control_id: r
        for r in (
            await conn.execute(
                text(
                    "SELECT id, control_id, status, evidence_id, gate_execution_id, reviewed_by, notes"
                    " FROM compliance_results WHERE run_id = :r AND superseded_at IS NULL FOR UPDATE"
                ),
                {"r": report.run_id},
            )
        ).all()
    }
    inserted = unchanged = 0
    for v in report.verdicts:
        row = current.get(v.control.id)
        if row is not None and _same(row, v):
            unchanged += 1
            continue
        if row is not None:
            await conn.execute(
                text("UPDATE compliance_results SET superseded_at = now() WHERE id = :id"), {"id": row.id}
            )
        rid = uuid7()
        await conn.execute(
            text(
                "INSERT INTO compliance_results (id, workspace_id, run_id, control_id, status, evidence_id,"
                " gate_execution_id, reviewed_by, notes) VALUES (:id, :ws, :r, :c, :s, :e, :g, :rv, :n)"
            ),
            {
                "id": rid,
                "ws": workspace_id,
                "r": report.run_id,
                "c": v.control.id,
                "s": v.status,
                "e": v.evidence_id,
                "g": v.gate_execution_id,
                "rv": v.reviewed_by,
                "n": v.notes,
            },
        )
        if v.evidence_id is not None:
            await conn.execute(
                text(
                    "INSERT INTO evidence_links (id, workspace_id, evidence_id, target_type, target_id, relation)"
                    " VALUES (:id, :ws, :e, 'compliance_result', :t, :rel)"
                ),
                {
                    "id": uuid7(),
                    "ws": workspace_id,
                    "e": v.evidence_id,
                    "t": rid,
                    "rel": _RELATION.get(v.status, "references"),
                },
            )
        inserted += 1
    return {"inserted": inserted, "unchanged": unchanged}


async def evaluate_run(
    conn: AsyncConnection, run_id: uuid.UUID, *, commit_sha: str | None = None, persist: bool = True
) -> ComplianceReport:
    """`compliance.evaluate(run)` (MODULE_BOUNDARIES §3): оценка и (по умолчанию) запись вердиктов."""
    workspace_id, _project_id, inp = await load_run_input(conn, run_id, commit_sha=commit_sha)
    report = evaluate(inp)
    if persist:
        await persist_report(conn, workspace_id, report)
    return report


async def compliance_matrix(conn: AsyncConnection, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Сохранённая матрица run для отчёта: standard → control → applicability → роль → check → evidence →
    verdict (актуальные строки `compliance_results`, с артефактом evidence и sha256)."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT s.code AS standard_code, s.edition, c.clause_ref, c.control_code, c.description,"
                    " c.verification_method, c.default_gate_type, pc.applicability, pc.approved_by,"
                    " coalesce(pc.agent_role, c.default_agent_role) AS agent_role,"
                    " cr.id AS result_id, cr.status, cr.notes, cr.reviewed_by, cr.checked_at,"
                    " cr.gate_execution_id, g.gate_type AS gate_type_executed, g.status AS gate_status,"
                    " g.commit_sha, cr.evidence_id, e.kind AS evidence_kind, e.verification_status,"
                    " a.id AS artifact_id, a.sha256 AS artifact_sha256"
                    " FROM compliance_results cr"
                    " JOIN runs r ON r.id = cr.run_id"
                    " JOIN standard_controls c ON c.id = cr.control_id"
                    " JOIN standards s ON s.id = c.standard_id"
                    " LEFT JOIN project_controls pc ON pc.project_id = r.project_id AND pc.control_id = c.id"
                    " LEFT JOIN gate_executions g ON g.id = cr.gate_execution_id"
                    " LEFT JOIN evidence e ON e.id = cr.evidence_id"
                    " LEFT JOIN artifacts a ON a.workspace_id = e.workspace_id AND a.id = e.artifact_id"
                    " WHERE cr.run_id = :r AND cr.superseded_at IS NULL ORDER BY s.code, c.control_code"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "standard": {"code": r["standard_code"], "edition": r["edition"]},
            "control": {
                "code": r["control_code"],
                "clause_ref": r["clause_ref"],
                "description": r["description"],
            },
            "applicability": {
                "value": r["applicability"] if r["approved_by"] else "undecided",
                "approved_by": r["approved_by"],
            },
            "agent_role": r["agent_role"],
            "check": {
                "method": r["verification_method"],
                "gate_type": r["default_gate_type"],
                "gate_execution_id": r["gate_execution_id"],
                "gate_status": r["gate_status"],
                "commit_sha": r["commit_sha"],
            },
            "evidence": {
                "id": r["evidence_id"],
                "kind": r["evidence_kind"],
                "verification_status": r["verification_status"],
                "artifact_id": r["artifact_id"],
                "sha256": r["artifact_sha256"],
            },
            "verdict": {
                "id": r["result_id"],
                "status": r["status"],
                "notes": r["notes"],
                "reviewed_by": r["reviewed_by"],
                "checked_at": r["checked_at"],
            },
        }
        for r in rows
    ]
