"""Единый источник времени (UTC)."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat_z(value: datetime) -> str:
    """ISO 8601 с суффиксом Z (API_DESIGN §1)."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
