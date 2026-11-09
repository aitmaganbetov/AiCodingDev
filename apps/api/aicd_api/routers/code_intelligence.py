"""Code Intelligence read API (B3, UI_REDESIGN_PLAN §3.6, G-06…G-09).

* `GET /projects/{project_id}/repositories/{repository_id}/symbols` — поиск символов READY-снимка индекса;
* `GET …/dependencies` — ограниченный граф зависимостей файлов вокруг пути/символа;
* `GET …/indexing-jobs` — очередь и история заданий индексации (статус, тайминги, ошибка);
* `GET /tasks/{task_id}/attempts/{attempt_id}/context-pack` — manifest Context Pack попытки (без текста фрагментов).

Только чтение существующих таблиц (миграция 0007) под RLS-контекстом workspace; чужой/несуществующий проект,
репозиторий, задача или попытка — 404 (AC-12). Пути хоста не отдаются: пути — относительные пути снимка.
"""

from __future__ import annotations

import re
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
from aicd_api.routers.runs import resolve_resource
from aicd_code_intelligence.context import store as context_store
from aicd_code_intelligence.index import queries
from aicd_code_intelligence.index.search import SearchQueryError
from aicd_identity import api as identity
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_platform.redaction import redact_text
from aicd_projects import explorer

router = APIRouter()

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
IndexCommit = Annotated[str | None, Query(min_length=7, max_length=64, pattern=r"^[0-9a-f]{7,64}$")]
SymbolKind = Literal[
    "module", "class", "interface", "function", "method", "variable", "constant", "type", "enum", "test", "route",
    "other",
]  # fmt: skip
JobStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]

_REPO = "/projects/{project_id}/repositories/{repository_id}"
_HOST_PATH = re.compile(r"(?<![\w.:])/(?:[^\s/'\"]+/)+[^\s'\"]*")


def safe_error(message: str | None) -> str | None:
    """Ошибка задания для UI: секреты редактируются, абсолютные пути хоста заменяются на `[path]`."""
    if not message:
        return None
    return _HOST_PATH.sub("[path]", redact_text(message))[:1000]


async def _repository_snapshot(
    request: Request,
    principal: identity.Principal,
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    commit: str | None,
) -> tuple[Any, queries.SnapshotRef | None]:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        await explorer.get_explorer_repository(conn, project_id, repository_id)  # чужой/не того проекта — 404
        try:
            snapshot = await queries.resolve_snapshot(conn, repository_id=repository_id, commit=commit)
        except queries.AmbiguousCommitError as exc:
            raise ProblemError(
                "validation_error", errors=[FieldError("/commit", "ambiguous_commit", "префикс commit неоднозначен")]
            ) from exc
    if commit is not None and snapshot is None:
        raise ProblemError("not_found", detail="Для этого commit нет готового снимка индекса")
    return access, snapshot


def _path_param(path: str | None) -> str | None:
    if path is None:
        return None
    rel = explorer.validate_tree_path(path)
    return rel or None


