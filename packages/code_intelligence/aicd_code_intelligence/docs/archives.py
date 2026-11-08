"""Безопасная распаковка архивов документации (SC-DOC-06, TH-08, TH-26; SECURITY_ARCHITECTURE §9).

Распаковка — только в память и только текстовых документов (`.md`, `.markdown`, `.txt`, `.rst`, `.html`,
`.htm`, LICENSE/COPYING/NOTICE); на диск ничего не пишется, поэтому zip-slip невозможен по построению,
но имена всё равно проверяются и любое нарушение отклоняет архив целиком (`ArchiveRejectedError` →
снимок `QUARANTINED`):

* ≤ `max_entries` записей; суммарно распаковано ≤ `max_total_bytes`; один документ ≤ `max_member_bytes`;
* коэффициент сжатия ≤ `max_ratio` — на файл и в целом, проверяется **при потоковой распаковке**
  (заголовки архива не доверяются);
* абсолютные пути, `..`, `\\`, буквы дисков, NUL/управляющие символы, symlink/hardlink, device/FIFO,
  зашифрованные записи, коллизии имён после NFC-нормализации и casefold — отказ;
* вложенные архивы не распаковываются (пропускаются и перечисляются в `skipped`).
"""

from __future__ import annotations

import gzip
import io
import stat
import tarfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import IO, Final

MIB: Final = 1024 * 1024
TEXT_SUFFIXES: Final = frozenset({".md", ".markdown", ".txt", ".rst", ".html", ".htm"})
LICENSE_NAMES: Final = frozenset({"license", "licence", "copying", "notice", "license.md", "license.txt", "licence.md"})
ARCHIVE_SUFFIXES: Final = frozenset(
    {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".whl", ".zst", ".lz", ".lzma"}
)
_CHUNK: Final = 64 * 1024


