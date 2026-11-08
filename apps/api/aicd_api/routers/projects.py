"""Проекты и репозитории (ТЗ §8)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from sqlalchemy import text
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
from aicd_code_intelligence.index.jobs import enqueue_indexing_job
from aicd_identity import api as identity
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_projects import api as projects
from aicd_projects import explorer

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
    project_id: Annotated[uuid.UUID, PathParam()],
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
    project_id: Annotated[uuid.UUID, PathParam()],
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
    project_id: Annotated[uuid.UUID, PathParam()],
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
    project_id: Annotated[uuid.UUID, PathParam()],
    body: RepositoryAttachRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "repository:attach")
    if body.provider == "github":
        # FR-PRJ-03 (P8-02): репозиторий подтверждается через установку GitHub App workspace.
        from aicd_api.routers import github as github_routes

        return await github_routes.attach_github_repository(
            request,
            access,
            project_id,
            remote_url=body.remote_url,
            local_path=body.local_path,
            default_branch=body.default_branch,
            payload=body.model_dump(),
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
        # Контракт attachRepository: «ставит indexing_job (асинхронно)» — HEAD commit на момент привязки;
        # исполняет `aicd-worker index` (пустой репозиторий без commit — нечего индексировать).
        head = (
            await conn.execute(
                text("SELECT last_synced_commit_sha FROM repositories WHERE id = :id"), {"id": row["id"]}
            )
        ).scalar()
        if head:
            await enqueue_indexing_job(
                conn, workspace_id=access.workspace_id, repository_id=row["id"], commit_sha=head, kind="full"
            )
        return CommandResult(
            201, views.repository(row), {"Location": f"/api/v1/projects/{project_id}/repositories/{row['id']}"}
        )

    return await run_idempotent(request, access.db(), body.model_dump(), command)


# --- Repository Explorer (FR-PRJ-05) ---------------------------------------------------------

Commit = Annotated[str | None, Query(min_length=1, max_length=255)]


async def _explorer_repo(
    request: Request, principal: identity.Principal, project_id: uuid.UUID, repository_id: uuid.UUID
) -> tuple[explorer.ExplorerRepository, Path]:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    state = app_state(request)
    # Транзакция только на чтение строки репозитория (RLS); git — вне транзакции и вне event loop.
    async with transaction(state.engine, access.db()) as conn:
        repo = await explorer.get_explorer_repository(conn, project_id, repository_id)
    return repo, explorer.repository_path(repo, state.settings.repository_roots)


@router.get("/{project_id}/repositories/{repository_id}/tree", **op("listRepositoryTree", "project:read"))
async def list_repository_tree(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    repository_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
    commit: Commit = None,
    path: Annotated[str | None, Query(max_length=4096)] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    repo, repo_path = await _explorer_repo(request, principal, project_id, repository_id)
    rel = explorer.validate_tree_path(path)
    state = app_state(request)
    filters = {"r": "repo_tree", "repo": str(repository_id), "commit": commit or "", "path": rel}
    offset = 0
    if cursor:
        # Курсор фиксирует разрешённый commit: ветка могла сдвинуться между страницами.
        sha, raw_offset = decode_cursor(state.secret_key, cursor, filters)
        commit_sha, offset = str(sha), int(raw_offset)
    else:
        commit_sha = await asyncio.to_thread(explorer.resolve_commit, repo_path, repo, commit)
    items = await asyncio.to_thread(explorer.list_tree, repo_path, commit_sha, rel)
    page = items[offset : offset + limit]
    next_cursor = (
        encode_cursor(state.secret_key, [commit_sha, offset + limit], filters) if offset + limit < len(items) else None
    )
    return json_response(
        {
            "repository_id": repo.id,
            "commit_sha": commit_sha,
            "path": rel,
            "items": [{"name": i.name, "path": i.path, "type": i.type, "mode": i.mode, "size": i.size} for i in page],
            "next_cursor": next_cursor,
        }
    )


@router.get("/{project_id}/repositories/{repository_id}/file", **op("getRepositoryFile", "project:read"))
async def get_repository_file(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    repository_id: Annotated[uuid.UUID, PathParam()],
    path: Annotated[str, Query(min_length=1, max_length=4096)],
    principal: identity.Principal = Depends(current_principal),
    commit: Commit = None,
) -> Response:
    repo, repo_path = await _explorer_repo(request, principal, project_id, repository_id)
    rel = explorer.validate_tree_path(path)
    commit_sha = await asyncio.to_thread(explorer.resolve_commit, repo_path, repo, commit)
    body = await asyncio.to_thread(explorer.read_file, repo_path, commit_sha, rel)
    return json_response({"repository_id": repo.id, **body})


@router.get("/{project_id}/repositories/{repository_id}/branches", **op("listRepositoryBranches", "project:read"))
async def list_repository_branches(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    repository_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    """Ветки локальной копии (OQ-45 (c)): ветка по умолчанию первой; курсор — смещение в снимке списка."""
    repo, repo_path = await _explorer_repo(request, principal, project_id, repository_id)
    state = app_state(request)
    filters = {"r": "repo_branches", "repo": str(repository_id)}
    offset = 0
    if cursor:
        (raw_offset,) = decode_cursor(state.secret_key, cursor, filters)
        offset = int(raw_offset)
    items, truncated = await asyncio.to_thread(explorer.list_branches, repo_path, repo.default_branch)
    page = items[offset : offset + limit]
    next_cursor = encode_cursor(state.secret_key, [offset + limit], filters) if offset + limit < len(items) else None
    return json_response(
        {
            "repository_id": repo.id,
            "default_branch": repo.default_branch,
            "truncated": truncated,
            "items": [
                {"name": b.name, "commit_sha": b.commit_sha, "committed_at": b.committed_at, "is_default": b.is_default}
                for b in page
            ],
            "next_cursor": next_cursor,
        }
    )


@router.get("/{project_id}/repositories/{repository_id}/commits", **op("listRepositoryCommits", "project:read"))
async def list_repository_commits(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    repository_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
    commit: Commit = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    """История commit (OQ-45 (c)); курсор фиксирует разрешённый SHA и смещение (глубина ≤ MAX_HISTORY_DEPTH)."""
    repo, repo_path = await _explorer_repo(request, principal, project_id, repository_id)
    state = app_state(request)
    filters = {"r": "repo_commits", "repo": str(repository_id), "commit": commit or ""}
    offset = 0
    if cursor:
        sha, raw_offset = decode_cursor(state.secret_key, cursor, filters)
        commit_sha, offset = str(sha), int(raw_offset)
    else:
        commit_sha = await asyncio.to_thread(explorer.resolve_commit, repo_path, repo, commit)
    items, more = await asyncio.to_thread(explorer.list_commits, repo_path, commit_sha, offset, limit)
    next_cursor = encode_cursor(state.secret_key, [commit_sha, offset + limit], filters) if more else None
    return json_response(
        {"repository_id": repo.id, "commit_sha": commit_sha, "items": items, "next_cursor": next_cursor}
    )
