"""Импорт документации в worker-index (очередь `docs`; P5-10, SECURITY_ARCHITECTURE §9, ADR-0012).

Вызов на одно внешнее действие `docs_download` (ledger `external_actions`, создан refresh-эндпоинтом через
`sys_plan_docs_download` или агентной загрузкой после approval `DOCS_DOWNLOAD`):

1. транзакция: источник включён; снимок `FETCHING` (ссылка на external_action); ledger `begin` (fencing);
2. вне транзакции: `security.ssrf` — единственный HTTP-путь (DNS pin, denylist, редиректы, MIME/размер);
3. нормализация (`content`): HTML-санитизация, архивы с лимитами, секции, лицензия; нарушение ⇒ QUARANTINED;
4. артефакты: исходник и нормализованный текст (kind `documentation`, project_id NULL — уровень workspace);
5. транзакция: снимок READY/QUARANTINED (+ content_hash, storage_ref, mime, size), секции, предыдущий
   READY → SUPERSEDED, `last_refreshed_at`; ledger `confirm` (external_ref = id снимка).
   Тот же content_hash, что у существующего снимка, ⇒ новый снимок не создаётся (строка FETCHING →
   SUPERSEDED с пометкой `unchanged`), подтверждение ledger ссылается на существующий снимок.

Ошибка загрузки ⇒ снимок FAILED (код ошибки без адресов), ledger `fail`.
Ledger и approvals — порты (`DocsDownloadPort`), реализует оркестрация; модуль её не импортирует.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_code_intelligence.docs.content import ProcessedDocument, process_content
from aicd_code_intelligence.docs.policy import DocsDownloadPort
from aicd_platform.artifacts import ArtifactStore, StoredArtifact
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.ssrf import FetchError, FetchResult, SsrfFetcher


@dataclass(frozen=True)
class ImportOutcome:
    snapshot_id: uuid.UUID
    status: str  # READY | QUARANTINED | FAILED | UNCHANGED
    sections: int = 0
    error: str | None = None
    hidden_fragments: int = 0
    license: str | None = None


class DocumentationImporter:
    def __init__(
        self, engine: AsyncEngine, store: ArtifactStore, fetcher: SsrfFetcher, ledger: DocsDownloadPort
    ) -> None:
        self._engine = engine
        self._store = store
        self._fetcher = fetcher
        self._ledger = ledger

    async def run(
        self, *, workspace_id: uuid.UUID, source_id: uuid.UUID, external_action_id: uuid.UUID, fencing_token: int
    ) -> ImportOutcome:
        ctx = DbContext(workspace_id=workspace_id, actor_type="system")
        snapshot_id = uuid7()
        async with transaction(self._engine, ctx) as conn:
            source = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, uri_or_file_ref, enabled, license_info FROM documentation_sources"
                            " WHERE id = :id FOR SHARE"
                        ),
                        {"id": source_id},
                    )
                )
                .mappings()
                .first()
            )
            if source is None or not source["enabled"]:
                await self._ledger.fail(
                    conn, external_action_id, error="source disabled or missing", fencing_token=None
                )
                raise LookupError("источник документации отключён или не найден")
            await conn.execute(
                text(
                    "INSERT INTO documentation_snapshots (id, workspace_id, source_id, external_action_id)"
                    " VALUES (:id, :ws, :s, :a)"
                ),
                {"id": snapshot_id, "ws": workspace_id, "s": source_id, "a": external_action_id},
            )
            await self._ledger.begin(conn, external_action_id, fencing_token)
        uri = str(source["uri_or_file_ref"])
        try:
            fetched = await self._fetcher.fetch(uri)
        except FetchError as exc:
            async with transaction(self._engine, ctx) as conn:
                await _finish_failed(conn, snapshot_id, f"{exc.code}: {exc.message}")
                await self._ledger.fail(conn, external_action_id, error=exc.code, fencing_token=fencing_token)
            return ImportOutcome(snapshot_id, "FAILED", error=exc.code)
        name = PurePosixPath(urlsplit(fetched.final_url).path).name or "index"
        processed = await asyncio.to_thread(process_content, fetched.mime_type, fetched.content, name=name)
        raw = await asyncio.to_thread(self._put, workspace_id, fetched.content)
        normalized = (
            await asyncio.to_thread(self._put, workspace_id, processed.normalized) if processed.normalized else None
        )
        license_info = processed.license or source["license_info"]
        async with transaction(self._engine, ctx) as conn:
            existing = (
                await conn.execute(
                    text(
                        "SELECT id FROM documentation_snapshots WHERE source_id = :s AND content_hash = :h"
                        " AND id <> :id"
                    ),
                    {"s": source_id, "h": fetched.sha256, "id": snapshot_id},
                )
            ).scalar_one_or_none()
            if existing is not None:
                await conn.execute(
                    text(
                        "UPDATE documentation_snapshots SET status = 'SUPERSEDED', fetched_at = now(),"
                        " error = :e WHERE id = :id"
                    ),
                    {"id": snapshot_id, "e": f"unchanged: identical to snapshot {existing}"},
                )
                await _touch_source(conn, source_id)
                await self._ledger.confirm(
                    conn,
                    external_action_id,
                    fencing_token=fencing_token,
                    external_ref=str(existing),
                    result={"snapshot_id": str(existing), "unchanged": True, "sha256": fetched.sha256},
                )
                return ImportOutcome(existing, "UNCHANGED")
            raw_id = await _insert_artifact(conn, workspace_id, raw, fetched.mime_type)
            normalized_id = (
                await _insert_artifact(conn, workspace_id, normalized, "text/markdown") if normalized else None
            )
            await _finish(conn, snapshot_id, source_id, fetched, processed, raw_id, normalized_id)
            await _touch_source(conn, source_id)
            await self._ledger.confirm(
                conn,
                external_action_id,
                fencing_token=fencing_token,
                external_ref=str(snapshot_id),
                result=_ledger_result(snapshot_id, fetched, processed, license_info),
            )
        return ImportOutcome(
            snapshot_id,
            processed.status,
            sections=len(processed.sections),
            error=processed.error,
            hidden_fragments=processed.hidden_fragments,
            license=license_info,
        )

    def _put(self, workspace_id: uuid.UUID, data: bytes) -> tuple[uuid.UUID, StoredArtifact]:
        artifact_id = uuid7()
        return artifact_id, self._store.put(workspace_id, artifact_id, io.BytesIO(data))


def _ledger_result(
    snapshot_id: uuid.UUID, fetched: FetchResult, processed: ProcessedDocument, license_info: str | None
) -> dict[str, Any]:
    return {
        "snapshot_id": str(snapshot_id),
        "status": processed.status,
        "sha256": fetched.sha256,
        "size_bytes": fetched.size_bytes,
        "mime_type": fetched.mime_type,
        "redirects": len(fetched.redirects),
        "sections": len(processed.sections),
        "hidden_fragments": processed.hidden_fragments,
        "parse_deferred": processed.parse_deferred,
        "license": license_info,
    }


async def _insert_artifact(
    conn: AsyncConnection, workspace_id: uuid.UUID, stored: tuple[uuid.UUID, StoredArtifact], mime: str
) -> uuid.UUID:
    artifact_id, info = stored
    await conn.execute(
        text(
            "INSERT INTO artifacts (id, workspace_id, project_id, run_id, kind, storage_ref, sha256, size_bytes,"
            " mime_type, is_sensitive, redaction_applied, retention_class)"
            " VALUES (:id, :ws, NULL, NULL, 'documentation', :ref, :sha, :size, :mime, false, false, 'standard')"
        ),
        {
            "id": artifact_id,
            "ws": workspace_id,
            "ref": info.storage_ref,
            "sha": info.sha256,
            "size": info.size_bytes,
            "mime": mime,
        },
    )
    return artifact_id


async def _finish_failed(conn: AsyncConnection, snapshot_id: uuid.UUID, error: str) -> None:
    await conn.execute(
        text("UPDATE documentation_snapshots SET status = 'FAILED', error = :e WHERE id = :id"),
        {"id": snapshot_id, "e": error[:4000]},
    )


async def _finish(
    conn: AsyncConnection,
    snapshot_id: uuid.UUID,
    source_id: uuid.UUID,
    fetched: FetchResult,
    processed: ProcessedDocument,
    raw_id: uuid.UUID,
    normalized_id: uuid.UUID | None,
) -> None:
    ready = processed.status == "READY"
    if ready:
        await conn.execute(
            text(
                "UPDATE documentation_snapshots SET status = 'SUPERSEDED'"
                " WHERE source_id = :s AND status = 'READY' AND id <> :id"
            ),
            {"s": source_id, "id": snapshot_id},
        )
    notes: list[str] = []
    if processed.error:
        notes.append(processed.error)
    if processed.hidden_fragments:
        notes.append(f"suspicious: {processed.hidden_fragments} hidden text fragment(s) excluded")
    if processed.parse_deferred:
        notes.append("pdf: parsing deferred to sandbox parser")
    await conn.execute(
        text(
            "UPDATE documentation_snapshots SET status = :st, content_hash = :h, storage_ref = :ref,"
            " size_bytes = :size, mime_type = :mime, fetched_at = now(), error = :err WHERE id = :id"
        ),
        {
            "id": snapshot_id,
            "st": processed.status,
            "h": fetched.sha256,
            # READY — нормализованный текст (то, что читают агенты); иначе — исходник для разбора.
            "ref": normalized_id if (ready and normalized_id) else raw_id,
            "size": fetched.size_bytes,
            "mime": fetched.mime_type,
            "err": "; ".join(notes)[:4000] or None,
        },
    )
    if not ready or normalized_id is None:
        return
    for section in processed.sections:
        await conn.execute(
            text(
                "INSERT INTO documentation_sections (id, workspace_id, snapshot_id, section_path, heading, body_ref,"
                " content_hash) VALUES (:id, app_current_workspace(), :snap, :path, :heading, :ref, :h)"
            ),
            {
                "id": uuid7(),
                "snap": snapshot_id,
                "path": section.section_path,
                "heading": section.heading,
                "ref": f"artifact:{normalized_id}#bytes={section.start}-{section.end}",
                "h": section.content_hash,
            },
        )


async def _touch_source(conn: AsyncConnection, source_id: uuid.UUID) -> None:
    await conn.execute(
        text("UPDATE documentation_sources SET last_refreshed_at = now() WHERE id = :id"), {"id": source_id}
    )
