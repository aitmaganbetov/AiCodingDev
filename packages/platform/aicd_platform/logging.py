"""Структурные JSON-логи с корреляцией и редактированием секретов (FR-OBS-01)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from aicd_platform import context
from aicd_platform.clock import isoformat_z, utcnow
from aicd_platform.redaction import redact_text, redact_value

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": isoformat_z(utcnow()),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        payload.update(context.snapshot())
        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        if extra:
            payload["extra"] = redact_value(extra)
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn access-лог дублирует наш middleware и пишет query string — отключаем.
    logging.getLogger("uvicorn.access").disabled = True
