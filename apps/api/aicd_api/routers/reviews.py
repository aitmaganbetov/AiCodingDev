"""Inline-комментарии ревью кода (UI redesign B5, ADR-0015; UI_REDESIGN_PLAN §3.10 G-11).

* `GET/POST /projects/{project_id}/repositories/{repository_id}/review-threads` — треды репозитория (фильтры run,
  задача, commit, файл, статус; `head_sha` — commit просматриваемого diff для `outdated`) / новый тред с первым
  комментарием;
* `GET /review-threads/{thread_id}` (ETag = version), `POST .../comments` (ответ), `POST .../resolve|unresolve`
  (Idempotency-Key + If-Match);
* `PATCH /review-comments/{comment_id}` — правка своего комментария (If-Match = revision, история правок),
  `GET /review-comments/{comment_id}/revisions`;
* `GET/POST /runs/{run_id}/reviews` — ревью run: APPROVE (решение существующего approval), REQUEST_CHANGES
  (поручение CHANGE_REQUEST), COMMENT — `aicd_orchestration.code_review`.

Комментарии агентам не доставляются: воздействие — только `submitReview` через конвейер поручений/approvals.
Чужой тред/комментарий/репозиторий неотличим от отсутствующего — 404 (резолверы миграции 0019, AC-12).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import (
    _audit_denied,
    _remember_rate,
    app_state,
    authorize,
    current_principal,
    project_access,
    require_step_up,
)
from aicd_api.hooks import get_hooks
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_api.routers.approvals import DIRECTIVES
from aicd_api.routers.directives import run_access
from aicd_api.routers.runs import resolve_resource
from aicd_collaboration import review_comments as service
from aicd_identity import api as identity
from aicd_identity.rbac import FOUR_EYES_ACTIONS, can_submit_review
from aicd_orchestration import code_review
from aicd_platform.db import transaction
from aicd_platform.errors import ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor

router = APIRouter(tags=["reviews"])

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1024)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
ThreadStatus = Literal["OPEN", "RESOLVED"]
Verdict = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
REVIEW_WRITES_PER_MINUTE = 60  # треды/ответы/правки — user (как мутации, но с отдельным бакетом)
REVIEWS_PER_MINUTE = 30  # отправка ревью — user + run (как поручения, API_DESIGN §10)
_BAD_PATH = re.compile(r"[\x00-\x1f\x7f\\]|(^/|(^|/)\.\.?(/|$)|//|/$)")
_REPO = "/projects/{project_id}/repositories/{repository_id}/review-threads"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, Field(min_length=1, max_length=8000)]

    @field_validator("body")
    @classmethod
    def _text(cls, value: str) -> str:
        if "\x00" in value:  # PostgreSQL text не хранит NUL
            raise ValueError("NUL-символ недопустим")
        if not value.strip():
            raise ValueError("пустой текст")
        return value


class ReviewAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: Sha
    base_commit_sha: Sha | None = None
    side: Literal["base", "head"] = "head"
    file_path: Annotated[str, Field(min_length=1, max_length=1024)]
    start_line: Annotated[int, Field(ge=1, le=10_000_000)]
    end_line: Annotated[int, Field(ge=1, le=10_000_000)]

    @field_validator("file_path")
    @classmethod
    def _relative(cls, value: str) -> str:
        if _BAD_PATH.search(value):
            raise ValueError("относительный путь без '.', '..', '//', '\\' и управляющих символов")
        return value


class ReviewThreadCreateRequest(_Body):
    anchor: ReviewAnchor
    run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    finding_id: uuid.UUID | None = None


class ReviewCommentCreateRequest(_Body):
    pass


class ReviewCommentUpdateRequest(_Body):
    pass


class ReviewSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    body: Annotated[str, Field(min_length=1, max_length=8000)] | None = None
    thread_ids: Annotated[list[uuid.UUID], Field(max_length=50)] = []
    task_id: uuid.UUID | None = None
    approval_id: uuid.UUID | None = None
    expected_run_version: Annotated[int, Field(ge=0)]
    requires_impact_preview: bool = True


# --- Доступ ------------------------------------------------------------------------------------------------


async def _repository_access(
    request: Request, principal: identity.Principal, project_id: uuid.UUID, repository_id: uuid.UUID, permission: str
) -> Any:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, permission)
    async with transaction(app_state(request).engine, access.db()) as conn:
        found = (
            await conn.execute(
                text("SELECT 1 FROM repositories WHERE id = :r AND project_id = :p"),
                {"r": repository_id, "p": project_id},
            )
        ).first()
    if found is None:
        raise ProblemError("not_found")
    return access


async def _thread_view(
    conn: AsyncConnection, thread_id: uuid.UUID, viewer: uuid.UUID, head_sha: str | None = None
) -> dict[str, Any]:
    thread = await service.load_thread(conn, thread_id)
    if thread is None:
        raise ProblemError("not_found")
    comments = await service.load_comments(conn, [thread_id])
    return service.thread_view(thread, comments[thread_id], viewer_id=viewer, head_sha=head_sha)


async def _write_rate(request: Request, principal: identity.Principal) -> None:
    state = app_state(request)
    _remember_rate(request, await state.limiter.hit(f"review:user:{principal.user_id}", REVIEW_WRITES_PER_MINUTE))


def _head_sha(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", value):
        raise ProblemError("validation_error", detail="head_sha — полный hex SHA commit (40 или 64 символа)")
    return value


# --- Треды репозитория -------------------------------------------------------------------------------------


@router.get(_REPO, **op("listReviewThreads", "review:read"))
async def list_review_threads(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    repository_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    run_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    commit_sha: Annotated[str | None, Query(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")] = None,
    file_path: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
    status: Annotated[list[ThreadStatus] | None, Query(max_length=2)] = None,
    head_sha: Annotated[str | None, Query(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await _repository_access(request, principal, project_id, repository_id, "review:read")
    state = app_state(request)
    statuses = tuple(sorted(set(status or [])))
    filters = {
        "r": "review_threads",
        "repo": str(repository_id),
        "run": str(run_id) if run_id else None,
        "task": str(task_id) if task_id else None,
        "commit": commit_sha,
        "path": file_path,
        "status": list(statuses),
    }
    after = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        after = (datetime.fromisoformat(t), uuid.UUID(i))
    query = service.ThreadFilters(run_id, task_id, commit_sha, file_path, statuses)
    async with transaction(state.engine, access.db()) as conn:
        rows = await service.list_threads(conn, repository_id, query, after=after, limit=limit + 1)
        page = rows[:limit]
        comments = await service.load_comments(conn, [r["id"] for r in page])
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    items = [
        service.thread_view(r, comments[r["id"]], viewer_id=principal.user_id, head_sha=_head_sha(head_sha))
        for r in page
    ]
    return json_response({"items": items, "next_cursor": next_cursor})


@router.post(_REPO, status_code=201, **op("createReviewThread", "review:write"))
async def create_review_thread(
    request: Request,
    project_id: Annotated[uuid.UUID, Path()],
    repository_id: Annotated[uuid.UUID, Path()],
    body: ReviewThreadCreateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await _repository_access(request, principal, project_id, repository_id, "review:write")
    await _write_rate(request, principal)
    a = body.anchor

    async def command(conn: AsyncConnection) -> CommandResult:
        thread_id = await service.create_thread(
            conn, workspace_id=access.workspace_id, repository_id=repository_id, user_id=principal.user_id,
            anchor=service.Anchor(a.commit_sha, a.file_path, a.start_line, a.end_line, a.side, a.base_commit_sha),
            body=body.body, run_id=body.run_id, task_id=body.task_id, finding_id=body.finding_id,
        )  # fmt: skip
        view = await _thread_view(conn, thread_id, principal.user_id)
        headers = {"ETag": etag(view["version"]), "Location": f"/api/v1/review-threads/{thread_id}"}
        return CommandResult(201, view, headers)

    return await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)


# --- Тред ----------------------------------------------------------------------------------------------------


@router.get("/review-threads/{thread_id}", **op("getReviewThread", "review:read"))
async def get_review_thread(
    request: Request,
    thread_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    head_sha: Annotated[str | None, Query(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")] = None,
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_review_thread_workspace", thread_id)
    await authorize(request, access, "review:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        view = await _thread_view(conn, thread_id, principal.user_id, _head_sha(head_sha))
    tag = etag(view["version"])
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return json_response(view, headers={"ETag": tag})


@router.post("/review-threads/{thread_id}/comments", status_code=201, **op("replyReviewThread", "review:write"))
async def reply_review_thread(
    request: Request,
    thread_id: Annotated[uuid.UUID, Path()],
    body: ReviewCommentCreateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_review_thread_workspace", thread_id)
    await authorize(request, access, "review:write")
    await _write_rate(request, principal)

    async def command(conn: AsyncConnection) -> CommandResult:
        comment_id = await service.reply(conn, thread_id=thread_id, user_id=principal.user_id, body=body.body)
        row = await service.load_comment(conn, comment_id)
        if row is None:  # pragma: no cover — нарушение инварианта
            raise LookupError(comment_id)
        view = service.comment_view(row, principal.user_id)
        headers = {"ETag": etag(view["revision"]), "Location": f"/api/v1/review-threads/{thread_id}"}
        return CommandResult(201, view, headers)

    return await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)


async def _set_status(
    request: Request, thread_id: uuid.UUID, principal: identity.Principal, *, resolve: bool
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_review_thread_workspace", thread_id)
    await authorize(request, access, "review:write")
    await _write_rate(request, principal)

    async def command(conn: AsyncConnection) -> CommandResult:
        expected = require_if_match(request.headers.get("if-match"))
        await service.set_status(
            conn, thread_id=thread_id, user_id=principal.user_id, resolve=resolve, expected_version=expected
        )
        view = await _thread_view(conn, thread_id, principal.user_id)
        return CommandResult(200, view, {"ETag": etag(view["version"])})

    return await run_idempotent(request, access.db(), {}, command)


@router.post("/review-threads/{thread_id}/resolve", **op("resolveReviewThread", "review:write"))
async def resolve_review_thread(
    request: Request,
    thread_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    return await _set_status(request, thread_id, principal, resolve=True)


@router.post("/review-threads/{thread_id}/unresolve", **op("unresolveReviewThread", "review:write"))
async def unresolve_review_thread(
    request: Request,
    thread_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    return await _set_status(request, thread_id, principal, resolve=False)


# --- Комментарий ---------------------------------------------------------------------------------------------


@router.patch("/review-comments/{comment_id}", **op("editReviewComment", "review:write"))
async def edit_review_comment(
    request: Request,
    comment_id: Annotated[uuid.UUID, Path()],
    body: ReviewCommentUpdateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_review_comment_workspace", comment_id)
    await authorize(request, access, "review:write")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        current = await service.load_comment(conn, comment_id)
    if current is None:
        raise ProblemError("not_found")
    if current["author_id"] != principal.user_id:  # ADR-0015 §2: правит только автор; отказ аудируется
        async with transaction(state.engine, access.db()) as conn:
            await _audit_denied(conn, access, "review:write:own", request)
        raise ProblemError("forbidden")
    expected = require_if_match(request.headers.get("if-match"))
    await _write_rate(request, principal)
    async with transaction(state.engine, access.db()) as conn:
        await service.edit_comment(
            conn, comment_id=comment_id, user_id=principal.user_id, body=body.body, expected_revision=expected
        )
        row = await service.load_comment(conn, comment_id)
    if row is None:  # pragma: no cover — нарушение инварианта
        raise ProblemError("not_found")
    view = service.comment_view(row, principal.user_id)
    return json_response(view, headers={"ETag": etag(view["revision"])})


@router.get("/review-comments/{comment_id}/revisions", **op("listReviewCommentRevisions", "review:read"))
async def list_review_comment_revisions(
    request: Request,
    comment_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_review_comment_workspace", comment_id)
    await authorize(request, access, "review:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        current = await service.load_comment(conn, comment_id)
        if current is None:
            raise ProblemError("not_found")
        items = await service.list_revisions(conn, comment_id)
    return json_response({"comment": service.comment_view(current, principal.user_id), "items": items})


# --- Ревью run -----------------------------------------------------------------------------------------------


@router.post("/runs/{run_id}/reviews", status_code=202, **op("submitReview", "review:submit"))
async def submit_review(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    body: ReviewSubmitRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await run_access(request, principal, run_id)
    await authorize(request, access, "review:submit")
    state = app_state(request)
    # ADR-0015 §2: право по вердикту — до Idempotency-Key (порядок API_DESIGN §6); отказ аудируется.
    if not can_submit_review(access.role, body.verdict):
        async with transaction(state.engine, access.db()) as conn:
            await _audit_denied(conn, access, f"review:submit:{body.verdict}", request)
        raise ProblemError("forbidden")
    _remember_rate(
        request, await state.limiter.hit(f"review:user:{principal.user_id}:run:{run_id}", REVIEWS_PER_MINUTE)
    )
    if body.verdict == "APPROVE" and body.approval_id is not None:
        # Тот же путь, что POST /approvals/{id}/decision: критический набор требует step-up (SC-IAM-08).
        async with transaction(state.engine, access.db()) as conn:
            action_type = (
                await conn.execute(text("SELECT action_type FROM approvals WHERE id = :id"), {"id": body.approval_id})
            ).scalar_one_or_none()
        if action_type in FOUR_EYES_ACTIONS:
            await require_step_up(request, principal, f"approval:decide:{action_type}")
    submitted: dict[str, Any] = {}

    async def command(conn: AsyncConnection) -> CommandResult:
        result = await code_review.submit_review(
            conn, DIRECTIVES, run_id=run_id, workspace_id=access.workspace_id, actor_id=principal.user_id,
            actor_role=access.role, verdict=body.verdict, body=body.body, thread_ids=body.thread_ids,
            task_id=body.task_id, approval_id=body.approval_id, expected_run_version=body.expected_run_version,
            requires_impact_preview=body.requires_impact_preview,
        )  # fmt: skip
        submitted["result"] = result
        row = await service.load_submission(conn, result.review_id)
        if row is None:  # pragma: no cover — нарушение инварианта
            raise LookupError(result.review_id)
        return CommandResult(202, service.submission_view(row))

    response = await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)
    result: code_review.ReviewResult | None = submitted.get("result")
    if result is not None and result.directive is not None:
        hooks = get_hooks(request)
        if result.directive.decided_approval is not None:
            await hooks.on_approval_decided(result.directive.decided_approval)
        await hooks.on_directive_changed(result.directive.directive_id, access.workspace_id)
    return response


@router.get("/runs/{run_id}/reviews", **op("listReviews", "review:read"))
async def list_reviews(
    request: Request,
    run_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Response:
    access = await run_access(request, principal, run_id)
    await authorize(request, access, "review:read")
    state = app_state(request)
    filters = {"r": "reviews", "run": str(run_id)}
    before = None
    if cursor:
        t, i = decode_cursor(state.secret_key, cursor, filters)
        before = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(state.engine, access.db()) as conn:
        rows = await service.list_submissions(conn, run_id, before=before, limit=limit + 1)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(state.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [service.submission_view(r) for r in page], "next_cursor": next_cursor})


__all__ = ["router"]
