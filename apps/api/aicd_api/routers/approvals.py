"""Approvals (ТЗ §8, API_DESIGN §5.3): список, просмотр, решение с four-eyes (P3-10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import (
    Access,
    _audit_denied,
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    require_step_up,
    workspace_access,
)
from aicd_api.hooks import get_hooks
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_identity import api as identity
from aicd_identity.rbac import FOUR_EYES_ACTIONS
from aicd_orchestration.approvals import Approvals, SelfApprovalContext, summary_for
from aicd_orchestration.directives import Directives
from aicd_platform import metrics
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(prefix="/approvals", tags=["approvals"])

ApprovalStatus = Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"]
ApprovalActionType = Literal[
    "PLAN_APPROVAL", "MERGE", "PRODUCTION_DEPLOY", "DESTRUCTIVE_MIGRATION", "DATA_DELETION", "SECRET_CHANGE",
    "UNRESTRICTED_NETWORK", "DOCS_DOWNLOAD", "CRITICAL_DIRECTIVE", "MANUAL_REVIEW", "SELF_REVIEW_FALLBACK",
    "LOCAL_EXECUTION_FALLBACK",
]  # fmt: skip
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]

APPROVALS = Approvals()
DIRECTIVES = Directives(APPROVALS)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVED", "REJECTED"]
    reason: Annotated[str, Field(max_length=4000)] | None = None


def approval_view(row: dict[str, Any], can_decide: bool) -> dict[str, Any]:
    requester = None
    if row["requested_by"] is not None:
        requester = {"id": row["requested_by"], "display_name": row.get("requested_by_name") or ""}
    decider = None
    if row["decided_by"] is not None:
        decider = {"id": row["decided_by"], "display_name": row.get("decided_by_name") or ""}
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "action_type": row["action_type"],
        "status": row["status"],
        "risk_level": row["risk_level"],
        "summary": summary_for(row["action_type"], row["subject"] or {}),
        "context": row["subject"] or {},
        "requested_by": {"actor_type": row["requested_by_type"], "user": requester, "role": row["requested_by_role"]},
        "decided_by": decider,
        "decision_reason": row["decision_reason"],
        "can_decide": can_decide,
        "expires_at": row["expires_at"],
        "created_at": row["requested_at"],
        "decided_at": row["decided_at"],
        "version": row["version"],
    }


async def _locate(request: Request, principal: identity.Principal, approval_id: uuid.UUID) -> Access:
    """Workspace approval через SECURITY DEFINER `sys_resolve_approval_workspace` (только для членов) —
    иначе 404, не раскрывая чужие ресурсы (AC-12); как `resolve_resource` в routers/runs.py."""
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        ws = (
            await conn.execute(text("SELECT sys_resolve_approval_workspace(:id)"), {"id": approval_id})
        ).scalar_one_or_none()
    if ws is None:
        metrics.AUTHZ_DENIED.labels("tenant").inc()  # 404 на чужой/несуществующий ресурс — метрика безопасности (AC-12)
        raise ProblemError("not_found")
    header = request.headers.get("x-workspace-id")
    if header is not None and header != str(ws):
        raise ProblemError("not_found")
    return await workspace_access(request, principal, ws)


async def _load_with_ctx(
    conn: AsyncConnection, access: Access, approval_id: uuid.UUID
) -> tuple[dict[str, Any], SelfApprovalContext]:
    row = await APPROVALS.get(conn, approval_id)
    if row is None:
        raise ProblemError("not_found")
    return row, await APPROVALS.self_approval_context(conn, access.workspace_id)


@router.get("", **op("listApprovals", "approval:read"))
async def list_approvals(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    status: Annotated[list[ApprovalStatus] | None, Query()] = None,
    run_id: uuid.UUID | None = None,
    action_type: Annotated[list[ApprovalActionType] | None, Query()] = None,
    decidable_by_me: bool | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "approval:read")
    state = app_state(request)
    filters = {
        "r": "approvals", "ws": str(access.workspace_id), "status": sorted(status or []),
        "run": str(run_id) if run_id else None, "at": sorted(action_type or []), "mine": decidable_by_me,
    }  # fmt: skip
    before = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        before = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        ctx = await APPROVALS.self_approval_context(conn, access.workspace_id)
        rows = await APPROVALS.list_for_viewer(
            conn, viewer_id=principal.user_id, viewer_role=access.role, ctx=ctx, statuses=list(status or []) or None,
            run_id=run_id, action_types=list(action_type or []) or None, decidable_by_me=decidable_by_me,
            before=before, limit=limit + 1,
        )  # fmt: skip
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["requested_at"].isoformat(), str(last["id"])], filters)
    items = [
        approval_view(r, APPROVALS.can_decide(r, actor_id=principal.user_id, actor_role=access.role, ctx=ctx))
        for r in page
    ]
    return json_response({"items": items, "next_cursor": next_cursor})


@router.get("/{approval_id}", **op("getApproval", "approval:read"))
async def get_approval(
    request: Request,
    approval_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await _locate(request, principal, approval_id)
    await authorize(request, access, "approval:read")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        row, ctx = await _load_with_ctx(conn, access, approval_id)
    tag = etag(row["version"])
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    can = APPROVALS.can_decide(row, actor_id=principal.user_id, actor_role=access.role, ctx=ctx)
    return json_response(approval_view(row, can), headers={"ETag": tag})


@router.post("/{approval_id}/decision", **op("decideApproval", "approval:decide"))
async def decide_approval(
    request: Request,
    approval_id: Annotated[uuid.UUID, Path()],
    body: ApprovalDecisionRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await _locate(request, principal, approval_id)
    await authorize(request, access, "approval:decide")
    state = app_state(request)
    # §5.3: роль по action_type и four-eyes — до Idempotency-Key (порядок API_DESIGN §6); отказ аудируется.
    async with transaction(state.engine, access.db()) as conn:
        row, ctx = await _load_with_ctx(conn, access, approval_id)
    verdict = APPROVALS.check(row, actor_id=principal.user_id, actor_role=access.role, ctx=ctx)
    if not verdict.allowed:
        async with transaction(state.engine, access.db()) as conn:
            await _audit_denied(conn, access, f"approval:decide:{row['action_type']}", request)
        raise ProblemError(verdict.code or "forbidden")
    if body.decision == "APPROVED" and row["action_type"] in FOUR_EYES_ACTIONS:
        # SC-IAM-08 / SC-APR-06: утверждение действия критического набора — только со свежим вводом пароля.
        await require_step_up(request, principal, f"approval:decide:{row['action_type']}")
    decided: dict[str, Any] = {}

    async def command(conn: AsyncConnection) -> CommandResult:
        expected = require_if_match(request.headers.get("if-match"))
        result = await DIRECTIVES.decide_approval(
            conn, approval_id, actor_id=principal.user_id, actor_role=access.role, decision=body.decision,
            reason=body.reason, expected_version=expected,
        )  # fmt: skip
        decided.update(result)
        return CommandResult(200, approval_view(result, False), {"ETag": etag(result["version"])})

    response = await run_idempotent(request, access.db(), body.model_dump(), command)
    if decided:
        hooks = get_hooks(request)
        await hooks.on_approval_decided(decided)
        directive_id = (decided.get("subject") or {}).get("directive_id")
        if decided["action_type"] == "CRITICAL_DIRECTIVE" and isinstance(directive_id, str):
            await hooks.on_directive_changed(uuid.UUID(directive_id), access.workspace_id)
    return response
