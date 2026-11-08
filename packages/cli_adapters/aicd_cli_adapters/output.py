"""Обработка вывода CLI (CLI_ADAPTER_CONTRACT §11, API_DESIGN §14): raw log, санитизированный вывод, лимиты.

Конвейер санитизации живого вывода (`cli.output`) выполняется в процессе исполнения до любой записи в БД/SSE:
декодирование UTF-8 с заменой, удаление ANSI/управляющих символов (кроме `\n`, `\t`), построчная буферизация
(`LineBuffer`, ≤ 64 KiB на строку — секрет не разрезается границей чанка), редактирование секретов
(`aicd_platform.redaction`), нарезка на чанки ≤ `EXCERPT_LIMIT` байт по границе символа (`sanitize_chunks`).
Полный санитизированный вывод пишется в `SanitizedTranscript` (artifact `cli_output`, текст).
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from typing import BinaryIO

from aicd_platform.redaction import REDACTED, redact_text

EXCERPT_LIMIT = 4096
LINE_LIMIT = 64 * 1024  # §14 п.2: построчная буферизация до 64 KiB на строку
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_excerpt(text: str) -> tuple[str, int, bool]:
    """Возвращает (текст, число редакций, обрезан ли)."""
    cleaned = clean_text(text)
    redacted = redact_text(cleaned)
    count = _count_redactions(cleaned, redacted)
    truncated = len(redacted.encode()) > EXCERPT_LIMIT
    if truncated:
        redacted = redacted.encode()[:EXCERPT_LIMIT].decode(errors="ignore")
    return redacted, count, truncated


def clean_text(text: str) -> str:
    """Шаг 1 конвейера: ANSI escape и управляющие символы (кроме \n и \t) удаляются."""
    return _CONTROL.sub("", _ANSI.sub("", text))


def _count_redactions(before: str, after: str) -> int:
    return max(after.count(REDACTED) - before.count(REDACTED), 0)


def sanitize_chunks(text: str, *, limit: int = EXCERPT_LIMIT) -> tuple[list[str], int]:
    """Шаги 1, 3, 4: очистка, редактирование всей строки целиком, затем нарезка на чанки ≤ `limit` байт UTF-8
    по границе символа. Возвращает (чанки, число редакций); пустая строка — один пустой чанк."""
    cleaned = clean_text(text)
    redacted = redact_text(cleaned)
    redactions = _count_redactions(cleaned, redacted)
    data = redacted.encode()
    chunks: list[str] = []
    start = 0
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and end > start and (data[end] & 0xC0) == 0x80:
            end -= 1  # не резать многобайтовый символ
        chunks.append(data[start:end].decode())
        start = end
    return chunks or [""], redactions


class LineBuffer:
    """Шаг 2: построчная буферизация потока байтов; строка длиннее `LINE_LIMIT` выдаётся частями
    (флаг `cut`), чтобы буфер не рос без предела."""

    def __init__(self, limit: int = LINE_LIMIT) -> None:
        self._limit = limit
        self._buf = b""

    def feed(self, data: bytes) -> list[tuple[str, bool]]:
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        out = [(line.decode(errors="replace").rstrip("\r"), False) for line in lines]
        while len(self._buf) > self._limit:
            head, self._buf = self._buf[: self._limit], self._buf[self._limit :]
            out.append((head.decode(errors="replace"), True))
        return out

    def flush(self) -> list[tuple[str, bool]]:
        rest, self._buf = self._buf, b""
        return [(rest.decode(errors="replace").rstrip("\r"), False)] if rest.strip() else []


@dataclass
class SanitizedTranscript:
    """Полный санитизированный вывод исполнения (API_DESIGN §14 п.5, artifact `cli_output`): строки
    `[поток] текст` после очистки и редактирования, без обрезки по 4 KiB; общий объём ограничен `max_bytes`."""

    max_bytes: int
    _file: BinaryIO = field(default_factory=lambda: tempfile.TemporaryFile(prefix="aicd-out-"))  # noqa: SIM115
    written: int = 0
    truncated: bool = False

    def write(self, stream: str, text: str) -> None:
        if self.truncated:
            return
        line = f"[{stream}] {redact_text(clean_text(text))}\n".encode()
        room = self.max_bytes - self.written
        if len(line) > room:
            line = line[: max(room, 0)].decode(errors="ignore").encode() + b"\n[truncated]\n"
            self.truncated = True
        self._file.write(line)
        self.written += len(line)

    def stream(self) -> BinaryIO:
        self._file.flush()
        self._file.seek(0)
        return self._file

    def close(self) -> None:
        self._file.close()


@dataclass
class RawLog:
    """Потоковая запись редактированного raw log во временный файл с лимитом объёма.

    Raw log — защищённый artifact; редакция известных секретов применяется и к нему (defence in depth).
    Запись построчная по каждому потоку (`LineBuffer`): секрет, разрезанный между фрагментами вывода процесса,
    редактируется целиком. Процесс не убивается из-за объёма: запись прекращается, выставляется `truncated`.
    """

    max_bytes: int
    _file: BinaryIO = field(default_factory=lambda: tempfile.TemporaryFile(prefix="aicd-raw-"))  # noqa: SIM115
    written: int = 0
    truncated: bool = False
    _lines: dict[str, LineBuffer] = field(default_factory=dict)

    def write(self, stream: str, data: bytes) -> None:
        for line, _ in self._lines.setdefault(stream, LineBuffer()).feed(data):
            self.write_line(stream, line)

    def finish(self) -> None:
        """Дописать незавершённые строки (после окончания процесса)."""
        for stream, buffer in self._lines.items():
            for line, _ in buffer.flush():
                self.write_line(stream, line)

    def write_line(self, stream: str, text: str) -> None:
        if self.truncated:
            return
        line = f"[{stream}] {redact_text(text)}\n".encode()
        room = self.max_bytes - self.written
        if len(line) > room:
            line = line[: max(room, 0)]
            self.truncated = True
        self._file.write(line)
        self.written += len(line)

    def stream(self) -> BinaryIO:
        self._file.flush()
        self._file.seek(0)
        return self._file

    def close(self) -> None:
        self._file.close()
