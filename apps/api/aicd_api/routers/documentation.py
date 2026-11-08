"""Политика документации и источники документации (ТЗ §8/§12; P5-09, P5-10).

* `GET/PATCH /projects/{project_id}/documentation-policy` — OFF/SMART/STRICT, ETag/If-Match по
  `project_settings.version` (поле `settings_version`), аудит `project.documentation_policy.update`.
* `GET/POST /documentation/sources` — уровень workspace (`X-Workspace-Id`); регистрация НЕ загружает.
* `POST /documentation/sources/{source_id}/refresh` — явный запрос человека = согласие на загрузку
  (approval не создаётся, API_DESIGN §19 п.7); `external_actions(PLANNED)` через SECURITY DEFINER
  `sys_plan_docs_download`; загрузка — worker-index (очередь `docs`) через `security.ssrf`.
Чужие ресурсы ⇒ 404 (AC-12).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
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
from aicd_api.idempotency import CommandResult, require_key, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_code_intelligence.docs import sources
from aicd_identity import api as identity
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(tags=["documentation"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
DocumentationMode = Literal["OFF", "SMART", "STRICT"]


class DocumentationPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documentation_mode: DocumentationMode


class DocumentationSourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    technology_version_id: uuid.UUID
    source_type: Literal["url", "file", "git"]
    uri_or_file_ref: Annotated[str, Field(min_length=1, max_length=2048)]
    trust_level: Literal["official", "internal", "community"]
    license_info: Annotated[str, Field(max_length=2000)] | None = None
    refresh_policy: Literal["manual", "scheduled"] = "manual"
    enabled: bool = True


# --- Политика -----------------------------------------------------------------------------------


@router.get("/projects/{project_id}/documentation-policy", **op("getDocumentationPolicy", "project:read"))
async def get_documentation_policy(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        row = await sources.get_policy(conn, project_id)
    if row is None:
        raise ProblemError("not_found")
    tag = etag(row["version"])
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return json_response(sources.policy_view(row), headers={"ETag": tag})


@router.patch("/projects/{project_id}/documentation-policy", **op("updateDocumentationPolicy", "project:update"))
async def update_documentation_policy(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    body: DocumentationPolicyUpdateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:update")
    expected = require_if_match(request.headers.get("if-match"))
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        row = await sources.update_policy(
            conn,
            project_id=project_id,
            workspace_id=access.workspace_id,
            actor_id=principal.user_id,
            expected_version=expected,
            mode=body.documentation_mode,
        )
    return json_response(sources.policy_view(row), headers={"ETag": etag(row["version"])})


# --- Источники ----------------------------------------------------------------------------------


@router.get("/documentation/sources", **op("listDocumentationSources", "documentation:read"))
async def list_documentation_sources(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    technology_version_id: uuid.UUID | None = None,
    enabled: bool | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "documentation:read")
    state = app_state(request)
    filters = {"r": "doc_sources", "ws": str(access.workspace_id), "tv": technology_version_id, "en": enabled}
    before = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        before = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        rows = await sources.list_sources(
            conn, technology_version_id=technology_version_id, enabled=enabled, before=before, limit=limit + 1
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [sources.source_view(r) for r in page], "next_cursor": next_cursor})


@router.post("/documentation/sources", status_code=201, **op("registerDocumentationSource", "documentation:register"))
async def register_documentation_source(
    request: Request,
    body: DocumentationSourceCreateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "documentation:register")

    async def command(conn: AsyncConnection) -> CommandResult:
        row = await sources.create_source(
            conn,
            workspace_id=access.workspace_id,
            actor_id=principal.user_id,
            technology_version_id=body.technology_version_id,
            source_type=body.source_type,
            uri=body.uri_or_file_ref,
            trust_level=body.trust_level,
            license_info=body.license_info,
            refresh_policy=body.refresh_policy,
            enabled=body.enabled,
        )
        return CommandResult(
            201,
            sources.source_view(row),
            {"Location": f"/api/v1/documentation/sources/{row['id']}"},
        )

    return await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)


async def _source_access(request: Request, principal: identity.Principal, source_id: uuid.UUID) -> Access:
    """Workspace источника среди workspace пользователя (RLS: виден только в своём workspace); иначе 404.
    `X-Workspace-Id`, если передан, сужает поиск и обязан совпасть."""
    state = app_state(request)
    header = request.headers.get("x-workspace-id")
    async with transaction(state.engine, DbContext(user_id=principal.user_id)) as conn:
        memberships = await identity.list_memberships(conn)
    for m in memberships:
        ws = m["workspace_id"]
        if header is not None and header != str(ws):
            continue
        async with transaction(state.engine, DbContext(user_id=principal.user_id, workspace_id=ws)) as conn:
            if await sources.source_workspace(conn, source_id) == ws:
                return await workspace_access(request, principal, ws)
    raise ProblemError("not_found")


@router.post(
    "/documentation/sources/{source_id}/refresh",
    status_code=202,
    **op("refreshDocumentationSource", "documentation:refresh"),
)
async def refresh_documentation_source(
    request: Request,
    source_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await _source_access(request, principal, source_id)
    await authorize(request, access, "documentation:refresh")
    key = require_key(request)

    async def command(conn: AsyncConnection) -> CommandResult:
        source = await sources.get_source(conn, source_id)
        if source is None:
            raise ProblemError("not_found")
        action_id = await sources.request_refresh(
            conn, source=source, workspace_id=access.workspace_id, actor_id=principal.user_id, idempotency_key=key
        )
        return CommandResult(
            202,
            {"source_id": source_id, "external_action_id": action_id, "status": "QUEUED", "accepted_at": utcnow()},
            {},
        )

    return await run_idempotent(request, access.db(), {"source_id": str(source_id)}, command)
