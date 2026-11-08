"""Случайные токены сессий/CSRF; в БД хранится только SHA-256 (ADR-0009)."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 256 бит


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def matches(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(token_hash(token), expected_hash)
