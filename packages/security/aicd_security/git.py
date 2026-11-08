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
import threading
from dataclasses import dataclass
from pathlib import Path

_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL")
_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,255}$")


class GitRepositoryError(ValueError):
    """Сообщение безопасно для API; `detail` (усечённый stderr git) — только для журналов сервера."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


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


def run_git_bytes(
    repo: Path, *args: str, stdin: bytes | None = None, timeout: float = 60.0, max_output_bytes: int | None = None
) -> bytes:
    """Как `run_git`, но stdin/stdout — байты (содержимое blob через `cat-file --batch`, P5-02).

    Фильтры (`smudge`/`textconv`) не применяются: `cat-file` без `--filters` отдаёт blob как есть.
    `max_output_bytes` — защита памяти: больший вывод считается ошибкой.
    """
    argv, env = _argv_env(repo, args)
    proc = subprocess.run(  # noqa: S603 — argv-массив, без shell
        argv, input=stdin, capture_output=True, timeout=timeout, env=env, check=False
    )
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-500:]
        raise GitRepositoryError(f"git {args[0] if args else ''} завершился с ошибкой", detail=detail)
    if max_output_bytes is not None and len(proc.stdout) > max_output_bytes:
        raise GitRepositoryError(f"git {args[0] if args else ''}: превышен лимит вывода")
    return proc.stdout


def run_git_bytes_prefix(repo: Path, *args: str, limit: int, timeout: float = 60.0) -> tuple[bytes, bool]:
    """Первые `limit` байт stdout git и признак, что вывод был длиннее (просмотр файла, FR-PRJ-05).

    Процесс завершается после чтения `limit + 1` байт — большой blob не загружается в память целиком;
    сторожевой таймер убивает процесс по `timeout`. stderr не читается (сообщение без деталей).
    """
    argv, env = _argv_env(repo, args)
    with subprocess.Popen(  # noqa: S603 — argv-массив, без shell
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
    ) as proc:
        watchdog = threading.Timer(timeout, proc.kill)
        watchdog.start()
        chunks: list[bytes] = []
        remaining = limit + 1
        overflow = False
        try:
            stream = proc.stdout
            while stream is not None and remaining > 0:
                chunk = stream.read(min(remaining, 1 << 16))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            overflow = remaining <= 0
            if overflow:
                proc.kill()
            returncode = proc.wait()
        finally:
            timed_out = not watchdog.is_alive() and not overflow and proc.returncode not in (0, None)
            watchdog.cancel()
    if timed_out:
        raise GitRepositoryError(f"git {args[0] if args else ''}: превышено время ожидания")
    if not overflow and returncode != 0:
        raise GitRepositoryError(f"git {args[0] if args else ''} завершился с ошибкой")
    return b"".join(chunks)[:limit], overflow


def _argv_env(repo: Path, args: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
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
    return argv, env


def _run(repo: Path, *args: str, timeout: float = 10.0) -> str:
    argv, env = _argv_env(repo, args)
    proc = subprocess.run(  # noqa: S603 — argv-массив, без shell
        argv, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
    if proc.returncode != 0:
        raise GitRepositoryError(
            f"git {args[0] if args else ''} завершился с ошибкой", detail=(proc.stderr or "").strip()[-500:]
        )
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
