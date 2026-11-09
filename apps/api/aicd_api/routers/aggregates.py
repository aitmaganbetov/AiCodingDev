"""Агрегаты workspace (UI redesign B1; UI_REDESIGN_PLAN §3.10 G-01…G-04): read-модели существующих таблиц.

* `GET /tasks` — задачи workspace (актуальные задачи плана) с последней попыткой: роль × провайдер × run × задача
  («Running AI Agents» — фильтр `status=LEASED&status=RUNNING`); keyset по `(created_at, id)` desc;
* `GET /findings` — находки всех run workspace (severity, статус, источник, проект, run); порядок и курсор —
  как у `GET /runs/{run_id}/findings` (severity desc, created_at, id); свободный текст редактируется;
* `GET /providers/{provider_id}/executions` — история `cli_executions` провайдера в workspace: статус, код выхода,
  длительность, категория ошибки, ссылки на задачу/run; без вывода CLI, команд, путей и учётных данных;
* `GET /activity` — лента `run_event_log` workspace (новые сверху, keyset `(created_at, event_id)` desc; индекс
  `ix_run_event_log_ws_created`, миграция 0017), фильтр по типам событий, run и проекту.

Tenant — заголовок `X-Workspace-Id` (не член ⇒ 404, AC-12); RLS — второй барьер (строки других workspace
невидимы). Ссылки на чужие `run_id`/`project_id`/`task_id` в фильтрах дают пустую выборку, а не ошибку.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy import text
from starlette.responses import Response

from aicd_api.deps import app_state, authorize, current_principal, parse_workspace_header, workspace_access
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_api.routers.events import _activity_item
from aicd_api.routers.quality import FINDING_SELECT, SEVERITY_ORDER, finding_view
from aicd_api.routers.runs import task_summary
from aicd_cli_adapters import registry
from aicd_identity import api as identity
from aicd_platform.db import transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter()

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
MAX_FILTER_VALUES = 20

TaskStatus = Literal[
    "PENDING", "READY", "LEASED", "RUNNING", "VERIFYING", "SUCCEEDED", "BLOCKED", "RETRY_WAIT", "FAILED", "CANCELLED"
]  # fmt: skip
AgentRole = Literal[
    "requirements", "architect", "planner", "developer", "reviewer", "security", "qa", "database", "performance",
    "devops", "maintenance", "documentation",
]  # fmt: skip
CliProvider = Literal["codex", "claude"]
Severity = Literal["info", "low", "medium", "high", "critical"]
FindingStatus = Literal["OPEN", "ACKNOWLEDGED", "FIXED", "VERIFIED_FIXED", "FALSE_POSITIVE", "ACCEPTED_RISK"]
FindingSource = Literal["gate", "agent_review", "manual", "github"]
ExecutionStatus = Literal[
    "QUEUED", "STARTING", "RUNNING", "EXITED", "COLLECTED", "TIMED_OUT", "CANCELLED", "CRASHED", "AUTH_REQUIRED",
    "RATE_LIMITED",
]  # fmt: skip
# Типы событий, которые проецируются в run_event_log (домен sse_event_type_persisted_t; heartbeat не хранится).
ActivityEventType = Literal[
    "run.state_changed", "task.state_changed", "agent.message", "cli.output", "directive.impact_ready",
    "directive.state_changed", "approval.requested", "approval.decided", "gate.completed", "run.completed",
    "chat.message", "review.comment",
]  # fmt: skip


def _multi(values: list[Any] | None) -> list[str]:
    """Повторяемый фильтр: не больше MAX_FILTER_VALUES значений, порядок и дубли не важны."""
    if not values:
        return []
    if len(values) > MAX_FILTER_VALUES:
        raise ProblemError("validation_error", detail=f"Не больше {MAX_FILTER_VALUES} значений фильтра")
    return sorted({str(v) for v in values})


def _keyset_desc(state_key: bytes, cursor: str, filters: dict[str, Any]) -> tuple[datetime, uuid.UUID]:
    key = decode_cursor(state_key, cursor, filters)
    try:
        return datetime.fromisoformat(str(key[0])), uuid.UUID(str(key[1]))
    except (IndexError, TypeError, ValueError) as exc:
        raise ProblemError("invalid_cursor") from exc


# --- GET /tasks ------------------------------------------------------------------------------------------

_TASKS_SELECT = (
    "SELECT t.*, r.project_id, r.status AS run_status, la.id AS att_id, la.attempt_no AS att_no,"
    " la.status AS att_status, la.provider::text AS current_provider, la.started_at AS att_started_at,"
    " la.finished_at AS att_finished_at"
    " FROM tasks t JOIN runs r ON r.id = t.run_id"
    " LEFT JOIN LATERAL (SELECT a.id, a.attempt_no, a.status, a.provider, a.started_at, a.finished_at"
    " FROM task_attempts a WHERE a.task_id = t.id ORDER BY a.attempt_no DESC LIMIT 1) la ON true"
)


def _workspace_task(row: dict[str, Any], deps: dict[uuid.UUID, list[uuid.UUID]]) -> dict[str, Any]:
    attempt = None
    if row["att_id"] is not None:
        attempt = {
            "id": row["att_id"],
            "attempt_no": row["att_no"],
            "status": row["att_status"],
            "provider": row["current_provider"],
            "started_at": row["att_started_at"],
            "finished_at": row["att_finished_at"],
        }
    return {
        **task_summary(row, deps),
        "project_id": row["project_id"],
        "run_status": row["run_status"],
        "created_at": row["created_at"],
        "current_attempt": attempt,
    }


@router.get("/tasks", tags=["tasks"], **op("listTasks", "run:read"))
async def list_tasks(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    status: Annotated[list[TaskStatus] | None, Query()] = None,
    agent_role: Annotated[list[AgentRole] | None, Query()] = None,
    provider: Annotated[list[CliProvider] | None, Query()] = None,
    run_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "run:read")
    state = app_state(request)
    statuses, roles, providers = _multi(status), _multi(agent_role), _multi(provider)
    filters = {
        "r": "ws_tasks", "ws": str(access.workspace_id), "s": statuses, "role": roles, "p": providers,
        "run": str(run_id) if run_id else None, "proj": str(project_id) if project_id else None,
    }  # fmt: skip
    clauses = ["t.workspace_id = app_current_workspace()", "t.superseded_at IS NULL"]
    params: dict[str, Any] = {"lim": limit + 1}
    if statuses:
        clauses.append("t.status = ANY(:s)")
        params["s"] = statuses
    if roles:
        clauses.append("t.agent_role = ANY(:roles)")
        params["roles"] = roles
    if providers:
        clauses.append("la.provider::text = ANY(:prov)")
        params["prov"] = providers
    if run_id:
        clauses.append("t.run_id = :run")
        params["run"] = run_id
    if project_id:
        clauses.append("r.project_id = :proj")
        params["proj"] = project_id
    if cursor:
        ct, cid = _keyset_desc(state.secret_key, cursor, filters)
        clauses.append("(t.created_at, t.id) < (:ct, :cid)")
        params.update(ct=ct, cid=cid)
    sql = _TASKS_SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY t.created_at DESC, t.id DESC LIMIT :lim"
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
        page = rows[:limit]
        edges = (
            await conn.execute(
                text("SELECT task_id, depends_on_task_id FROM task_dependencies WHERE task_id = ANY(:ids)"),
                {"ids": [r["id"] for r in page]},
            )
        ).all()
    deps: dict[uuid.UUID, list[uuid.UUID]] = {}
    for task, depends_on in edges:
        deps.setdefault(task, []).append(depends_on)
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [_workspace_task(r, deps) for r in page], "next_cursor": next_cursor})


# --- GET /findings ---------------------------------------------------------------------------------------


@router.get("/findings", tags=["quality"], **op("listFindings", "quality:read"))
async def list_findings(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    severity: Annotated[list[Severity] | None, Query()] = None,
    status: Annotated[list[FindingStatus] | None, Query()] = None,
    source: Annotated[list[FindingSource] | None, Query()] = None,
    project_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "quality:read")
    state = app_state(request)
    severities, statuses, sources = _multi(severity), _multi(status), _multi(source)
    filters = {
        "r": "ws_findings", "ws": str(access.workspace_id), "sev": severities, "st": statuses, "src": sources,
        "proj": str(project_id) if project_id else None, "run": str(run_id) if run_id else None,
    }  # fmt: skip
    clauses = ["f.workspace_id = app_current_workspace()"]
    params: dict[str, Any] = {"lim": limit + 1}
    if severities:
        clauses.append("f.severity::text = ANY(:sev)")
        params["sev"] = severities
    if statuses:
        clauses.append("f.status::text = ANY(:st)")
        params["st"] = statuses
    if sources:
        clauses.append("f.source = ANY(:src)")
        params["src"] = sources
    if run_id:
        clauses.append("f.run_id = :run")
        params["run"] = run_id
    if project_id:
        clauses.append("f.run_id IN (SELECT r.id FROM runs r WHERE r.project_id = :proj)")
        params["proj"] = project_id
    if cursor:
        key = decode_cursor(state.secret_key, cursor, filters)
        try:
            params.update(kr=int(key[0]), kt=datetime.fromisoformat(str(key[1])), kid=uuid.UUID(str(key[2])))
        except (IndexError, TypeError, ValueError) as exc:
            raise ProblemError("invalid_cursor") from exc
        clauses.append(f"({SEVERITY_ORDER}, f.created_at, f.id) > (:kr, :kt, :kid)")
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


# --- GET /providers/{provider_id}/executions -------------------------------------------------------------

_EXECUTIONS_SELECT = (
    "SELECT e.id, e.installation_id, e.task_attempt_id, a.attempt_no, a.task_id, t.run_id, t.agent_role,"
    " e.status::text AS status, e.exit_code, e.failure_category::text AS failure_category, e.timeout_seconds,"
    " e.output_artifact_id, e.created_at, e.started_at, e.finished_at,"
    " (EXTRACT(EPOCH FROM COALESCE(e.exited_at, e.finished_at) - e.started_at) * 1000)::bigint AS duration_ms"
    " FROM cli_executions e"
    " JOIN cli_installations i ON i.id = e.installation_id"
    " JOIN task_attempts a ON a.id = e.task_attempt_id"
    " JOIN tasks t ON t.id = a.task_id"
)


def _execution_view(row: dict[str, Any], provider: str) -> dict[str, Any]:
    duration = row["duration_ms"]
    return {
        "id": row["id"],
        "provider": provider,
        "installation_id": row["installation_id"],
        "task_attempt_id": row["task_attempt_id"],
        "attempt_no": row["attempt_no"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "agent_role": row["agent_role"],
        "status": row["status"],
        "exit_code": row["exit_code"],
        "failure_category": row["failure_category"],
        "timeout_seconds": row["timeout_seconds"],
        "output_artifact_id": row["output_artifact_id"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_ms": max(int(duration), 0) if duration is not None else None,
        "links": {"task": f"/api/v1/tasks/{row['task_id']}", "run": f"/api/v1/runs/{row['run_id']}"},
    }


@router.get("/providers/{provider_id}/executions", tags=["providers"], **op("listProviderExecutions", "provider:read"))
async def list_provider_executions(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    status: Annotated[list[ExecutionStatus] | None, Query()] = None,
    run_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:read")
    state = app_state(request)
    statuses = _multi(status)
    filters = {
        "r": "provider_executions", "ws": str(access.workspace_id), "prov": str(provider_id), "s": statuses,
        "run": str(run_id) if run_id else None, "task": str(task_id) if task_id else None,
        "ca": created_after.isoformat() if created_after else None,
        "cb": created_before.isoformat() if created_before else None,
    }  # fmt: skip
    clauses = ["e.workspace_id = app_current_workspace()", "i.provider_id = :prov"]
    params: dict[str, Any] = {"prov": provider_id, "lim": limit + 1}
    if statuses:
        clauses.append("e.status::text = ANY(:s)")
        params["s"] = statuses
    if run_id:
        clauses.append("t.run_id = :run")
        params["run"] = run_id
    if task_id:
        clauses.append("a.task_id = :task")
        params["task"] = task_id
    if created_after:
        clauses.append("e.created_at >= :ca")
        params["ca"] = created_after
    if created_before:
        clauses.append("e.created_at < :cb")
        params["cb"] = created_before
    if cursor:
        ct, cid = _keyset_desc(state.secret_key, cursor, filters)
        clauses.append("(e.created_at, e.id) < (:ct, :cid)")
        params.update(ct=ct, cid=cid)
    sql = _EXECUTIONS_SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY e.created_at DESC, e.id DESC LIMIT :lim"
    async with transaction(state.engine, access.db()) as conn:
        code = await registry.provider_code(conn, provider_id)
        if code is None:
            raise ProblemError("not_found")
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [_execution_view(r, code) for r in page], "next_cursor": next_cursor})


# --- GET /activity ---------------------------------------------------------------------------------------


@router.get("/activity", tags=["runs"], **op("listActivity", "run:read"))
async def list_activity(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    event_type: Annotated[list[ActivityEventType] | None, Query()] = None,
    run_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "run:read")
    state = app_state(request)
    types = _multi(event_type)
    filters = {
        "r": "ws_activity", "ws": str(access.workspace_id), "t": types,
        "run": str(run_id) if run_id else None, "proj": str(project_id) if project_id else None,
    }  # fmt: skip
    clauses = ["l.workspace_id = app_current_workspace()"]
    params: dict[str, Any] = {"lim": limit + 1}
    if types:
        clauses.append("l.event_type = ANY(:types)")
        params["types"] = types
    if run_id:
        clauses.append("l.run_id = :run")
        params["run"] = run_id
    if project_id:
        clauses.append("l.run_id IN (SELECT r.id FROM runs r WHERE r.project_id = :proj)")
        params["proj"] = project_id
    if cursor:
        ct, cid = _keyset_desc(state.secret_key, cursor, filters)
        clauses.append("(l.created_at, l.event_id) < (:ct, :cid)")
        params.update(ct=ct, cid=cid)
    sql = (
        "SELECT l.run_id, l.seq, l.event_id, l.event_type, l.schema_version, l.task_id, l.payload, l.occurred_at,"  # noqa: S608 — условия из фиксированного списка, значения — bind
        " l.created_at FROM run_event_log l WHERE "
        + " AND ".join(clauses)
        + " ORDER BY l.created_at DESC, l.event_id DESC LIMIT :lim"
    )
    async with transaction(state.engine, access.db()) as conn:
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["event_id"])], filters)
    return json_response({"items": [_activity_item(r) for r in page], "next_cursor": next_cursor})
