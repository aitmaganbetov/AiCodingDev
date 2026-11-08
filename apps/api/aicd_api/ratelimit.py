"""Rate limiting (API_DESIGN §10): окно фиксированной длины в Redis, локальный fallback.

При недоступности Redis используется счётчик в памяти процесса (fail-safe, не fail-open).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("aicd.ratelimit")

WINDOW_SECONDS = 60


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._local: dict[str, tuple[int, int]] = {}

    async def hit(self, bucket: str, limit: int, *, window_seconds: int = WINDOW_SECONDS) -> RateDecision:
        """Фиксированное окно `window_seconds` (по умолчанию минута; probe провайдера — час, API_DESIGN §10)."""
        now = int(time.time())
        window = now // window_seconds
        reset = window_seconds - (now % window_seconds)
        suffix = f"{window}" if window_seconds == WINDOW_SECONDS else f"{window_seconds}:{window}"
        key = f"aicd:rl:{bucket}:{suffix}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds + 5)
                count, _ = await pipe.execute()
        except (RedisError, OSError):
            log.warning("redis недоступен, используется локальный rate limit")
            count = self._local_hit(key, window, window_seconds)
        count = int(count)
        return RateDecision(allowed=count <= limit, limit=limit, remaining=max(0, limit - count), reset_seconds=reset)

    def _local_hit(self, key: str, window: int, window_seconds: int = WINDOW_SECONDS) -> int:
        """Локальный счётчик: запись живёт до конца своего окна (окна разной длины не вытесняют друг друга)."""
        now = time.time()
        stale = [k for k, (expires, _) in self._local.items() if expires <= now]
        for k in stale:
            del self._local[k]
        expires_at = (window + 1) * window_seconds
        _, count = self._local.get(key, (expires_at, 0))
        self._local[key] = (expires_at, count + 1)
        return count + 1
