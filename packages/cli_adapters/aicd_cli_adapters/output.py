"""Обработка вывода CLI (CLI_ADAPTER_CONTRACT §11): raw log, санитизированные выдержки, лимиты."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from typing import BinaryIO

from aicd_platform.redaction import REDACTED, redact_text

EXCERPT_LIMIT = 4096
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_excerpt(text: str) -> tuple[str, int, bool]:
    """Возвращает (текст, число редакций, обрезан ли)."""
    cleaned = _CONTROL.sub("", _ANSI.sub("", text))
    redacted = redact_text(cleaned)
    count = redacted.count(REDACTED) - cleaned.count(REDACTED)
    truncated = len(redacted.encode()) > EXCERPT_LIMIT
    if truncated:
        redacted = redacted.encode()[:EXCERPT_LIMIT].decode(errors="ignore")
    return redacted, max(count, 0), truncated


@dataclass
class RawLog:
    """Потоковая запись редактированного raw log во временный файл с лимитом объёма.

    Raw log — защищённый artifact; редакция известных секретов применяется и к нему (defence in depth).
    Процесс не убивается из-за объёма: запись прекращается, выставляется `truncated`.
    """

    max_bytes: int
    _file: BinaryIO = field(default_factory=lambda: tempfile.TemporaryFile(prefix="aicd-raw-"))  # noqa: SIM115
    written: int = 0
    truncated: bool = False

    def write(self, stream: str, data: bytes) -> None:
        if self.truncated:
            return
        line = f"[{stream}] ".encode() + redact_text(data.decode(errors="replace")).encode()
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
