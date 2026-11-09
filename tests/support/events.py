"""Проверка SSE-конвертов по нормативной схеме и разбор кадров text/event-stream."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
SSE_SCHEMA = ROOT / "packages" / "contracts" / "schemas" / "events" / "1.0" / "sse-event.schema.json"


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SSE_SCHEMA.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_sse(envelope: dict[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(envelope), key=lambda e: list(e.path))
    assert not errors, "; ".join(f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5])


NOTIFICATION_SCHEMA = SSE_SCHEMA.with_name("notification-event.schema.json")


@lru_cache(maxsize=1)
def _notification_validator() -> Draft202012Validator:
    schema = json.loads(NOTIFICATION_SCHEMA.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_notification_event(envelope: dict[str, Any]) -> None:
    """Кадр `notification.created` потока workspace (notification-event.schema.json)."""
    errors = sorted(_notification_validator().iter_errors(envelope), key=lambda e: list(e.path))
    assert not errors, "; ".join(f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5])


def parse_frame(lines: list[str]) -> dict[str, str]:
    """Поля одного SSE-кадра (`id`, `event`, `data`, `retry`); комментарии игнорируются."""
    fields: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        fields[name] = value[1:] if value.startswith(" ") else value
    return fields
