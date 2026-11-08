"""Изолированные Git worktree на задачу (ADR-0006, P2-09).

* ветка `aicd/<run>/<task>` от зафиксированного `base_commit_sha`;
* каталог worktree — внутри выделенного корня платформы, вне основного клона;
* git вызывается без хуков/конфигов репозитория (aicd_security.git.run_git);
* diff вычисляет платформа сама (CLI_ADAPTER_CONTRACT §11.4), включая новые файлы;
* осиротевшие каталоги обнаруживаются для recovery reconciler (DATA_FLOW §7).
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_security.git import GitRepositoryError, run_git, validate_branch_name

_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeInfo:
    worktree_id: uuid.UUID
    path: Path
    path_ref: str
    branch_name: str
    base_commit_sha: str


def branch_for(run_id: uuid.UUID, task_id: uuid.UUID | None, worktree_id: uuid.UUID) -> str:
    # Последние 12 hex — случайная часть UUIDv7 (первые — время, совпадают у соседних ID).
    return validate_branch_name(f"aicd/{run_id.hex[-12:]}/{(task_id or worktree_id).hex[-12:]}")


class WorktreeManager:
    def __init__(self, root: Path) -> None:
        self._root = Path(os.path.realpath(root))
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path_for(self, worktree_id: uuid.UUID) -> Path:
        return self._root / str(worktree_id)

    def create(
        self,
        repo: Path,
        *,
        worktree_id: uuid.UUID,
        run_id: uuid.UUID,
        task_id: uuid.UUID | None,
        base_commit_sha: str,
    ) -> WorktreeInfo:
        if not _SHA.fullmatch(base_commit_sha):
            raise WorktreeError("base_commit_sha должен быть полным SHA коммита")
        repo_real = Path(os.path.realpath(repo))
        if self._root.is_relative_to(repo_real):
            raise WorktreeError("корень worktree не может находиться внутри репозитория")
        try:
            resolved = run_git(repo_real, "rev-parse", "--verify", "--quiet", f"{base_commit_sha}^{{commit}}")
        except GitRepositoryError as exc:
            raise WorktreeError("базовый коммит не найден в репозитории") from exc
        if resolved != base_commit_sha:
            raise WorktreeError("базовый коммит разрешился в другой объект")
        path = self.path_for(worktree_id)
        if path.exists():
            raise WorktreeError("каталог worktree уже существует")
        branch = branch_for(run_id, task_id, worktree_id)
        try:
            run_git(repo_real, "worktree", "add", "--quiet", "-b", branch, str(path), base_commit_sha)
        except GitRepositoryError as exc:
            raise WorktreeError("не удалось создать worktree") from exc
        return WorktreeInfo(worktree_id, path, str(worktree_id), branch, base_commit_sha)

    def diff(self, info_path: Path, base_commit_sha: str) -> bytes:
        """Полный бинарный diff рабочего дерева относительно базового коммита (вкл. новые файлы)."""
        path = self._checked(info_path)
        run_git(path, "add", "--all")
        return (
            run_git(path, "diff", "--cached", "--binary", "--no-color", "--no-ext-diff", base_commit_sha) + "\n"
        ).encode()

    def commit_snapshot(self, info_path: Path, message: str) -> str | None:
        """WIP/итоговый коммит платформы в ветке задачи (safe point, DATA_FLOW §6.2). None — нет изменений."""
        path = self._checked(info_path)
        run_git(path, "add", "--all")
        if not run_git(path, "status", "--porcelain"):
            return None
        run_git(
            path,
            "-c",
            "user.name=AiCodingDev",
            "-c",
            "user.email=aicd@localhost",
            "commit",
            "--quiet",
            "--no-verify",
            "-m",
            message[:500],
        )
        return run_git(path, "rev-parse", "HEAD")

    def remove(self, repo: Path, info_path: Path) -> None:
        path = self._checked(info_path)
        try:
            run_git(Path(os.path.realpath(repo)), "worktree", "remove", "--force", str(path))
        except GitRepositoryError:
            shutil.rmtree(path, ignore_errors=True)
            run_git(Path(os.path.realpath(repo)), "worktree", "prune")

    def find_orphans(self, known: set[uuid.UUID]) -> list[uuid.UUID]:
        orphans = []
        for entry in self._root.iterdir():
            try:
                wid = uuid.UUID(entry.name)
            except ValueError:
                continue
            if wid not in known:
                orphans.append(wid)
        return sorted(orphans)

    def _checked(self, path: Path) -> Path:
        real = Path(os.path.realpath(path))
        if real.parent != self._root:
            raise WorktreeError("путь не принадлежит корню worktree")
        return real


# --- учёт в БД (таблица worktrees; контекст workspace уже установлен) ------------------


async def record(
    conn: AsyncConnection,
    info: WorktreeInfo,
    *,
    workspace_id: uuid.UUID,
    repository_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    worker_host_id: uuid.UUID | None,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO worktrees (id, workspace_id, repository_id, run_id, task_id, branch_name, base_commit_sha,"
            " path_ref, worker_host_id) VALUES (:id, :ws, :repo, :run, :task, :branch, :base, :ref, :host)"
        ),
        {
            "id": info.worktree_id,
            "ws": workspace_id,
            "repo": repository_id,
            "run": run_id,
            "task": task_id,
            "branch": info.branch_name,
            "base": info.base_commit_sha,
            "ref": info.path_ref,
            "host": worker_host_id,
        },
    )


async def mark_status(
    conn: AsyncConnection, worktree_id: uuid.UUID, status: str, head_commit_sha: str | None = None
) -> None:
    params: dict[str, Any] = {"id": worktree_id, "s": status, "head": head_commit_sha}
    await conn.execute(
        text(
            "UPDATE worktrees SET status = :s, head_commit_sha = COALESCE(:head, head_commit_sha),"
            " removed_at = CASE WHEN :s = 'REMOVED' THEN now() ELSE NULL END WHERE id = :id"
        ),
        params,
    )
