"""Проекты и репозитории (ТЗ §8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api import views
from aicd_api.deps import (
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    project_access,
    workspace_access,
)
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_api.schemas import (
    ProjectCreateRequest,
    ProjectStatus,
    ProjectUpdateRequest,
    RepositoryAttachRequest,
)
from aicd_identity import api as identity
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_projects import api as projects

router = APIRouter(prefix="/projects", tags=["projects"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]


@router.get("", **op("listProjects", "project:read"))
async def list_projects(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    status: ProjectStatus | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "project:read")
    state = app_state(request)
    filters = {"r": "projects", "ws": str(access.workspace_id), "status": status}
    before = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        before = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        rows = await projects.list_projects(conn, status=status, before=before, limit=limit + 1)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [views.project(r) for r in page], "next_cursor": next_cursor})


@router.post("", status_code=201, **op("createProject", "project:create"))
async def create_project(
    request: Request, body: ProjectCreateRequest, principal: identity.Principal = Depends(current_principal)
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "project:create")

    async def command(conn: AsyncConnection) -> CommandResult:
        row = await projects.create_project(
            conn,
            workspace_id=access.workspace_id,
            actor_id=principal.user_id,
            name=body.name,
            description=body.description,
            documentation_mode=body.documentation_mode,
            quality_profile=body.quality_profile,
        )
        return CommandResult(
            201, views.project(row), {"Location": f"/api/v1/projects/{row['id']}", "ETag": etag(row["version"])}
        )

    return await run_idempotent(request, access.db(), body.model_dump(), command)


@router.get("/{project_id}", **op("getProject", "project:read"))
async def get_project(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        row = await projects.get_project(conn, project_id)
    if row is None:
        raise ProblemError("not_found")
    tag = etag(row["version"])
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return json_response(views.project(row), headers={"ETag": tag})


@router.patch("/{project_id}", **op("updateProject", "project:update"))
async def update_project(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    body: ProjectUpdateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:update")
    expected = require_if_match(request.headers.get("if-match"))
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise ProblemError("validation_error", errors=[FieldError("/", "min_properties", "нет изменяемых полей")])
    for field in ("name", "status", "quality_profile"):
        if field in changes and changes[field] is None:
            raise ProblemError("validation_error", errors=[FieldError(f"/{field}", "null", "значение обязательно")])
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        row = await projects.update_project(
            conn,
            project_id=project_id,
            workspace_id=access.workspace_id,
            actor_id=principal.user_id,
            expected_version=expected,
            changes=changes,
        )
    return json_response(views.project(row), headers={"ETag": etag(row["version"])})


@router.get("/{project_id}/repositories", **op("listRepositories", "project:read"))
async def list_repositories(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    state = app_state(request)
    filters = {"r": "repositories", "p": str(project_id)}
    after = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        after = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        rows = await projects.list_repositories(conn, project_id, after, limit + 1)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [views.repository(r) for r in page], "next_cursor": next_cursor})


@router.post("/{project_id}/repositories", status_code=201, **op("attachRepository", "repository:attach"))
async def attach_repository(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    body: RepositoryAttachRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "repository:attach")
    if body.provider == "github":
        # GitHub App и привязка GitHub-репозиториев — Phase 8 (P8-02).
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/provider", "not_supported", "Привязка GitHub-репозиториев появится в Phase 8")],
        )
    if not body.local_path:
        raise ProblemError("validation_error", errors=[FieldError("/local_path", "required", "обязательно для local")])
    state = app_state(request)
    local_path = body.local_path

    async def command(conn: AsyncConnection) -> CommandResult:
        row = await projects.attach_local_repository(
            conn,
            project_id=project_id,
            workspace_id=access.workspace_id,
            actor_id=principal.user_id,
            local_path=local_path,
            default_branch=body.default_branch,
            roots=state.settings.repository_roots,
        )
        return CommandResult(
            201, views.repository(row), {"Location": f"/api/v1/projects/{project_id}/repositories/{row['id']}"}
        )

    return await run_idempotent(request, access.db(), body.model_dump(), command)
