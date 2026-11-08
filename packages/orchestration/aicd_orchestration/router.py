"""Router провайдеров CLI (ROUTING_POLICY §4–§6, P4-07/P4-08).

Чистая функция `route(request, candidates)`: жёсткие фильтры H1–H8 с причинами исключения, правило
независимого ревью (§5) с политиками fallback, взвешенная оценка, stickiness и tie-break. Результат несёт
`score_breakdown` для `routing_decisions` (§4.3). Обход лимитов невозможен по построению: кандидаты —
только установки оператора, отфильтрованные по auth/availability/cooldown; при их отсутствии — BLOCKED.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from aicd_orchestration.quality import Quality

Outcome = Literal["SELECTED", "FALLBACK", "BLOCKED", "WAIT"]
Fallback = Literal["BLOCK", "MANUAL_REVIEW", "SELF_REVIEW_FALLBACK"]
Independence = Literal["independent", "self_review", "human_review", "not_required"]

DEFAULT_WEIGHTS: dict[str, float] = {
    "w_q": 0.40,
    "w_c": 0.10,
    "w_a": 0.15,
    "w_l": 0.10,
    "w_d": 0.10,
    "w_h": 0.05,
    "w_r": 0.10,
}
STICKINESS_BONUS = 0.15
TIE_EPSILON = 0.01
# Роли, для которых действует правило независимости (ROUTING_POLICY §5, H7).
REVIEW_ROLES = frozenset({"reviewer", "security"})
REVIEW_TASK_ROLES = frozenset({"qa", "architect", "database"})  # только для review-задач этих ролей
# Исключения по политике workspace/run (не по состоянию установки) — для текста причины BLOCKED.
WORKSPACE_POLICY_REASONS = frozenset(
    {"policy_disallowed", "workspace_disabled", "role_not_allowed", "workspace_sessions_zero"}
)
DEFAULT_FALLBACK: dict[str, Fallback] = {
    "basic": "SELF_REVIEW_FALLBACK",
    "strict": "MANUAL_REVIEW",
    "critical": "BLOCK",
}


@dataclass(frozen=True)
class Candidate:
    installation_id: uuid.UUID
    provider: str
    enabled: bool
    installed: bool
    compatibility: Literal["supported", "untested", "unsupported"]
    auth_status: str  # AUTHENTICATED / UNKNOWN / AUTH_REQUIRED / ...
    availability: str  # AVAILABLE / DEGRADED / UNAVAILABLE / RATE_LIMITED
    active: int
    max_concurrent: int
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    cooldown_until: datetime | None = None
    recent_success_within_hour: bool = False  # для auth_status UNKNOWN (H3)
    recent_failure_ages_h: tuple[float, ...] = ()  # PROCESS_FAILED/TIMEOUT/OUTPUT_PARSE_ERROR за 24 ч
    # Безопасные настройки провайдера в workspace (PATCH /providers/{id}/settings, ROUTING_POLICY §4.1a):
    # выключен в workspace, разрешённые роли (None — все), сессии провайдера в workspace сейчас и лимит (None — нет).
    workspace_enabled: bool = True
    allowed_roles: frozenset[str] | None = None
    workspace_active: int = 0
    workspace_max_sessions: int | None = None


@dataclass(frozen=True)
class RouteRequest:
    role: str
    level: int
    now: datetime
    allowed_providers: frozenset[str]
    profile: Literal["basic", "strict", "critical"] = "strict"
    is_review_task: bool = False  # review-задача qa/architect/database
    authors: frozenset[str] = frozenset()  # провайдеры попыток, создавших проверяемые коммиты
    independence_required_from_level: int = 3
    independence_fallback: Fallback | None = None  # None — значение по умолчанию профиля
    allow_untested: bool = False
    requires_resume_session: bool = False
    previous_provider: str | None = None  # stickiness для повторов/FIX-итераций
    previous_failed_twice_by_provider: bool = False  # PROCESS_FAILED/TIMEOUT подряд ≥ 2 → без бонуса
    implementer_provider: str | None = None  # для diversity (qa/security L4+)
    run_assignments: Mapping[str, int] = field(default_factory=dict)
    hints: Mapping[str, float] = field(default_factory=dict)
    quality: Mapping[str, Quality] = field(default_factory=dict)
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def __post_init__(self) -> None:
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("сумма весов маршрутизации должна быть 1.0")


@dataclass(frozen=True)
class RouteDecision:
    outcome: Outcome
    selected: Candidate | None
    blocked_reason: str | None
    review_independence: Independence
    fallback: Fallback | None
    needs_approval: str | None  # SELF_REVIEW_FALLBACK / MANUAL_REVIEW — approval, который нужно запросить
    reason: str
    breakdown: dict[str, Any]


def independence_required(req: RouteRequest) -> bool:
    review = req.role in REVIEW_ROLES or (req.is_review_task and req.role in REVIEW_TASK_ROLES)
    return review and req.level >= req.independence_required_from_level


def _hard_filter(c: Candidate, req: RouteRequest) -> str | None:
    """Причина исключения по H1–H6 и настройкам провайдера в workspace (H7 — независимость, H8 — concurrency —
    отдельно)."""
    if not c.enabled:
        return "disabled"
    if not c.installed:
        return "not_installed"
    if c.compatibility == "unsupported":
        return "unsupported_version"
    if c.compatibility == "untested" and not req.allow_untested:
        return "untested_version"
    if c.auth_status != "AUTHENTICATED" and not (c.auth_status == "UNKNOWN" and c.recent_success_within_hour):
        return "auth_required"
    if c.availability == "RATE_LIMITED" or (c.cooldown_until is not None and req.now < c.cooldown_until):
        return "rate_limited"
    if c.availability not in ("AVAILABLE", "DEGRADED"):
        return "unavailable"
    caps = c.capabilities
    if not caps.get("noninteractive_mode"):
        return "capability_missing:noninteractive_mode"
    if caps.get("machine_readable_output", "none") == "none":
        return "capability_missing:machine_readable_output"
    if req.requires_resume_session and not caps.get("resume_session"):
        return "capability_missing:resume_session"
    if c.provider not in req.allowed_providers:
        return "policy_disallowed"
    if not c.workspace_enabled:
        return "workspace_disabled"
    if c.allowed_roles is not None and req.role not in c.allowed_roles:
        return "role_not_allowed"
    if c.workspace_max_sessions == 0:
        return "workspace_sessions_zero"
    return None


def _slot_reason(c: Candidate) -> str | None:
    """Причина отсутствия свободного слота (H8): лимит установки или лимит сессий провайдера в workspace —
    действует более строгий; None — слот есть."""
    if c.active >= c.max_concurrent:
        return "concurrency_full"
    if c.workspace_max_sessions is not None and c.workspace_active >= c.workspace_max_sessions:
        return "workspace_concurrency_full"
    return None


def _components(c: Candidate, req: RouteRequest) -> dict[str, float]:
    q = req.quality.get(c.provider)
    caps = c.capabilities
    desirable = (
        caps.get("machine_readable_output") == "jsonl_stream",
        bool(caps.get("tool_events_exposed")),
        bool(caps.get("token_usage_exposed")),
    )
    diversity = 0.5
    if req.implementer_provider and req.role in ("qa", "security") and req.level >= 4:
        diversity = 0.0 if c.provider == req.implementer_provider else 1.0
    recent = min(1.0, sum(math.exp(-age / 6) for age in c.recent_failure_ages_h if 0 <= age <= 24) / 3)
    return {
        "Q": round(q.q if q is not None else 0.5, 4),
        "C": round(sum(desirable) / len(desirable), 4),
        "A": 1.0 if c.availability == "AVAILABLE" else 0.5,
        "L": round(1 - c.active / c.max_concurrent, 4) if c.max_concurrent > 0 else 0.0,
        "D": diversity,
        "H": float(req.hints.get(c.provider, 0.5)),
        "R": round(recent, 4),
    }


def _score(components: Mapping[str, float], w: Mapping[str, float]) -> float:
    return (
        w["w_q"] * components["Q"]
        + w["w_c"] * components["C"]
        + w["w_a"] * components["A"]
        + w["w_l"] * components["L"]
        + w["w_d"] * components["D"]
        + w["w_h"] * components["H"]
        + w["w_r"] * (1 - components["R"])
    )


def _rank(pool: list[Candidate], req: RouteRequest) -> tuple[Candidate, list[dict[str, Any]], str | None]:
    scored: list[tuple[float, Candidate, dict[str, Any]]] = []
    for c in pool:
        comps = _components(c, req)
        sticky = (
            STICKINESS_BONUS
            if req.previous_provider == c.provider and not req.previous_failed_twice_by_provider
            else 0.0
        )
        total = round(_score(comps, req.weights) + sticky, 4)
        scored.append(
            (
                total,
                c,
                {
                    "installation_id": str(c.installation_id),
                    "provider": c.provider,
                    "components": comps,
                    "stickiness": sticky,
                    "score": total,
                },
            )
        )
    scored.sort(key=lambda t: -t[0])
    best_score = scored[0][0]
    tied = [t for t in scored if best_score - t[0] < TIE_EPSILON]
    tie_break: str | None = None
    if len(tied) > 1:

        def load(t: tuple[float, Candidate, dict[str, Any]]) -> float:
            c = t[1]
            return c.active / c.max_concurrent if c.max_concurrent else 1.0

        def key(t: tuple[float, Candidate, dict[str, Any]]) -> tuple[float, int, str, str]:
            c = t[1]
            return (load(t), req.run_assignments.get(c.provider, 0), c.provider, str(c.installation_id))

        tied.sort(key=key)
        first, second = tied[0], tied[1]
        if load(first) != load(second):
            tie_break = "lower_load"
        elif req.run_assignments.get(first[1].provider, 0) != req.run_assignments.get(second[1].provider, 0):
            tie_break = "fewer_run_assignments"
        else:
            tie_break = "lexicographic"
        winner = first[1]
    else:
        winner = scored[0][1]
    return winner, [t[2] for t in scored], tie_break


def route(req: RouteRequest, candidates: list[Candidate]) -> RouteDecision:
    excluded: list[dict[str, str]] = []
    passed: list[Candidate] = []
    for c in candidates:
        reason = _hard_filter(c, req)
        if reason is None:
            passed.append(c)
        else:
            excluded.append({"installation_id": str(c.installation_id), "provider": c.provider, "reason": reason})
    breakdown: dict[str, Any] = {
        "weights": dict(req.weights),
        "excluded": excluded,
        "quality_inputs": {p: q.inputs() for p, q in req.quality.items()},
    }
    need_independence = independence_required(req)
    independent = [c for c in passed if c.provider not in req.authors] if need_independence else passed
    if need_independence:
        for c in passed:
            if c.provider in req.authors:
                excluded.append(
                    {
                        "installation_id": str(c.installation_id),
                        "provider": c.provider,
                        "reason": "independence_conflict",
                    }
                )

    def decide(pool: list[Candidate], independence: Independence, fallback: Fallback | None) -> RouteDecision:
        free = [c for c in pool if _slot_reason(c) is None]
        if not free:
            for c in pool:
                excluded.append(
                    {"installation_id": str(c.installation_id), "provider": c.provider, "reason": _slot_reason(c) or ""}
                )
            breakdown["review_independence"] = independence
            return RouteDecision(
                "WAIT",
                None,
                None,
                independence,
                fallback,
                None,
                "все подходящие установки заняты — задача ждёт слот",
                breakdown,
            )
        winner, ranked, tie = _rank(free, req)
        breakdown.update(
            {
                "candidates": ranked,
                "tie_break": tie,
                "selected_installation_id": str(winner.installation_id),
                "review_independence": independence,
            }
        )
        outcome: Outcome = "SELECTED" if fallback is None else "FALLBACK"
        approval = "SELF_REVIEW_FALLBACK" if fallback == "SELF_REVIEW_FALLBACK" else None
        text = f"{winner.provider}: score {next(r['score'] for r in ranked if r['provider'] == winner.provider)}"
        if fallback:
            text += f"; независимый провайдер недоступен — {fallback} (маркировка {independence})"
        return RouteDecision(outcome, winner, None, independence, fallback, approval, text[:500], breakdown)

    if independent:
        return decide(independent, "independent" if need_independence else "not_required", None)

    if need_independence and passed:
        # Кандидаты есть, но все — авторы проверяемого изменения: политика fallback независимости (§5).
        fallback = req.independence_fallback or DEFAULT_FALLBACK[req.profile]
        if fallback == "SELF_REVIEW_FALLBACK" and req.profile == "critical":
            fallback = "BLOCK"  # самопроверка запрещена в профиле critical
        breakdown["independence_fallback"] = fallback
        if fallback == "SELF_REVIEW_FALLBACK":
            # Самопроверка никогда не маркируется independent (ТЗ §4).
            return decide(passed, "self_review", fallback)
        if fallback == "MANUAL_REVIEW":
            breakdown["review_independence"] = "human_review"
            return RouteDecision(
                "BLOCKED",
                None,
                "MANUAL_REVIEW",
                "human_review",
                fallback,
                "MANUAL_REVIEW",
                "независимый провайдер недоступен — требуется ручное ревью",
                breakdown,
            )
        breakdown["review_independence"] = "not_required"
        return RouteDecision(
            "BLOCKED",
            None,
            "PROVIDER_UNAVAILABLE",
            "not_required",
            fallback,
            None,
            "независимый провайдер недоступен — ожидание (BLOCK)",
            breakdown,
        )

    # Выключенные оператором установки не участвуют в выборе причины: AUTH_REQUIRED — если все включённые
    # кандидаты отсеяны только по авторизации (ROUTING_POLICY §4.1).
    reasons = {e["reason"] for e in excluded} - {"disabled"}
    blocked = "AUTH_REQUIRED" if reasons == {"auth_required"} else "PROVIDER_UNAVAILABLE"
    breakdown["review_independence"] = "not_required"
    text = f"нет допустимых установок CLI ({blocked}); обход лимитов не выполняется"
    if reasons and reasons <= WORKSPACE_POLICY_REASONS:
        text = f"провайдеры недоступны по настройкам workspace ({', '.join(sorted(reasons))}); {text}"
    return RouteDecision("BLOCKED", None, blocked, "not_required", None, None, text[:500], breakdown)
