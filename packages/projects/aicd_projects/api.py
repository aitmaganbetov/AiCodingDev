"""Публичный фасад модуля projects. Контекст RLS и права проверяются вызывающим слоем."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Any, Final

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


def _violated_constraint(exc: IntegrityError) -> str:
    """Имя нарушенного ограничения: от него зависит, *что именно* занято (каталог или репозиторий)."""
    diag = getattr(exc.orig, "diag", None)
    return str(getattr(diag, "constraint_name", "") or "")


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


_WORKSPACE_SELECT = "SELECT id, name, slug, settings, version, created_at FROM workspaces WHERE id = :id"


async def get_workspace(conn: AsyncConnection, workspace_id: uuid.UUID) -> dict[str, Any] | None:
    """Workspace текущего RLS-контекста (`app.workspace_id`); чужой — None."""
    row = (await conn.execute(text(_WORKSPACE_SELECT), {"id": workspace_id})).mappings().first()
    return dict(row) if row else None


async def update_workspace(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_version: int,
    name: str | None,
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge-patch названия и настроек workspace (OQ-45 (f)) с optimistic locking и аудитом в той же транзакции.

    Права проверяет вызывающий слой (`workspace:update`, для `settings` — `workspace:policy`). Несовпадение версии ⇒
    412 с актуальным ETag. Аудит: `workspace.update` (имена изменённых полей) и `policy.changed` на каждый ключ политики
    (`from`/`to`), SECURITY_ARCHITECTURE §11.
    """
    current = await get_workspace(conn, workspace_id)
    if current is None:
        raise ProblemError("not_found")
    if int(current["version"]) != expected_version:
        raise ProblemError("precondition_failed", headers={"ETag": f'"v{current["version"]}"'})
    old_settings: dict[str, Any] = dict(current["settings"] or {})
    changed: list[str] = []
    policy_changes: dict[str, tuple[Any, Any]] = {}
    if name is not None and name != current["name"]:
        changed.append("name")
    for key, value in (settings or {}).items():
        if old_settings.get(key) != value:
            policy_changes[key] = (old_settings.get(key), value)
    if policy_changes:
        changed.append("settings")
    if not changed:
        return current  # no-op: версия не меняется
    row = (
        (
            await conn.execute(
                text(
                    "UPDATE workspaces SET name = :name, settings = settings || CAST(:patch AS jsonb),"
                    " version = version + 1 WHERE id = :id AND version = :v"
                    " RETURNING id, name, slug, settings, version, created_at"
                ),
                {
                    "id": workspace_id,
                    "v": expected_version,
                    "name": name if name is not None else current["name"],
                    "patch": json.dumps({k: v[1] for k, v in policy_changes.items()}),
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ProblemError("precondition_failed")
    await audit.record(
        conn,
        action="workspace.update",
        resource_type="workspace",
        resource_id=workspace_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"changed": changed, "version": int(row["version"])},
    )
    for key, (before, after) in sorted(policy_changes.items()):
        await audit.record(
            conn,
            action="policy.changed",
            resource_type="workspace",
            resource_id=workspace_id,
            actor_type="user",
            actor_id=actor_id,
            workspace_id=workspace_id,
            metadata={"policy": f"workspace.{key}", "from": before, "to": after},
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

# latest_index_snapshot (контракт Repository): последний READY-снимок индекса кода репозитория (P5-02).
_REPO_SELECT = (
    "SELECT r.id, r.project_id, r.provider, r.remote_url, r.default_branch, r.local_root_ref, r.last_synced_at,"
    " r.created_at, s.id AS index_snapshot_id, s.commit_sha AS index_commit_sha, s.status AS index_status"
    " FROM repositories r LEFT JOIN LATERAL (SELECT c.id, c.commit_sha, c.status FROM code_index_snapshots c"
    "   WHERE c.repository_id = r.id AND c.status = 'READY' ORDER BY c.created_at DESC, c.id DESC LIMIT 1) s ON true"
)


async def list_repositories(
    conn: AsyncConnection, project_id: uuid.UUID, after: tuple[datetime, uuid.UUID] | None, limit: int
) -> list[dict[str, Any]]:
    # Отвязанные не показываются: для проекта их нет, хотя строка жива ради истории прогонов.
    where = " WHERE r.project_id = :p AND r.status <> 'DETACHED'" + (
        " AND (r.created_at, r.id) > (:t, :id)" if after else ""
    )
    rows = (
        (
            await conn.execute(
                text(_REPO_SELECT + where + " ORDER BY r.created_at, r.id LIMIT :lim"),
                {"p": project_id, "t": after[0] if after else None, "id": after[1] if after else None, "lim": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


#: Прогоны этих состояний ещё работают с репозиторием: отвязать его — оборвать их на полпути.
_ACTIVE_RUN_STATES: Final = (
    "CREATED", "DISCOVERY", "PLANNED", "AWAITING_APPROVAL", "RUNNING", "VERIFYING", "PAUSED", "BLOCKED",
)  # fmt: skip


async def detach_repository(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repository_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> bool:
    """Отвязать репозиторий от проекта: `status = DETACHED` (FR-PRJ-02). Возвращает «статус изменился».

    Строка остаётся: на неё ссылаются прогоны, worktrees, PR, треды ревью и снимки индекса (все FK —
    `ON DELETE RESTRICT`), и история должна читаться после отвязки. Каталог и GitHub-репозиторий при этом
    освобождаются (частичные уникальные индексы, миграция 0023) — тот же репозиторий можно привязать заново.
    Файлы рабочей копии на диске платформа не трогает: она их не всегда создавала, а удаление необратимо.

    Повторная отвязка — не ошибка (идемпотентно). Отвязка при незавершённых прогонах — `409`: прогон продолжил
    бы работать с репозиторием, которого в проекте уже нет.
    """
    row = (
        (
            await conn.execute(
                text(
                    "SELECT id, provider, status, local_root_ref, full_name FROM repositories"
                    " WHERE id = :id AND project_id = :p FOR UPDATE"
                ),
                {"id": repository_id, "p": project_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ProblemError("not_found")
    if row["status"] == "DETACHED":
        return False
    active = (
        await conn.execute(
            text("SELECT count(*) FROM runs WHERE repository_id = :id AND status = ANY(:states)"),
            {"id": repository_id, "states": list(_ACTIVE_RUN_STATES)},
        )
    ).scalar_one()
    if active:
        raise ProblemError(
            "conflict",
            detail=f"Репозиторий занят: незавершённых прогонов — {active}. Дождитесь их или отмените.",
        )
    await conn.execute(
        text("UPDATE repositories SET status = 'DETACHED', updated_at = now() WHERE id = :id"),
        {"id": repository_id},
    )
    await audit.record(
        conn,
        action="repository.detach",
        resource_type="repository",
        resource_id=repository_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={
            "provider": row["provider"],
            "local_root_ref": row["local_root_ref"],
            "full_name": row["full_name"],
        },
    )
    return True


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
        raise ProblemError(
            "conflict",
            detail="Этот каталог уже привязан к репозиторию в этом рабочем пространстве",
            errors=[FieldError("/local_path", "local_path_taken", "каталог уже привязан")],
        ) from exc
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
    row = (await conn.execute(text(_REPO_SELECT + " WHERE r.id = :id"), {"id": repo_id})).mappings().one()
    return dict(row)


#: Обход корней репозиториев для формы привязки: насколько глубоко искать и сколько каталогов показать.
#: Ограничения — не политика безопасности (её держит `resolve_in_roots`), а защита от долгого обхода.
ROOT_SCAN_MAX_DEPTH: Final = 3
ROOT_SCAN_LIMIT: Final = 200


def scan_repository_roots(
    roots: dict[str, Path],
    *,
    attached: Collection[str] = (),
    max_depth: int = ROOT_SCAN_MAX_DEPTH,
    limit: int = ROOT_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Разрешённые корни и найденные в них Git-репозитории — «что разрешено привязать» для UI (FR-PRJ-02).

    Репозиторием считается каталог с `.git` (внутрь него обход не идёт). Это быстрая подсказка: пригодность
    каталога всё равно проверяет `git_guard.inspect_repository` при привязке. Symlink не разыменовывается
    (каталог-symlink пропускается), чтобы обход не вышел за корень и не зациклился;
    нечитаемые каталоги пропускаются — право видеть их у процесса API может и не быть.

    `attached` — `local_root_ref` уже привязанных репозиториев workspace: такой каталог занят
    (`uq_repositories_local_root`), и форма помечает его, чтобы выбор не приводил к `conflict`.

    Возвращает по одному элементу на корень:
    `{name, path, repositories: [{ref, path, name, attached}], truncated}`, где `ref` —
    `<корень>/<относительный путь>`, который принимает `attachRepository`.
    """
    taken = set(attached)
    result: list[dict[str, Any]] = []
    for name, root in roots.items():
        root_real = Path(os.path.realpath(root))
        found: list[dict[str, Any]] = []
        truncated = False
        stack: list[tuple[Path, int]] = [(root_real, 0)]
        while stack:
            current, depth = stack.pop()
            if len(found) >= limit:
                truncated = True
                break
            try:
                entries = sorted(current.iterdir())
            except OSError:
                continue
            if (current / ".git").exists():
                rel = "" if current == root_real else current.relative_to(root_real).as_posix()
                ref = f"{name}/{rel}" if rel else name
                found.append({"ref": ref, "path": str(current), "name": rel or name, "attached": ref in taken})
                continue  # вложенные репозитории (submodules) не предлагаются отдельно
            if depth >= max_depth:
                continue
            for entry in reversed(entries):
                if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                    continue
                stack.append((entry, depth + 1))
        found.sort(key=lambda item: item["ref"])
        result.append({"name": name, "path": str(root_real), "repositories": found, "truncated": truncated})
    result.sort(key=lambda item: item["name"])
    return result


def validate_github_local_copy(local_path: str, default_branch: str | None, roots: dict[str, Path]) -> None:
    """Проверка локальной копии GitHub-репозитория до асинхронной проверки доступа (422 сразу в API);
    те же проверки повторяет `attach_github_repository` в воркере."""
    if not roots:
        raise ProblemError("policy_denied", detail="Корни локальных репозиториев не настроены")
    try:
        resolved = resolve_in_roots(local_path, roots)
        git_guard.inspect_repository(resolved.absolute)
        if default_branch is not None:
            git_guard.validate_branch_name(default_branch)
    except (UnsafePathError, git_guard.GitRepositoryError) as exc:
        raise ProblemError(
            "validation_error", errors=[FieldError("/local_path", "invalid_repository", str(exc))]
        ) from exc


def resolve_clone_target(local_path: str, default_branch: str | None, roots: dict[str, Path]) -> Path:
    """Каталог, в котором воркер создаст рабочую копию клонированием (FR-PRJ-03, `create_local_copy`).

    Требования — те же границы, что и у существующей копии, плюс «ничего не перезаписываем»: путь внутри
    allowlist-корней, сам корень целью быть не может (в нём лежат другие копии), каталог отсутствует или пуст,
    родительский каталог существует. Клонирование всё равно повторит проверку (`clone_repository`): между
    этой проверкой в API и работой воркера каталог мог появиться.
    """
    if not roots:
        raise ProblemError("policy_denied", detail="Корни локальных репозиториев не настроены")
    try:
        resolved = resolve_in_roots(local_path, roots)
        if default_branch is not None:
            git_guard.validate_branch_name(default_branch)
    except (UnsafePathError, git_guard.GitRepositoryError) as exc:
        raise ProblemError("validation_error", errors=[FieldError("/local_path", "invalid_path", str(exc))]) from exc
    target = resolved.absolute
    problem: str | None = None
    if str(resolved.relative) in ("", "."):
        problem = "корень нельзя использовать как рабочую копию: укажите каталог внутри него"
    elif target.exists() and (not target.is_dir() or any(target.iterdir())):
        problem = "каталог уже существует и не пуст"
    elif not target.parent.is_dir():
        problem = "родительский каталог не существует"
    if problem is not None:
        raise ProblemError("validation_error", errors=[FieldError("/local_path", "unavailable_target", problem)])
    return target


async def attach_github_repository(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    local_path: str,
    remote_url: str,
    full_name: str,
    github_repo_id: int,
    github_installation_id: uuid.UUID,
    default_branch: str,
    roots: dict[str, Path],
) -> dict[str, Any]:
    """Привязка GitHub-репозитория (FR-PRJ-03, P8-02): локальная рабочая копия из allowlist-корней + репозиторий
    GitHub, доступ к которому подтверждён установкой GitHub App (проверяет вызывающий через GitHub API)."""
    if not roots:
        raise ProblemError("policy_denied", detail="Корни локальных репозиториев не настроены")
    try:
        resolved = resolve_in_roots(local_path, roots)
        info = git_guard.inspect_repository(resolved.absolute)
        branch = git_guard.validate_branch_name(default_branch)
    except (UnsafePathError, git_guard.GitRepositoryError) as exc:
        raise ProblemError(
            "validation_error", errors=[FieldError("/local_path", "invalid_repository", str(exc))]
        ) from exc
    repo_id = uuid7()
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO repositories (id, workspace_id, project_id, provider, remote_url, default_branch,"
                    " local_root_ref, github_installation_id, github_repo_id, full_name, last_synced_at,"
                    " last_synced_commit_sha)"
                    " VALUES (:id, :ws, :p, 'github', :url, :b, :ref, :inst, :gid, :fn, now(), :sha)"
                ),
                {
                    "id": repo_id,
                    "ws": workspace_id,
                    "p": project_id,
                    "url": remote_url,
                    "b": branch,
                    "ref": resolved.ref,
                    "inst": github_installation_id,
                    "gid": github_repo_id,
                    "fn": full_name,
                    "sha": info.head_sha,
                },
            )
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        # Занято может быть разное, и оператору нужно знать что: каталог (uq_repositories_local_root) или
        # сам репозиторий GitHub (uq_repositories_github_repo). Оба ограничения — в пределах workspace.
        if _violated_constraint(exc) == "uq_repositories_local_root":
            raise ProblemError(
                "conflict",
                detail="Этот каталог уже привязан к репозиторию в этом рабочем пространстве",
                errors=[FieldError("/local_path", "local_path_taken", "каталог уже привязан")],
            ) from exc
        raise ProblemError(
            "conflict",
            detail="Этот репозиторий GitHub уже привязан в этом рабочем пространстве",
            errors=[FieldError("/remote_url", "repository_taken", "репозиторий уже привязан")],
        ) from exc
    await audit.record(
        conn,
        action="repository.attach",
        resource_type="repository",
        resource_id=repo_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={
            "provider": "github",
            "full_name": full_name,
            "github_repo_id": github_repo_id,
            "local_root_ref": resolved.ref,
            "project_id": str(project_id),
        },
    )
    row = (await conn.execute(text(_REPO_SELECT + " WHERE r.id = :id"), {"id": repo_id})).mappings().one()
    return dict(row)
