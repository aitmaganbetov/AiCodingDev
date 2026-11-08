"""Нормализация загруженной документации: текст, секции, лицензия, карантин (SC-DOC-05…08).

Вход — уже проверенный `security.ssrf` ответ (MIME из allowlist, magic bytes совпадают, размер в лимите).
Выход — `ProcessedDocument`: нормализованный UTF-8 текст (один артефакт) и секции с диапазонами байт
(`documentation_sections.body_ref = artifact:<id>#bytes=<start>-<end>`, конец включительно, как в HTTP Range).

* HTML → `html_text` (активное содержимое удалено, скрытый текст исключён и посчитан);
* Markdown/text — как есть (только UTF-8; иначе декодирование с заменой);
* zip / tar / tar.gz — `archives` (нарушение ⇒ `QUARANTINED`), gzip одного документа — распаковка с
  лимитами; документы внутри — как выше; вложенные архивы не распаковываются;
* PDF — в процессе API/worker не разбирается (SC-DOC-07: парсер только в одноразовом контейнере без
  сети) — сохраняется исходник, секций нет (`parse_deferred=True`).

Содержимое — НЕДОВЕРЕННЫЕ данные (prompt injection): здесь оно только хранится и режется на секции.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final

from aicd_code_intelligence.docs.archives import (
    LICENSE_NAMES,
    ArchiveContents,
    ArchiveLimits,
    ArchiveRejectedError,
    extract_tar,
    extract_zip,
    gunzip_single,
    is_tar,
    peek_gzip,
)
from aicd_code_intelligence.docs.html_text import html_to_text

MAX_SECTIONS: Final = 5000
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

_LICENSE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"SPDX-License-Identifier:\s*([A-Za-z0-9.+-]+(?:\s+(?:OR|AND|WITH)\s+[A-Za-z0-9.+-]+)*)"), ""),
    (re.compile(r"Apache License\s*,?\s*Version 2\.0", re.I), "Apache-2.0"),
    (re.compile(r"\bMIT License\b|Permission is hereby granted, free of charge", re.I), "MIT"),
    (re.compile(r"GNU AFFERO GENERAL PUBLIC LICENSE\s+Version 3", re.I), "AGPL-3.0"),
    (re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE\s+Version 3", re.I), "LGPL-3.0"),
    (re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 3", re.I), "GPL-3.0"),
    (re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 2", re.I), "GPL-2.0"),
    (re.compile(r"Mozilla Public License,?\s+(?:Version|v\.)\s*2\.0", re.I), "MPL-2.0"),
    (re.compile(r"Redistribution and use in source and binary forms", re.I), "BSD"),
    (re.compile(r"Creative Commons Attribution[- ]ShareAlike 4\.0", re.I), "CC-BY-SA-4.0"),
    (re.compile(r"Creative Commons Attribution 4\.0", re.I), "CC-BY-4.0"),
    (re.compile(r"Python Software Foundation License", re.I), "PSF-2.0"),
)


def detect_license(text: str) -> str | None:
    head = text[:20000]
    for pattern, spdx in _LICENSE_PATTERNS:
        m = pattern.search(head)
        if m:
            return (spdx or m.group(1)).strip()[:200]
    return None


@dataclass(frozen=True)
class SectionSpan:
    section_path: str
    heading: str | None
    start: int
    end: int  # включительно
    content_hash: str


@dataclass
class ProcessedDocument:
    status: str  # READY | QUARANTINED
    normalized: bytes = b""
    sections: list[SectionSpan] = field(default_factory=list)
    license: str | None = None
    title: str | None = None
    hidden_fragments: int = 0
    skipped: list[str] = field(default_factory=list)
    parse_deferred: bool = False
    error: str | None = None


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _slug(text: str) -> str:
    s = re.sub(r"[^\w-]+", "-", text.lower(), flags=re.UNICODE).strip("-")
    return s[:80] or "section"


def split_sections(markdown: str) -> list[tuple[str | None, str]]:
    """Разбиение по ATX-заголовкам вне блоков кода: [(heading, текст секции, включая заголовок)]."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    in_fence = False
    for line in markdown.splitlines(keepends=True):
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line.rstrip("\n"))
        if m:
            sections.append((m.group(2).strip()[:1000], [line]))
        else:
            sections[-1][1].append(line)
    return [(h, "".join(lines)) for h, lines in sections if "".join(lines).strip()]


