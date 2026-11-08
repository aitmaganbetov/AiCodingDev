"""Сериализация ответов: UTC ISO 8601 с `Z`, UUID строками (API_DESIGN §1)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from typing import Any

from aicd_platform.clock import isoformat_z


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return isoformat_z(value)
    if isinstance(value, uuid.UUID | IPv4Address | IPv6Address):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"не сериализуемо: {type(value).__name__}")


def dumps(value: Any) -> bytes:
    return json.dumps(value, default=_default, ensure_ascii=False, separators=(",", ":")).encode()


def to_jsonable(value: Any) -> Any:
    return json.loads(dumps(value))
