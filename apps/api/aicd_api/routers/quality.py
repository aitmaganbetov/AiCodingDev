"""Качество, compliance, отчёт run и evidence (P6-11; ТЗ §8, §13, §14; FR-QG-07, FR-STD-05, AC-14).

* `GET /runs/{id}/quality` — актуальные выполнения gates (по задаче/commit), агрегированный вердикт по
  обязательным gates, находки по severity и состоянию жизненного цикла;
* `GET /runs/{id}/compliance` — матрица контролей (standard → control → check → evidence → verdict),
  cursor-пагинация, предупреждение «не сертификация» (QUALITY_GATES §8.4);
* `GET /runs/{id}/report` — детерминированный JSON `RunReport` или тот же отчёт в `text/markdown` (Accept);
* `GET /tasks/{id}/evidence`, `GET /evidence/{id}` — evidence с commit, командой, exit code, временем,
  окружением и sha256 артефакта (AC-14).

Только чтение (роль БД aicd_app, RLS по workspace). Ресурс чужого workspace неотличим от отсутствующего —
404 через SECURITY DEFINER резолверы (`sys_resolve_run_workspace`, `sys_resolve_task_workspace`,
`sys_resolve_evidence_workspace`). Свободный текст (команды, окружение, причины, заметки, заголовки)
проходит редактирование секретов на чтении.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import Access, app_state, authorize, current_principal
from aicd_api.report_markdown import render_run_report
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_api.routers.approvals import APPROVALS, approval_view
from aicd_api.routers.runs import resolve_resource, run_view, task_summary
from aicd_identity import api as identity
from aicd_platform.clock import utcnow
from aicd_platform.db import transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_platform.redaction import redact_text, redact_value
from aicd_standards.api import DISCLAIMER, compliance_matrix

router = APIRouter()

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
EvidenceStatus = Literal["PENDING", "VERIFIED", "REJECTED", "UNVERIFIED"]
ComplianceStatus = Literal["PASS", "FAIL", "NOT_APPLICABLE", "UNVERIFIED", "MANUAL_REVIEW"]

SEVERITIES = ("info", "low", "medium", "high", "critical")
FINDING_STATUSES = ("OPEN", "ACKNOWLEDGED", "FIXED", "VERIFIED_FIXED", "FALSE_POSITIVE", "ACCEPTED_RISK")
OPEN_FINDING_STATUSES = ("OPEN", "ACKNOWLEDGED", "FIXED")  # FIXED без VERIFIED_FIXED не закрыта (§5.3)
COMPLIANCE_STATUSES = ("PASS", "FAIL", "NOT_APPLICABLE", "UNVERIFIED", "MANUAL_REVIEW")
QUALITY_FINDINGS_LIMIT = 200  # QualityReport.findings: maxItems
# artifacts.kind → ArtifactKind контракта; виды без пары — `other`.
_ARTIFACT_KINDS = {
    "git_diff", "test_report", "scan_report", "sbom", "context_pack", "cli_output", "other", "coverage_report",
    "gate_log", "applicability_report", "signature",
}  # fmt: skip
_ARTIFACT_KIND_ALIASES = {"cli_raw_log": "raw_cli_log"}
_APPLICABILITY = {"applicable": "APPLICABLE", "tailored": "APPLICABLE", "not_applicable": "NOT_APPLICABLE"}


def aggregate_verdict(statuses: Iterable[str]) -> str:
    """Вердикт run по актуальным выполнениям обязательных gates (QualityReport.verdict, QUALITY_GATES §5).

    Нет выполнений ⇒ PENDING; FAIL или SKIPPED обязательного gate ⇒ FAIL (SKIPPED ≠ PASS); ERROR ⇒ ERROR;
    идущие ⇒ RUNNING/PENDING; PASS — только если все PASS/NOT_APPLICABLE и есть хотя бы один PASS
    («пустая» верификация из одних NOT_APPLICABLE — не PASS). Вызывающий передаёт PASS без VERIFIED
    evidence как ERROR (недоверенный результат)."""
    seen = set(statuses)
    if not seen:
        return "PENDING"
    for status, verdict in (("FAIL", "FAIL"), ("SKIPPED", "FAIL"), ("ERROR", "ERROR"), ("RUNNING", "RUNNING")):
        if status in seen:
            return verdict
    if "PENDING" in seen:
        return "PENDING"
    return "PASS" if "PASS" in seen else "FAIL"


def _opt_text(value: str | None) -> str | None:
    return None if value is None else redact_text(value)


def _artifact_kind(kind: str) -> str:
    kind = _ARTIFACT_KIND_ALIASES.get(kind, kind)
    return kind if kind in _ARTIFACT_KINDS or kind == "raw_cli_log" else "other"


# --- evidence ---------------------------------------------------------------------------------------

_EVIDENCE_SELECT = (
    "SELECT e.id, e.run_id, e.task_id, e.kind, e.producer, e.verification_status::text AS verification_status,"
    " e.commit_sha, e.command, e.exit_code, e.environment, e.started_at, e.finished_at, e.created_at,"
    " a.id AS artifact_id, a.kind AS artifact_kind, a.sha256, a.size_bytes, a.mime_type"
    " FROM evidence e JOIN artifacts a ON a.workspace_id = e.workspace_id AND a.id = e.artifact_id"
)


async def _evidence_links(conn: AsyncConnection, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    if not ids:
        return out
    rows = (
        await conn.execute(
            text(
                "SELECT evidence_id, target_type, target_id, relation FROM evidence_links"
                " WHERE evidence_id = ANY(:ids) ORDER BY created_at, id"
            ),
            {"ids": list(ids)},
        )
    ).all()
    for r in rows:
        out.setdefault(r[0], []).append({"target_type": r[1], "target_id": r[2], "relation": r[3]})
    return out


def evidence_view(r: dict[str, Any], links: list[dict[str, Any]]) -> dict[str, Any]:
    """`Evidence` (AC-14): commit, команда, exit code, время, окружение, sha256 артефакта."""
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "task_id": r["task_id"],
        "kind": r["kind"],
        "producer": r["producer"],
        "verification_status": r["verification_status"],
        "commit_sha": r["commit_sha"],
        "command": _opt_text(r["command"]),
        "exit_code": r["exit_code"],
        "environment": redact_value(dict(r["environment"] or {})),
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "artifact": {
            "id": r["artifact_id"],
            "kind": _artifact_kind(r["artifact_kind"]),
            "sha256": r["sha256"],
            "size_bytes": r["size_bytes"],
            "mime_type": r["mime_type"],
            "download_url": f"/api/v1/artifacts/{r['artifact_id']}/download",
        },
        "links": links,
        "created_at": r["created_at"],
    }


async def _evidence_rows(conn: AsyncConnection, where: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    sql = _EVIDENCE_SELECT + " WHERE " + where + " ORDER BY e.created_at, e.id"
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


async def _evidence_views(conn: AsyncConnection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links = await _evidence_links(conn, [r["id"] for r in rows])
    return [evidence_view(r, links.get(r["id"], [])) for r in rows]


@router.get("/tasks/{task_id}/evidence", tags=["tasks"], **op("listTaskEvidence", "evidence:read"))
async def list_task_evidence(
    request: Request,
    task_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    verification_status: EvidenceStatus | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    """Evidence задачи: собственные (`evidence.task_id`) и связанные с ней через `evidence_links`."""
    access = await resolve_resource(request, principal, "sys_resolve_task_workspace", task_id)
    await authorize(request, access, "evidence:read")
    state = app_state(request)
    filters = {"r": "task_evidence", "t": str(task_id), "vs": verification_status}
    clauses = [
        "(e.task_id = :t OR EXISTS (SELECT 1 FROM evidence_links l WHERE l.evidence_id = e.id"
        " AND l.target_type = 'task' AND l.target_id = :t))"
    ]
    params: dict[str, Any] = {"t": task_id, "lim": limit + 1}
    if verification_status:
        clauses.append("e.verification_status = CAST(:vs AS evidence_verification_status_t)")
        params["vs"] = verification_status
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        clauses.append("(e.created_at, e.id) > (:ct, :cid)")
        params.update(ct=datetime.fromisoformat(t), cid=uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        rows = [
            dict(r)
            for r in (
                await conn.execute(
                    text(
                        _EVIDENCE_SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY e.created_at, e.id LIMIT :lim"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        ]
        page = rows[:limit]
        items = await _evidence_views(conn, page)
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": items, "next_cursor": next_cursor})


@router.get("/evidence/{evidence_id}", tags=["evidence"], **op("getEvidence", "evidence:read"))
async def get_evidence(
    request: Request,
    evidence_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_evidence_workspace", evidence_id)
    await authorize(request, access, "evidence:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        rows = await _evidence_rows(conn, "e.id = :id", {"id": evidence_id})
        if not rows:
            raise ProblemError("not_found")
        (item,) = await _evidence_views(conn, rows)
    return json_response(item)


# --- quality ----------------------------------------------------------------------------------------


def _finding_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "task_id": r["task_id"],
        "gate_execution_id": r["gate_execution_id"],
        "severity": r["severity"],
        "rule_id": redact_text(r["rule_id"]),
        "title": redact_text(r["title"]),
        "cwe_id": r["cwe_id"],
        "file_path": _opt_text(r["file_path"]),
        "line": r["line"],
        "status": r["status"],
        "evidence_id": r["evidence_id"],
    }


def _gate_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "gate_id": r["gate_id"],
        "gate_type": r["gate_type"],
        "required": r["required"],
        "task_id": r["task_id"],
        "status": r["status"],
        "evidence_id": r["evidence_id"],
        "commit_sha": r["commit_sha"],
        "error_code": r["error_code"],
        "not_applicable_reason": _opt_text(r["not_applicable_reason"]),
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
    }


async def _findings(conn: AsyncConnection, run_id: uuid.UUID, limit: int | None) -> list[dict[str, Any]]:
    """Находки run: severity desc, затем по времени (детерминированно)."""
    sql = (
        "SELECT f.id, f.task_id, f.gate_execution_id, f.severity::text AS severity, f.rule_id, f.title, f.cwe_id,"
        " f.file_path, f.line, f.status::text AS status, f.evidence_id FROM findings f WHERE f.run_id = :r"
        " ORDER BY array_position(ARRAY['critical','high','medium','low','info'], f.severity::text), f.created_at, f.id"
    )
    params: dict[str, Any] = {"r": run_id}
    if limit is not None:
        sql += " LIMIT :lim"
        params["lim"] = limit
    return [_finding_view(dict(r)) for r in (await conn.execute(text(sql), params)).mappings().all()]


async def quality_report(conn: AsyncConnection, run_id: uuid.UUID) -> dict[str, Any]:
    """`QualityReport`. Учитываются актуальные выполнения (не заменённые повтором) актуальных задач плана."""
    profile = (
        await conn.execute(text("SELECT quality_profile FROM runs WHERE id = :r"), {"r": run_id})
    ).scalar_one_or_none()
    if profile is None:
        raise ProblemError("not_found")
    gates = [
        dict(r)
        for r in (
            await conn.execute(
                text(
                    "SELECT g.id, g.gate_id, g.gate_type, g.required, g.task_id, g.status::text AS status,"
                    " g.evidence_id, g.commit_sha, g.error_code, g.not_applicable_reason, g.started_at,"
                    " g.finished_at, e.verification_status::text AS evidence_status"
                    " FROM gate_executions g"
                    " LEFT JOIN tasks t ON t.workspace_id = g.workspace_id AND t.id = g.task_id"
                    " LEFT JOIN evidence e ON e.workspace_id = g.workspace_id AND e.id = g.evidence_id"
                    " WHERE g.run_id = :r AND g.superseded_at IS NULL AND (g.task_id IS NULL OR t.superseded_at IS NULL)"
                    " ORDER BY g.created_at, g.id"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .all()
    ]
    verdict = aggregate_verdict(
        "ERROR" if g["status"] == "PASS" and g["evidence_status"] != "VERIFIED" else g["status"]
        for g in gates
        if g["required"]
    )
    counts = (
        await conn.execute(
            text(
                "SELECT severity::text, status::text, count(*) FROM findings WHERE run_id = :r GROUP BY severity, status"
            ),
            {"r": run_id},
        )
    ).all()
    by_severity = dict.fromkeys(SEVERITIES, 0)
    by_status = dict.fromkeys(FINDING_STATUSES, 0)
    for severity, status, n in counts:
        by_status[status] = by_status.get(status, 0) + int(n)
        if status in OPEN_FINDING_STATUSES:
            by_severity[severity] = by_severity.get(severity, 0) + int(n)
    return {
        "run_id": run_id,
        "quality_profile": profile,
        "verdict": verdict,
        "gate_executions": [_gate_view(g) for g in gates],
        "findings_summary": by_severity,
        "findings_by_status": by_status,
        "findings": await _findings(conn, run_id, QUALITY_FINDINGS_LIMIT),
        "findings_total": sum(by_status.values()),
    }


@router.get("/runs/{run_id}/quality", tags=["quality"], **op("getRunQuality", "quality:read"))
async def get_run_quality(
    request: Request, run_id: Annotated[uuid.UUID, Path()], principal: identity.Principal = Depends(current_principal)
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_run_workspace", run_id)
    await authorize(request, access, "quality:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        body = await quality_report(conn, run_id)
    return json_response(body)


# --- compliance -------------------------------------------------------------------------------------

_COMPLIANCE_FROM = (
    " FROM compliance_results cr"
    " JOIN runs r ON r.id = cr.run_id"
    " JOIN standard_controls c ON c.id = cr.control_id"
    " JOIN standards s ON s.id = c.standard_id"
    " LEFT JOIN project_controls pc ON pc.project_id = r.project_id AND pc.control_id = c.id"
    " LEFT JOIN users u ON u.id = cr.reviewed_by"
)


def _applicability(value: str | None, approved_by: Any) -> str:
    """project_controls → ApplicabilityStatus: не утверждённая человеком применимость — PENDING_REVIEW."""
    if value is None or approved_by is None:
        return "PENDING_REVIEW"
    return _APPLICABILITY.get(value, "PENDING_REVIEW")


def _user_ref(user_id: Any, name: str | None) -> dict[str, Any] | None:
    return None if user_id is None else {"id": user_id, "display_name": name or ""}


def _compliance_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "control_id": r["control_id"],
        "standard_code": r["standard_code"],
        "edition": r["edition"],
        "clause_ref": r["clause_ref"],
        "control_code": r["control_code"],
        "applicability": _applicability(r["applicability"], r["approved_by"]),
        "agent_role": r["agent_role"],
        "status": r["status"],
        "verification_method": r["verification_method"],
        "gate_type": r["default_gate_type"],
        "gate_execution_id": r["gate_execution_id"],
        "evidence_id": r["evidence_id"],
        "reviewed_by": _user_ref(r["reviewed_by"], r["reviewer_name"]),
        "checked_at": r["checked_at"],
    }


async def compliance_summary(
    conn: AsyncConnection, run_id: uuid.UUID, standard_code: str | None = None
) -> dict[str, int]:
    sql = "SELECT cr.status::text, count(*)" + _COMPLIANCE_FROM + " WHERE cr.run_id = :r AND cr.superseded_at IS NULL"
    params: dict[str, Any] = {"r": run_id}
    if standard_code:
        sql += " AND s.code = :std"
        params["std"] = standard_code
    summary = dict.fromkeys(COMPLIANCE_STATUSES, 0)
    for status, n in (await conn.execute(text(sql + " GROUP BY cr.status"), params)).all():
        summary[status] = int(n)
    return summary


@router.get("/runs/{run_id}/compliance", tags=["quality"], **op("getRunCompliance", "quality:read"))
async def get_run_compliance(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    standard_code: Annotated[str | None, Query(max_length=100)] = None,
    status: ComplianceStatus | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    """Матрица контролей run: актуальные вердикты `compliance_results` (порядок — стандарт, контроль)."""
    access = await resolve_resource(request, principal, "sys_resolve_run_workspace", run_id)
    await authorize(request, access, "quality:read")
    state = app_state(request)
    filters = {"r": "compliance", "run": str(run_id), "std": standard_code, "s": status}
    clauses = ["cr.run_id = :r", "cr.superseded_at IS NULL"]
    params: dict[str, Any] = {"r": run_id, "lim": limit + 1}
    if standard_code:
        clauses.append("s.code = :std")
        params["std"] = standard_code
    if status:
        clauses.append("cr.status = CAST(:st AS compliance_status_t)")
        params["st"] = status
    if cursor:
        code, control, rid = decode_cursor(state.secret_key, cursor, filters)
        clauses.append("(s.code, c.control_code, cr.id) > (:kc, :kcc, :kid)")
        params.update(kc=str(code), kcc=str(control), kid=uuid.UUID(str(rid)))
    sql = (
        "SELECT cr.id, cr.control_id, s.code AS standard_code, s.edition, c.clause_ref, c.control_code,"
        " c.verification_method, c.default_gate_type, pc.applicability, pc.approved_by,"
        " coalesce(pc.agent_role, c.default_agent_role)::text AS agent_role, cr.status::text AS status,"
        " cr.gate_execution_id, cr.evidence_id, cr.reviewed_by, u.display_name AS reviewer_name, cr.checked_at"
        + _COMPLIANCE_FROM
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY s.code, c.control_code, cr.id LIMIT :lim"
    )
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
        summary = await compliance_summary(conn, run_id, standard_code)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            state.secret_key, [last["standard_code"], last["control_code"], str(last["id"])], filters
        )
    return json_response(
        {
            "items": [_compliance_row(r) for r in page],
            "next_cursor": next_cursor,
            "summary": summary,
            "disclaimer": DISCLAIMER,
        }
    )


# --- report -----------------------------------------------------------------------------------------


async def _trace_matrix(conn: AsyncConnection, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Трассировка standard → control → applicability → роль → check → evidence → verdict
    (`aicd_standards.compliance_matrix`) в форме `ComplianceTraceRow`."""
    rows = await compliance_matrix(conn, run_id)
    reviewers = {r["verdict"]["reviewed_by"] for r in rows if r["verdict"]["reviewed_by"] is not None}
    names: dict[Any, str] = {}
    if reviewers:
        names = dict(
            (
                await conn.execute(
                    text("SELECT id, display_name FROM users WHERE id = ANY(:ids)"), {"ids": list(reviewers)}
                )
            ).all()
        )
    out = []
    for r in rows:
        ev, verdict, check = r["evidence"], r["verdict"], r["check"]
        applicability = r["applicability"]
        out.append(
            {
                "standard": {"code": r["standard"]["code"], "edition": r["standard"]["edition"]},
                "control": {
                    "code": r["control"]["code"],
                    "clause_ref": r["control"]["clause_ref"],
                    "description": r["control"]["description"],
                },
                "applicability": _applicability(
                    None if applicability["value"] == "undecided" else applicability["value"],
                    applicability["approved_by"],
                ),
                "agent_role": r["agent_role"],
                "check": {
                    "method": check["method"],
                    "gate_type": check["gate_type"],
                    "gate_execution_id": check["gate_execution_id"],
                    "gate_status": check["gate_status"],
                    "commit_sha": check["commit_sha"],
                },
                "evidence": None
                if ev["id"] is None
                else {
                    "id": ev["id"],
                    "kind": ev["kind"],
                    "verification_status": ev["verification_status"],
                    "artifact_sha256": ev["sha256"],
                },
                "verdict": {
                    "status": verdict["status"],
                    "notes": _opt_text(verdict["notes"]),
                    "reviewed_by": _user_ref(verdict["reviewed_by"], names.get(verdict["reviewed_by"])),
                    "checked_at": verdict["checked_at"],
                },
            }
        )
    return out


