"""UUIDv7 (RFC 9562) — первичные ключи генерируются приложением (ADR-0008)."""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_seq = 0


def uuid7() -> uuid.UUID:
    """Возвращает UUIDv7, монотонный в пределах процесса.

    48 бит — Unix-время в мс, 12 бит rand_a используются как счётчик внутри одной
    миллисекунды (метод 1 RFC 9562 §6.2), 62 бита rand_b — случайные.
    """
    global _last_ms, _seq
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _seq = int.from_bytes(os.urandom(2), "big") & 0x07FF  # оставляем запас для инкремента
        else:
            _seq += 1
            if _seq > 0x0FFF:  # переполнение счётчика: «занимаем» следующую миллисекунду
                _last_ms += 1
                _seq = 0
            ms = _last_ms
        rand_a = _seq
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)
