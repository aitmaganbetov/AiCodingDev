"""Состав установки GitHub App («что разрешено») — чтение воркером, кэш для UI (FR-PRJ-03).

Форма привязки репозитория к проекту предлагает выбор из репозиториев, доступных установке, вместо ручного
ввода URL. Список может прочитать только воркер: закрытый ключ App есть лишь у него (OQ-47 (2),
SECURITY_ARCHITECTURE §6.1). Поэтому — та же двухфазность, что у `github_verification`, но без записи
в БД: список не состояние платформы, а проекция внешнего состояния GitHub (`repo_cache`).

1. API (`request_sync`) кладёт событие outbox `github.repositories.sync` (202; повторные нажатия гасит
   `repo_cache.claim_refresh`).
2. Воркер (`GitHubRepositorySync.sync`) берёт installation token операции `list` (`metadata: read` —
   минимально возможные права, SC-GH-04), читает `/installation/repositories` и пишет в `repo_cache`.

Список ничего не разрешает: доступ к выбранному репозиторию всё равно проверяется заново при привязке
(`github_verification`, kind=`repository`). Устаревший или пустой кэш — лишь неудобство UI.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_integrations_github.api import GitHubClient, GitHubError, repo_cache, store
from aicd_platform import outbox
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction

log = logging.getLogger("aicd.github")

SYNC_EVENT: Final = "github.repositories.sync"


async def request_sync(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    installation_row_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Событие в транзакции вызывающего: API (кнопка «обновить», `actor_id`) или воркер по вебхуку GitHub
    (`actor_id=None`). Аудита нет: читается публичный состав установки (x-audit-action: none)."""
    await outbox.emit(
        conn,
        event_type=SYNC_EVENT,
        aggregate_type="github_installation",
        aggregate_id=installation_row_id,
        workspace_id=workspace_id,
        payload={
            "installation_row_id": str(installation_row_id),
            **({"requested_by": str(actor_id)} if actor_id is not None else {}),
        },
    )


class GitHubRepositorySync:
    """Воркер: `/installation/repositories` → `repo_cache`."""

    def __init__(self, engine: AsyncEngine, client: GitHubClient, redis: Any, *, environment: str) -> None:
        self.engine = engine
        self.client = client
        self.redis = redis
        self.environment = environment

    async def sync(self, ws: uuid.UUID, installation_row_id: uuid.UUID) -> str:
        ctx = DbContext(workspace_id=ws, actor_type="system")
        async with transaction(self.engine, ctx) as conn:
            row = await store.get_installation(conn, installation_row_id)
        if row is None or row["status"] != "ACTIVE":
            # Установка отключена/ещё не проверена: показывать нечего — и прежний список больше не верен.
            await repo_cache.drop(self.redis, environment=self.environment, installation_id=installation_row_id)
            return "ignored"
        try:
            token = await self.client.installation_token(int(row["installation_id"]), "list")
            repos = await self.client.installation_repositories(token)
        except GitHubError as exc:
            # Недоступность GitHub не должна ронять обработку события: UI покажет прежний список и дату.
            log.warning("github repositories sync failed: %s", exc, extra={"installation": str(installation_row_id)})
            return "failed"
        await repo_cache.save(
            self.redis,
            environment=self.environment,
            installation_id=installation_row_id,
            repos=repos,
            now=utcnow(),
        )
        return "synced"

    async def forget(self, installation_row_id: uuid.UUID) -> None:
        """Список установки больше не верен (отключение, `installation_repositories` от GitHub)."""
        await repo_cache.drop(self.redis, environment=self.environment, installation_id=installation_row_id)
