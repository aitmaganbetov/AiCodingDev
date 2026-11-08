"""Cursor-пагинация: base64url(JSON) + усечённый HMAC-SHA256 (API_DESIGN §7)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from aicd_platform.errors import ProblemError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def filters_hash(filters: dict[str, Any]) -> str:
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode_cursor(key: bytes, keyset: list[Any], filters: dict[str, Any]) -> str:
    body = json.dumps({"v": 1, "k": keyset, "f": filters_hash(filters)}, separators=(",", ":"), default=str)
    raw = body.encode()
    sig = hmac.new(key, raw, hashlib.sha256).digest()[:16]
    return _b64(raw) + "." + _b64(sig)


def decode_cursor(key: bytes, cursor: str, filters: dict[str, Any]) -> list[Any]:
    try:
        body_part, sig_part = cursor.split(".", 1)
        raw = _unb64(body_part)
        sig = _unb64(sig_part)
    except (ValueError, TypeError) as exc:
        raise ProblemError("invalid_cursor") from exc
    expected = hmac.new(key, raw, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(sig, expected):
        raise ProblemError("invalid_cursor")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ProblemError("invalid_cursor") from exc
    if data.get("v") != 1 or data.get("f") != filters_hash(filters) or not isinstance(data.get("k"), list):
        raise ProblemError("invalid_cursor")
    keyset: list[Any] = data["k"]
    return keyset
