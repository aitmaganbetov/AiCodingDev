"""Очередь `github` (P8-03…P8-05): PR workflow, merge после approval, обработка вебхуков.

Маршрутизация outbox → актор `github.handle_event`:

* `github.pr.requested` (API `POST /runs/{run_id}/pull-requests`) → `GitHubWorkflow.open_pull_request`;
* `approval.decided` со статусом APPROVED для `MERGE` предмета `scope=github` → `GitHubWorkflow.merge`;
* `github.webhook.received` (API вебхука после проверки HMAC) → `GitHubWorkflow.process_webhook`;
* `github.verification.requested` (API подключения установки / привязки GitHub-репозитория) →
  `GitHubVerifier.verify`: закрытый ключ App есть только у воркера (OQ-47 (2), SECURITY_ARCHITECTURE §6.1).

Отметка `processed_events(github.handle_event, event_id)` фиксируется только после успешной обработки
(сбой ⇒ откат ⇒ повтор Dramatiq). Повтор после краха между вызовом GitHub и `CONFIRMED` безопасен:
ledger сверяет состояние GitHub до любого повторного вызова (ADR-0012).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_integrations_github.api import GitHubClient, build_client
from aicd_orchestration.github_flow import PR_REQUESTED_EVENT, WEBHOOK_EVENT, GitHubWorkflow
from aicd_orchestration.github_verification import VERIFICATION_EVENT, GitHubVerifier
from aicd_platform import context
from aicd_worker.handlers import claimed
from aicd_worker.runtime import Runtime
from aicd_worker.settings import WorkerSettings

GITHUB_CONSUMER: Final = "github.handle_event"
GITHUB_EVENT_TYPES: Final = frozenset({PR_REQUESTED_EVENT, WEBHOOK_EVENT, VERIFICATION_EVENT})


def is_github_event(event: Mapping[str, Any]) -> bool:
    etype = event.get("event_type")
    if etype in GITHUB_EVENT_TYPES:
        return True
    if etype != "approval.decided":
        return False
    payload = event.get("payload") or {}
    subject = payload.get("subject") if isinstance(payload, Mapping) else None
    return (
        payload.get("action_type") == "MERGE"
        and payload.get("status") == "APPROVED"
        and isinstance(subject, Mapping)
        and subject.get("scope") == "github"
    )


def build_workflow(
    settings: WorkerSettings, engine: AsyncEngine, client: GitHubClient | None = None
) -> GitHubWorkflow | None:
    """Исполнитель GitHub-действий, если GitHub App настроен (иначе интеграция выключена)."""
    client = client or build_client(settings)
    if not client.configured:
        return None
    return GitHubWorkflow(
        engine,
        client,
        repository_roots=settings.repository_roots,
        git_url=settings.github_git_url or settings.github_web_url,
        proxy_url=settings.egress_proxy_url,
    )


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


async def handle_event(rt: Runtime, event: dict[str, Any]) -> str:
    event_id = _uuid(event.get("event_id"))
    if event_id is None or not is_github_event(event):
        return "ignored"
    workflow = rt.github
    if workflow is None:
        raise RuntimeError("GitHub App не настроен на воркере — событие будет повторено")
    ws = _uuid(event.get("workspace_id"))
    if ws is not None:
        context.bind(workspace_id=ws)
    async with claimed(rt.engine, GITHUB_CONSUMER, event_id) as first:
        if not first:
            return "duplicate"
        etype = event.get("event_type")
        payload = event.get("payload") or {}
        if etype == WEBHOOK_EVENT:
            return await workflow.process_webhook(event)
        if ws is None:
            return "ignored"
        if etype == PR_REQUESTED_EVENT:
            return await workflow.open_pull_request(ws, payload)
        if etype == VERIFICATION_EVENT:
            verification_id = _uuid(payload.get("verification_id")) or _uuid(event.get("aggregate_id"))
            if verification_id is None:
                return "ignored"
            verifier = GitHubVerifier(workflow.engine, workflow.client, repository_roots=workflow.repository_roots)
            return await verifier.verify(ws, verification_id)
        approval_id = _uuid(payload.get("approval_id")) or _uuid(event.get("aggregate_id"))
        if approval_id is None:
            return "ignored"
        return await workflow.merge(ws, approval_id)
