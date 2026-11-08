"""Workspaces и участники (ТЗ §8; members — дополнение к ТЗ)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api import views
from aicd_api.deps import Access, app_state, authorize, current_principal, workspace_access
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_api.schemas import (
    MemberCreateRequest,
    MemberUpdateRequest,
    WorkspaceCreateRequest,
    WorkspaceUpdateRequest,
)
from aicd_identity import api as identity
from aicd_identity.rbac import Role
from aicd_platform.db import DbContext, apply_context, transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_projects import api as projects

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]


@router.get("", **op("listWorkspaces", "authenticated"))
async def list_workspaces(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    state = app_state(request)
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        rows = await identity.list_memberships(conn)  # сортировка created_at DESC, id DESC
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, {"r": "workspaces"})
        key = (datetime.fromisoformat(t), uuid.UUID(i))
        rows = [r for r in rows if (r["created_at"], r["workspace_id"]) < key]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            state.secret_key, [last["created_at"].isoformat(), str(last["workspace_id"])], {"r": "workspaces"}
        )
    items = [views.workspace({**r, "id": r["workspace_id"]}, r["role"]) for r in page]
    return json_response({"items": items, "next_cursor": next_cursor})


@router.post("", status_code=201, **op("createWorkspace", "workspace:create"))
async def create_workspace(
    request: Request,
    body: WorkspaceCreateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    state = app_state(request)
    if not state.settings.allow_workspace_creation:
        raise ProblemError("policy_denied", detail="Создание workspace отключено администратором")
    workspace_id = uuid7()

    async def command(conn: AsyncConnection) -> CommandResult:
        # RLS: создание разрешено только при app.workspace_id = id новой строки (§19.3).
        await apply_context(conn, DbContext(user_id=principal.user_id, workspace_id=workspace_id))
        row = await projects.create_workspace(
            conn, workspace_id=workspace_id, name=body.name, slug=body.slug, owner_id=principal.user_id
        )
        return CommandResult(
            201,
            views.workspace({**row, "version": 0}, Role.OWNER.value),
            {"Location": f"/api/v1/workspaces/{workspace_id}", "ETag": etag(0)},
        )

    return await run_idempotent(request, DbContext(user_id=principal.user_id), body.model_dump(), command)


# --- Workspace (OQ-45 (f)) ------------------------------------------------------------------


@router.get("/{workspace_id}", **op("getWorkspace", "member:read"))
async def get_workspace(
    request: Request,
    workspace_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, workspace_id)
    await authorize(request, access, "member:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        row = await projects.get_workspace(conn, workspace_id)
    if row is None:
        raise ProblemError("not_found")
    return json_response(views.workspace(row, access.role.value), headers={"ETag": etag(row["version"])})


@router.patch("/{workspace_id}", **op("updateWorkspace", "workspace:update"))
async def update_workspace(
    request: Request,
    workspace_id: Annotated[uuid.UUID, Path()],
    body: WorkspaceUpdateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    """Название — owner/admin; политики (`settings`) — только owner (x-permission-depends-on: settings)."""
    access = await workspace_access(request, principal, workspace_id)
    await authorize(request, access, "workspace:update")
    changes = body.model_dump(exclude_unset=True)
    if "settings" in changes:
        await authorize(request, access, "workspace:policy")
    expected = require_if_match(request.headers.get("if-match"))
    if not changes:
        raise ProblemError("validation_error", errors=[FieldError("/", "min_properties", "нет изменяемых полей")])
    if "name" in changes and changes["name"] is None:
        raise ProblemError("validation_error", errors=[FieldError("/name", "null", "значение обязательно")])
    settings: dict[str, object] | None = None
    if "settings" in changes:
        raw = changes["settings"]
        if not raw:
            raise ProblemError(
                "validation_error", errors=[FieldError("/settings", "min_properties", "нет изменяемых настроек")]
            )
        for key, value in raw.items():
            if value is None:
                raise ProblemError(
                    "validation_error", errors=[FieldError(f"/settings/{key}", "null", "значение обязательно")]
                )
        settings = dict(raw)
    async with transaction(app_state(request).engine, access.db()) as conn:
        row = await projects.update_workspace(
            conn,
            workspace_id=workspace_id,
            actor_id=principal.user_id,
            expected_version=expected,
            name=changes.get("name"),
            settings=settings,
        )
    return json_response(views.workspace(row, access.role.value), headers={"ETag": etag(row["version"])})


# --- Members ------------------------------------------------------------------------------


async def _ws_access(request: Request, workspace_id: uuid.UUID, principal: identity.Principal) -> Access:
    return await workspace_access(request, principal, workspace_id)


@router.get("/{workspace_id}/members", **op("listMembers", "member:read"))
async def list_members(
    request: Request,
    workspace_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await _ws_access(request, workspace_id, principal)
    await authorize(request, access, "member:read")
    state = app_state(request)
    after = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, {"r": "members", "ws": str(workspace_id)})
        after = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        rows = await identity.list_members(conn, after, limit + 1)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            state.secret_key,
            [last["joined_at"].isoformat(), str(last["user_id"])],
            {"r": "members", "ws": str(workspace_id)},
        )
    return json_response({"items": [views.member(r) for r in page], "next_cursor": next_cursor})


@router.post("/{workspace_id}/members", status_code=201, **op("addMember", "member:manage"))
async def add_member(
    request: Request,
    workspace_id: Annotated[uuid.UUID, Path()],
    body: MemberCreateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await _ws_access(request, workspace_id, principal)
    await authorize(request, access, "member:manage")

    async def command(conn: AsyncConnection) -> CommandResult:
        row = await identity.add_member(
            conn, workspace_id=workspace_id, actor_id=principal.user_id, email=str(body.email), role=body.role
        )
        return CommandResult(
            201,
            views.member(row),
            {"Location": f"/api/v1/workspaces/{workspace_id}/members/{row['user_id']}", "ETag": etag(row["version"])},
        )

    return await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)


@router.patch("/{workspace_id}/members/{user_id}", **op("updateMember", "member:manage"))
async def update_member(
    request: Request,
    workspace_id: Annotated[uuid.UUID, Path()],
    user_id: Annotated[uuid.UUID, Path()],
    body: MemberUpdateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await _ws_access(request, workspace_id, principal)
    await authorize(request, access, "member:manage")
    expected = require_if_match(request.headers.get("if-match"))
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        row = await identity.change_member_role(
            conn,
            workspace_id=workspace_id,
            actor_id=principal.user_id,
            user_id=user_id,
            role=body.role,
            expected_version=expected,
        )
    return json_response(views.member(row), headers={"ETag": etag(row["version"])})


@router.delete("/{workspace_id}/members/{user_id}", status_code=204, **op("removeMember", "member:manage"))
async def remove_member(
    request: Request,
    workspace_id: Annotated[uuid.UUID, Path()],
    user_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await _ws_access(request, workspace_id, principal)
    await authorize(request, access, "member:manage")
    expected = require_if_match(request.headers.get("if-match"))
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        await identity.remove_member(
            conn, workspace_id=workspace_id, actor_id=principal.user_id, user_id=user_id, expected_version=expected
        )
    return Response(status_code=204)
