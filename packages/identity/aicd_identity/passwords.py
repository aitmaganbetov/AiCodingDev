"""Хэширование паролей argon2id (ADR-0009, SECURITY_ARCHITECTURE §4)."""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# m=64 MiB, t=3, p=1 — параметры ADR-0009 (уточняются бенчмарком на целевом железе).
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

# Хэш-«пустышка» для выравнивания времени ответа при несуществующем пользователе.
_DUMMY_HASH = _hasher.hash("aicd-timing-equalizer-not-a-password")


class WeakPasswordError(ValueError):
    pass


def validate_password_policy(password: str, *, email: str | None = None) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"пароль должен содержать не менее {MIN_PASSWORD_LENGTH} символов")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError("пароль слишком длинный")
    if email and email.split("@", 1)[0].lower() in password.lower():
        raise WeakPasswordError("пароль не должен содержать имя учётной записи")
    if len(set(password)) < 4:
        raise WeakPasswordError("пароль слишком однообразный")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Проверяет пароль; при отсутствии хэша всё равно тратит время на проверку (защита от тайминга)."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
