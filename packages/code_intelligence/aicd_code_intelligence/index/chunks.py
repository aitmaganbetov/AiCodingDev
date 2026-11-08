"""Фрагменты (`knowledge_chunks`) для FTS/trigram-поиска (P5-04; DATABASE_DESIGN §10, §10.5).

Текст кода в БД не копируется: `text_ref = git:<blob_sha>#L<from>-L<to>` (текст берётся из git),
в БД хранятся `search_vector` (tsvector конфигурации `simple`), `content_hash` и метаданные.

* `code_file` — окна по `chunk_lines` строк на весь файл;
* `code_symbol` — каждый символ, кроме `module` (большой символ — окна по `max_symbol_chunk_lines`).

Текст для tsvector дополняется путём, именем символа и разбиением идентификаторов
(`getUserName` → `get user name`), чтобы запрос «user name» находил camelCase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aicd_code_intelligence.index.symbols import ExtractedSymbol, line_count, sha256_hex

TS_CONFIG = "simple"
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_MAX_FTS_CHARS = 200_000


@dataclass(frozen=True)
class Chunk:
    source: str  # 'code_file' | 'code_symbol'
    symbol_index: int | None  # индекс символа в FileAnalysis.symbols (для code_symbol)
    chunk_index: int
    start_line: int
    end_line: int
    content_hash: str
    token_estimate: int
    fts_text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def text_ref(self, blob_sha: str) -> str:
        return f"git:{blob_sha}#L{self.start_line}-L{self.end_line}"


def split_identifiers(text: str) -> str:
    """Части составных идентификаторов (camelCase/PascalCase/snake_case) для tsvector."""
    out: list[str] = []
    seen: set[str] = set()
    for ident in _IDENT.findall(text):
        for piece in ident.split("_"):
            parts = _CAMEL.findall(piece)
            if len(parts) > 1:
                for part in parts:
                    low = part.lower()
                    if low not in seen:
                        seen.add(low)
                        out.append(low)
    return " ".join(out)


def fts_document(*parts: str) -> str:
    body = "\n".join(p for p in parts if p)
    body = body[:_MAX_FTS_CHARS]
    return f"{body}\n{split_identifiers(body)}"


def estimate_tokens(data: bytes) -> int:
    return (len(data) + 3) // 4


def _windows(start: int, end: int, size: int) -> list[tuple[int, int]]:
    out = []
    line = start
    while line <= end:
        out.append((line, min(end, line + size - 1)))
        line += size
    return out


def build_chunks(
    *,
    path: str,
    language: str | None,
    content: bytes,
    symbols: tuple[ExtractedSymbol, ...],
    chunk_lines: int,
    max_symbol_chunk_lines: int,
) -> list[Chunk]:
    total = line_count(content)
    if total == 0:
        return []
    lines = content.split(b"\n")

    def segment(a: int, b: int) -> bytes:
        return b"\n".join(lines[a - 1 : b])

    chunks: list[Chunk] = []
    for idx, (a, b) in enumerate(_windows(1, total, chunk_lines)):
        data = segment(a, b)
        text = data.decode("utf-8", "replace")
        chunks.append(
            Chunk(
                source="code_file",
                symbol_index=None,
                chunk_index=idx,
                start_line=a,
                end_line=b,
                content_hash=sha256_hex(data),
                token_estimate=estimate_tokens(data),
                fts_text=fts_document(path, text),
                metadata={"path": path, "language": language, "start_line": a, "end_line": b},
            )
        )
    for sym_idx, sym in enumerate(symbols):
        if sym.kind == "module":
            continue
        end = min(sym.end_line, total)
        start = min(sym.start_line, end)
        for idx, (a, b) in enumerate(_windows(start, end, max_symbol_chunk_lines)):
            data = segment(a, b)
            text = data.decode("utf-8", "replace")
            chunks.append(
                Chunk(
                    source="code_symbol",
                    symbol_index=sym_idx,
                    chunk_index=idx,
                    start_line=a,
                    end_line=b,
                    content_hash=sha256_hex(data),
                    token_estimate=estimate_tokens(data),
                    fts_text=fts_document(sym.name, sym.qualified_name or "", sym.doc or "", text),
                    metadata={
                        "path": path,
                        "language": language,
                        "start_line": a,
                        "end_line": b,
                        "symbol": sym.name,
                        "qualified_name": sym.qualified_name,
                        "kind": sym.kind,
                        **({"doc": sym.doc} if sym.doc else {}),
                    },
                )
            )
    return chunks
