"""Runs и tasks (ТЗ §8, P3-08, P4-12): создание, чтение, DAG, команды pause/resume/cancel, попытки, ACP-сообщения."""

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
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    project_access,
    workspace_access,
)
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, parse_if_match
from aicd_api.routers import op
from aicd_identity import api as identity
from aicd_identity.rbac import Role
from aicd_orchestration import runs as runs_service
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_platform.redaction import redact_value

router = APIRouter(tags=["runs"])

RunMode = Literal[
    "CREATE", "DEVELOP", "DEBUG", "AUDIT", "MODERNIZE", "SECURITY", "OPTIMIZE", "TEST", "MAINTAIN", "DOCUMENT", "REVIEW"
]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]


class RunRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["adaptive", "fixed"] = "adaptive"
    allowed_providers: Annotated[list[Literal["codex", "claude"]], Field(min_length=1)] | None = None


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: uuid.UUID
    mode: RunMode
    objective: Annotated[str, Field(min_length=1, max_length=20000)]
    routing: RunRouting | None = None
    quality_profile: Literal["basic", "strict", "critical"] | None = None
    approval_policy: Literal["risk_based", "always", "critical_only"] = "risk_based"


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Annotated[str, Field(max_length=2000)] | None = None


async def _resolve(request: Request, principal: identity.Principal, fn: str, resource_id: uuid.UUID) -> Access:
    """Workspace ресурса через SECURITY DEFINER функцию (только для членов) — иначе 404 (AC-12)."""
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        ws = (await conn.execute(text(f"SELECT {fn}(:id)"), {"id": resource_id})).scalar_one_or_none()
        role = await identity.workspace_role(conn, ws) if ws else None
    if ws is None or role is None:
        raise ProblemError("not_found")
    header = request.headers.get("x-workspace-id")
    if header is not None and header != str(ws):
        raise ProblemError("not_found")
    return Access(principal, ws, Role(role))


def _run_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workspace_id": row["workspace_id"],
        "mode": row["mode"],
        "objective": row["objective"],
        "status": row["status"],
        "blocked_reason": row["blocked_reason"],
        "requested_by": {"id": row["requested_by"], "display_name": row["requested_by_name"]},
        "quality_profile": row["quality_profile"],
        "version": row["version"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


_RUN_SELECT = "SELECT r.*, u.display_name AS requested_by_name FROM runs r JOIN users u ON u.id = r.requested_by"


@router.post("/runs", status_code=201, **op("createRun", "run:create"))
async def create_run(
    request: Request, body: RunCreateRequest, principal: identity.Principal = Depends(current_principal)
) -> Response:
    access = await project_access(request, principal, body.project_id)
    await authorize(request, access, "run:create")

    async def command(conn: AsyncConnection) -> CommandResult:
        created = await runs_service.create_run(
            conn,
            workspace_id=access.workspace_id,
            project_id=body.project_id,
            requested_by=principal.user_id,
            mode=body.mode,
            objective=body.objective,
            quality_profile=body.quality_profile,
            approval_policy=body.approval_policy,
            allowed_providers=list(body.routing.allowed_providers)
            if body.routing and body.routing.allowed_providers
            else None,
            routing_strategy=body.routing.strategy if body.routing else "adaptive",
        )
        run_id = created["run_id"]
        return CommandResult(
            201,
            {"run_id": run_id, "status": "CREATED", "event_stream": f"/api/v1/runs/{run_id}/events"},
            {"Location": f"/api/v1/runs/{run_id}", "ETag": etag(0)},
        )

    return await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)


@router.get("/runs", **op("listRuns", "run:read"))
async def list_runs(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    project_id: uuid.UUID | None = None,
    status: Annotated[list[str] | None, Query()] = None,
    mode: RunMode | None = None,
    created_after: datetime | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "run:read")
    state = app_state(request)
    filters = {
        "r": "runs",
        "ws": str(access.workspace_id),
        "p": str(project_id),
        "s": sorted(status or []),
        "m": mode,
        "ca": str(created_after),
    }
    clauses = ["r.workspace_id = app_current_workspace()"]
    params: dict[str, Any] = {"lim": limit + 1}
    if project_id:
        clauses.append("r.project_id = :p")
        params["p"] = project_id
    if status:
        clauses.append("r.status = ANY(:s)")
        params["s"] = status
    if mode:
        clauses.append("r.mode = :m")
        params["m"] = mode
    if created_after:
        clauses.append("r.created_at > :ca")
        params["ca"] = created_after
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        clauses.append("(r.created_at, r.id) < (:ct, :cid)")
        params.update(ct=datetime.fromisoformat(t), cid=uuid.UUID(i))
    sql = _RUN_SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY r.created_at DESC, r.id DESC LIMIT :lim"
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [_run_summary(r) for r in page], "next_cursor": next_cursor})


