"""Определённый стек проекта (`GET /projects/{project_id}/technologies`, P5-07, FR-PRJ-04).

Чужой/несуществующий проект ⇒ 404 (AC-12). Стек записывает worker-index (`technologies.store`).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from starlette.responses import Response

from aicd_api.deps import (
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    project_access,
    workspace_access,
)
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_code_intelligence.technologies import store
from aicd_identity import api as identity
from aicd_platform.db import transaction
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(prefix="/projects", tags=["projects"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]


@router.get("/{project_id}/technologies", **op("listTechnologies", "project:read"))
async def list_technologies(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    state = app_state(request)
    filters = {"r": "technologies", "p": str(project_id)}
    after = None
    if cursor:
        e, n, v = decode_cursor(state.secret_key, cursor, filters)
        after = (e, n, uuid.UUID(v))
    async with transaction(state.engine, access.db()) as conn:
        rows = await store.list_project_technologies(conn, project_id, after=after, limit=limit + 1)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            state.secret_key, [last["ecosystem"], last["name"], str(last["technology_version_id"])], filters
        )
    return json_response({"items": [store.view(r) for r in page], "next_cursor": next_cursor})


workspace_router = APIRouter(tags=["documentation"])


@workspace_router.get("/technologies", **op("listWorkspaceTechnologies", "documentation:read"))
async def list_workspace_technologies(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    """Технологии workspace (OQ-45 (c)): стек проектов и источники документации, без чужого каталога."""
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "documentation:read")
    state = app_state(request)
    filters = {"r": "ws_technologies", "ws": str(access.workspace_id), "q": q}
    after = None
    if cursor:
        e, n, v = decode_cursor(state.secret_key, cursor, filters)
        after = (e, n, uuid.UUID(v))
    async with transaction(state.engine, access.db()) as conn:
        rows = await store.list_workspace_technologies(conn, after=after, query=q, limit=limit + 1)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            state.secret_key, [last["ecosystem"], last["name"], str(last["technology_version_id"])], filters
        )
    return json_response({"items": [store.workspace_view(r) for r in page], "next_cursor": next_cursor})
