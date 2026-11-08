"""GitHub-интеграция (P8-02…P8-05, P8-09): установка GitHub App, привязка репозиториев, вебхуки, PR, merge.

* Установка: `POST …/installations/setup` выдаёт `state` (HMAC, привязан к workspace и пользователю сессии,
  однократный, 10 мин) и URL установки App; GitHub возвращает пользователя на страницу UI (Setup URL App) с
  `installation_id` и `state`; UI вызывает `POST …/installations` (сессия + CSRF) — сервер проверяет `state`,
  читает установку через GitHub API (JWT App) и отклоняет установки с лишними правами (SC-GH-04).
* Вебхук `POST /integrations/github/webhook` — без сессии и CSRF: аутентификация HMAC по сырому телу
  (SC-GH-01), дедупликация GUID и событие outbox — `sys_github_webhook_receive` (SC-GH-02).
* PR/merge: запрос PR и approval MERGE фиксируются транзакционно; внешние действия исполняет воркер через
  ledger `external_actions` (ADR-0012).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api import views
from aicd_api.deps import (
    Access,
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    project_access,
    rate_limit_anonymous,
    workspace_access,
)
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_api.routers.runs import resolve_resource
from aicd_code_intelligence.index.jobs import enqueue_indexing_job
from aicd_identity import api as identity
from aicd_integrations_github import api as gh
from aicd_orchestration import github_flow
from aicd_platform import audit
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from aicd_projects import api as projects
from aicd_security.git import GitRepositoryError

log = logging.getLogger("aicd.github")

router = APIRouter(tags=["github"])

WEBHOOK_PATH = "/integrations/github/webhook"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallationConnectRequest(_Strict):
    installation_id: Annotated[int, Field(ge=1, le=2**53)]
    state: Annotated[str, Field(min_length=1, max_length=1024)]
    setup_action: Literal["install", "update"] | None = None


class PullRequestCreateRequest(_Strict):
    task_id: uuid.UUID | None = None
    base_branch: Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._/-]+$")] | None = None
    title: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")] | None = None


class MergeRequestBody(_Strict):
    merge_method: Literal["merge", "squash", "rebase"] = "merge"


def github_client(request: Request) -> gh.GitHubClient:
    """Клиент GitHub процесса API (кэш installation token — в памяти процесса)."""
    client = getattr(request.app.state, "aicd_github", None)
    if not isinstance(client, gh.GitHubClient):
        client = gh.build_client(app_state(request).settings)
        request.app.state.aicd_github = client
    if not client.configured:
        raise ProblemError("service_unavailable", detail="GitHub App не настроен")
    return client


def installation_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "installation_id": row["installation_id"],
        "account_login": row["account_login"],
        "account_type": row["account_type"],
        "repository_selection": row["repository_selection"],
        "permissions": row["permissions"] or {},
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def pull_request_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "repository_id": row["repository_id"],
        "repository_full_name": row["repository_full_name"],
        "run_id": row["run_id"],
        "pr_number": row["pr_number"],
        "url": row["url"],
        "state": row["state"],
        "head_branch": row["head_branch"],
        "base_branch": row["base_branch"],
        "head_sha": row["head_sha"],
        "merged_commit_sha": row["merged_commit_sha"],
        "create_action_id": row["create_action_id"],
        "merge_action_id": row["merge_action_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "merged_at": row["merged_at"],
        "closed_at": row["closed_at"],
    }


# --- Установка GitHub App -----------------------------------------------------------------------------


@router.get("/integrations/github/installations", **op("listGitHubInstallations", "github:read"))
async def list_installations(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "github:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        rows = await gh.store.list_installations(conn)
    return json_response({"items": [installation_view(r) for r in rows]})


@router.post("/integrations/github/installations/setup", **op("startGitHubInstallation", "github:manage"))
async def start_installation(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "github:manage")
    state = app_state(request)
    settings = state.settings
    if not settings.github_app_slug or settings.github_app_id is None:
        raise ProblemError("service_unavailable", detail="GitHub App не настроен")
    token, issued = gh.install_state.issue(
        state.secret_key, access.workspace_id, principal.user_id, settings.github_install_state_ttl_seconds
    )
    url = (
        f"{settings.github_web_url.rstrip('/')}/apps/{quote(settings.github_app_slug, safe='')}/installations/new"
        f"?state={quote(token, safe='')}"
    )
    return json_response(
        {"install_url": url, "state": token, "expires_at": datetime.fromtimestamp(issued.expires_at, tz=UTC)}
    )


@router.post("/integrations/github/installations", status_code=201, **op("connectGitHubInstallation", "github:manage"))
async def connect_installation(
    request: Request, body: InstallationConnectRequest, principal: identity.Principal = Depends(current_principal)
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "github:manage")
    state = app_state(request)
    try:
        verified = gh.install_state.verify(
            state.secret_key, body.state, workspace_id=access.workspace_id, user_id=principal.user_id
        )
    except gh.install_state.InstallStateError as exc:
        await _audit_denied(request, access, "github.installation.connect", {"reason": f"state_{exc}"})
        raise ProblemError(
            "validation_error", errors=[FieldError("/state", "invalid_state", "state недействителен или истёк")]
        ) from exc
    client = github_client(request)
    try:
        installation = await client.installation(body.installation_id)
    except gh.GitHubError as exc:
        if exc.status == 404:
            raise ProblemError(
                "validation_error",
                errors=[FieldError("/installation_id", "not_found", "установка GitHub App не найдена")],
            ) from exc
        raise ProblemError("service_unavailable", detail="GitHub API недоступен") from exc
    if installation.app_id is not None and installation.app_id != state.settings.github_app_id:
        raise ProblemError(
            "validation_error", errors=[FieldError("/installation_id", "foreign_app", "установка другого App")]
        )
    excess = gh.permissions.excess_permissions(installation.permissions)
    missing = gh.permissions.missing_permissions(installation.permissions)
    if excess or missing:
        await _audit_denied(request, access, "github.installation.connect", {"excess": excess, "missing": missing})
        raise ProblemError(
            "validation_error",
            detail="Права установки GitHub App не совпадают с минимальным набором (SC-GH-04)",
            errors=[FieldError("/installation_id", "permissions", ", ".join(excess + missing)[:500])],
        )
    nonce_key = f"aicd:{state.settings.environment}:github_install_state:{verified.nonce}"

    async def command(conn: AsyncConnection) -> CommandResult:
        # Однократность state: повтор того же Idempotency-Key возвращает сохранённый ответ раньше этой точки.
        if not await state.redis.set(nonce_key, "1", nx=True, ex=state.settings.github_install_state_ttl_seconds):
            raise ProblemError("validation_error", errors=[FieldError("/state", "state_used", "state уже использован")])
        try:
            row, created = await gh.store.connect_installation(
                conn, workspace_id=access.workspace_id, installation=installation, actor_id=principal.user_id
            )
        except gh.store.InstallationConflictError as exc:
            raise ProblemError("conflict", detail="Установка подключена к другому workspace") from exc
        await audit.record(
            conn,
            action="github.installation.connect",
            resource_type="github_installation",
            resource_id=row["id"],
            actor_type="user",
            actor_id=principal.user_id,
            workspace_id=access.workspace_id,
            metadata={
                "installation_id": installation.installation_id,
                "account_login": installation.account_login,
                "permissions": installation.permissions,
                "reconnected": not created,
            },
        )
        return CommandResult(201, installation_view(row))

    return await run_idempotent(request, access.db(), body.model_dump(), command)


@router.delete(
    "/integrations/github/installations/{github_installation_id}",
    status_code=204,
    **op("disconnectGitHubInstallation", "github:manage"),
)
async def disconnect_installation(
    request: Request,
    github_installation_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "github:manage")
    async with transaction(app_state(request).engine, access.db()) as conn:
        row = await gh.store.get_installation(conn, github_installation_id, lock=True)
        if row is None:
            raise ProblemError("not_found")
        if row["status"] != "REVOKED":
            await gh.store.set_installation_status(conn, github_installation_id, "REVOKED")
            await audit.record(
                conn,
                action="github.installation.disconnect",
                resource_type="github_installation",
                resource_id=github_installation_id,
                actor_type="user",
                actor_id=principal.user_id,
                workspace_id=access.workspace_id,
                metadata={"installation_id": row["installation_id"]},
            )
    return Response(status_code=204)


async def _audit_denied(request: Request, access: Access, action: str, metadata: dict[str, Any]) -> None:
    async with transaction(app_state(request).engine, access.db()) as conn:
        await audit.record(
            conn,
            action=action,
            resource_type="workspace",
            resource_id=access.workspace_id,
            actor_type="user",
            actor_id=access.principal.user_id,
            workspace_id=access.workspace_id,
            outcome="denied",
            metadata=metadata,
        )


# --- Привязка GitHub-репозитория (attachRepository, provider=github) --------------------------------------


async def attach_github_repository(
    request: Request,
    access: Access,
    project_id: uuid.UUID,
    *,
    remote_url: str | None,
    local_path: str | None,
    default_branch: str | None,
    payload: dict[str, Any],
) -> Response:
    """FR-PRJ-03: GitHub-репозиторий, доступный активной установке workspace, + локальная рабочая копия."""
    state = app_state(request)
    errors = []
    if not remote_url:
        errors.append(FieldError("/remote_url", "required", "обязательно для github"))
    if not local_path:
        errors.append(FieldError("/local_path", "required", "локальная рабочая копия репозитория обязательна"))
    if errors:
        raise ProblemError("validation_error", errors=errors)
    if remote_url is None or local_path is None:  # pragma: no cover — проверено выше
        raise ProblemError("validation_error")
    try:
        ref = gh.parse_remote_url(remote_url, state.settings.github_web_url)
    except ValueError as exc:
        raise ProblemError("validation_error", errors=[FieldError("/remote_url", "invalid", str(exc))]) from exc
    async with transaction(state.engine, access.db()) as conn:
        installations = [r for r in await gh.store.list_installations(conn) if r["status"] == "ACTIVE"]
    if not installations:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/provider", "no_installation", "в workspace нет активной установки GitHub App")],
        )
    client = github_client(request)
    found: tuple[dict[str, Any], gh.RepoInfo] | None = None
    for inst in installations:
        if str(inst["account_login"]).lower() != ref.owner.lower():
            continue
        try:
            token = await client.installation_token(int(inst["installation_id"]), "read", repositories=[ref.name])
            found = (inst, await client.repository(token, ref))
            break
        except gh.GitHubError as exc:
            if exc.status in (0,) or exc.status >= 500:
                raise ProblemError("service_unavailable", detail="GitHub API недоступен") from exc
            continue
    if found is None:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/remote_url", "not_accessible", "репозиторий недоступен установкам GitHub App")],
        )
    inst, info = found
    canonical = f"{state.settings.github_web_url.rstrip('/')}/{info.full_name}"

    async def command(conn: AsyncConnection) -> CommandResult:
        row = await projects.attach_github_repository(
            conn,
            project_id=project_id,
            workspace_id=access.workspace_id,
            actor_id=access.principal.user_id,
            local_path=local_path,
            remote_url=canonical,
            full_name=info.full_name,
            github_repo_id=info.id,
            github_installation_id=inst["id"],
            default_branch=default_branch or info.default_branch,
            roots=state.settings.repository_roots,
        )
        head = (
            await conn.execute(
                text("SELECT last_synced_commit_sha FROM repositories WHERE id = :id"), {"id": row["id"]}
            )
        ).scalar()
        if head:  # как для local: первичная индексация (repository.attached → code_intelligence)
            await enqueue_indexing_job(
                conn, workspace_id=access.workspace_id, repository_id=row["id"], commit_sha=head, kind="full"
            )
        return CommandResult(
            201, views.repository(row), {"Location": f"/api/v1/projects/{project_id}/repositories/{row['id']}"}
        )

    return await run_idempotent(request, access.db(), payload, command)


# --- Вебхук ---------------------------------------------------------------------------------------------


@router.post(WEBHOOK_PATH, status_code=202, **op("receiveGitHubWebhook", "public"))
async def receive_webhook(request: Request) -> Response:
    state = app_state(request)
    settings = state.settings
    await rate_limit_anonymous(request, "gh_webhook", settings.rate_limit_anonymous_per_ip_per_minute * 10)
    secrets = [s.get_secret_value().encode() for s in settings.github_webhook_secrets()]
    if not secrets:
        raise ProblemError("service_unavailable", detail="Вебхуки GitHub не настроены")
    body = await request.body()
    if len(body) > settings.github_webhook_max_bytes:
        raise ProblemError("payload_too_large")
    try:
        guid = gh.webhooks.parse_delivery_guid(request.headers.get("x-github-delivery"))
        event = gh.webhooks.parse_event(request.headers.get("x-github-event"))
    except gh.webhooks.WebhookRejectedError as exc:
        await _audit_webhook(state, None, "denied", {"reason": exc.code})
        raise ProblemError("malformed_request", detail="Некорректные заголовки доставки GitHub") from exc
    digest = gh.webhooks.payload_sha256(body)
    # Подпись проверяется по сырому телу ДО разбора JSON (SC-GH-01).
    if not gh.webhooks.signature_valid(body, request.headers.get("x-hub-signature-256"), secrets):
        async with transaction(state.engine, DbContext()) as conn:
            await gh.store.receive_delivery(
                conn, delivery_guid=guid, event=event, action=None, installation_id=None, signature_valid=False,
                payload_sha256=digest, status="REJECTED", error="invalid signature", hint={},
            )  # fmt: skip
            await _audit(conn, guid, "denied", {"reason": "invalid_signature", "event": event})
        log.warning("github webhook signature invalid", extra={"event": event})
        raise ProblemError("unauthenticated", detail="Подпись вебхука не прошла проверку")
    if event in gh.webhooks.ALLOWED_EVENTS:
        try:
            hint = gh.webhooks.extract_hint(event, body)
        except gh.webhooks.WebhookRejectedError as exc:
            await _audit_webhook(state, guid, "denied", {"reason": exc.code, "event": event})
            raise ProblemError("malformed_request", detail="Тело вебхука не является JSON-объектом") from exc
        status, action, installation_id, data = "RECEIVED", hint.action, hint.installation_id, hint.data
    else:
        status, action, installation_id, data = "IGNORED", None, None, {}
    async with transaction(state.engine, DbContext()) as conn:
        outcome = await gh.store.receive_delivery(
            conn, delivery_guid=guid, event=event, action=action, installation_id=installation_id,
            signature_valid=True, payload_sha256=digest, status=status,
            error=None if status == "RECEIVED" else "event not in allowlist", hint=data,
        )  # fmt: skip
        if outcome != "duplicate":
            await _audit(conn, guid, "success", {"event": event, "action": action, "outcome": outcome})
    if outcome == "duplicate":
        return json_response({"status": "duplicate"}, status_code=200)
    return json_response({"status": outcome}, status_code=202)


async def _audit(conn: AsyncConnection, guid: uuid.UUID | None, outcome: str, metadata: dict[str, Any]) -> None:
    await audit.record(
        conn,
        action="github.webhook.received" if outcome == "success" else "github.webhook.rejected",
        resource_type="github_webhook_delivery",
        resource_id=guid,
        actor_type="github",
        actor_id=None,
        workspace_id=None,
        outcome=outcome,
        metadata=metadata,
    )


async def _audit_webhook(state: Any, guid: uuid.UUID | None, outcome: str, metadata: dict[str, Any]) -> None:
    async with transaction(state.engine, DbContext()) as conn:
        await _audit(conn, guid, outcome, metadata)


# --- PR workflow -------------------------------------------------------------------------------------------


@router.post("/runs/{run_id}/pull-requests", status_code=202, **op("requestPullRequest", "pull_request:create"))
async def request_pull_request(
    request: Request,
    run_id: Annotated[uuid.UUID, PathParam()],
    body: PullRequestCreateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_run_workspace", run_id)
    await authorize(request, access, "pull_request:create")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        source = await github_flow.load_pr_source(conn, run_id, body.task_id)
    repo_path = github_flow.repository_path(source.repo, state.settings.repository_roots)
    try:
        paths = await asyncio.to_thread(github_flow.changed_paths, repo_path, source.base_sha, source.head_sha)
    except (GitRepositoryError, OSError) as exc:  # коммит/копия недоступны: 409 без деталей ФС
        raise ProblemError("invalid_state_transition", detail="Коммит run недоступен в локальной копии") from exc
    # SC-GH-05: изменения CI-конфигурации — отдельный approval MANUAL_REVIEW до push (фиксируется отдельно,
    # чтобы запрос approval сохранился при отказе в создании PR).
    async with transaction(state.engine, access.db()) as conn:
        pending = await github_flow.require_ci_push_approval(conn, source, paths, requested_by=principal.user_id)
    if pending is not None:
        raise ProblemError(
            "policy_denied",
            detail="Изменения CI-конфигурации требуют approval MANUAL_REVIEW перед push",
            extensions={"approval_id": str(pending)},
        )

    async def command(conn: AsyncConnection) -> CommandResult:
        job = await github_flow.request_pull_request(
            conn, source, base_branch=body.base_branch, title=body.title, actor_id=principal.user_id
        )
        return CommandResult(202, {**{k: v for k, v in job.items() if k != "requested_by"}, "status": "QUEUED"})

    return await run_idempotent(request, access.db(), body.model_dump(mode="json"), command)


@router.get("/projects/{project_id}/pull-requests", **op("listPullRequests", "github:read"))
async def list_pull_requests(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    principal: identity.Principal = Depends(current_principal),
    run_id: uuid.UUID | None = None,
    state: Literal["OPEN", "CLOSED", "MERGED"] | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "github:read")
    app = app_state(request)
    filters = {"r": "pull_requests", "p": str(project_id), "run": str(run_id) if run_id else None, "s": state}
    before = None
    if cursor:
        t, i = decode_cursor(app.secret_key, cursor, filters)
        before = (datetime.fromisoformat(t), uuid.UUID(i))
    async with transaction(app.engine, access.db()) as conn:
        rows = await gh.store.list_pull_requests(
            conn, project_id=project_id, run_id=run_id, state=state, before=before, limit=limit + 1
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(app.secret_key, [last["created_at"].isoformat(), str(last["id"])], filters)
    return json_response({"items": [pull_request_view(r) for r in page], "next_cursor": next_cursor})


@router.post(
    "/projects/{project_id}/pull-requests/{pull_request_id}/merge-request",
    status_code=202,
    **op("requestPullRequestMerge", "pull_request:merge"),
)
async def request_merge(
    request: Request,
    project_id: Annotated[uuid.UUID, PathParam()],
    pull_request_id: Annotated[uuid.UUID, PathParam()],
    body: MergeRequestBody,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await project_access(request, principal, project_id)
    await authorize(request, access, "pull_request:merge")

    async def command(conn: AsyncConnection) -> CommandResult:
        pr = await gh.store.get_pull_request(conn, pull_request_id)  # api: только SELECT на pull_requests
        if pr is None or pr["project_id"] != project_id:
            raise ProblemError("not_found")
        approval_id = await github_flow.request_merge(conn, pr, actor_id=principal.user_id, method=body.merge_method)
        return CommandResult(
            202,
            {"pull_request_id": pull_request_id, "approval_id": approval_id, "status": "AWAITING_APPROVAL"},
            {"Location": f"/api/v1/approvals/{approval_id}"},
        )

    return await run_idempotent(request, access.db(), body.model_dump(), command)
