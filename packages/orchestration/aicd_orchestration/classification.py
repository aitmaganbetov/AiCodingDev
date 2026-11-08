"""Классификатор сложности L1–L5 и оценка риска (ROUTING_POLICY §2–§3, P4-06).

Чистые функции над детерминированными сигналами. Конфликт оценок разрешается максимумом (безопасная
сторона); понизить уровень ниже floor можно только утверждённым policy override, который фиксируется
в результате (для snapshot и `routing_decisions.score_breakdown.classification`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ContentClass = Literal["docs_only", "tests_only", "formatting_only", "code"]
MigrationKind = Literal["none", "additive", "destructive"]
RiskLevel = Literal["low", "medium", "high", "critical"]

SENSITIVE_CATEGORIES = frozenset(
    {"auth", "crypto", "permissions", "secrets_handling", "payments", "migrations", "ci_workflow", "iac", "public_api"}
)
_L4_CATEGORIES = frozenset({"auth", "crypto", "permissions", "secrets_handling", "payments", "ci_workflow", "iac"})
# Минимальный уровень режима (AGENT_ROLES §4, правило F6).
MODE_MINIMUM: dict[str, int] = {
    "CREATE": 3,
    "DEVELOP": 2,
    "DEBUG": 2,
    "AUDIT": 3,
    "MODERNIZE": 4,
    "SECURITY": 4,
    "OPTIMIZE": 3,
    "TEST": 2,
    "MAINTAIN": 2,
    "DOCUMENT": 1,
    "REVIEW": 3,
}


@dataclass(frozen=True)
class Signals:
    files_estimated: int = 0
    modules_touched: int = 0
    sensitive_categories: frozenset[str] = frozenset()
    migration_kind: MigrationKind = "none"
    public_api_change: bool = False
    architecture_change: bool = False
    content_class: ContentClass = "code"
    coverage_touched: float | None = None  # 0..1; None — неизвестно
    expected_diff_lines: int = 0
    major_dependency_update: bool = False
    protected_action_planned: bool = False  # merge / deploy / migration apply

    def __post_init__(self) -> None:
        unknown = self.sensitive_categories - SENSITIVE_CATEGORIES
        if unknown:
            raise ValueError(f"неизвестные категории: {sorted(unknown)}")
        if self.files_estimated < 0 or self.modules_touched < 0 or self.expected_diff_lines < 0:
            raise ValueError("счётчики сигналов не могут быть отрицательными")


@dataclass(frozen=True)
class Override:
    """Утверждённый admin policy override (`complexity_overrides`): разрешает уровень ниже floor."""

    max_level: int
    justification: str
    policy_version: int


@dataclass(frozen=True)
class Classification:
    level: int
    floor: int
    floor_rules: tuple[str, ...]
    planner_estimate: int | None
    mode_minimum: int
    user_minimum: int | None
    risk_points: int
    risk_level: RiskLevel
    override_applied: Override | None = None
    risk_factors: tuple[str, ...] = field(default_factory=tuple)

    def breakdown(self) -> dict[str, object]:
        data: dict[str, object] = {
            "level": f"L{self.level}",
            "floor": f"L{self.floor}",
            "floor_rules": list(self.floor_rules),
            "planner_estimate": f"L{self.planner_estimate}" if self.planner_estimate else None,
            "mode_minimum": f"L{self.mode_minimum}",
            "risk_points": self.risk_points,
            "risk_level": self.risk_level,
            "risk_factors": list(self.risk_factors),
        }
        if self.user_minimum:
            data["user_minimum"] = f"L{self.user_minimum}"
        if self.override_applied is not None:
            data["override"] = {
                "max_level": f"L{self.override_applied.max_level}",
                "justification": self.override_applied.justification,
                "policy_version": self.override_applied.policy_version,
            }
        return data


def _check_level(value: int | None, name: str) -> None:
    if value is not None and not 1 <= value <= 5:
        raise ValueError(f"{name} вне 1..5")


def floor_level(s: Signals, mode: str) -> tuple[int, tuple[str, ...]]:
    rules: list[tuple[str, int]] = []
    if s.content_class in ("docs_only", "formatting_only") and s.files_estimated <= 3:
        rules.append(("F1", 1))
    if s.content_class in ("code", "tests_only"):
        rules.append(("F2", 2))
    if s.files_estimated > 10 or s.modules_touched > 2 or s.public_api_change or s.migration_kind == "additive":
        rules.append(("F3", 3))
    if s.sensitive_categories & _L4_CATEGORIES or s.migration_kind == "destructive":
        rules.append(("F4", 4))
    if s.architecture_change:
        rules.append(("F5", 5))
    rules.append(("F6", MODE_MINIMUM[mode]))
    top = max(level for _, level in rules)
    return top, tuple(name for name, _ in rules)


def risk(s: Signals, mode: str, *, planner_risk: RiskLevel | None = None) -> tuple[int, RiskLevel, tuple[str, ...]]:
    points = 0
    factors: list[str] = []

    def add(name: str, value: int) -> None:
        nonlocal points
        points += value
        factors.append(f"{name}:+{value}")

    categories = s.sensitive_categories - {"public_api", "migrations"}
    if categories:
        add("sensitive_categories", min(3 * len(categories), 6))
    if s.migration_kind == "destructive":
        add("migration_destructive", 4)
    elif s.migration_kind == "additive":
        add("migration_additive", 1)
    if s.public_api_change:
        add("public_api_change", 2)
    if s.sensitive_categories & {"ci_workflow", "iac"}:
        add("ci_or_iac", 2)
    if s.content_class == "code" and (s.coverage_touched is None or s.coverage_touched < 0.5):
        add("low_or_unknown_coverage", 1)
    if s.expected_diff_lines > 500:
        add("large_diff", 1)
    if s.major_dependency_update:
        add("major_dependency_update", 2)
    if mode in ("SECURITY", "MODERNIZE"):
        add("mode", 1)
    if s.protected_action_planned:
        add("protected_action", 2)
    level: RiskLevel = "low" if points <= 1 else "medium" if points <= 3 else "high" if points <= 6 else "critical"
    order: tuple[RiskLevel, ...] = ("low", "medium", "high", "critical")
    if planner_risk is not None and order.index(planner_risk) > order.index(level):
        level = planner_risk  # planner может только повысить риск
    return points, level, tuple(factors)


def classify(
    signals: Signals,
    mode: str,
    *,
    planner_estimate: int | None = None,
    user_minimum: int | None = None,
    planner_risk: RiskLevel | None = None,
    override: Override | None = None,
) -> Classification:
    if mode not in MODE_MINIMUM:
        raise ValueError(f"неизвестный режим {mode}")
    _check_level(planner_estimate, "planner_estimate")
    _check_level(user_minimum, "user_minimum")
    floor, rules = floor_level(signals, mode)
    mode_min = MODE_MINIMUM[mode]
    level = max(floor, planner_estimate or 1, mode_min, user_minimum or 1)
    applied: Override | None = None
    if override is not None:
        _check_level(override.max_level, "override.max_level")
        if not override.justification.strip():
            raise ValueError("override без обоснования не применяется")
        # Override снимает детерминированный floor и оценку planner, но не минимум пользователя (тот может только
        # повышать) и не минимум режима.
        lowered = max(override.max_level, mode_min, user_minimum or 1)
        if lowered < level:
            level, applied = lowered, override
    points, risk_level, factors = risk(signals, mode, planner_risk=planner_risk)
    return Classification(
        level=level,
        floor=floor,
        floor_rules=rules,
        planner_estimate=planner_estimate,
        mode_minimum=mode_min,
        user_minimum=user_minimum,
        risk_points=points,
        risk_level=risk_level,
        override_applied=applied,
        risk_factors=factors,
    )
