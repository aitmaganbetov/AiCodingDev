"""Загрузка документации в воркере (очередь `docs`; P5-10, AC-10, ADR-0012, SECURITY_ARCHITECTURE §9).

Событие outbox `documentation_source.refresh_requested` {source_id, external_action_id} порождают ручной
refresh (`POST /documentation/sources/{id}/refresh`, согласие человека) и оркестратор после APPROVED
`DOCS_DOWNLOAD` (`docs_flow.plan_download`). Обработчик:

1. отметка `processed_events(docs.import_documentation, event_id)` фиксируется только после успешной обработки
   (повторная доставка после успеха — no-op; сбой — повтор Dramatiq);
2. внешнее действие `docs_download` в статусе PLANNED/EXECUTING ⇒ аренда `external_action` (fencing token
   ledger) и `DocumentationImporter.run` — единственный HTTP-путь через `security.ssrf`
   (`ProxyTransport(AICD_EGRESS_PROXY_URL)` при заданном egress-proxy, иначе `PinnedTransport`);
   CONFIRMED/FAILED (повтор после краха) — загрузка не повторяется;
3. `Orchestrator.on_docs_available` — задачи BLOCKED(DOCS_REQUIRED) активных run проектов источника
   оцениваются заново (снимок READY ⇒ новая попытка с разделом docs), READY-задачи ставятся в exec.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_code_intelligence.docs.importer import DocumentationImporter
from aicd_orchestration import external_actions
from aicd_orchestration.docs_ports import DocsLedger
from aicd_platform import context, leases
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_security.ssrf import FetchTransport, PinnedTransport, ProxyTransport, SsrfFetcher, SsrfPolicy
from aicd_worker.handlers import claimed, execution_message
from aicd_worker.runtime import Runtime
from aicd_worker.settings import WorkerSettings

log = logging.getLogger("aicd.worker.docs")

DOCS_CONSUMER: Final = "docs.import_documentation"
REFRESH_EVENT: Final = "documentation_source.refresh_requested"
LEASE_TTL_SECONDS: Final = 600  # больше общего тайм-аута загрузки SsrfPolicy.total_timeout

DocsOutcome = Literal["ignored", "duplicate", "imported", "skipped"]


def build_fetcher(settings: WorkerSettings) -> SsrfFetcher:
    transport: FetchTransport = (
        ProxyTransport(settings.egress_proxy_url) if settings.egress_proxy_url else PinnedTransport()
    )
    policy = SsrfPolicy(max_bytes=settings.docs_max_bytes, total_timeout=settings.docs_fetch_timeout_seconds)
    return SsrfFetcher(policy, transport=transport)


def build_importer(
    settings: WorkerSettings, engine: AsyncEngine, store: ArtifactStore, fetcher: SsrfFetcher | None = None
) -> DocumentationImporter:
    return DocumentationImporter(engine, store, fetcher or build_fetcher(settings), DocsLedger())


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


async def import_documentation(rt: Runtime, event: dict[str, Any]) -> DocsOutcome:
    payload = event.get("payload") or {}
    event_id = _uuid(event.get("event_id"))
    ws = _uuid(event.get("workspace_id"))
    source_id = _uuid(payload.get("source_id"))
    action_id = _uuid(payload.get("external_action_id"))
    if event.get("event_type") != REFRESH_EVENT or event_id is None or ws is None:
        return "ignored"
    if source_id is None or action_id is None:
        return "ignored"
    if rt.docs_importer is None:
        raise RuntimeError("импортёр документации не настроен в runtime воркера")
    context.bind(workspace_id=ws)
    async with claimed(rt.engine, DOCS_CONSUMER, event_id) as first:
        if not first:
            return "duplicate"
        outcome = await _import(rt, rt.docs_importer, ws, source_id, action_id)
        for task_id in await rt.orchestrator.on_docs_available(ws, source_id):
            await rt.dispatch_execution(execution_message(task_id, ws, event_id))
        return outcome


async def _import(
    rt: Runtime, importer: DocumentationImporter, ws: uuid.UUID, source_id: uuid.UUID, action_id: uuid.UUID
) -> DocsOutcome:
    ctx = DbContext(workspace_id=ws, actor_type="system")
    worker_id = leases.worker_identity("docs")
    async with transaction(rt.engine, ctx) as conn:
        action = await external_actions.get(conn, action_id)
        if action is None or action.action_type != "docs_download":
            return "ignored"
        if action.status not in ("PLANNED", "EXECUTING"):
            return "skipped"  # загрузка уже завершена (повтор после краха между import и отметкой)
        lease = await leases.acquire(
            conn,
            resource_type="external_action",
            resource_id=action_id,
            worker_id=worker_id,
            workspace_id=ws,
            ttl_seconds=LEASE_TTL_SECONDS,
        )
    if lease is None:
        raise RuntimeError("загрузку уже выполняет другой воркер")  # повтор Dramatiq после освобождения аренды
    try:
        result = await importer.run(
            workspace_id=ws, source_id=source_id, external_action_id=action_id, fencing_token=lease.fencing_token
        )
        log.info(
            "docs.imported",
            extra={"source_id": str(source_id), "status": result.status, "snapshot_id": str(result.snapshot_id)},
        )
    except LookupError:
        # Источник отключён/удалён: импортёр откатил свою транзакцию — фиксируем отказ в ledger отдельно.
        async with transaction(rt.engine, ctx) as conn:
            await DocsLedger().fail(conn, action_id, error="source disabled or missing", fencing_token=None)
    finally:
        async with transaction(rt.engine, ctx) as conn:
            await leases.release(conn, lease)
    return "imported"
