"""Политика документации проекта и источники документации — доступ к БД (P5-09, P5-10).

* Политика хранится в `project_settings.documentation_mode` (0001); ETag — `"v<project_settings.version>"`
  (в API поле `settings_version`, как в `ProjectSettings`). Изменение действует для новых runs и следующих
  context packs; уже выданные context packs не меняются (снимок режима хранится в runs/context_packs).
* Источники — `documentation_sources` (уровень workspace, OQ-DB-05). Регистрация НЕ загружает содержимое.
  Refresh — `sys_plan_docs_download()` (SECURITY DEFINER, 0007): ledger `external_actions` остаётся
  во владении оркестрации/worker, у `aicd_app` нет INSERT на него.

Перечисления контракта ≠ CHECK БД (открытый вопрос, сопоставление в API-слое):
  source_type   API url|file|git        → БД url | pdf/markdown (по расширению/MIME артефакта) | git;
                БД html → API url, pdf/markdown → API file;
  trust_level   API official|internal|community → БД как есть; БД vendor/untrusted → API community;
  refresh_policy API manual|scheduled  → БД manual | weekly; БД daily/weekly/monthly → API scheduled.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_code_intelligence.docs.policy import DocumentationMode
from aicd_platform import audit, outbox
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7
from aicd_security.ssrf import SsrfBlockedError, SsrfPolicy, parse_url

_ARTIFACT_REF = re.compile(r"^artifact:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

API_TO_DB_REFRESH: Final = {"manual": "manual", "scheduled": "weekly"}
DB_TO_API_SOURCE_TYPE: Final = {"url": "url", "html": "url", "pdf": "file", "markdown": "file", "git": "git"}
DB_TO_API_TRUST: Final = {
    "official": "official",
    "internal": "internal",
    "community": "community",
    "vendor": "community",
    "untrusted": "community",
}


# --- Политика -----------------------------------------------------------------------------------


async def get_policy(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                text("SELECT project_id, documentation_mode, version FROM project_settings WHERE project_id = :p"),
                {"p": project_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def policy_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": row["project_id"],
        "documentation_mode": row["documentation_mode"],
        "settings_version": row["version"],
    }


async def update_policy(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_version: int,
    mode: DocumentationMode | str,
) -> dict[str, Any]:
    new_mode = DocumentationMode(mode)
    current = await get_policy(conn, project_id)
    if current is None:
        raise ProblemError("not_found")
    if current["version"] != expected_version:
        raise ProblemError("precondition_failed", headers={"ETag": f'"v{current["version"]}"'})
    result = await conn.execute(
        text(
            "UPDATE project_settings SET documentation_mode = :m, updated_by = :by, version = version + 1"
            " WHERE project_id = :p AND version = :v"
        ),
        {"m": new_mode.value, "by": actor_id, "p": project_id, "v": expected_version},
    )
    if result.rowcount != 1:
        raise ProblemError("precondition_failed")
    await audit.record(
        conn,
        action="project.documentation_policy.update",
        resource_type="project_settings",
        resource_id=project_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"from": current["documentation_mode"], "to": new_mode.value},
    )
    updated = await get_policy(conn, project_id)
    if updated is None:  # pragma: no cover
        raise RuntimeError("project_settings vanished inside transaction")
    return updated


# --- Источники ----------------------------------------------------------------------------------

_SOURCE_SELECT = (
    "SELECT s.id, s.workspace_id, s.technology_version_id, s.source_type, s.uri_or_file_ref, s.trust_level,"
    " s.license_info, s.refresh_policy, s.enabled, s.approval_id, s.created_by, s.last_refreshed_at, s.created_at,"
    " ls.id AS snap_id, ls.content_hash AS snap_hash, ls.fetched_at AS snap_fetched_at, ls.status AS snap_status,"
    " ls.created_at AS snap_created_at"
    " FROM documentation_sources s"
    " LEFT JOIN LATERAL (SELECT d.id, d.content_hash, d.fetched_at, d.status, d.created_at"
    "   FROM documentation_snapshots d WHERE d.source_id = s.id AND d.status <> 'SUPERSEDED'"
    "   ORDER BY d.created_at DESC, d.id DESC LIMIT 1) ls ON true"
)


def source_view(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = None
    if row.get("snap_id") is not None:
        snapshot = {
            "id": row["snap_id"],
            "fetched_at": row["snap_fetched_at"] or row["snap_created_at"],
            "status": row["snap_status"],
        }
        if row.get("snap_hash"):
            snapshot["content_hash"] = row["snap_hash"]
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "technology_version_id": row["technology_version_id"],
        "source_type": DB_TO_API_SOURCE_TYPE[row["source_type"]],
        "uri_or_file_ref": row["uri_or_file_ref"],
        "trust_level": DB_TO_API_TRUST[row["trust_level"]],
        "license_info": row["license_info"],
        "refresh_policy": "manual" if row["refresh_policy"] == "manual" else "scheduled",
        "enabled": row["enabled"],
        "latest_snapshot": snapshot,
        "created_at": row["created_at"],
    }


async def get_source(conn: AsyncConnection, source_id: uuid.UUID) -> dict[str, Any] | None:
    row = (await conn.execute(text(_SOURCE_SELECT + " WHERE s.id = :id"), {"id": source_id})).mappings().first()
    return dict(row) if row else None


async def source_workspace(conn: AsyncConnection, source_id: uuid.UUID) -> uuid.UUID | None:
    """workspace источника — под RLS виден только членам workspace (иначе 404)."""
    ws: uuid.UUID | None = (
        await conn.execute(text("SELECT workspace_id FROM documentation_sources WHERE id = :id"), {"id": source_id})
    ).scalar_one_or_none()
    return ws


async def list_sources(
    conn: AsyncConnection,
    *,
    technology_version_id: uuid.UUID | None,
    enabled: bool | None,
    before: tuple[Any, uuid.UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    clauses = ["s.workspace_id = app_current_workspace()"]
    params: dict[str, Any] = {"lim": limit}
    if technology_version_id is not None:
        clauses.append("s.technology_version_id = :tv")
        params["tv"] = technology_version_id
    if enabled is not None:
        clauses.append("s.enabled = :en")
        params["en"] = enabled
    if before:
        clauses.append("(s.created_at, s.id) < (:t, :id)")
        params.update(t=before[0], id=before[1])
    sql = _SOURCE_SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY s.created_at DESC, s.id DESC LIMIT :lim"
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


def _invalid(pointer: str, code: str, message: str) -> ProblemError:
    return ProblemError("validation_error", errors=[FieldError(pointer, code, message)])


def validate_remote_uri(uri: str) -> str:
    """Статическая проверка SSRF при регистрации (без DNS): https, порт 443, без userinfo, внешнее имя.
    Полный конвейер (DNS, pinned IP, редиректы, MIME, размер) — при загрузке (`security.ssrf`)."""
    try:
        parse_url(uri, SsrfPolicy())
    except SsrfBlockedError as exc:
        raise _invalid("/uri_or_file_ref", "ssrf_blocked", exc.message) from exc
    return uri


async def _db_source_type(conn: AsyncConnection, api_type: str, uri: str) -> str:
    if api_type == "url":
        validate_remote_uri(uri)
        return "url"
    if api_type == "git":
        validate_remote_uri(uri)
        return "git"
    # file: загруженный артефакт workspace или https-ссылка на файл .pdf/.md
    m = _ARTIFACT_REF.fullmatch(uri)
    if m:
        mime = (
            await conn.execute(
                text("SELECT mime_type FROM artifacts WHERE id = :id AND workspace_id = app_current_workspace()"),
                {"id": uuid.UUID(m.group(1))},
            )
        ).scalar_one_or_none()
        if mime is None:
            raise ProblemError("not_found", detail="Артефакт не найден")
        return "pdf" if mime == "application/pdf" else "markdown"
    validate_remote_uri(uri)
    path = uri.split("?", 1)[0].split("#", 1)[0].lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith((".md", ".markdown", ".txt")):
        return "markdown"
    raise _invalid(
        "/uri_or_file_ref", "unsupported_file", "для file ожидается artifact:<uuid> или https-ссылка на .pdf/.md"
    )


async def create_source(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    technology_version_id: uuid.UUID,
    source_type: str,
    uri: str,
    trust_level: str,
    license_info: str | None,
    refresh_policy: str,
    enabled: bool,
) -> dict[str, Any]:
    exists = (
        await conn.execute(text("SELECT 1 FROM technology_versions WHERE id = :id"), {"id": technology_version_id})
    ).scalar_one_or_none()
    if exists is None:
        raise ProblemError("not_found", detail="Версия технологии не найдена")
    db_type = await _db_source_type(conn, source_type, uri)
    source_id = uuid7()
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO documentation_sources (id, workspace_id, technology_version_id, source_type,"
                    " uri_or_file_ref, trust_level, license_info, refresh_policy, enabled, created_by)"
                    " VALUES (:id, :ws, :tv, :st, :uri, :tl, :li, :rp, :en, :by)"
                ),
                {
                    "id": source_id,
                    "ws": workspace_id,
                    "tv": technology_version_id,
                    "st": db_type,
                    "uri": uri,
                    "tl": trust_level,
                    "li": license_info,
                    "rp": API_TO_DB_REFRESH[refresh_policy],
                    "en": enabled,
                    "by": actor_id,
                },
            )
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise ProblemError(
                "conflict",
                detail="Источник с таким адресом уже зарегистрирован",
                errors=[FieldError("/uri_or_file_ref", "conflict", "адрес уже зарегистрирован")],
            ) from exc
        if getattr(exc.orig, "sqlstate", None) == "23514":
            raise _invalid("/uri_or_file_ref", "invalid", "адрес не соответствует типу источника") from exc
        raise
    await audit.record(
        conn,
        action="documentation_source.register",
        resource_type="documentation_source",
        resource_id=source_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"source_type": db_type, "trust_level": trust_level, "enabled": enabled},
    )
    row = await get_source(conn, source_id)
    if row is None:  # pragma: no cover
        raise RuntimeError("documentation_source vanished inside transaction")
    return row


async def request_refresh(
    conn: AsyncConnection,
    *,
    source: dict[str, Any],
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    idempotency_key: str,
) -> uuid.UUID:
    """Явный refresh человеком = согласие на загрузку (API_DESIGN §19 п.7): PLANNED `docs_download`
    в ledger через `sys_plan_docs_download`, событие outbox для очереди `docs`, аудит."""
    if not source["enabled"]:
        raise ProblemError("conflict", detail="Источник отключён")
    if _ARTIFACT_REF.fullmatch(source["uri_or_file_ref"]):
        raise ProblemError("conflict", detail="Источник-артефакт не загружается из сети")
    request = {"source_id": str(source["id"]), "url": source["uri_or_file_ref"], "requested_by": str(actor_id)}
    try:
        async with conn.begin_nested():
            action_id: uuid.UUID | None = (
                await conn.execute(
                    text("SELECT sys_plan_docs_download(:sid, :key, CAST(:req AS jsonb))"),
                    {"sid": source["id"], "key": idempotency_key[:255], "req": _json(request)},
                )
            ).scalar_one()
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None)
        if state == "55000":
            raise ProblemError("conflict", detail="Источник отключён") from exc
        if state == "42501":
            raise ProblemError("forbidden") from exc
        raise
    if action_id is None:
        raise ProblemError("not_found")
    await outbox.emit(
        conn,
        event_type="documentation_source.refresh_requested",
        aggregate_type="documentation_source",
        aggregate_id=source["id"],
        workspace_id=workspace_id,
        run_id=None,
        payload={"source_id": str(source["id"]), "external_action_id": str(action_id)},
    )
    await audit.record(
        conn,
        action="documentation_source.refresh_requested",
        resource_type="documentation_source",
        resource_id=source["id"],
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"external_action_id": str(action_id)},
    )
    return action_id


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
