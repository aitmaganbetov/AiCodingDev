"""Эвристический сканер prompt-injection (AGENT_COMMUNICATION_PROTOCOL §8 п.5).

Сообщение не блокируется (ложные срабатывания), а помечается `suspected_injection`: флаг попадает в
`agent_activity_events`, SSE `agent.message` и чат. Payload — всегда недоверенные данные: брокер его
не исполняет и не интерпретирует как инструкции (§8 п.1–2).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

BASE64_BLOCK_BYTES = 4096

_MARKERS = re.compile(
    r"(?i)("
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+(instructions|rules|prompts?)"
    r"|disregard\s+(all\s+)?(the\s+)?(previous|prior|above|system)"
    r"|forget\s+(all\s+)?(your|the|previous)\s+(instructions|rules)"
    r"|you\s+are\s+now\s+(a|an|the)\b"
    r"|new\s+system\s+prompt"
    r"|игнорируй\s+(все\s+)?(предыдущие|прошлые)\s+(инструкции|указания)"
    r"|забудь\s+(все\s+)?(предыдущие\s+)?инструкции"
    r")"
)
# Попытки закрыть fenced-блок UNTRUSTED DATA или выдать себя за системный блок (AGENT_ROLES §3).
_FENCE = re.compile(r"(?im)(^\s*(```|~~~)|END[ _-]?UNTRUSTED|UNTRUSTED[ _-]?DATA|<\s*/?\s*(system|policy)\s*>)")
_BASE64 = re.compile(r"[A-Za-z0-9+/=\s]{" + str(BASE64_BLOCK_BYTES) + ",}")


def _strings(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > 20:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, depth + 1)


def _has_base64_block(text: str) -> bool:
    for match in _BASE64.finditer(text):
        compact = re.sub(r"\s+", "", match.group(0))
        if len(compact) >= BASE64_BLOCK_BYTES:
            return True
    return False


def scan(payload: Any) -> list[str]:
    """Сработавшие эвристики (`marker`, `fence`, `base64_block`); пустой список — подозрений нет."""
    hits: set[str] = set()
    for text in _strings(payload):
        if _MARKERS.search(text):
            hits.add("marker")
        if _FENCE.search(text):
            hits.add("fence")
        if len(text) >= BASE64_BLOCK_BYTES and _has_base64_block(text):
            hits.add("base64_block")
    return sorted(hits)
