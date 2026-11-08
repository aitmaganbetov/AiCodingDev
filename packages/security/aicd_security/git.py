"""Проверка локального Git-репозитория без выполнения кода из репозитория.

Git вызывается с argv-массивом (без shell), с отключёнными хуками/fsmonitor/внешними
программами из конфигурации репозитория и чистым окружением — содержимое репозитория
считается недоверенным (SECURITY_ARCHITECTURE, TH-11).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL")
_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,255}$")


class GitRepositoryError(ValueError):
    pass


@dataclass(frozen=True)
class GitRepoInfo:
    top_level: Path
    head_sha: str | None
    current_branch: str | None
    is_bare: bool


def _git_binary() -> str:
    path = shutil.which("git")
    if path is None:
        raise GitRepositoryError("git не установлен")
    return path


def run_git(repo: Path, *args: str, timeout: float = 60.0) -> str:
    """Публичная обёртка для доверенных операций платформы (worktree, diff); те же меры изоляции."""
    return _run(repo, *args, timeout=timeout)


def _run(repo: Path, *args: str, timeout: float = 10.0) -> str:
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "HOME": "/nonexistent",
        }
    )
    argv = [
        _git_binary(),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.sshCommand=/bin/false",
        "-c",
        f"safe.directory={repo}",
        "-C",
        str(repo),
        *args,
    ]
    proc = subprocess.run(  # noqa: S603 — argv-массив, без shell
        argv, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
    if proc.returncode != 0:
        raise GitRepositoryError(f"git {args[0] if args else ''} завершился с ошибкой")
    return proc.stdout.rstrip("\n")


def inspect_repository(path: Path) -> GitRepoInfo:
    if not path.is_dir():
        raise GitRepositoryError("каталог не существует")
    try:
        is_bare = _run(path, "rev-parse", "--is-bare-repository") == "true"
    except GitRepositoryError as exc:
        raise GitRepositoryError("каталог не является корректным Git-репозиторием") from exc
    top = path if is_bare else Path(_run(path, "rev-parse", "--show-toplevel"))
    if os.path.realpath(top) != os.path.realpath(path):
        raise GitRepositoryError("путь должен указывать на корень репозитория")
    try:
        head = _run(path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    except GitRepositoryError:
        head = None  # пустой репозиторий без коммитов
    try:
        branch = _run(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    except GitRepositoryError:
        branch = None
    return GitRepoInfo(top_level=Path(top), head_sha=head or None, current_branch=branch, is_bare=is_bare)


def validate_branch_name(name: str) -> str:
    if not _BRANCH_RE.fullmatch(name) or name.endswith((".lock", "/", ".")) or "//" in name or "@{" in name:
        raise GitRepositoryError("недопустимое имя ветки")
    return name
