"""Хранение определённого стека (DATABASE_DESIGN §6.5–§6.7, миграция 0007).

* `technologies`/`technology_versions` — G-справочники: INSERT только `aicd_worker` (определение стека
  исполняется в worker-index), UPDATE/DELETE запрещены — `ON CONFLICT DO NOTHING` + повторный SELECT.
* `project_technologies` — T-таблица (RLS по workspace). Пересчёт по новому commit заменяет
  автоматически определённые строки; строки, подтверждённые человеком (`confirmed_by`) или добавленные
  вручную (`detected_from = 'manual'`), сохраняются.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_code_intelligence.technologies.detect import DetectedTechnology
from aicd_platform.ids import uuid7


async def _technology_id(conn: AsyncConnection, ecosystem: str, name: str) -> uuid.UUID:
    await conn.execute(
        text(
            "INSERT INTO technologies (id, name, ecosystem) VALUES (:id, :n, :e)"
            " ON CONFLICT ON CONSTRAINT uq_technologies_ecosystem_name DO NOTHING"
        ),
        {"id": uuid7(), "n": name, "e": ecosystem},
    )
    tid: uuid.UUID = (
        await conn.execute(
            text("SELECT id FROM technologies WHERE ecosystem = :e AND name = :n"), {"e": ecosystem, "n": name}
        )
    ).scalar_one()
    return tid


async def _version_id(conn: AsyncConnection, technology_id: uuid.UUID, label: str) -> uuid.UUID:
    await conn.execute(
        text(
            "INSERT INTO technology_versions (id, technology_id, version_label) VALUES (:id, :t, :v)"
            " ON CONFLICT ON CONSTRAINT uq_technology_versions DO NOTHING"
        ),
        {"id": uuid7(), "t": technology_id, "v": label},
    )
    vid: uuid.UUID = (
        await conn.execute(
            text("SELECT id FROM technology_versions WHERE technology_id = :t AND version_label = :v"),
            {"t": technology_id, "v": label},
        )
    ).scalar_one()
    return vid


async def ensure_technology_version(conn: AsyncConnection, ecosystem: str, name: str, version_label: str) -> uuid.UUID:
    """id версии технологии из G-справочника (создаётся при отсутствии; роль `aicd_worker`)."""
    return await _version_id(conn, await _technology_id(conn, ecosystem, name), version_label)


async def persist_detection(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    commit_sha: str,
    detected: Sequence[DetectedTechnology],
) -> list[uuid.UUID]:
    """Записывает результат определения стека (роль `aicd_worker`, контекст workspace)."""
    await conn.execute(
        text(
            "DELETE FROM project_technologies WHERE project_id = :p AND confirmed_by IS NULL"
            " AND detected_from <> 'manual'"
        ),
        {"p": project_id},
    )
    ids: list[uuid.UUID] = []
    for item in detected:
        vid = await ensure_technology_version(conn, item.ecosystem, item.name, item.version_label)
        await conn.execute(
            text(
                "INSERT INTO project_technologies (workspace_id, project_id, technology_version_id, detected_from,"
                " detected_commit_sha) VALUES (:ws, :p, :v, :src, :sha)"
                " ON CONFLICT ON CONSTRAINT pk_project_technologies DO UPDATE"
                " SET detected_commit_sha = EXCLUDED.detected_commit_sha, detected_at = now()"
            ),
            {"ws": workspace_id, "p": project_id, "v": vid, "src": item.detected_from, "sha": commit_sha},
        )
        ids.append(vid)
    return ids


_SELECT = (
    "SELECT t.id AS technology_id, t.name, t.ecosystem, tv.id AS technology_version_id, tv.version_label,"
    " tv.support_status, pt.detected_from, pt.detected_commit_sha, pt.detected_at, pt.confirmed_by,"
    " u.display_name AS confirmed_by_name"
    " FROM project_technologies pt"
    " JOIN technology_versions tv ON tv.id = pt.technology_version_id"
    " JOIN technologies t ON t.id = tv.technology_id"
    " LEFT JOIN users u ON u.id = pt.confirmed_by"
)


async def list_project_technologies(
    conn: AsyncConnection, project_id: uuid.UUID, *, after: tuple[str, str, uuid.UUID] | None, limit: int
) -> list[dict[str, Any]]:
    """Упорядочено по (ecosystem, name, technology_version_id) — стабильный курсор."""
    clauses = ["pt.project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "lim": limit}
    if after:
        clauses.append("(t.ecosystem, t.name, tv.id) > (:e, :n, :v)")
        params.update(e=after[0], n=after[1], v=after[2])
    sql = _SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY t.ecosystem, t.name, tv.id LIMIT :lim"
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


def view(row: dict[str, Any]) -> dict[str, Any]:
    confirmed = row.get("confirmed_by")
    return {
        "technology_id": row["technology_id"],
        "name": row["name"],
        "ecosystem": row["ecosystem"],
        "technology_version_id": row["technology_version_id"],
        "version_label": row["version_label"],
        "support_status": row["support_status"],
        "detected_from": row["detected_from"],
        "confirmed_by": {"id": confirmed, "display_name": row.get("confirmed_by_name") or ""} if confirmed else None,
    }


async def list_workspace_technologies(
    conn: AsyncConnection,
    *,
    after: tuple[str, str, uuid.UUID] | None,
    query: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Версии технологий workspace текущего RLS-контекста (OQ-45 (c)): определённые в проектах и указанные в
    источниках документации. Глобальный каталог других workspace не раскрывается — выборка только из T-таблиц.

    Упорядочено по (ecosystem, name, technology_version_id) — стабильный курсор.
    """
    clauses = ["true"]
    params: dict[str, Any] = {"lim": limit}
    if after:
        clauses.append("(t.ecosystem, t.name, tv.id) > (:e, :n, :v)")
        params.update(e=after[0], n=after[1], v=after[2])
    if query:
        clauses.append("t.name ILIKE :q ESCAPE '\\'")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["q"] = f"%{escaped}%"
    sql = (
        "WITH used AS ("  # noqa: S608 — условия из фиксированных фрагментов, значения — параметры
        "  SELECT technology_version_id AS v, count(DISTINCT project_id) AS projects, 0 AS sources"
        "  FROM project_technologies WHERE workspace_id = app_current_workspace() GROUP BY technology_version_id"
        "  UNION ALL"
        "  SELECT technology_version_id, 0, count(*) FROM documentation_sources"
        "  WHERE workspace_id = app_current_workspace() AND technology_version_id IS NOT NULL"
        "  GROUP BY technology_version_id"
        "), agg AS (SELECT v, sum(projects)::int AS project_count, sum(sources)::int AS source_count FROM used GROUP BY v)"
        " SELECT t.id AS technology_id, t.name, t.ecosystem, tv.id AS technology_version_id, tv.version_label,"
        " tv.support_status, agg.project_count, agg.source_count"
        " FROM agg JOIN technology_versions tv ON tv.id = agg.v JOIN technologies t ON t.id = tv.technology_id"
        " WHERE " + " AND ".join(clauses) + " ORDER BY t.ecosystem, t.name, tv.id LIMIT :lim"
    )
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


def workspace_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "technology_id": row["technology_id"],
        "name": row["name"],
        "ecosystem": row["ecosystem"],
        "technology_version_id": row["technology_version_id"],
        "version_label": row["version_label"],
        "support_status": row["support_status"],
        "project_count": int(row["project_count"] or 0),
        "source_count": int(row["source_count"] or 0),
    }