@router.get("/runs/{run_id}", **op("getRun", "run:read"))
async def get_run(
    request: Request, run_id: Annotated[uuid.UUID, Path()], principal: identity.Principal = Depends(current_principal)
) -> Response:
    access = await _resolve(request, principal, "sys_resolve_run_workspace", run_id)
    await authorize(request, access, "run:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        row = (await conn.execute(text(_RUN_SELECT + " WHERE r.id = :id"), {"id": run_id})).mappings().first()
        if row is None:
            raise ProblemError("not_found")
        counts: dict[str, int] = dict(
            (
                await conn.execute(
                    text(
                        "SELECT status, count(*) FROM tasks WHERE run_id = :id AND superseded_at IS NULL GROUP BY status"
                    ),
                    {"id": run_id},
                )
            ).all()
        )
        pending = (
            await conn.execute(
                text("SELECT count(*) FROM approvals WHERE run_id = :id AND status = 'PENDING'"), {"id": run_id}
            )
        ).scalar_one()
    snapshot = row["policy_snapshot"] or {}
    body = {
        **_run_summary(dict(row)),
        "repository_id": row["repository_id"],
        "base_commit_sha": row["base_commit_sha"],
        "approval_policy": row["approval_policy"],
        "plan_version": row["plan_version"],
        "routing": snapshot.get("routing", {}),
        "task_counts": {k: int(v) for k, v in counts.items()},
        "pending_approvals": int(pending),
        "updated_at": row["updated_at"],
        "links": {"tasks": f"/api/v1/runs/{run_id}/tasks", "events": f"/api/v1/runs/{run_id}/events"},
    }
    tag = etag(row["version"])
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return json_response(body, headers={"ETag": tag})


@router.get("/runs/{run_id}/tasks", **op("getRunTasks", "run:read"))
async def get_run_tasks(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    status: str | None = None,
    agent_role: str | None = None,
    limit: Limit = 200,
    cursor: Cursor = None,
) -> Response:
    access = await _resolve(request, principal, "sys_resolve_run_workspace", run_id)
    await authorize(request, access, "run:read")
    state = app_state(request)
    filters = {"r": "tasks", "run": str(run_id), "s": status, "role": agent_role}
    clauses = ["t.run_id = :run", "t.superseded_at IS NULL"]
    params: dict[str, Any] = {"run": run_id, "lim": limit + 1}
    if status:
        clauses.append("t.status = :s")
        params["s"] = status
    if agent_role:
        clauses.append("t.agent_role = :role")
        params["role"] = agent_role
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        clauses.append("(t.created_at, t.id) > (:ct, :cid)")
        params.update(ct=datetime.fromisoformat(t), cid=uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        run = (await conn.execute(text("SELECT plan_version FROM runs WHERE id = :id"), {"id": run_id})).scalar_one()
        rows = [
            dict(r)
            for r in (
                await conn.execute(
                    text(
                        "SELECT t.*, (SELECT a.provider FROM task_attempts a WHERE a.task_id = t.id ORDER BY a.attempt_no DESC"  # noqa: S608 — условия из фиксированного списка, значения — bind
                        " LIMIT 1) AS current_provider FROM tasks t WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY t.created_at, t.id LIMIT :lim"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        ]
        page = rows[:limit]
        ids = [r["id"] for r in page]
        edges = [
            dict(e)
            for e in (
                await conn.execute(
                    text(
                        "SELECT task_id, depends_on_task_id, dependency_type FROM task_dependencies WHERE task_id = ANY(:ids)"
                    ),
                    {"ids": ids},
                )
            )
            .mappings()
            .all()
        ]
    deps: dict[uuid.UUID, list[uuid.UUID]] = {}
    for e in edges:
        deps.setdefault(e["task_id"], []).append(e["depends_on_task_id"])
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response(
        {
            "run_id": run_id,
            "plan_version": run,
            "tasks": [_task_summary(r, deps) for r in page],
            "edges": edges,
            "next_cursor": next_cursor,
        }
    )


def _task_summary(row: dict[str, Any], deps: dict[uuid.UUID, list[uuid.UUID]]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "agent_role": row["agent_role"],
        "title": row["title"],
        "status": row["status"],
        "blocked_reason": row["blocked_reason"],
        "complexity": f"L{row['complexity']}",
        "risk_level": row["risk_level"],
        "current_provider": row.get("current_provider"),
        "attempt_count": row["attempt_count"],
        "max_attempts": row["max_attempts"],
        "depends_on": deps.get(row["id"], []),
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


async def _run_command(
    request: Request,
    run_id: uuid.UUID,
    principal: identity.Principal,
    command: Literal["pause", "resume", "cancel"],
    body: CommandBody | None,
) -> Response:
    access = await _resolve(request, principal, "sys_resolve_run_workspace", run_id)
    await authorize(request, access, "run:control")
    expected = parse_if_match(request.headers.get("if-match"))

    async def do(conn: AsyncConnection) -> CommandResult:
        state = await runs_service.command(
            conn,
            run_id=run_id,
            workspace_id=access.workspace_id,
            actor_id=principal.user_id,
            command=command,
            expected_version=expected,
            reason=body.reason if body else None,
        )
        return CommandResult(
            202,
            {
                "run_id": run_id,
                "command": command,
                "status": state.status,
                "version": state.version,
                "accepted_at": utcnow(),
            },
            {"ETag": etag(state.version)},
        )

    return await run_idempotent(request, access.db(), {"command": command, **(body.model_dump() if body else {})}, do)


@router.post("/runs/{run_id}/pause", status_code=202, **op("pauseRun", "run:control"))
async def pause_run(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    body: CommandBody | None = None,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    return await _run_command(request, run_id, principal, "pause", body)


@router.post("/runs/{run_id}/resume", status_code=202, **op("resumeRun", "run:control"))
async def resume_run(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    body: CommandBody | None = None,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    return await _run_command(request, run_id, principal, "resume", body)


@router.post("/runs/{run_id}/cancel", status_code=202, **op("cancelRun", "run:control"))
async def cancel_run(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    body: CommandBody | None = None,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    return await _run_command(request, run_id, principal, "cancel", body)


@router.get("/tasks/{task_id}", **op("getTask", "run:read"))
async def get_task(
    request: Request, task_id: Annotated[uuid.UUID, Path()], principal: identity.Principal = Depends(current_principal)
) -> Response:
    access = await _resolve(request, principal, "sys_resolve_task_workspace", task_id)
    await authorize(request, access, "run:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT t.*, (SELECT a.provider FROM task_attempts a WHERE a.task_id = t.id ORDER BY a.attempt_no DESC"
                        " LIMIT 1) AS current_provider FROM tasks t WHERE t.id = :id"
                    ),
                    {"id": task_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ProblemError("not_found")
        deps = list(
            (
                await conn.execute(
                    text("SELECT depends_on_task_id FROM task_dependencies WHERE task_id = :id"), {"id": task_id}
                )
            )
            .scalars()
            .all()
        )
    data = dict(row)
    body = {
        **_task_summary(data, {task_id: deps}),
        "instructions": data["instructions"],
        "acceptance_criteria": list(data["acceptance_criteria"] or []),
        "review_loop_count": data["review_loop_count"],
        "next_attempt_at": data["next_attempt_at"],
        "timeout_seconds": data["timeout_seconds"],
        "created_at": data["created_at"],
        "links": {
            "run": f"/api/v1/runs/{data['run_id']}",
            "attempts": f"/api/v1/tasks/{task_id}/attempts",
            "messages": f"/api/v1/tasks/{task_id}/messages",
        },
    }
    return json_response(body, headers={"ETag": etag(data["version"])})


@router.get("/tasks/{task_id}/attempts", **op("listTaskAttempts", "run:read"))
async def list_attempts(
    request: Request,
    task_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await _resolve(request, principal, "sys_resolve_task_workspace", task_id)
    await authorize(request, access, "run:read")
    state = app_state(request)
    filters = {"r": "attempts", "t": str(task_id)}
    after = 0
    if cursor:
        after = int(decode_cursor(state.secret_key, cursor, filters)[0])
    async with transaction(state.engine, access.db()) as conn:
        rows = [
            dict(r)
            for r in (
                await conn.execute(
                    text(
                        "SELECT a.*, t.agent_role, rd.reason AS routing_reason FROM task_attempts a"
                        " JOIN tasks t ON t.id = a.task_id"
                        " LEFT JOIN routing_decisions rd ON rd.id = a.routing_decision_id"
                        " WHERE a.task_id = :t AND a.attempt_no > :after ORDER BY a.attempt_no LIMIT :lim"
                    ),
                    {"t": task_id, "after": after, "lim": limit + 1},
                )
            )
            .mappings()
            .all()
        ]
        page = rows[:limit]
        execs = [
            dict(e)
            for e in (
                await conn.execute(
                    text(
                        "SELECT id, task_attempt_id, status, exit_code, failure_category, output_artifact_id, started_at,"
                        " finished_at FROM cli_executions WHERE task_attempt_id = ANY(:ids) ORDER BY created_at"
                    ),
                    {"ids": [r["id"] for r in page]},
                )
            )
            .mappings()
            .all()
        ]
    by_attempt: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for e in execs:
        attempt = e.pop("task_attempt_id")
        by_attempt.setdefault(attempt, []).append(e)
    items = [
        {
            "id": r["id"],
            "task_id": r["task_id"],
            "attempt_no": r["attempt_no"],
            "status": r["status"],
            "provider": r["provider"],
            "agent_role": r["agent_role"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "failure_category": r["failure_category"],
            "executions": by_attempt.get(r["id"], []),
            "routing_reason": r["routing_reason"],
        }
        for r in page
    ]
    next_cursor = encode_cursor(state.secret_key, [page[-1]["attempt_no"]], filters) if len(rows) > limit else None
    return json_response({"items": items, "next_cursor": next_cursor})


AcpMessageType = Literal[
    "TASK_ASSIGN", "HANDOFF", "QUESTION", "ANSWER", "REVIEW_REQUEST", "FINDING", "FIX_REQUEST", "EVIDENCE_SUBMITTED",
    "DECISION_PROPOSAL", "DECISION_RECORDED", "BLOCKED", "TASK_COMPLETED",
]  # fmt: skip


def _acp_message(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": r["id"],
        "protocol": r["protocol"],
        "run_id": r["run_id"],
        "task_id": r["task_id"],
        "task_attempt_id": r["task_attempt_id"],
        "from": {"role": r["sender_role"], "provider": r["sender_provider"], "user_id": r["sender_user_id"]},
        "to": {"role": r["recipient_role"], "task_id": r["recipient_task_id"]},
        "type": r["message_type"],
        "correlation_id": r["correlation_id"],
        "reply_to_id": r["reply_to_id"],
        "schema_version": r["schema_version"],
        # Payload — недоверенные данные агента (ACP §8): отдаются как данные, секреты редактируются на чтении.
        "payload": redact_value(r["payload"]),
        "created_at": r["created_at"],
    }


@router.get("/tasks/{task_id}/messages", **op("listTaskMessages", "run:read"))
async def list_task_messages(
    request: Request,
    task_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    type: Annotated[list[AcpMessageType] | None, Query()] = None,  # noqa: A002 — имя query-параметра из контракта
    correlation_id: uuid.UUID | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    """ACP-сообщения задачи (P4-12): отправленные из задачи (`task_id`) и адресованные ей (`to.task_id`)."""
    access = await _resolve(request, principal, "sys_resolve_task_workspace", task_id)
    await authorize(request, access, "run:read")
    state = app_state(request)
    filters = {"r": "task_messages", "t": str(task_id), "ty": sorted(type or []), "c": str(correlation_id)}
    clauses = ["(m.task_id = :t OR m.recipient_task_id = :t)"]
    params: dict[str, Any] = {"t": task_id, "lim": limit + 1}
    if type:
        clauses.append("m.message_type::text = ANY(:types)")
        params["types"] = list(type)
    if correlation_id:
        clauses.append("m.correlation_id = :corr")
        params["corr"] = correlation_id
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        clauses.append("(m.created_at, m.id) > (:ct, :cid)")
        params.update(ct=datetime.fromisoformat(t), cid=uuid.UUID(i))
    sql = (
        "SELECT m.id, m.protocol, m.run_id, m.task_id, m.task_attempt_id, m.sender_role,"  # noqa: S608 — условия из фиксированного списка, значения — bind
        " m.sender_provider::text AS sender_provider, m.sender_user_id, m.recipient_role, m.recipient_task_id,"
        " m.message_type::text AS message_type, m.correlation_id, m.reply_to_id, m.schema_version, m.payload,"
        " m.created_at FROM agent_messages m WHERE " + " AND ".join(clauses) + " ORDER BY m.created_at, m.id LIMIT :lim"
    )
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [_acp_message(r) for r in page], "next_cursor": next_cursor})
