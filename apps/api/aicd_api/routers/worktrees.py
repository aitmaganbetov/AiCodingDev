"""Worktrees проекта (B4, UI_REDESIGN_PLAN §3.10 G-10): `GET /projects/{project_id}/worktrees`, только чтение.

Строки `worktrees` (миграция 0002) репозиториев проекта под RLS workspace: ветка, base/head commit, связь с run и
задачей, статус, время создания/изменения/удаления. Пути не отдаются: ни `path_ref` (относительный путь в корне
worktree хоста), ни `worker_host_id` — расположение на хосте не раскрывается (SECURITY_ARCHITECTURE: пути хоста
не покидают воркер). Чужой/несуществующий проект — 404 (AC-12).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from sqlalchemy import text
from starlette.responses import Response

from aicd_api.deps import app_state, authorize, current_principal, project_access
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_identity import api as identity
from aicd_platform.db import transaction
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(prefix="/projects", tags=["projects"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
WorktreeStatus = Literal["ACTIVE", "LOCKED", "REMOVED", "ORPHANED"]


def worktree_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "repository_id": r["repository_id"],
        "run_id": r["run_id"],
        "task_id": r["task_id"],
        "branch_name": r["branch_name"],
        "base_commit_sha": r["base_commit_sha"],
        "head_commit_sha": r["head_commit_sha"],
        "status": r["status"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "removed_at": r["removed_at"],
    }


@router.get("/{project_id}/worktrees", **op("listProjectWorktrees", "project:read"))
async def list_project_worktrees(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
    repository_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    status: Annotated[list[WorktreeStatus] | None, Query()] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    state = app_state(request)
    statuses = sorted(set(status)) if status else None
    filters = {
        "r": "worktrees",
        "p": str(project_id),
        "repo": str(repository_id) if repository_id else None,
        "run": str(run_id) if run_id else None,
        "status": statuses,
    }
    before = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        before = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        rows = [
            dict(r)
            for r in (
                await conn.execute(
                    text(
                        "SELECT w.id, w.repository_id, w.run_id, w.task_id, w.branch_name, w.base_commit_sha,"
                        "       w.head_commit_sha, w.status, w.created_at, w.updated_at, w.removed_at"
                        "  FROM worktrees w"
                        "  JOIN repositories r ON r.workspace_id = w.workspace_id AND r.id = w.repository_id"
                        " WHERE r.project_id = :p"
                        "   AND (CAST(:repo AS uuid) IS NULL OR w.repository_id = :repo)"
                        "   AND (CAST(:run AS uuid) IS NULL OR w.run_id = :run)"
                        "   AND (CAST(:statuses AS text[]) IS NULL OR w.status = ANY(CAST(:statuses AS text[])))"
                        "   AND (CAST(:bt AS timestamptz) IS NULL"
                        "        OR (w.created_at, w.id) < (CAST(:bt AS timestamptz), CAST(:bi AS uuid)))"
                        " ORDER BY w.created_at DESC, w.id DESC LIMIT :lim"
                    ),
                    {
                        "p": project_id,
                        "repo": repository_id,
                        "run": run_id,
                        "statuses": statuses,
                        "bt": before[0] if before else None,
                        "bi": before[1] if before else None,
                        "lim": limit + 1,
                    },
                )
            ).mappings()
        ]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [worktree_view(r) for r in page], "next_cursor": next_cursor})