class ArchiveRejectedError(Exception):
    """Архив нарушает ограничения; `code` — для `documentation_snapshots.error`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 10_000
    max_total_bytes: int = 500 * MIB
    max_member_bytes: int = 20 * MIB
    max_ratio: int = 100


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    data: bytes


@dataclass
class ArchiveContents:
    members: list[ArchiveMember] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    total_uncompressed: int = 0


def safe_member_path(name: str) -> str:
    """Нормализованный относительный POSIX-путь записи или ArchiveRejectedError."""
    if not name or len(name) > 1024:
        raise ArchiveRejectedError("ARCHIVE_PATH", "пустое или слишком длинное имя записи")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in name):
        raise ArchiveRejectedError("ARCHIVE_PATH", "управляющие символы/NUL в имени записи")
    if "\\" in name:
        raise ArchiveRejectedError("ARCHIVE_PATH", "обратная косая черта в имени записи")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveRejectedError("ARCHIVE_PATH", "абсолютный путь в архиве")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ArchiveRejectedError("ARCHIVE_PATH", "компонент '..' в архиве (path traversal)")
    if not parts:
        raise ArchiveRejectedError("ARCHIVE_PATH", "пустое имя записи")
    return unicodedata.normalize("NFC", "/".join(parts))


def _is_document(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix.lower() in TEXT_SUFFIXES or p.name.lower() in LICENSE_NAMES


def _is_archive(path: str) -> bool:
    return any(s.lower() in ARCHIVE_SUFFIXES for s in PurePosixPath(path).suffixes)


class _Budget:
    def __init__(self, limits: ArchiveLimits, compressed_total: int) -> None:
        self.limits = limits
        self.compressed_total = max(compressed_total, 1)
        self.total = 0
        self.entries = 0
        self.seen: set[str] = set()

    def entry(self, path: str) -> None:
        self.entries += 1
        if self.entries > self.limits.max_entries:
            raise ArchiveRejectedError("ARCHIVE_TOO_MANY_ENTRIES", "слишком много записей")
        key = path.casefold()
        if key in self.seen:
            raise ArchiveRejectedError("ARCHIVE_PATH", "коллизия имён после нормализации")
        self.seen.add(key)

    def account(self, n: int) -> None:
        self.total += n
        if self.total > self.limits.max_total_bytes:
            raise ArchiveRejectedError("ARCHIVE_TOO_LARGE", "превышен суммарный размер распаковки")
        if self.total > self.compressed_total * self.limits.max_ratio:
            raise ArchiveRejectedError("ARCHIVE_BOMB", "превышен общий коэффициент сжатия")


def _read_member(stream: IO[bytes] | gzip.GzipFile, compressed: int, budget: _Budget, *, check_ratio: bool) -> bytes:
    out = bytearray()
    limit_ratio = max(compressed, 1) * budget.limits.max_ratio
    while chunk := stream.read(_CHUNK):
        out += chunk
        budget.account(len(chunk))
        if check_ratio and len(out) > limit_ratio:
            raise ArchiveRejectedError("ARCHIVE_BOMB", "превышен коэффициент сжатия записи")
        if len(out) > budget.limits.max_member_bytes:
            raise ArchiveRejectedError("ARCHIVE_TOO_LARGE", "документ в архиве слишком большой")
    return bytes(out)


def extract_zip(data: bytes, limits: ArchiveLimits | None = None) -> ArchiveContents:
    limits = limits or ArchiveLimits()
    budget = _Budget(limits, len(data))
    result = ArchiveContents()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise ArchiveRejectedError("ARCHIVE_INVALID", "повреждённый zip") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise ArchiveRejectedError("ARCHIVE_TOO_MANY_ENTRIES", "слишком много записей")
        for info in infos:
            # orig_filename — имя до нормализации zipfile (обрезка по NUL, замена разделителей).
            path = safe_member_path(info.orig_filename)
            budget.entry(path)
            mode = (info.external_attr >> 16) & 0xFFFF
            ftype = stat.S_IFMT(mode)
            if ftype == stat.S_IFLNK:
                raise ArchiveRejectedError("ARCHIVE_LINK", "symlink в архиве")
            if ftype not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ArchiveRejectedError("ARCHIVE_SPECIAL", "специальный файл в архиве")
            if info.flag_bits & 0x1:
                raise ArchiveRejectedError("ARCHIVE_ENCRYPTED", "зашифрованная запись")
            if info.is_dir():
                continue
            if _is_archive(path):
                result.skipped.append(path)
                continue
            if not _is_document(path):
                result.skipped.append(path)
                continue
            try:
                with zf.open(info) as stream:
                    body = _read_member(stream, info.compress_size, budget, check_ratio=True)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
                raise ArchiveRejectedError("ARCHIVE_INVALID", "ошибка распаковки записи") from exc
            result.members.append(ArchiveMember(path, body))
    result.total_uncompressed = budget.total
    return result


class _CappedReader(io.RawIOBase):
    """Поток распаковки gzip с обрывом по лимиту (защита до разбора tar-заголовков)."""

    def __init__(self, inner: gzip.GzipFile, budget: _Budget) -> None:
        self._inner = inner
        self._budget = budget

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:  # type: ignore[override]
        data = self._inner.read(len(b))
        self._budget.account(len(data))
        n = len(data)
        b[:n] = data
        return n


def _tar_members(tf: tarfile.TarFile, budget: _Budget, result: ArchiveContents) -> None:
    for member in tf:
        path = safe_member_path(member.name)
        budget.entry(path)
        if member.issym() or member.islnk():
            raise ArchiveRejectedError("ARCHIVE_LINK", "symlink/hardlink в архиве")
        if member.isdev() or member.isfifo() or not (member.isreg() or member.isdir()):
            raise ArchiveRejectedError("ARCHIVE_SPECIAL", "специальный файл в архиве")
        if member.isdir():
            continue
        if _is_archive(path) or not _is_document(path):
            result.skipped.append(path)
            continue
        if member.size > budget.limits.max_member_bytes:
            raise ArchiveRejectedError("ARCHIVE_TOO_LARGE", "документ в архиве слишком большой")
        stream = tf.extractfile(member)
        if stream is None:  # pragma: no cover — обычный файл всегда читается
            continue
        out = bytearray()
        while chunk := stream.read(_CHUNK):
            out += chunk
            if len(out) > budget.limits.max_member_bytes:
                raise ArchiveRejectedError("ARCHIVE_TOO_LARGE", "документ в архиве слишком большой")
        result.members.append(ArchiveMember(path, bytes(out)))


def extract_tar(data: bytes, limits: ArchiveLimits | None = None, *, gzipped: bool = False) -> ArchiveContents:
    """tar / tar.gz в потоковом режиме (`r|`): распакованный поток ограничен бюджетом до разбора заголовков."""
    limits = limits or ArchiveLimits()
    budget = _Budget(limits, len(data))
    result = ArchiveContents()
    raw: IO[bytes] = io.BytesIO(data)
    if gzipped:
        raw = io.BufferedReader(_CappedReader(gzip.GzipFile(fileobj=raw), budget))
    else:
        budget.account(len(data))
    try:
        with tarfile.open(fileobj=raw, mode="r|") as tf:
            _tar_members(tf, budget, result)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise ArchiveRejectedError("ARCHIVE_INVALID", "повреждённый tar") from exc
    result.total_uncompressed = budget.total
    return result


def is_tar(data: bytes) -> bool:
    return len(data) >= 262 and data[257:262] == b"ustar"


def gunzip_single(data: bytes, limits: ArchiveLimits | None = None) -> bytes:
    """Один gzip-сжатый документ (не tar) с потоковой проверкой коэффициента."""
    limits = limits or ArchiveLimits()
    budget = _Budget(limits, len(data))
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            return _read_member(gz, len(data), budget, check_ratio=True)
    except (OSError, EOFError, zlib.error) as exc:
        raise ArchiveRejectedError("ARCHIVE_INVALID", "повреждённый gzip") from exc


def peek_gzip(data: bytes, n: int = 512) -> bytes:
    """Первые `n` байт распакованного gzip (определение tar внутри gzip)."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            return gz.read(n)
    except (OSError, EOFError, zlib.error) as exc:
        raise ArchiveRejectedError("ARCHIVE_INVALID", "повреждённый gzip") from exc