@router.get(_REPO + "/symbols", tags=["projects"], **op("listRepositorySymbols", "project:read"))
async def list_repository_symbols(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    repository_id: Annotated[uuid.UUID, PathParam()],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    principal: identity.Principal = Depends(current_principal),
    kind: SymbolKind | None = None,
    path: Annotated[str | None, Query(max_length=4096)] = None,
    commit: IndexCommit = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    rel = _path_param(path)
    access, snapshot = await _repository_snapshot(request, principal, project_id, repository_id, commit)
    state = app_state(request)
    filters = {"r": "symbols", "repo": str(repository_id), "q": q, "kind": kind, "path": rel, "commit": commit}
    offset = 0
    if cursor:
        snap_raw, raw_offset = decode_cursor(state.secret_key, cursor, filters)
        offset = int(raw_offset)
        if snapshot is None or str(snapshot.id) != str(snap_raw):
            raise ProblemError("invalid_cursor", detail="Снимок индекса изменился — начните поиск заново")
    if snapshot is None:
        return json_response({"repository_id": repository_id, "snapshot": None, "items": [], "next_cursor": None})
    page_limit = min(limit, queries.MAX_SYMBOL_RESULTS - offset)
    hits: list[queries.SymbolHit] = []
    if page_limit > 0:
        try:
            async with transaction(state.engine, access.db()) as conn:
                hits = await queries.search_symbols(
                    conn, snapshot_id=snapshot.id, query=q, kind=kind, path=rel, offset=offset, limit=page_limit + 1
                )
        except SearchQueryError as exc:
            raise ProblemError("validation_error", errors=[FieldError("/q", "invalid_query", str(exc))]) from exc
    page = hits[:page_limit]
    more = len(hits) > page_limit and offset + page_limit < queries.MAX_SYMBOL_RESULTS
    next_cursor = encode_cursor(state.secret_key, [str(snapshot.id), offset + page_limit], filters) if more else None
    return json_response(
        {
            "repository_id": repository_id,
            "snapshot": snapshot.view(),
            "items": [
                {
                    "id": h.id,
                    "name": h.name,
                    "qualified_name": h.qualified_name,
                    "kind": h.kind,
                    "path": h.path,
                    "start_line": h.start_line,
                    "end_line": h.end_line,
                    "signature": redact_text(h.signature) if h.signature else None,
                    "score": h.score,
                    "match": h.match,
                }
                for h in page
            ],
            "next_cursor": next_cursor,
        }
    )


@router.get(_REPO + "/dependencies", tags=["projects"], **op("getRepositoryDependencies", "project:read"))
async def get_repository_dependencies(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    repository_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
    path: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    symbol: uuid.UUID | None = None,
    depth: Annotated[int, Query(ge=1, le=queries.MAX_GRAPH_DEPTH)] = 1,
    direction: Literal["out", "in", "both"] = "both",
    commit: IndexCommit = None,
) -> Response:
    if (path is None) == (symbol is None):
        raise ProblemError(
            "validation_error", errors=[FieldError("/", "one_of", "укажите ровно один корень: path или symbol")]
        )
    rel = _path_param(path) if path is not None else None
    if path is not None and rel is None:
        raise ProblemError(
            "validation_error", errors=[FieldError("/path", "invalid_path", "путь не может быть корнем")]
        )
    access, snapshot = await _repository_snapshot(request, principal, project_id, repository_id, commit)
    if snapshot is None:
        return json_response(
            {
                "repository_id": repository_id,
                "snapshot": None,
                "depth": depth,
                "direction": direction,
                "roots": [],
                "nodes": [],
                "edges": [],
                "truncated": False,
                "limits": {"max_nodes": queries.MAX_GRAPH_NODES, "max_edges": queries.MAX_GRAPH_EDGES},
            }
        )
    try:
        async with transaction(app_state(request).engine, access.db()) as conn:
            graph = await queries.dependency_graph(
                conn, snapshot_id=snapshot.id, path=rel, symbol_id=symbol, depth=depth, direction=direction
            )
    except queries.GraphRootNotFoundError as exc:
        raise ProblemError("not_found", detail="Путь или символ отсутствует в снимке индекса") from exc
    return json_response(
        {
            "repository_id": repository_id,
            "snapshot": snapshot.view(),
            "depth": depth,
            "direction": direction,
            "roots": graph.roots,
            "nodes": graph.nodes,
            "edges": graph.edges,
            "truncated": graph.truncated,
            "limits": {"max_nodes": queries.MAX_GRAPH_NODES, "max_edges": queries.MAX_GRAPH_EDGES},
        }
    )


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max(0.0, (end - start).total_seconds()), 3)


def _job_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "repository_id": r["repository_id"],
        "commit_sha": r["commit_sha"],
        "kind": r["kind"],
        "status": r["status"],
        "attempt_count": r["attempt_count"],
        "base_snapshot_id": r["base_snapshot_id"],
        "snapshot_id": r["snapshot_id"],
        "file_count": r["file_count"],
        "symbol_count": r["symbol_count"],
        "created_at": r["created_at"],
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "queue_seconds": _seconds(r["created_at"], r["started_at"]),
        "duration_seconds": _seconds(r["started_at"], r["finished_at"]),
        "error": safe_error(r["error"]),
    }


@router.get(_REPO + "/indexing-jobs", tags=["projects"], **op("listIndexingJobs", "project:read"))
async def list_indexing_jobs(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    repository_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
    status: JobStatus | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "project:read")
    state = app_state(request)
    filters = {"r": "indexing_jobs", "repo": str(repository_id), "status": status}
    before = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        before = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        await explorer.get_explorer_repository(conn, project_id, repository_id)
        rows = await queries.list_indexing_jobs(
            conn, repository_id=repository_id, status=status, before=before, limit=limit + 1
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [_job_view(r) for r in page], "next_cursor": next_cursor})


@router.get(
    "/tasks/{task_id}/attempts/{attempt_id}/context-pack", tags=["tasks"], **op("getAttemptContextPack", "run:read")
)
async def get_attempt_context_pack(
    request: Request,
    task_id: Annotated[uuid.UUID, PathParam()],
    attempt_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_task_workspace", task_id)
    await authorize(request, access, "run:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        found = (
            await conn.execute(
                text("SELECT 1 FROM task_attempts WHERE id = :a AND task_id = :t"), {"a": attempt_id, "t": task_id}
            )
        ).first()
        if found is None:
            raise ProblemError("not_found")
        row = await context_store.load_context_pack(conn, task_attempt_id=attempt_id)
    if row is None:
        raise ProblemError("not_found", detail="Для этой попытки Context Pack не собирался")
    manifest = context_store.manifest_view(row["manifest"] if isinstance(row["manifest"], dict) else {})
    return json_response(
        {
            "id": row["id"],
            "task_id": task_id,
            "attempt_id": attempt_id,
            "snapshot_id": row["snapshot_id"],
            "base_commit": row["source_commit"],
            "snapshot_commit": row["snapshot_commit"],
            "policy_version": row["policy_version"],
            "documentation_mode": row["documentation_mode"],
            "token_budget": row["token_budget"],
            "size_bytes": row["size_bytes"],
            "content_hash": row["content_hash"],
            "artifact_id": row["artifact_id"],
            "created_at": row["created_at"],
            **manifest,
        }
    )
