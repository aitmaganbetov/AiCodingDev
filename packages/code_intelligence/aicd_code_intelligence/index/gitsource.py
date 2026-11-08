"""Чтение дерева commit и blob без checkout (P5-02).

* дерево — `git ls-tree -r -z -l --full-tree <commit>` (режим, тип, blob sha, размер, путь);
* содержимое — `git cat-file --batch` пачками по суммарному размеру (без `--filters`: smudge/textconv
  из конфигурации недоверенного репозитория не исполняются);
* git вызывается через `aicd_security.git` (argv без shell, хуки/fsmonitor отключены, чистое окружение).

Пути из дерева недоверенные: путь с управляющими символами, `..`, ведущим `/` или длиннее 4096
отбрасывается (`unsafe_paths`) и никогда не используется для записи на диск.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from aicd_security.git import GitRepositoryError, run_git_bytes

COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BATCH_BYTES = 32 * 1024 * 1024
_MAX_TREE_OUTPUT = 256 * 1024 * 1024


class GitSourceError(RuntimeError):
    """Ошибка чтения объектов git (сообщение безопасно для `indexing_jobs.error`)."""


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    object_type: str
    blob_sha: str
    size: int | None


def validate_commit_sha(commit_sha: str) -> str:
    if not COMMIT_SHA_RE.fullmatch(commit_sha):
        raise GitSourceError("commit_sha должен быть полным hex SHA")
    return commit_sha


def is_safe_tree_path(path: str) -> bool:
    if not path or len(path) > 4096 or path.startswith("/"):
        return False
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in path):
        return False
    return not any(part in ("..", ".", "") for part in path.split("/")) and "\\" not in path


def parse_ls_tree(raw: bytes) -> tuple[list[TreeEntry], list[str]]:
    """Разбор вывода `ls-tree -r -z -l`. Возвращает (записи, отброшенные небезопасные пути)."""
    entries: list[TreeEntry] = []
    unsafe: list[str] = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, sep, raw_path = record.partition(b"\t")
        if not sep:
            raise GitSourceError("неожиданный формат ls-tree")
        fields = meta.split()
        if len(fields) != 4:
            raise GitSourceError("неожиданный формат ls-tree")
        mode, otype, sha, size_raw = (f.decode("ascii", "replace") for f in fields)
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            unsafe.append(raw_path.decode("utf-8", "replace"))
            continue
        if not is_safe_tree_path(path):
            unsafe.append(path)
            continue
        size = int(size_raw) if size_raw.isdigit() else None
        entries.append(TreeEntry(path=path, mode=mode, object_type=otype, blob_sha=sha, size=size))
    entries.sort(key=lambda e: e.path)
    return entries, unsafe


def list_tree(repo: Path, commit_sha: str) -> tuple[list[TreeEntry], list[str]]:
    validate_commit_sha(commit_sha)
    try:
        run_git_bytes(repo, "cat-file", "-e", f"{commit_sha}^{{commit}}", timeout=30)
        raw = run_git_bytes(
            repo, "ls-tree", "-r", "-z", "-l", "--full-tree", commit_sha, timeout=120, max_output_bytes=_MAX_TREE_OUTPUT
        )
    except GitRepositoryError as exc:
        raise GitSourceError(f"commit {commit_sha[:12]} недоступен в репозитории") from exc
    return parse_ls_tree(raw)


def parse_cat_file_batch(raw: bytes, expected: list[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    pos = 0
    for sha in expected:
        nl = raw.find(b"\n", pos)
        if nl < 0:
            raise GitSourceError("обрезанный вывод cat-file")
        header = raw[pos:nl].decode("ascii", "replace").split()
        if len(header) == 2 and header[1] == "missing":
            raise GitSourceError(f"объект {sha[:12]} отсутствует")
        if len(header) != 3 or header[0] != sha:
            raise GitSourceError("неожиданный заголовок cat-file")
        size = int(header[2])
        start = nl + 1
        end = start + size
        if end > len(raw) or raw[end : end + 1] != b"\n":
            raise GitSourceError("обрезанный вывод cat-file")
        out[sha] = raw[start:end]
        pos = end + 1
    return out


def _batches(entries: Iterable[TreeEntry]) -> Iterator[list[TreeEntry]]:
    batch: list[TreeEntry] = []
    total = 0
    for entry in entries:
        size = entry.size or 0
        if batch and total + size > _BATCH_BYTES:
            yield batch
            batch, total = [], 0
        batch.append(entry)
        total += size
    if batch:
        yield batch


def read_blobs(repo: Path, entries: Iterable[TreeEntry]) -> dict[str, bytes]:
    """Содержимое blob по sha (дубликаты содержимого читаются один раз)."""
    unique: dict[str, TreeEntry] = {}
    for entry in entries:
        unique.setdefault(entry.blob_sha, entry)
    result: dict[str, bytes] = {}
    for batch in _batches(unique.values()):
        shas = [e.blob_sha for e in batch]
        for sha in shas:
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
                raise GitSourceError("недопустимый sha объекта")
        expected_bytes = sum((e.size or 0) + 128 for e in batch)
        try:
            raw = run_git_bytes(
                repo,
                "cat-file",
                "--batch",
                stdin=("\n".join(shas) + "\n").encode("ascii"),
                timeout=300,
                max_output_bytes=expected_bytes + 4096,
            )
        except GitRepositoryError as exc:
            raise GitSourceError("не удалось прочитать объекты git") from exc
        result.update(parse_cat_file_batch(raw, shas))
    return result
