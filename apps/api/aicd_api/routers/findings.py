"""Находки и решения человека по ним (OQ-43 (d); QUALITY_GATES §5.3, DATABASE_DESIGN §12.6, §18.3).

* `GET /runs/{run_id}/findings` — полный список находок run с фильтрами (severity, status, задача, контроль),
  keyset-пагинация по (severity desc, created_at, id); `QualityReport.findings` усечён до 200;
* `GET /findings/{finding_id}` — находка с ETag `"v<version>"` (If-None-Match ⇒ 304);
* `POST /findings/{finding_id}/transitions` — ACKNOWLEDGED / FALSE_POSITIVE / ACCEPTED_RISK / повторное
  открытие (Idempotency-Key + If-Match, обязательная причина, audit `finding.transition`). Право
  `finding:triage` уточняется целевым статусом (`rbac.can_transition_finding`: ACCEPTED_RISK — admin+).

Чужая находка неотличима от отсутствующей — 404 через `sys_resolve_finding_workspace` (миграция 0010).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import _audit_denied, app_state, authorize, current_principal
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_api.routers.quality import FINDING_SELECT, SEVERITY_ORDER, finding_view
from aicd_api.routers.runs import resolve_resource
from aicd_identity import api as identity
from aicd_identity.rbac import can_transition_finding
from aicd_platform import audit
from aicd_platform.db import transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_verification.triage import human_transition

router = APIRouter(tags=["quality"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
Severity = Literal["info", "low", "medium", "high", "critical"]
FindingStatus = Literal["OPEN", "ACKNOWLEDGED", "FIXED", "VERIFIED_FIXED", "FALSE_POSITIVE", "ACCEPTED_RISK"]


class FindingTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_status: Literal["OPEN", "ACKNOWLEDGED", "FALSE_POSITIVE", "ACCEPTED_RISK"]
    reason: Annotated[str, Field(min_length=1, max_length=4000)]


async def _finding(conn: AsyncConnection, finding_id: uuid.UUID) -> dict[str, Any]:
    row = (await conn.execute(text(FINDING_SELECT + " WHERE f.id = :id"), {"id": finding_id})).mappings().first()
    if row is None:
        raise ProblemError("not_found")
    return dict(row)


@router.get("/runs/{run_id}/findings", **op("listRunFindings", "quality:read"))
async def list_run_findings(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    severity: Annotated[list[Severity] | None, Query()] = None,
    status: Annotated[list[FindingStatus] | None, Query()] = None,
    task_id: uuid.UUID | None = None,
    control_ref: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9._-]{1,64}$")] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_run_workspace", run_id)
    await authorize(request, access, "quality:read")
    state = app_state(request)
    filters = {
        "r": "findings", "run": str(run_id), "sev": sorted(severity or []), "st": sorted(status or []),
        "t": str(task_id) if task_id else None, "c": control_ref,
    }  # fmt: skip
    clauses = ["f.run_id = :r"]
    params: dict[str, Any] = {"r": run_id, "lim": limit + 1}
    if severity:
        clauses.append("f.severity::text = ANY(:sev)")
        params["sev"] = list(severity)
    if status:
        clauses.append("f.status::text = ANY(:st)")
        params["st"] = list(status)
    if task_id:
        clauses.append("f.task_id = :t")
        params["t"] = task_id
    if control_ref:
        clauses.append(":cref = ANY(f.control_refs)")
        params["cref"] = control_ref
    if cursor:
        rank, created, fid = decode_cursor(state.secret_key, cursor, filters)
        clauses.append(f"({SEVERITY_ORDER}, f.created_at, f.id) > (:kr, :kt, :kid)")
        try:
            params.update(kr=int(rank), kt=datetime.fromisoformat(str(created)), kid=uuid.UUID(str(fid)))
        except (TypeError, ValueError) as exc:
            raise ProblemError("invalid_cursor") from exc
    select = FINDING_SELECT.replace(" FROM findings f", f", {SEVERITY_ORDER} AS sev_rank FROM findings f", 1)
    sql = select + " WHERE " + " AND ".join(clauses) + f" ORDER BY {SEVERITY_ORDER}, f.created_at, f.id LIMIT :lim"
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            state.secret_key, [last["sev_rank"], last["created_at"].isoformat(), str(last["id"])], filters
        )
    return json_response({"items": [finding_view(r) for r in page], "next_cursor": next_cursor})


@router.get("/findings/{finding_id}", **op("getFinding", "quality:read"))
async def get_finding(
    request: Request,
    finding_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_finding_workspace", finding_id)
    await authorize(request, access, "quality:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        row = await _finding(conn, finding_id)
    tag = etag(row["version"])
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return json_response(finding_view(row), headers={"ETag": tag})


@router.post("/findings/{finding_id}/transitions", **op("transitionFinding", "finding:triage"))
async def transition_finding(
    request: Request,
    finding_id: Annotated[uuid.UUID, Path()],
    body: FindingTransitionRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_finding_workspace", finding_id)
    await authorize(request, access, "finding:triage")
    # §5.5: право по целевому статусу — до Idempotency-Key (порядок API_DESIGN §6); отказ аудируется.
    if not can_transition_finding(access.role, body.to_status):
        async with transaction(app_state(request).engine, access.db()) as conn:
            await _audit_denied(conn, access, f"finding:triage:{body.to_status}", request)
        raise ProblemError("forbidden")

    async def command(conn: AsyncConnection) -> CommandResult:
        expected = require_if_match(request.headers.get("if-match"))
        result = await human_transition(
            conn, finding_id, to_status=body.to_status, reason=body.reason, actor_id=principal.user_id,
            expected_version=expected,
        )  # fmt: skip
        await audit.record(
            conn,
            action="finding.transition",
            resource_type="finding",
            resource_id=finding_id,
            actor_type="user",
            actor_id=principal.user_id,
            workspace_id=access.workspace_id,
            metadata={
                "run_id": str(result["run_id"]),
                "from": result["from_status"],
                "to": result["to_status"],
                "severity": result["severity"],
            },
        )
        row = await _finding(conn, finding_id)
        return CommandResult(200, finding_view(row), {"ETag": etag(row["version"])})

    return await run_idempotent(request, access.db(), body.model_dump(), command)
