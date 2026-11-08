"""Публичный фасад модуля projects. Контекст RLS и права проверяются вызывающим слоем."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform import audit
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7
from aicd_security import git as git_guard
from aicd_security.paths import UnsafePathError, resolve_in_roots


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Только нарушение уникальности — 409; прочие нарушения ограничений — дефект/500."""
    return getattr(exc.orig, "sqlstate", None) == "23505"


# --- Workspaces -------------------------------------------------------------------------


async def create_workspace(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, name: str, slug: str, owner_id: uuid.UUID
) -> dict[str, Any]:
    """Создаёт workspace и членство owner в одной транзакции.

    Контекст `app.workspace_id` должен быть равен `workspace_id` (RLS WITH CHECK, DATABASE_DESIGN §19.3).
    """
    try:
        async with conn.begin_nested():
            await conn.execute(
                text("INSERT INTO workspaces (id, name, slug, created_by) VALUES (:id, :name, :slug, :by)"),
                {"id": workspace_id, "name": name, "slug": slug, "by": owner_id},
            )
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        raise ProblemError(
            "conflict",
            detail="Workspace с таким slug уже существует",
            errors=[FieldError("/slug", "conflict", "slug занят")],
        ) from exc
    await conn.execute(
        text("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:ws, :uid, 'owner')"),
        {"ws": workspace_id, "uid": owner_id},
    )
    await audit.record(
        conn,
        action="workspace.create",
        resource_type="workspace",
        resource_id=workspace_id,
        actor_type="user",
        actor_id=owner_id,
        workspace_id=workspace_id,
        metadata={"slug": slug},
    )
    row = (
        (
            await conn.execute(
                text("SELECT id, name, slug, settings, created_at FROM workspaces WHERE id = :id"),
                {"id": workspace_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


# --- Projects ----------------------------------------------------------------------------

_PROJECT_SELECT = (
    "SELECT p.id, p.workspace_id, p.name, p.description, p.status, p.version, p.created_at, p.updated_at,"
    " s.documentation_mode, s.quality_profile, s.routing_policy_id, s.security_profile,"
    " s.version AS settings_version"
    " FROM projects p JOIN project_settings s ON s.project_id = p.id"
)


async def list_projects(
    conn: AsyncConnection,
    *,
    status: str | None,
    before: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    clauses = ["p.workspace_id = app_current_workspace()"]
    params: dict[str, Any] = {"lim": limit}
    if status:
        clauses.append("p.status = :status")
        params["status"] = status
    else:
        clauses.append("p.status <> 'DELETION_PENDING'")
    if before:
        clauses.append("(p.created_at, p.id) < (:t, :id)")
        params.update(t=before[0], id=before[1])
    sql = _PROJECT_SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY p.created_at DESC, p.id DESC LIMIT :lim"
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


async def get_project(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, Any] | None:
    row = (await conn.execute(text(_PROJECT_SELECT + " WHERE p.id = :id"), {"id": project_id})).mappings().first()
    return dict(row) if row else None


async def create_project(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    description: str | None,
    documentation_mode: str,
    quality_profile: str,
) -> dict[str, Any]:
    project_id = uuid7()
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO projects (id, workspace_id, name, description, created_by)"
                    " VALUES (:id, :ws, :name, :d, :by)"
                ),
                {"id": project_id, "ws": workspace_id, "name": name, "d": description, "by": actor_id},
            )
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        raise ProblemError(
            "conflict",
            detail="Проект с таким именем уже существует",
            errors=[FieldError("/name", "conflict", "имя занято")],
        ) from exc
    await conn.execute(
        text(
            "INSERT INTO project_settings (project_id, workspace_id, documentation_mode, quality_profile, updated_by)"
            " VALUES (:id, :ws, :dm, :qp, :by)"
        ),
        {"id": project_id, "ws": workspace_id, "dm": documentation_mode, "qp": quality_profile, "by": actor_id},
    )
    await audit.record(
        conn,
        action="project.create",
        resource_type="project",
        resource_id=project_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
    )
    project = await get_project(conn, project_id)
    if project is None:  # pragma: no cover
        raise RuntimeError("project row vanished inside transaction")
    return project


_PROJECT_FIELDS = {"name", "description", "status"}
_SETTINGS_FIELDS = {"quality_profile", "routing_policy_id"}


async def update_project(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_version: int,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """JSON merge-patch проекта; версия проекта растёт при любом изменении (ETag проекта)."""
    current = await get_project(conn, project_id)
    if current is None:
        raise ProblemError("not_found")
    if current["version"] != expected_version:
        raise ProblemError("precondition_failed", headers={"ETag": f'"v{current["version"]}"'})
    if changes.get("routing_policy_id") is not None:
        # Таблица policies появится в Phase 3 (P3-01); до этого ссылку задать нельзя.
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/routing_policy_id", "not_supported", "Политики маршрутизации появятся в Phase 3")],
        )
    project_changes = {k: v for k, v in changes.items() if k in _PROJECT_FIELDS}
    settings_changes = {k: v for k, v in changes.items() if k in _SETTINGS_FIELDS}
    assignments = ", ".join(f"{k} = :{k}" for k in project_changes)
    sep = ", " if assignments else ""
    try:
        async with conn.begin_nested():
            result = await conn.execute(
                # Имена колонок — из allowlist _PROJECT_FIELDS, значения — bind-параметры.
                text(f"UPDATE projects SET {assignments}{sep}version = version + 1 WHERE id = :id AND version = :v"),  # noqa: S608
                {**project_changes, "id": project_id, "v": expected_version},
            )
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        raise ProblemError("conflict", errors=[FieldError("/name", "conflict", "имя занято")]) from exc
    if result.rowcount != 1:
        raise ProblemError("precondition_failed")
    if settings_changes:
        s_assign = ", ".join(f"{k} = :{k}" for k in settings_changes)
        await conn.execute(
            text(
                f"UPDATE project_settings SET {s_assign}, updated_by = :by, version = version + 1"  # noqa: S608 — имена полей из allowlist
                " WHERE project_id = :id"
            ),
            {**settings_changes, "id": project_id, "by": actor_id},
        )
    await audit.record(
        conn,
        action="project.update",
        resource_type="project",
        resource_id=project_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"fields": sorted(changes)},
    )
    updated = await get_project(conn, project_id)
    if updated is None:  # pragma: no cover
        raise RuntimeError("project row vanished inside transaction")
    return updated


# --- Repositories --------------------------------------------------------------------------

_REPO_SELECT = (
    "SELECT id, project_id, provider, remote_url, default_branch, local_root_ref, last_synced_at, created_at"
    " FROM repositories"
)


async def list_repositories(
    conn: AsyncConnection, project_id: uuid.UUID, after: tuple[datetime, uuid.UUID] | None, limit: int
) -> list[dict[str, Any]]:
    where = " WHERE project_id = :p" + (" AND (created_at, id) > (:t, :id)" if after else "")
    rows = (
        (
            await conn.execute(
                text(_REPO_SELECT + where + " ORDER BY created_at, id LIMIT :lim"),
                {"p": project_id, "t": after[0] if after else None, "id": after[1] if after else None, "lim": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def attach_local_repository(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    local_path: str,
    default_branch: str | None,
    roots: dict[str, Path],
) -> dict[str, Any]:
    """Привязка локального Git-репозитория из allowlist-корней (FR-PRJ-02, P1-12).

    Ошибки пути и Git возвращаются как 422 без раскрытия структуры ФС хоста.
    """
    if not roots:
        raise ProblemError("policy_denied", detail="Корни локальных репозиториев не настроены")
    try:
        resolved = resolve_in_roots(local_path, roots)
        info = git_guard.inspect_repository(resolved.absolute)
        branch = git_guard.validate_branch_name(default_branch or info.current_branch or "main")
    except (UnsafePathError, git_guard.GitRepositoryError) as exc:
        raise ProblemError(
            "validation_error", errors=[FieldError("/local_path", "invalid_repository", str(exc))]
        ) from exc
    repo_id = uuid7()
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO repositories (id, workspace_id, project_id, provider, default_branch,"
                    " local_root_ref, last_synced_at, last_synced_commit_sha)"
                    " VALUES (:id, :ws, :p, 'local', :b, :ref, now(), :sha)"
                ),
                {
                    "id": repo_id,
                    "ws": workspace_id,
                    "p": project_id,
                    "b": branch,
                    "ref": resolved.ref,
                    "sha": info.head_sha,
                },
            )
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        raise ProblemError("conflict", detail="Репозиторий уже привязан в этом workspace") from exc
    await audit.record(
        conn,
        action="repository.attach",
        resource_type="repository",
        resource_id=repo_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"provider": "local", "local_root_ref": resolved.ref, "project_id": str(project_id)},
    )
    row = (await conn.execute(text(_REPO_SELECT + " WHERE id = :id"), {"id": repo_id})).mappings().one()
    return dict(row)
