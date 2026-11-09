"""Применимость контролей проекта — экран Standards & Compliance (ТЗ §13; QUALITY_GATES §8; OQ-43 (c)).

* `GET /projects/{project_id}/controls` — каждый активный контроль реестра: применимость (черновик системы /
  утверждение admin / нет строки), обоснование, ответственная роль, блокирующий статус (явный и умолчание
  платформы для профиля проекта), предупреждение «не сертификация»;
* `POST /projects/{project_id}/controls/{control_id}/approval` — утверждение (черновик → утверждено) или
  пересмотр: applicability, rationale, роль, blocking; утверждает пользователь сессии (триггер §13.3),
  право `project:update` (admin+), Idempotency-Key, audit `project_control.approve`.

Чужой/несуществующий проект ⇒ 404 (AC-12).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import app_state, authorize, current_principal, project_access
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_identity import api as identity
from aicd_platform import audit
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_platform.redaction import redact_text
from aicd_standards import api as standards

router = APIRouter(prefix="/projects", tags=["projects"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
ApprovalState = Literal["approved", "draft", "missing"]
AgentRole = Literal[
    "requirements", "architect", "planner", "developer", "reviewer", "security", "qa", "database", "performance",
    "devops", "maintenance", "documentation",
]  # fmt: skip
_APPLICABILITY = {"applicable": "APPLICABLE", "tailored": "APPLICABLE", "not_applicable": "NOT_APPLICABLE"}


class ProjectControlApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applicability: Literal["applicable", "not_applicable", "tailored"]
    rationale: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    agent_role: AgentRole | None = None
    blocking: bool | None = None


def project_control_view(r: dict[str, Any]) -> dict[str, Any]:
    approved = r["approved_by"] is not None
    explicit = r["blocking"]
    return {
        "control_id": r["control_id"],
        "standard_code": r["standard_code"],
        "edition": r["edition"],
        "standard_title": r["standard_title"],
        "clause_ref": r["clause_ref"],
        "control_code": r["control_code"],
        "description": r["description"],
        "verification_method": r["verification_method"],
        "gate_type": r["default_gate_type"],
        "default_agent_role": r["default_agent_role"],
        "approval_state": r["approval_state"],
        "applicability": _APPLICABILITY.get(r["applicability"], "PENDING_REVIEW") if approved else "PENDING_REVIEW",
        "proposed_applicability": r["applicability"],
        "rationale": None if r["rationale"] is None else redact_text(r["rationale"]),
        "agent_role": r["agent_role"] or r["default_agent_role"],
        "blocking": explicit,
        "effective_blocking": bool(r["default_blocking"] if explicit is None else explicit),
        "approved_by": {"id": r["approved_by"], "display_name": r["approver_name"] or ""} if approved else None,
        "approved_at": r["approved_at"],
        "updated_at": r["updated_at"],
    }


@router.get("/{project_id}/controls", **op("listProjectControls", "project:read"))
async def list_project_controls(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    standard_code: Annotated[str | None, Query(max_length=100)] = None,
    approval_state: ApprovalState | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    state = app_state(request)
    filters = {"r": "project_controls", "p": str(project_id), "std": standard_code, "a": approval_state}
    after = None
    if cursor:
        code, control, cid = decode_cursor(state.secret_key, cursor, filters)
        try:
            after = (str(code), str(control), uuid.UUID(str(cid)))
        except ValueError as exc:
            raise ProblemError("invalid_cursor") from exc
    async with transaction(state.engine, access.db()) as conn:
        profile = await standards.project_quality_profile(conn, project_id)
        rows = await standards.list_project_controls(
            conn, project_id, profile=profile, standard_code=standard_code, approval_state=approval_state,
            after=after, limit=limit + 1,
        )  # fmt: skip
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            state.secret_key, [last["standard_code"], last["control_code"], str(last["control_id"])], filters
        )
    return json_response(
        {
            "items": [project_control_view(r) for r in page],
            "next_cursor": next_cursor,
            "quality_profile": profile,
            "disclaimer": standards.DISCLAIMER,
        }
    )


@router.post("/{project_id}/controls/{control_id}/approval", **op("approveProjectControl", "project:update"))
async def approve_project_control(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    control_id: Annotated[uuid.UUID, Path()],
    body: ProjectControlApprovalRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:update")

    async def command(conn: AsyncConnection) -> CommandResult:
        profile = await standards.project_quality_profile(conn, project_id)
        before = await standards.list_project_controls(conn, project_id, profile=profile, control_id=control_id)
        if not before:
            raise ProblemError("not_found")
        if body.applicability != "applicable" and body.rationale is None:
            raise ProblemError(
                "validation_error",
                errors=[FieldError("/rationale", "required", "обязателен для not_applicable и tailored")],
            )
        await standards.approve_project_control(
            conn, workspace_id=access.workspace_id, project_id=project_id, control_id=control_id,
            user_id=principal.user_id, applicability=body.applicability, rationale=body.rationale,
            agent_role=body.agent_role, blocking=body.blocking,
        )  # fmt: skip
        (row,) = await standards.list_project_controls(conn, project_id, profile=profile, control_id=control_id)
        await audit.record(
            conn,
            action="project_control.approve",
            resource_type="project_control",
            resource_id=control_id,
            actor_type="user",
            actor_id=principal.user_id,
            workspace_id=access.workspace_id,
            metadata={
                "project_id": str(project_id),
                "control_code": row["control_code"],
                "from": {"state": before[0]["approval_state"], "applicability": before[0]["applicability"]},
                "to": {"applicability": body.applicability, "blocking": body.blocking},
            },
        )
        return CommandResult(200, project_control_view(row))

    return await run_idempotent(request, access.db(), body.model_dump(), command)
