"""История качества: сглаженный verified completion rate (ROUTING_POLICY §7, P4-11).

VCR считается для пары (провайдер, роль) только в пределах одного workspace (межтенантная агрегация
запрещена). Попытки взвешиваются экспоненциальным затуханием (период полураспада 30 дней) в окне 90 дней;
результат сглаживается Beta-prior α = β = 2, поэтому без истории Q = 0,5 (нейтрально).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

ALPHA = 2.0
BETA = 2.0
HALF_LIFE_DAYS = 30.0
WINDOW_DAYS = 90.0

FAILURES = frozenset(
    {
        "GATE_FAILED",
        "REVIEW_REJECTED",
        "PROCESS_FAILED",
        "OUTPUT_PARSE_ERROR",
        "TIMEOUT",
        "SANDBOX_DENIED",
        "POLICY_DENIED",
    }
)
# Не характеризуют качество агента (окружение, лимиты, отмена) — не учитываются ни как успех, ни как неудача.
EXCLUDED = frozenset(
    {
        "AUTH_REQUIRED",
        "RATE_LIMITED",
        "CLI_NOT_INSTALLED",
        "UNSUPPORTED_VERSION",
        "CANCELLED",
        "LEASE_LOST",
        "WORKER_CRASH",
        "INTERNAL_ERROR",
    }
)


@dataclass(frozen=True)
class AttemptOutcome:
    finished_at: datetime
    succeeded: bool  # SUCCEEDED + обязательные гейты PASS (+ одобренное ревью для реализующих ролей)
    failure_category: str | None = None


@dataclass(frozen=True)
class Quality:
    q: float
    s_w: float
    n_w: float

    def inputs(self) -> dict[str, float]:
        return {"s": round(self.s_w, 4), "n": round(self.n_w, 4)}


def weight(age_days: float) -> float:
    if age_days < 0:
        age_days = 0.0
    if age_days > WINDOW_DAYS:
        return 0.0
    return float(0.5 ** (age_days / HALF_LIFE_DAYS))


def score(outcomes: Iterable[AttemptOutcome], *, now: datetime) -> Quality:
    s_w = n_w = 0.0
    for o in outcomes:
        if not o.succeeded and (o.failure_category is None or o.failure_category in EXCLUDED):
            continue  # нечем охарактеризовать агента
        if not o.succeeded and o.failure_category not in FAILURES:
            continue  # неизвестная категория — не штрафуем агента за неклассифицированное
        w = weight((now - o.finished_at).total_seconds() / 86400)
        if w == 0.0:
            continue
        n_w += w
        if o.succeeded:
            s_w += w
    return Quality(q=(s_w + ALPHA) / (n_w + ALPHA + BETA), s_w=s_w, n_w=n_w)


async def load(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, role: str, providers: Iterable[str], now: datetime
) -> dict[str, Quality]:
    """VCR по провайдерам для роли в текущем workspace (RLS + явный фильтр workspace).

    Успех v0: попытка SUCCEEDED (её задача прошла обязательные гейты — без этого engine не переводит попытку
    в SUCCEEDED, AC-11). Учёт отмены вердикта ревью/MANUAL_REVIEW добавится с review loops (P4-10).
    """
    rows = (
        await conn.execute(
            text(
                "SELECT a.provider, a.finished_at, a.status = 'SUCCEEDED' AS ok, a.failure_category"
                " FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
                " WHERE a.workspace_id = :ws AND t.agent_role = :role AND a.finished_at IS NOT NULL"
                " AND a.finished_at >= :since AND a.status IN ('SUCCEEDED','FAILED')"
            ),
            {"ws": workspace_id, "role": role, "since": now - timedelta(days=WINDOW_DAYS)},
        )
    ).all()
    by_provider: dict[str, list[AttemptOutcome]] = {p: [] for p in providers}
    for provider, finished_at, ok, category in rows:
        if provider in by_provider:
            by_provider[provider].append(AttemptOutcome(finished_at, bool(ok), category))
    return {p: score(items, now=now) for p, items in by_provider.items()}
