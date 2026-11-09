"""Редактирование секретов в логах, событиях и сообщениях об ошибках (NFR-SEC-02, SC-SEC-*).

Шаблоны консервативны: лучше лишний раз замаскировать, чем допустить утечку.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Приватные ключи PEM
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    # Bearer/Basic токены в заголовках
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # Типичные ключи провайдеров и платформ
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    # Пароли в URL: scheme://user:password@host
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    # key=value для чувствительных имён
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|cookie|session)"
        # значение без кавычек не захватывает кавычки: иначе в JSON/YAML терялась бы закрывающая кавычка
        r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"
    ),
)

_SENSITIVE_KEYS = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|cookie|authorization|"
    r"session|csrf|credential)"
)


def redact_text(text: str) -> str:
    result = text
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            result = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", result)
        else:
            result = pattern.sub(REDACTED, result)
    return result


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Рекурсивно редактирует структуры: значения чувствительных ключей заменяются целиком."""
    if _depth > 20:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(k): (REDACTED if _SENSITIVE_KEYS.search(str(k)) else redact_value(v, _depth=_depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(v, _depth=_depth + 1) for v in value]
    return value
