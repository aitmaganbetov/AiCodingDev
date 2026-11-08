"""Политика повторов задач (DATA_FLOW §3.2, CLI_ADAPTER_CONTRACT §6.1, P3-07)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

BASE_SECONDS: Final = 30
CAP_SECONDS: Final = 15 * 60
RATE_LIMIT_BASE: Final = 300
RATE_LIMIT_CAP: Final = 3600
JITTER: Final = 0.2

# Ретраемые категории (failure_category). RATE_LIMITED не расходует max_attempts.
RETRYABLE: Final = frozenset(
    {
        "TIMEOUT",
        "RATE_LIMITED",
        "PROCESS_FAILED",
        "OUTPUT_PARSE_ERROR",
        "LEASE_LOST",
        "WORKER_CRASH",
        "GATE_FAILED",
        "REVIEW_REJECTED",
    }
)
BLOCKING: Final[dict[str, str]] = {"AUTH_REQUIRED": "AUTH_REQUIRED"}
NOT_COUNTED: Final = frozenset({"RATE_LIMITED", "LEASE_LOST"})


@dataclass(frozen=True)
class RetryDecision:
    action: Literal["retry", "block", "fail"]
    next_attempt_at: datetime | None = None
    blocked_reason: str | None = None
    counts_attempt: bool = True


def backoff_seconds(
    attempt: int, *, rate_limited: bool = False, retry_after: int | None = None, rng: random.Random | None = None
) -> float:
    """min(base·2^(n−1), cap) ± 20% jitter; для RATE_LIMITED — не меньше retry-after CLI."""
    rnd = rng or random.Random()  # noqa: S311 — jitter, не криптография
    base, cap = (RATE_LIMIT_BASE, RATE_LIMIT_CAP) if rate_limited else (BASE_SECONDS, CAP_SECONDS)
    raw = min(base * 2 ** max(attempt - 1, 0), cap)
    value = float(raw) * (1 + rnd.uniform(-JITTER, JITTER))
    if retry_after is not None:
        value = max(value, float(retry_after))
    return value


def decide(
    *,
    failure_category: str,
    attempt_count: int,
    max_attempts: int,
    retry_spent_seconds: int,
    retry_budget_seconds: int,
    now: datetime,
    retry_after: int | None = None,
    rng: random.Random | None = None,
) -> RetryDecision:
    """Решение после неуспешной попытки. Бюджет — и по числу попыток, и по суммарному времени."""
    if failure_category in BLOCKING:
        return RetryDecision("block", blocked_reason=BLOCKING[failure_category], counts_attempt=False)
    if failure_category not in RETRYABLE:
        return RetryDecision("fail")
    counts = failure_category not in NOT_COUNTED
    if counts and attempt_count >= max_attempts:
        return RetryDecision("fail")
    delay = backoff_seconds(
        attempt_count, rate_limited=failure_category == "RATE_LIMITED", retry_after=retry_after, rng=rng
    )
    if retry_spent_seconds + delay > retry_budget_seconds:
        return RetryDecision("fail")
    return RetryDecision("retry", next_attempt_at=now + timedelta(seconds=delay), counts_attempt=counts)
