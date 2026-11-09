"""Repository Explorer (FR-PRJ-05): дерево файлов и просмотр файла по commit без checkout.

Источник — локальный Git-репозиторий проекта (`repositories.local_root_ref` внутри allowlist-корней);
git вызывается через `aicd_security.git` (argv без shell, хуки/fsmonitor/фильтры отключены).

* commit — полный/сокращённый hex SHA (7–64) или имя ветки (`validate_branch_name`, только `refs/heads/`);
  без параметра — HEAD ветки по умолчанию, затем последний READY-снимок индекса, затем
  `last_synced_commit_sha`. Ответ всегда содержит полный разрешённый SHA;
* путь — относительный POSIX-путь внутри дерева commit: запрещены ведущий `/`, `\\`, компоненты
  `..`/`.`/пустые, управляющие символы и любой компонент `.git`;
* дерево — один уровень (`ls-tree -z -l <tree>`), каталоги первыми, затем по имени (байтовый порядок);
* файл — не больше `MAX_FILE_BYTES` (иначе `truncated`), бинарность — NUL-байт или не UTF-8;
  содержимое отдаётся только как текст (без HTML), бинарный файл — без содержимого.

* ветки — `for-each-ref refs/heads/` (не более `MAX_BRANCHES`); история — `git log` от разрешённого commit
  (глубина не более `MAX_HISTORY_DEPTH`), без email автора, тема commit — однострочный текст с редактированием секретов.

Блокирующие вызовы git выполняются вызывающим слоем вне транзакции БД (в пуле потоков).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.redaction import redact_text
from aicd_security import git as git_guard
from aicd_security.paths import UnsafePathError, resolve_ref

MAX_FILE_BYTES = 512 * 1024
_MAX_TREE_OUTPUT = 32 * 1024 * 1024
_HEX_RE = re.compile(r"^[0-9a-f]{7,64}$")
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BINARY_SNIFF_BYTES = 8000

_TYPE_RANK = {"tree": 0, "blob": 1, "commit": 2}


@dataclass(frozen=True)
class ExplorerRepository:
    id: uuid.UUID
    project_id: uuid.UUID
    default_branch: str
    local_root_ref: str | None
    index_commit_sha: str | None
    last_synced_commit_sha: str | None


@dataclass(frozen=True)
class TreeItem:
    name: str
    path: str
    type: str  # directory | file | symlink | submodule
    mode: str
    size: int | None
    object_type: str


async def get_explorer_repository(
    conn: AsyncConnection, project_id: uuid.UUID, repository_id: uuid.UUID
) -> ExplorerRepository:
    """Репозиторий проекта (RLS workspace); чужой или отсутствующий — 404."""
    row = (
        (
            await conn.execute(
                text(
                    "SELECT r.id, r.project_id, r.default_branch, r.local_root_ref, r.last_synced_commit_sha,"
                    " (SELECT c.commit_sha FROM code_index_snapshots c WHERE c.repository_id = r.id"
                    "   AND c.status = 'READY' ORDER BY c.created_at DESC, c.id DESC LIMIT 1) AS index_commit_sha"
                    " FROM repositories r WHERE r.id = :id AND r.project_id = :p"
                ),
                {"id": repository_id, "p": project_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ProblemError("not_found")
    return ExplorerRepository(
        id=row["id"],
        project_id=row["project_id"],
        default_branch=row["default_branch"],
        local_root_ref=row["local_root_ref"],
        index_commit_sha=row["index_commit_sha"],
        last_synced_commit_sha=row["last_synced_commit_sha"],
    )


def repository_path(repo: ExplorerRepository, roots: dict[str, Path]) -> Path:
    if not repo.local_root_ref:
        raise ProblemError("conflict", detail="У репозитория нет локальной копии для просмотра")
    try:
        path = resolve_ref(repo.local_root_ref, roots)
    except UnsafePathError as exc:
        raise ProblemError("service_unavailable", detail="Локальная копия репозитория недоступна") from exc
    if not path.is_dir():
        raise ProblemError("service_unavailable", detail="Локальная копия репозитория недоступна")
    return path


def validate_tree_path(raw: str | None) -> str:
    """Нормализованный относительный путь ('' — корень). Бросает 422 при небезопасном пути."""
    if raw is None or raw in ("", "/"):
        return ""
    value = raw

    def bad(message: str) -> ProblemError:
        return ProblemError("validation_error", errors=[FieldError("/path", "invalid_path", message)])

    if len(value) > 4096:
        raise bad("слишком длинный путь")
    if value.startswith("/"):
        raise bad("абсолютный путь запрещён")
    if "\\" in value or any(ord(ch) < 0x20 or ch == "\x7f" for ch in value):
        raise bad("недопустимые символы в пути")
    value = value.removesuffix("/")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise bad("компоненты '.', '..' и пустые запрещены")
    if any(part.lower() == ".git" for part in parts):
        raise bad("каталог .git недоступен")
    return value


def _git_error_to_not_found(detail: str) -> ProblemError:
    return ProblemError("not_found", detail=detail)


def resolve_commit(repo_path: Path, repo: ExplorerRepository, commit: str | None) -> str:
    """Полный SHA commit. Ошибки формата — 422, отсутствие — 404."""
    candidates: list[str] = []
    if commit is None or commit == "":
        try:
            branch = git_guard.validate_branch_name(repo.default_branch)
            candidates.append(f"refs/heads/{branch}")
        except git_guard.GitRepositoryError:
            pass
        candidates += [c for c in (repo.index_commit_sha, repo.last_synced_commit_sha) if c]
        if not candidates:
            raise _git_error_to_not_found("В репозитории нет commit для просмотра")
    elif _HEX_RE.fullmatch(commit):
        candidates.append(commit)
    else:
        try:
            branch = git_guard.validate_branch_name(commit)
        except git_guard.GitRepositoryError as exc:
            raise ProblemError(
                "validation_error",
                errors=[FieldError("/commit", "invalid_commit", "ожидается hex SHA или имя ветки")],
            ) from exc
        candidates.append(f"refs/heads/{branch}")
    for candidate in candidates:
        try:
            sha = git_guard.run_git(
                repo_path, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{candidate}^{{commit}}", timeout=30
            ).strip()
        except git_guard.GitRepositoryError:
            continue
        if _FULL_SHA_RE.fullmatch(sha):
            return sha
    raise _git_error_to_not_found("Commit не найден в репозитории")


def _list_tree_object(repo_path: Path, tree_ref: str, parent: str) -> list[TreeItem]:
    raw = git_guard.run_git_bytes(
        repo_path, "ls-tree", "-z", "-l", "--end-of-options", tree_ref, timeout=60, max_output_bytes=_MAX_TREE_OUTPUT
    )
    items: list[TreeItem] = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, sep, raw_name = record.partition(b"\t")
        fields = meta.split()
        if not sep or len(fields) != 4:
            raise ProblemError("internal_error", detail="Неожиданный формат дерева git")
        mode, otype, _sha, size_raw = (f.decode("ascii", "replace") for f in fields)
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            continue  # имя не UTF-8 — не отображается (не адресуется через API)
        if (
            not name
            or "/" in name
            or name in (".", "..")
            or name.lower() == ".git"
            or any(ord(ch) < 0x20 or ch == "\x7f" for ch in name)
        ):
            continue
        if otype == "tree":
            kind = "directory"
        elif otype == "commit":
            kind = "submodule"
        elif mode == "120000":
            kind = "symlink"
        else:
            kind = "file"
        items.append(
            TreeItem(
                name=name,
                path=f"{parent}/{name}" if parent else name,
                type=kind,
                mode=mode,
                size=int(size_raw) if size_raw.isdigit() else None,
                object_type=otype,
            )
        )
    items.sort(key=lambda i: (_TYPE_RANK.get(i.object_type, 3), i.name.encode("utf-8")))
    return items


def _object_type(repo_path: Path, ref: str) -> str | None:
    try:
        return git_guard.run_git(repo_path, "cat-file", "-t", "--end-of-options", ref, timeout=30).strip()
    except git_guard.GitRepositoryError:
        return None


def list_tree(repo_path: Path, commit_sha: str, path: str) -> list[TreeItem]:
    """Содержимое каталога `path` (один уровень) в commit `commit_sha`."""
    ref = f"{commit_sha}^{{tree}}" if path == "" else f"{commit_sha}:{path}"
    otype = _object_type(repo_path, ref)
    if otype is None:
        raise _git_error_to_not_found("Путь не найден в commit")
    if otype != "tree":
        raise ProblemError("validation_error", errors=[FieldError("/path", "not_a_directory", "путь — не каталог")])
    try:
        return _list_tree_object(repo_path, ref, path)
    except git_guard.GitRepositoryError as exc:
        raise ProblemError("service_unavailable", detail="Не удалось прочитать дерево git") from exc


def _decode_text(data: bytes, truncated: bool) -> str | None:
    """Текст UTF-8 или None для бинарного содержимого (NUL-байт или не UTF-8)."""
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return None
    # Усечённый файл может оборваться внутри многобайтного символа UTF-8 — допускаем до 3 байт хвоста.
    for cut in range(4 if truncated else 1):
        try:
            return data[: len(data) - cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def read_file(repo_path: Path, commit_sha: str, path: str) -> dict[str, Any]:
    """Содержимое файла (текст) или признаки binary/truncated; каталог/submodule — 422."""
    if path == "":
        raise ProblemError("validation_error", errors=[FieldError("/path", "required", "укажите путь к файлу")])
    parent, _, name = path.rpartition("/")
    parent_ref = f"{commit_sha}^{{tree}}" if parent == "" else f"{commit_sha}:{parent}"
    if _object_type(repo_path, parent_ref) != "tree":
        raise _git_error_to_not_found("Путь не найден в commit")
    try:
        entries = _list_tree_object(repo_path, parent_ref, parent)
    except git_guard.GitRepositoryError as exc:
        raise ProblemError("service_unavailable", detail="Не удалось прочитать дерево git") from exc
    entry = next((e for e in entries if e.name == name), None)
    if entry is None:
        raise _git_error_to_not_found("Путь не найден в commit")
    if entry.type in ("directory", "submodule"):
        raise ProblemError("validation_error", errors=[FieldError("/path", "not_a_file", "путь — не файл")])
    try:
        data, overflow = git_guard.run_git_bytes_prefix(
            repo_path, "cat-file", "blob", f"{commit_sha}:{path}", limit=MAX_FILE_BYTES, timeout=60
        )
    except git_guard.GitRepositoryError as exc:
        raise ProblemError("service_unavailable", detail="Не удалось прочитать файл git") from exc
    size = entry.size if entry.size is not None else len(data)
    truncated = overflow or size > len(data)
    content = _decode_text(data, truncated)
    binary = content is None
    return {
        "commit_sha": commit_sha,
        "path": path,
        "type": entry.type,
        "mode": entry.mode,
        "size": size,
        "max_bytes": MAX_FILE_BYTES,
        "truncated": truncated,
        "is_binary": binary,
        "encoding": None if binary else "utf-8",
        "content": content,
    }


# --- Ветки и история commit (OQ-45 (c)) ----------------------------------------------------------

MAX_BRANCHES = 1000
MAX_HISTORY_DEPTH = 1000
_SUBJECT_MAX = 500
_AUTHOR_MAX = 200
_REC_SEP = "\x1e"
_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class BranchItem:
    name: str
    commit_sha: str
    committed_at: str | None
    is_default: bool


def _clean_text(value: str, limit: int) -> str:
    """Однострочный текст без управляющих символов (метаданные git — недоверенные данные)."""
    cleaned = "".join(" " if ord(ch) < 0x20 or ch == "\x7f" else ch for ch in value).strip()
    return redact_text(cleaned)[:limit]


def _iso_or_none(value: str) -> str | None:
    value = value.strip()
    return value or None


def list_branches(repo_path: Path, default_branch: str) -> tuple[list[BranchItem], bool]:
    """Локальные ветки: ветка по умолчанию первой, остальные — новые commit первыми, затем по имени.

    Возвращает (ветки, truncated). Имена, не проходящие `validate_branch_name`, пропускаются.
    """
    try:
        raw = git_guard.run_git_bytes(
            repo_path,
            "for-each-ref",
            "--sort=-committerdate",
            f"--count={MAX_BRANCHES + 1}",
            "--format=%(refname)%00%(objectname)%00%(committerdate:iso-strict)",
            "refs/heads/",
            timeout=30,
            max_output_bytes=_MAX_TREE_OUTPUT,
        )
    except git_guard.GitRepositoryError as exc:
        raise ProblemError("service_unavailable", detail="Не удалось прочитать ветки git") from exc
    items: list[BranchItem] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        parts = line.split("\x00")
        if len(parts) != 3 or not parts[0].startswith("refs/heads/"):
            continue
        name = parts[0].removeprefix("refs/heads/")
        try:
            git_guard.validate_branch_name(name)
        except git_guard.GitRepositoryError:
            continue
        if not _FULL_SHA_RE.fullmatch(parts[1]):
            continue
        items.append(BranchItem(name, parts[1], _iso_or_none(parts[2]), name == default_branch))
    truncated = len(items) > MAX_BRANCHES
    items = items[:MAX_BRANCHES]
    items.sort(key=lambda b: (not b.is_default,))  # стабильная сортировка: default первой, порядок git сохранён
    return items, truncated


def list_commits(repo_path: Path, commit_sha: str, offset: int, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """История от `commit_sha` (порядок git log): страница `limit` commit после `offset`; второй элемент — есть ли ещё
    в пределах `MAX_HISTORY_DEPTH`. Email автора не отдаётся; тема — однострочный текст с редактированием секретов."""
    if offset >= MAX_HISTORY_DEPTH:
        return [], False
    count = min(limit + 1, MAX_HISTORY_DEPTH - offset)
    fmt = _FIELD_SEP.join(("%H", "%P", "%an", "%aI", "%cI", "%s")) + _REC_SEP
    try:
        raw = git_guard.run_git_bytes(
            repo_path,
            "log",
            "--no-show-signature",
            "--no-decorate",
            "--no-color",
            "--encoding=UTF-8",
            f"--format={fmt}",
            f"--max-count={count}",
            f"--skip={offset}",
            "--end-of-options",
            commit_sha,
            timeout=60,
            max_output_bytes=_MAX_TREE_OUTPUT,
        )
    except git_guard.GitRepositoryError as exc:
        raise ProblemError("service_unavailable", detail="Не удалось прочитать историю git") from exc
    items: list[dict[str, Any]] = []
    for record in raw.decode("utf-8", "replace").split(_REC_SEP):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) != 6 or not _FULL_SHA_RE.fullmatch(fields[0]):
            continue
        parents = [p for p in fields[1].split() if _FULL_SHA_RE.fullmatch(p)]
        items.append(
            {
                "sha": fields[0],
                "parents": parents,
                "author_name": _clean_text(fields[2], _AUTHOR_MAX),
                "authored_at": _iso_or_none(fields[3]),
                "committed_at": _iso_or_none(fields[4]),
                "subject": _clean_text(fields[5], _SUBJECT_MAX),
            }
        )
    more = len(items) > limit and offset + limit < MAX_HISTORY_DEPTH
    return items[:limit], more