class _Builder:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.sections: list[SectionSpan] = []
        self.paths: set[str] = set()

    def add_document(self, prefix: str, markdown: str) -> None:
        for idx, (heading, body) in enumerate(split_sections(markdown), start=1):
            if len(self.sections) >= MAX_SECTIONS:
                raise ArchiveRejectedError("TOO_MANY_SECTIONS", "слишком много секций")
            data = body.encode("utf-8")
            path = f"{prefix}#{idx:04d}-{_slug(heading or 'intro')}"[:1024]
            if path in self.paths:  # pragma: no cover — индекс делает путь уникальным
                path = f"{path[:1000]}-{len(self.sections)}"
            self.paths.add(path)
            start = len(self.buf)
            self.buf += data
            self.sections.append(SectionSpan(path, heading, start, len(self.buf) - 1, hashlib.sha256(data).hexdigest()))
            if not self.buf.endswith(b"\n"):
                self.buf += b"\n"


def _to_markdown(path: str, data: bytes) -> tuple[str, int, str | None, str | None]:
    """(markdown, скрытых фрагментов, лицензия, заголовок) одного текстового документа."""
    text = _decode(data)
    if PurePosixPath(path).suffix.lower() in (".html", ".htm"):
        converted = html_to_text(text)
        return converted.markdown, len(converted.hidden_fragments), converted.license_hint, converted.title
    return text, 0, None, None


def _process_archive(contents: ArchiveContents) -> ProcessedDocument:
    builder = _Builder()
    hidden = 0
    license_text: str | None = None
    for member in sorted(contents.members, key=lambda m: m.path):
        markdown, h, lic, _title = _to_markdown(member.path, member.data)
        hidden += h
        if PurePosixPath(member.path).name.lower() in LICENSE_NAMES:
            license_text = license_text or detect_license(markdown) or "см. файл " + member.path
        license_text = license_text or lic
        builder.add_document(member.path, markdown)
    return ProcessedDocument(
        status="READY",
        normalized=bytes(builder.buf),
        sections=builder.sections,
        license=license_text,
        hidden_fragments=hidden,
        skipped=contents.skipped,
    )


def process_content(
    mime: str, data: bytes, *, name: str = "document", limits: ArchiveLimits | None = None
) -> ProcessedDocument:
    """MIME уже проверен `security.ssrf`. Ошибки архивов ⇒ QUARANTINED (содержимое не индексируется)."""
    try:
        if mime == "application/pdf":
            return ProcessedDocument(status="READY", parse_deferred=True)
        if mime == "application/zip":
            return _process_archive(extract_zip(data, limits))
        if mime == "application/x-tar":
            return _process_archive(extract_tar(data, limits))
        if mime == "application/gzip":
            if is_tar(peek_gzip(data)):
                return _process_archive(extract_tar(data, limits, gzipped=True))
            inner = gunzip_single(data, limits)
            if b"\x00" in inner[:8192]:
                raise ArchiveRejectedError("MIME_MISMATCH", "gzip содержит не текстовый документ")
            base = name.removesuffix(".gz")
            data, name = inner, base if PurePosixPath(base).suffix else base + ".md"
            mime = "text/html" if PurePosixPath(name).suffix.lower() in (".html", ".htm") else "text/markdown"
        doc_name = name if PurePosixPath(name).suffix else name + (".html" if mime == "text/html" else ".md")
        if mime == "text/html" and PurePosixPath(doc_name).suffix.lower() not in (".html", ".htm"):
            doc_name += ".html"
        markdown, hidden, lic, title = _to_markdown(doc_name, data)
        builder = _Builder()
        builder.add_document(PurePosixPath(doc_name).name, markdown)
        return ProcessedDocument(
            status="READY",
            normalized=bytes(builder.buf),
            sections=builder.sections,
            license=lic or detect_license(markdown),
            title=title,
            hidden_fragments=hidden,
        )
    except ArchiveRejectedError as exc:
        return ProcessedDocument(status="QUARANTINED", error=f"{exc.code}: {exc.message}")
