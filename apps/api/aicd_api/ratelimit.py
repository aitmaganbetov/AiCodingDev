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

    async def hit(self, bucket: str, limit: int) -> RateDecision:
        now = int(time.time())
        window = now // WINDOW_SECONDS
        reset = WINDOW_SECONDS - (now % WINDOW_SECONDS)
        key = f"aicd:rl:{bucket}:{window}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, WINDOW_SECONDS + 5)
                count, _ = await pipe.execute()
        except (RedisError, OSError):
            log.warning("redis недоступен, используется локальный rate limit")
            count = self._local_hit(key, window)
        count = int(count)
        return RateDecision(allowed=count <= limit, limit=limit, remaining=max(0, limit - count), reset_seconds=reset)

    def _local_hit(self, key: str, window: int) -> int:
        stale = [k for k, (w, _) in self._local.items() if w < window]
        for k in stale:
            del self._local[k]
        _, count = self._local.get(key, (window, 0))
        self._local[key] = (window, count + 1)
        return count + 1
