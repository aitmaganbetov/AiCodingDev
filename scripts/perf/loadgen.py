"""Минимальный генератор нагрузки (P9-04, NFR-PERF-01): asyncio + httpx, без новых зависимостей.

Модель — закрытый цикл (closed loop): `concurrency` виртуальных клиентов, каждый шлёт следующий запрос сразу
после ответа на предыдущий, в течение `duration` секунд (после `warmup` секунд прогрева, не входящих в
статистику). Латентность — от отправки запроса до полного чтения тела ответа (`time.perf_counter`).
Перцентили — по всем измеренным значениям (nearest-rank), без выборки.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank перцентиль (p ∈ (0, 100]) по отсортированной копии."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class Stats:
    name: str
    latencies: list[float] = field(default_factory=list)  # секунды, только успешные
    statuses: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    elapsed: float = 0.0

    def record(self, seconds: float, status: str, ok: bool) -> None:
        self.statuses[status] += 1
        if ok:
            self.latencies.append(seconds)
        else:
            self.errors[status] += 1

    def summary(self) -> dict[str, Any]:
        total = sum(self.statuses.values())
        ms = [x * 1000 for x in self.latencies]
        return {
            "name": self.name,
            "requests": total,
            "ok": len(self.latencies),
            "errors": sum(self.errors.values()),
            "error_rate": (sum(self.errors.values()) / total) if total else 0.0,
            "duration_s": round(self.elapsed, 2),
            "throughput_rps": round(len(self.latencies) / self.elapsed, 1) if self.elapsed else 0.0,
            "p50_ms": round(percentile(ms, 50), 1),
            "p95_ms": round(percentile(ms, 95), 1),
            "p99_ms": round(percentile(ms, 99), 1),
            "max_ms": round(max(ms), 1) if ms else math.nan,
            "statuses": dict(self.statuses),
        }


Request = Callable[[httpx.AsyncClient, int], Awaitable[httpx.Response]]


async def closed_loop(
    name: str,
    clients: list[httpx.AsyncClient],
    request: Request,
    *,
    duration: float,
    warmup: float = 2.0,
    ok: Callable[[httpx.Response], bool] = lambda r: r.status_code < 400,
) -> Stats:
    """`len(clients)` виртуальных клиентов (у каждого свой httpx-клиент/сессия); `request(client, i)` — запрос."""
    stats = Stats(name)
    measure_from = time.perf_counter() + warmup
    stop_at = measure_from + duration

    async def user(client: httpx.AsyncClient) -> None:
        i = 0
        while (now := time.perf_counter()) < stop_at:
            started = now
            try:
                response = await request(client, i)
                await response.aread()
                status, good = str(response.status_code), ok(response)
            except httpx.HTTPError as exc:
                status, good = type(exc).__name__, False
            finished = time.perf_counter()
            if started >= measure_from:
                stats.record(finished - started, status, good)
            i += 1

    await asyncio.gather(*(user(c) for c in clients))
    stats.elapsed = duration
    return stats
