"""Скачивание артефакта (`GET /artifacts/{artifact_id}/download`, P7-08/P7-10; дополнение к ТЗ).

Содержимое всегда отдаётся потоком через API (200 `application/octet-stream`, `Content-Disposition:
attachment`, `Cache-Control: no-store`): браузерный клиент читает артефакты (diff задачи, отчёты) через
`fetch`, а CSP SPA разрешает только `connect-src 'self'` — редирект на подписанный URL другого origin
был бы заблокирован. Доступ:

* артефакт чужого workspace неотличим от отсутствующего — 404 через `sys_resolve_artifact_workspace`
  (миграция 0011, AC-12); истёкший (`expires_at` в прошлом) или пропавший в хранилище — тоже 404;
* `artifact:download` — всем ролям workspace; `cli_raw_log` (сырые логи CLI до редактирования) —
  дополнительно `artifact:download_raw` (admin/owner, API_DESIGN §5.1); отказ аудируется;
* каждое скачивание пишет audit `artifact.download`;
* целостность: поток проверяется по `artifacts.sha256`; при расхождении передача обрывается (клиент
  получает незавершённый ответ, а не подменённые данные).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy import text
from starlette.concurrency import iterate_in_threadpool
from starlette.responses import Response, StreamingResponse

from aicd_api.deps import _audit_denied, app_state, authorize, current_principal
from aicd_api.routers import op
from aicd_api.routers.runs import resolve_resource
from aicd_identity import api as identity
from aicd_identity.rbac import has_permission
from aicd_platform import audit
from aicd_platform.artifacts import ArtifactIntegrityError, ArtifactNotFoundError
from aicd_platform.db import transaction
from aicd_platform.errors import ProblemError

router = APIRouter()
log = logging.getLogger("aicd.api.artifacts")

RAW_KIND = "cli_raw_log"
_EXTENSIONS = {
    "application/json": "json",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/x-diff": "diff",
    "text/x-patch": "patch",
    "application/xml": "xml",
    "text/html": "html",
    "application/gzip": "gz",
    "application/zip": "zip",
}


def download_filename(kind: str, artifact_id: uuid.UUID, mime_type: str) -> str:
    """Безопасное имя файла: только из вида и id артефакта (не из пользовательских данных)."""
    ext = _EXTENSIONS.get(mime_type, "bin")
    return f"{kind}-{artifact_id}.{ext}"


async def _guarded(chunks: Iterator[bytes], artifact_id: uuid.UUID) -> AsyncIterator[bytes]:
    try:
        async for chunk in iterate_in_threadpool(chunks):
            yield chunk
    except ArtifactIntegrityError:
        log.error("artifact integrity mismatch during download", extra={"artifact_id": str(artifact_id)})
        raise


@router.get("/artifacts/{artifact_id}/download", tags=["evidence"], **op("downloadArtifact", "artifact:download"))
async def download_artifact(
    request: Request,
    artifact_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await resolve_resource(request, principal, "sys_resolve_artifact_workspace", artifact_id)
    await authorize(request, access, "artifact:download")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, kind, storage_ref, sha256, mime_type, size_bytes,"
                        " expires_at IS NOT NULL AND expires_at <= now() AS expired"
                        " FROM artifacts WHERE id = :id"
                    ),
                    {"id": artifact_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["expired"]:
            raise ProblemError("not_found")
        raw_denied = row["kind"] == RAW_KIND and not has_permission(access.role, "artifact:download_raw")
        if raw_denied:
            await _audit_denied(conn, access, "artifact:download_raw", request)
    if raw_denied:  # отказ уже зафиксирован в audit (транзакция закрыта до исключения)
        raise ProblemError("forbidden")
    storage_ref, sha256 = str(row["storage_ref"]), str(row["sha256"])
    try:
        chunks = await asyncio.to_thread(state.artifacts.open, storage_ref, expected_sha256=sha256)
    except ArtifactNotFoundError as exc:
        raise ProblemError("not_found") from exc
    except ValueError as exc:  # повреждённый storage_ref в БД — объект недоступен, не 500
        log.error("invalid artifact storage_ref", extra={"artifact_id": str(artifact_id)})
        raise ProblemError("not_found") from exc
    async with transaction(state.engine, access.db()) as conn:
        await audit.record(
            conn,
            action="artifact.download",
            resource_type="artifact",
            resource_id=artifact_id,
            actor_type="user",
            actor_id=principal.user_id,
            workspace_id=access.workspace_id,
            metadata={"kind": row["kind"], "sha256": sha256, "size_bytes": int(row["size_bytes"])},
        )
    filename = download_filename(str(row["kind"]), artifact_id, str(row["mime_type"]))
    return StreamingResponse(
        _guarded(chunks, artifact_id),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "Content-Length": str(int(row["size_bytes"])),
            "X-Content-Type-Options": "nosniff",
        },
    )
