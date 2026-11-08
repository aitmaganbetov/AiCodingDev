"""Поручения run (ТЗ §8.2, API_DESIGN §5.4/§12; P3-13): intake, просмотр, confirm/cancel.

`202` означает «поручение надёжно сохранено (state + outbox)», а не «доставлено» или «выполнено»:
представление `Directive` разделяет `status`, `delivery` и `completion` (API_DESIGN §12.4).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import Access, _audit_denied, _remember_rate, app_state, authorize, current_principal
from aicd_api.deps import workspace_access as _workspace_access
from aicd_api.hooks import get_hooks
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_api.routers.approvals import APPROVALS, DIRECTIVES
from aicd_identity import api as identity
from aicd_identity.rbac import can_submit_directive
from aicd_orchestration import directives as service
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(prefix="/runs/{run_id}/directives", tags=["chat"])

AgentRole = Literal[
    "requirements", "architect", "planner", "developer", "reviewer", "security", "qa", "database", "performance",
    "devops", "maintenance", "documentation",
]  # fmt: skip
DirectiveType = Literal["ASK", "NEW_TASK", "CHANGE_REQUEST", "STOP", "PAUSE", "RESUME", "REVIEW", "APPROVE", "REJECT"]
DirectiveStatus = Literal[
    "RECEIVED", "IMPACT_ANALYSIS_PENDING", "IMPACT_READY", "AWAITING_CONFIRMATION", "CONFIRMED", "CHECKPOINTING",
    "REPLANNING", "DISPATCHED", "ACKNOWLEDGED", "COMPLETED", "REJECTED", "CANCELLED", "FAILED",
]  # fmt: skip
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
DIRECTIVES_PER_MINUTE = 30  # API_DESIGN §10: директивы — user + run


class DirectiveTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["run", "role", "task", "approval"]
    role: AgentRole | None = None
    task_id: uuid.UUID | None = None
    approval_id: uuid.UUID | None = None


class DirectiveCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: DirectiveTarget
    directive_type: DirectiveType
    message: Annotated[str, Field(min_length=1, max_length=8000)]
    expected_run_version: Annotated[int, Field(ge=0)]
    requires_impact_preview: bool = True


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(max_length=2000)] | None = None


def _ack_status(value: str) -> str:
    return value if value in ("PENDING", "DELIVERED", "ACKNOWLEDGED", "REJECTED", "EXPIRED") else "FAILED"


def directive_view(d: dict[str, Any]) -> dict[str, Any]:
    if d["directive_type"] in ("APPROVE", "REJECT"):
        target: dict[str, Any] = {"type": "approval", "approval_id": d["approval_id"]}
    else:
        target = {"type": d["target_type"]}
        if d["target_role"] is not None:
            target["role"] = d["target_role"]
        if d["target_task_id"] is not None:
            target["task_id"] = d["target_task_id"]
    impacts = d.get("impacts", [])
    acks = d.get("acknowledgements", [])
    resulting = [i["task_id"] for i in impacts if i["impact_type"] == "TASK_ADD"]
    return {
        "id": d["id"],
        "run_id": d["run_id"],
        "chat_message_id": d["chat_message_id"],
        "target": target,
        "directive_type": d["directive_type"],
        "message": d["message"],
        "status": d["status"],
        "impact_status": service.impact_status_view(d["status"], d["impact_status"]),
        "impacts": [{"task_id": i["task_id"], "impact_type": i["impact_type"], "resolution": i["resolution"]}
                    for i in impacts],
        "impact_summary": service.impact_summary(d["directive_type"], d["impact_status"],
                                                 [i["impact_type"] for i in impacts]),
        "requires_confirmation": bool(d["requires_impact_preview"] or
                                      (d["approval_id"] is not None and d["directive_type"] not in ("APPROVE", "REJECT"))),
        "approval_id": d["approval_id"] if d["directive_type"] not in ("APPROVE", "REJECT") else None,
        "delivery": {
            "state": service.delivery_state(d["status"], [a["status"] for a in acks]),
            "acknowledgements": [{"recipient_role": a["recipient_role"], "status": _ack_status(a["status"]),
                                  "task_id": a["recipient_task_id"], "delivered_at": a["delivered_at"],
                                  "acknowledged_at": a["acknowledged_at"]} for a in acks],
        },
        "completion": {"state": service.completion_state(d["status"]), "resulting_task_ids": resulting,
                       "completed_at": d["completed_at"] if d["status"] == "COMPLETED" else None},
        "submitted_by": {"id": d["submitted_by"], "display_name": d.get("submitted_by_name") or ""},
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "version": d["version"],
    }  # fmt: skip


async def run_access(request: Request, principal: identity.Principal, run_id: uuid.UUID) -> Access:
    """workspace run через SECURITY DEFINER `sys_resolve_run_workspace`; не член ⇒ 404 (AC-12)."""
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        ws = await service.resolve_run_workspace(conn, run_id)
    if ws is None:
        raise ProblemError("not_found")
    header = request.headers.get("x-workspace-id")
    if header is not None and header != str(ws):
        raise ProblemError("not_found")
    return await _workspace_access(request, principal, ws)


async def _view(conn: AsyncConnection, run_id: uuid.UUID, directive_id: uuid.UUID) -> dict[str, Any]:
    d = await DIRECTIVES.get(conn, directive_id, run_id=run_id)
    if d is None:
        raise ProblemError("not_found")
    return directive_view(d)


@router.post("", status_code=202, **op("createDirective", "directive:create"))
async def create_directive(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    body: DirectiveCreateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await run_access(request, principal, run_id)
    await authorize(request, access, "directive:create")
    state = app_state(request)
    if not can_submit_directive(access.role, body.directive_type):
        async with transaction(state.engine, access.db()) as conn:
            await _audit_denied(conn, access, f"directive:create:{body.directive_type}", request)
        raise ProblemError("forbidden")
    _remember_rate(
        request, await state.limiter.hit(f"directive:user:{principal.user_id}:run:{run_id}", DIRECTIVES_PER_MINUTE)
    )
    target = service.Target(body.target.type, body.target.role, body.target.task_id, body.target.approval_id)
    submitted: dict[str, Any] = {}

    async def command(conn: AsyncConnection) -> CommandResult:
        result = await DIRECTIVES.submit(
            conn, run_id=run_id, workspace_id=access.workspace_id, actor_id=principal.user_id,
            actor_role=access.role, directive_type=body.directive_type, target=target, message=body.message,
            expected_run_version=body.expected_run_version, requires_impact_preview=body.requires_impact_preview,
        )  # fmt: skip
        submitted["result"] = result
        accepted = {
            "directive_id": result.directive_id,
            "status": result.status,
            "chat_message_id": result.chat_message_id,
            "status_url": f"/api/v1/runs/{run_id}/directives/{result.directive_id}",
        }
        return CommandResult(202, accepted)

    response = await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)
    result: service.SubmitResult | None = submitted.get("result")
    if result is not None:
        hooks = get_hooks(request)
        if result.decided_approval is not None:
            decided = result.decided_approval
            await hooks.on_approval_decided(decided)
            critical_directive = (decided.get("subject") or {}).get("directive_id")
            if decided["action_type"] == "CRITICAL_DIRECTIVE" and isinstance(critical_directive, str):
                await hooks.on_directive_changed(uuid.UUID(critical_directive), access.workspace_id)
        await hooks.on_directive_changed(result.directive_id, access.workspace_id)
    return response


@router.get("", **op("listDirectives", "chat:read"))
async def list_directives(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    status: Annotated[list[DirectiveStatus] | None, Query()] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await run_access(request, principal, run_id)
    await authorize(request, access, "chat:read")
    state = app_state(request)
    filters = {"r": "directives", "run": str(run_id), "status": sorted(status or [])}
    before = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        before = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        rows = await DIRECTIVES.list_for_run(
            conn, run_id, statuses=list(status or []) or None, before=before, limit=limit + 1
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [directive_view(r) for r in page], "next_cursor": next_cursor})


@router.get("/{directive_id}", **op("getDirective", "chat:read"))
async def get_directive(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    directive_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await run_access(request, principal, run_id)
    await authorize(request, access, "chat:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        view = await _view(conn, run_id, directive_id)
    tag = etag(view["version"])
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return json_response(view, headers={"ETag": tag})


async def _command(
    request: Request, run_id: uuid.UUID, directive_id: uuid.UUID, body: CommandBody | None,
    principal: identity.Principal, action: Literal["confirm", "cancel"],
) -> Response:  # fmt: skip
    access = await run_access(request, principal, run_id)
    await authorize(request, access, "directive:confirm")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        current = await DIRECTIVES.get(conn, directive_id, run_id=run_id)
    if current is None:
        raise ProblemError("not_found")
    if not service.can_manage(principal.user_id, access.role, current["submitted_by"]):
        async with transaction(state.engine, access.db()) as conn:
            await _audit_denied(conn, access, f"directive:{action}:own", request)
        raise ProblemError("forbidden")
    reason = body.reason if body else None
    done: dict[str, Any] = {}

    async def command(conn: AsyncConnection) -> CommandResult:
        expected = require_if_match(request.headers.get("if-match"))
        fn = DIRECTIVES.confirm if action == "confirm" else DIRECTIVES.cancel
        await fn(conn, directive_id, actor_id=principal.user_id, actor_role=access.role, expected_version=expected,
                 reason=reason)  # fmt: skip
        view = await _view(conn, run_id, directive_id)
        done["ok"] = True
        return CommandResult(202, view, {"ETag": etag(view["version"])})

    response = await run_idempotent(request, access.db(), body.model_dump() if body else {}, command)
    if done:
        await get_hooks(request).on_directive_changed(directive_id, access.workspace_id)
    return response


@router.post("/{directive_id}/confirm", status_code=202, **op("confirmDirective", "directive:confirm"))
async def confirm_directive(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    directive_id: Annotated[uuid.UUID, Path()],
    body: Annotated[CommandBody | None, Body()] = None,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    return await _command(request, run_id, directive_id, body, principal, "confirm")


@router.post("/{directive_id}/cancel", status_code=202, **op("cancelDirective", "directive:confirm"))
async def cancel_directive(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    directive_id: Annotated[uuid.UUID, Path()],
    body: Annotated[CommandBody | None, Body()] = None,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    return await _command(request, run_id, directive_id, body, principal, "cancel")


__all__ = ["APPROVALS", "DIRECTIVES", "router", "run_access"]