async def _report_tasks(conn: AsyncConnection, run_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = [
        dict(r)
        for r in (
            await conn.execute(
                text(
                    "SELECT t.*, (SELECT a.provider FROM task_attempts a WHERE a.task_id = t.id ORDER BY a.attempt_no DESC"
                    " LIMIT 1) AS current_provider FROM tasks t WHERE t.run_id = :r AND t.superseded_at IS NULL"
                    " ORDER BY t.created_at, t.id"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .all()
    ]
    deps: dict[uuid.UUID, list[uuid.UUID]] = {}
    for task, dep in (
        await conn.execute(
            text(
                "SELECT task_id, depends_on_task_id FROM task_dependencies WHERE task_id = ANY(:ids)"
                " ORDER BY task_id, depends_on_task_id"
            ),
            {"ids": [r["id"] for r in rows]},
        )
    ).all():
        deps.setdefault(task, []).append(dep)
    return [{**task_summary(r, deps), "title": redact_text(r["title"])} for r in rows]


async def _report_approvals(conn: AsyncConnection, access: Access, run_id: uuid.UUID) -> list[dict[str, Any]]:
    ctx = await APPROVALS.self_approval_context(conn, access.workspace_id)
    rows = await APPROVALS.list_for_viewer(
        conn, viewer_id=access.principal.user_id, viewer_role=access.role, ctx=ctx,
        statuses=["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"], run_id=run_id, limit=10_000,
    )  # fmt: skip
    rows.sort(key=lambda r: (r["requested_at"], str(r["id"])))
    out = []
    for r in rows:
        view = approval_view(
            r, APPROVALS.can_decide(r, actor_id=access.principal.user_id, actor_role=access.role, ctx=ctx)
        )
        view.update(
            summary=redact_text(view["summary"]),
            context=redact_value(view["context"]),
            decision_reason=_opt_text(view["decision_reason"]),
        )
        out.append(view)
    return out


async def run_report(conn: AsyncConnection, access: Access, run_id: uuid.UUID) -> dict[str, Any]:
    """`RunReport`: один снимок данных для JSON и Markdown (одинаковый порядок при одинаковых данных)."""
    run = await run_view(conn, run_id)
    run["objective"] = redact_text(run["objective"])
    evidence = await _evidence_views(conn, await _evidence_rows(conn, "e.run_id = :r", {"r": run_id}))
    external = [
        {"id": r[0], "action_type": r[1], "status": r[2], "external_ref": _opt_text(r[3])}
        for r in (
            await conn.execute(
                text(
                    "SELECT id, action_type, status, external_ref FROM external_actions WHERE run_id = :r"
                    " ORDER BY planned_at, id"
                ),
                {"r": run_id},
            )
        ).all()
    ]
    return {
        "run": run,
        "quality": await quality_report(conn, run_id),
        "compliance_summary": await compliance_summary(conn, run_id),
        "compliance_matrix": await _trace_matrix(conn, run_id),
        "disclaimer": DISCLAIMER,
        "tasks": await _report_tasks(conn, run_id),
        "approvals": await _report_approvals(conn, access, run_id),
        "evidence": evidence,
        "findings": await _findings(conn, run_id, None),
        "external_actions": external,
        "generated_at": utcnow(),
        "report_artifact": None,
    }


def _wants_markdown(accept: str | None) -> bool:
    """Content negotiation по Accept (API_DESIGN §1): text/markdown, только если он предпочтительнее JSON."""
    if not accept:
        return False
    best: dict[str, float] = {}
    for part in accept.split(","):
        media, *params = (p.strip() for p in part.split(";"))
        q = 1.0
        for p in params:
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        best[media.lower()] = max(q, best.get(media.lower(), 0.0))
    md = best.get("text/markdown", best.get("text/*", 0.0))
    js = max(best.get("application/json", 0.0), best.get("application/*", 0.0), best.get("*/*", 0.0))
    return md > 0 and md > js


@router.get("/runs/{run_id}/report", tags=["quality"], **op("getRunReport", "quality:read"))
async def get_run_report(
    request: Request, run_id: Annotated[uuid.UUID, Path()], principal: identity.Principal = Depends(current_principal)
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_run_workspace", run_id)
    await authorize(request, access, "quality:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        report = await run_report(conn, access, run_id)
    if _wants_markdown(request.headers.get("accept")):
        return Response(
            render_run_report(report),
            media_type="text/markdown; charset=utf-8",
            headers={"Vary": "Accept"},
        )
    return json_response(report, headers={"Vary": "Accept"})
