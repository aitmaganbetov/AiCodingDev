"""Проверки GitHub App на воркере (OQ-47 (2), SECURITY_ARCHITECTURE §6.1: закрытый ключ App — только у воркера).

API не держит ключ App и не обращается к GitHub API с JWT/installation token. Операции, которым нужна проверка
через GitHub, разделены на две фазы:

1. API (`request_verification`) проверяет то, что можно проверить локально (state установки, путь локальной
   копии, RBAC), и фиксирует в одной транзакции строку `github_verification_requests` (PENDING) + событие
   outbox `github.verification.requested` + аудит; привязка репозитория отвечает `202` (ресурс проверки), подключение
   установки — `201` с установкой в статусе `PENDING` (контракт сохранён) и `verification_id`;
   статус — `GET /integrations/github/verifications/{id}`.
2. Воркер (`GitHubVerifier.verify`) читает установку/репозиторий через GitHub API и в одной транзакции записывает
   результат (`SUCCEEDED` + `result_id` | `FAILED` + `error_code`) вместе с эффектом: подключение установки
   (`store.connect_installation`) или привязка репозитория (`projects.attach_github_repository` + задание индексации).

Повтор события (дубль outbox/Dramatiq) безопасен: результат записывается только для строки в состоянии PENDING
(`FOR UPDATE`), терминальные состояния неизменяемы (триггер `trg_gvr_transition`, миграция 0014).
Недоступность GitHub (сеть, 5xx) — `FAILED github_unavailable` (повтор — новым запросом пользователя), чтобы
запрос не оставался PENDING бесконечно.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_code_intelligence.index.jobs import enqueue_indexing_job
from aicd_integrations_github.api import (
    GitHubClient,
    GitHubError,
    Installation,
    RepoInfo,
    RepoRef,
    permissions,
    store,
    verifications,
)
from aicd_platform import audit, outbox
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7
from aicd_projects import api as projects

log = logging.getLogger("aicd.github")

VERIFICATION_EVENT: Final = "github.verification.requested"
Kind = Literal["installation", "repository"]

verification_view = verifications.view
get_verification = verifications.get


async def request_verification(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    kind: Kind,
    params: dict[str, Any],
    actor_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """API: строка PENDING + событие outbox + аудит в транзакции вызывающего."""
    vid = uuid7()
    await verifications.insert(
        conn,
        verification_id=vid,
        workspace_id=workspace_id,
        kind=kind,
        params=params,
        requested_by=actor_id,
        project_id=project_id,
    )
    await outbox.emit(
        conn,
        event_type=VERIFICATION_EVENT,
        aggregate_type="github_verification",
        aggregate_id=vid,
        workspace_id=workspace_id,
        payload={"verification_id": str(vid), "kind": kind},
    )
    await audit.record(
        conn,
        action="github.verification.requested",
        resource_type="github_verification",
        resource_id=vid,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"kind": kind, **{k: params[k] for k in ("installation_id", "full_name") if k in params}},
    )
    row = await get_verification(conn, vid)
    if row is None:  # pragma: no cover — нарушение инварианта транзакции
        raise LookupError("verification")
    return row


class _Failed(Exception):
    def __init__(self, code: str, detail: str, audit_meta: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail[:500]
        self.audit_meta = audit_meta


def _unavailable(exc: GitHubError) -> bool:
    return exc.status == 0 or exc.status >= 500


class GitHubVerifier:
    """Воркер: проверка установки/доступа к репозиторию через GitHub API (ключ App — только здесь)."""

    def __init__(
        self,
        engine: AsyncEngine,
        client: GitHubClient,
        *,
        repository_roots: Mapping[str, Path],
    ) -> None:
        self.engine = engine
        self.client = client
        self.app_id = client.app_id
        self.repository_roots = dict(repository_roots)

    async def verify(self, ws: uuid.UUID, verification_id: uuid.UUID) -> str:
        ctx = DbContext(workspace_id=ws, actor_type="system")
        async with transaction(self.engine, ctx) as conn:
            row = await get_verification(conn, verification_id)
        if row is None or row["status"] != "PENDING":
            return "ignored"
        params = dict(row["params"] or {})
        try:
            if row["kind"] == "installation":
                installation = await self._check_installation(params)
                return await self._finish(ctx, row, lambda conn: self._connect(conn, row, installation))
            inst, info = await self._check_repository(ctx, params)
            return await self._finish(ctx, row, lambda conn: self._attach(conn, row, params, inst, info))
        except _Failed as failed:
            return await self._fail(ctx, row, failed)

    # --- проверки через GitHub API (вне транзакции БД) -----------------------------------------------

    async def _check_installation(self, params: Mapping[str, Any]) -> Installation:
        installation_id = int(params["installation_id"])
        try:
            installation = await self.client.installation(installation_id)
        except GitHubError as exc:
            if exc.status == 404:
                raise _Failed("not_found", "установка GitHub App не найдена") from exc
            raise _Failed("github_unavailable", "GitHub API недоступен") from exc
        if installation.app_id is not None and installation.app_id != self.app_id:
            raise _Failed("foreign_app", "установка другого App")
        excess = permissions.excess_permissions(installation.permissions)
        missing = permissions.missing_permissions(installation.permissions)
        if excess or missing:
            raise _Failed(
                "permissions",
                ", ".join(excess + missing),
                {"excess": excess, "missing": missing},
            )
        return installation

    async def _check_repository(self, ctx: DbContext, params: Mapping[str, Any]) -> tuple[dict[str, Any], RepoInfo]:
        ref = RepoRef(owner=str(params["owner"]), name=str(params["name"]))
        async with transaction(self.engine, ctx) as conn:
            installations = [r for r in await store.list_installations(conn) if r["status"] == "ACTIVE"]
        if not installations:
            raise _Failed("no_installation", "в workspace нет активной установки GitHub App")
        for inst in installations:
            if str(inst["account_login"]).lower() != ref.owner.lower():
                continue
            try:
                token = await self.client.installation_token(
                    int(inst["installation_id"]), "read", repositories=[ref.name]
                )
                return inst, await self.client.repository(token, ref)
            except GitHubError as exc:
                if _unavailable(exc):
                    raise _Failed("github_unavailable", "GitHub API недоступен") from exc
                continue
        raise _Failed("not_accessible", "репозиторий недоступен установкам GitHub App")

    # --- запись результата -----------------------------------------------------------------------------

    async def _finish(self, ctx: DbContext, row: Mapping[str, Any], effect: Any) -> str:
        async with transaction(self.engine, ctx) as conn:
            current = await get_verification(conn, row["id"], lock=True)
            if current is None or current["status"] != "PENDING":
                return "duplicate"
            try:
                async with conn.begin_nested():
                    result_id = await effect(conn)
            except _Failed as failed:
                await self._record_failure(conn, row, failed)
                return "failed"
            await verifications.succeed(conn, row["id"], result_id)
        return "succeeded"

    async def _fail(self, ctx: DbContext, row: Mapping[str, Any], failed: _Failed) -> str:
        async with transaction(self.engine, ctx) as conn:
            current = await get_verification(conn, row["id"], lock=True)
            if current is None or current["status"] != "PENDING":
                return "duplicate"
            await self._record_failure(conn, row, failed)
        return "failed"

    async def _record_failure(self, conn: AsyncConnection, row: Mapping[str, Any], failed: _Failed) -> None:
        if row["kind"] == "installation":
            # Установка, созданная API в статусе PENDING (или повторно проверяемая), не подключается.
            await conn.execute(
                text(
                    "UPDATE github_installations SET status = 'REVOKED' WHERE installation_id = :i AND status = 'PENDING'"
                ),
                {"i": int(row["params"]["installation_id"])},
            )
        await verifications.fail(conn, row["id"], failed.code, failed.detail)
        action = "github.installation.connect" if row["kind"] == "installation" else "repository.attach"
        await audit.record(
            conn,
            action=action,
            resource_type="github_verification",
            resource_id=row["id"],
            actor_type="user",
            actor_id=row["requested_by"],
            workspace_id=row["workspace_id"],
            outcome="denied",
            metadata={"reason": failed.code, **(failed.audit_meta or {})},
        )
        log.info("github verification failed", extra={"kind": row["kind"], "reason": failed.code})

    async def _connect(self, conn: AsyncConnection, row: Mapping[str, Any], installation: Installation) -> uuid.UUID:
        try:
            inst_row, created = await store.connect_installation(
                conn, workspace_id=row["workspace_id"], installation=installation, actor_id=row["requested_by"]
            )
        except store.InstallationConflictError as exc:
            raise _Failed("conflict", "установка подключена к другому workspace") from exc
        await audit.record(
            conn,
            action="github.installation.connect",
            resource_type="github_installation",
            resource_id=inst_row["id"],
            actor_type="user",
            actor_id=row["requested_by"],
            workspace_id=row["workspace_id"],
            metadata={
                "installation_id": installation.installation_id,
                "account_login": installation.account_login,
                "permissions": installation.permissions,
                "reconnected": not created,
                "verification_id": str(row["id"]),
            },
        )
        result: uuid.UUID = inst_row["id"]
        return result

    async def _attach(
        self,
        conn: AsyncConnection,
        row: Mapping[str, Any],
        params: Mapping[str, Any],
        inst: Mapping[str, Any],
        info: RepoInfo,
    ) -> uuid.UUID:
        project_id = row["project_id"]
        canonical = f"{str(params['web_url']).rstrip('/')}/{info.full_name}"
        try:
            repo = await projects.attach_github_repository(
                conn,
                project_id=project_id,
                workspace_id=row["workspace_id"],
                actor_id=row["requested_by"],
                local_path=str(params["local_path"]),
                remote_url=canonical,
                full_name=info.full_name,
                github_repo_id=info.id,
                github_installation_id=inst["id"],
                default_branch=params.get("default_branch") or info.default_branch,
                roots=self.repository_roots,
            )
        except ProblemError as exc:
            code = exc.errors[0].code if exc.errors else exc.code
            detail = exc.errors[0].message if exc.errors else (exc.detail or code)
            raise _Failed(code, detail) from exc
        head = (
            await conn.execute(
                text("SELECT last_synced_commit_sha FROM repositories WHERE id = :id"), {"id": repo["id"]}
            )
        ).scalar()
        if head:  # как для local: первичная индексация (repository.attached → code_intelligence)
            await enqueue_indexing_job(
                conn, workspace_id=row["workspace_id"], repository_id=repo["id"], commit_sha=head, kind="full"
            )
        result: uuid.UUID = repo["id"]
        return result
