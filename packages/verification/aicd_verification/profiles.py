"""Профили качества basic/strict/critical и план gates задачи/run (QUALITY_GATES §2.2, §3.2; P6-02).

Матрица `PROFILE_MATRIX` — машиночитаемая форма таблицы §2.2: уровень gate_type в профиле
* `R`  — обязателен, если применим (детектор стека);
* `R*` — обязателен при условии (условие — та же применимость: конфигурация typechecker, наличие
  миграций, набора тестов …), иначе `NOT_APPLICABLE`;
* `O`  — выполняется информативно (advisory): не блокирует, но результат и находки сохраняются;
* `-`  — в профиле не выполняется: `NOT_APPLICABLE` с причиной «profile …».

`plan_gates` объединяет: строки `quality_gates` профиля run (snapshot `required`), матрицу профиля и
применимость (детектор по commit + `project_technologies`). Эффективная обязательность =
`row.required OR матрица(R/R*)` — переопределения workspace могут только ужесточать (§2.2); SCOPE
обязателен всегда. Пороги профиля: блокирующая severity находок, coverage core, ratchet.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from aicd_verification.gates import Applicability

Level = Literal["R", "R*", "O", "-"]
ProfileCode = Literal["basic", "strict", "critical"]
PROFILES: Final[tuple[ProfileCode, ...]] = ("basic", "strict", "critical")

GATE_TYPES: Final[tuple[str, ...]] = (
    "FORMAT",
    "LINT",
    "TYPECHECK",
    "BUILD",
    "UNIT",
    "INTEGRATION",
    "E2E",
    "SAST",
    "SCA",
    "SECRETS",
    "IAC",
    "POLICY",
    "MIGRATION",
    "PERF",
    "REVIEW",
    "COVERAGE",
    "SBOM",
    "SIGN",
    "SCOPE",
)

# gate_type → (basic, strict, critical); QUALITY_GATES §2.2.
_MATRIX_ROWS: Final[Mapping[str, tuple[Level, Level, Level]]] = {
    "FORMAT": ("R", "R", "R"),
    "LINT": ("R", "R", "R"),
    "TYPECHECK": ("R*", "R", "R"),
    "BUILD": ("R", "R", "R"),
    "UNIT": ("R", "R", "R"),
    "INTEGRATION": ("R*", "R", "R"),
    "E2E": ("R*", "R", "R"),
    "SAST": ("R", "R", "R"),
    "SCA": ("R", "R", "R"),
    "SECRETS": ("R", "R", "R"),
    "IAC": ("O", "R*", "R"),
    "POLICY": ("-", "O", "R*"),
    "MIGRATION": ("R*", "R*", "R*"),
    "PERF": ("R*", "R*", "R*"),
    "REVIEW": ("R", "R", "R"),
    "COVERAGE": ("O", "R", "R"),
    "SBOM": ("-", "O", "R"),
    "SIGN": ("-", "-", "R*"),
    "SCOPE": ("R", "R", "R"),
}
PROFILE_MATRIX: Final[Mapping[str, Mapping[str, Level]]] = {
    code: {gt: levels[i] for gt, levels in _MATRIX_ROWS.items()} for i, code in enumerate(PROFILES)
}


@dataclass(frozen=True)
class ProfileThresholds:
    code: ProfileCode
    finding_block_severity: str  # находки ≥ этого уровня блокируют завершение
    coverage_min_percent: float | None  # None — coverage только отчёт (basic)
    coverage_max_drop_pp: float | None  # ratchet: допустимое снижение, п.п. (None — без ratchet)
    accept_risk_max_severity: str  # максимальная severity, которую человек может принять как риск
    tool_missing_effect: Literal["MANUAL_REVIEW", "FAIL"]  # §3.2 (статус gate всё равно ERROR, не PASS)


THRESHOLDS: Final[Mapping[str, ProfileThresholds]] = {
    "basic": ProfileThresholds("basic", "critical", None, None, "critical", "MANUAL_REVIEW"),
    "strict": ProfileThresholds("strict", "high", 85.0, 1.0, "critical", "MANUAL_REVIEW"),
    "critical": ProfileThresholds("critical", "medium", 85.0, 0.0, "high", "FAIL"),
}
STRICT_FAIL_ON_MISSING: Final = frozenset(
    {"BUILD", "UNIT", "INTEGRATION", "E2E", "SAST", "SCA", "SECRETS", "MIGRATION"}
)


def level(profile: str, gate_type: str) -> Level:
    return PROFILE_MATRIX.get(profile, PROFILE_MATRIX["critical"]).get(gate_type, "R")


def thresholds(profile: str, definition: Mapping[str, Any] | None = None) -> ProfileThresholds:
    """Пороги профиля; `quality_profiles.definition` может только ужесточить (finding/coverage)."""
    base = THRESHOLDS.get(profile, THRESHOLDS["critical"])
    if not definition:
        return base
    from aicd_verification.findings import SEVERITY_RANK  # локально: findings не зависит от профилей

    block = base.finding_block_severity
    raw_block = definition.get("finding_block_severity")
    if isinstance(raw_block, str) and raw_block in SEVERITY_RANK and SEVERITY_RANK[raw_block] < SEVERITY_RANK[block]:
        block = raw_block
    cov = base.coverage_min_percent
    raw_cov = definition.get("coverage_min_percent")
    if isinstance(raw_cov, int | float) and not isinstance(raw_cov, bool) and 0 <= raw_cov <= 100:
        cov = max(float(raw_cov), cov or 0.0)
    return ProfileThresholds(
        base.code,
        block,
        cov,
        base.coverage_max_drop_pp,
        base.accept_risk_max_severity,
        base.tool_missing_effect,
    )


def tool_missing_effect(profile: str, gate_type: str) -> Literal["MANUAL_REVIEW", "FAIL"]:
    """§3.2: эффект отсутствия инструмента для отчёта/approval (статус gate — ERROR, никогда PASS)."""
    if profile == "critical":
        return "FAIL"
    if profile == "strict" and gate_type in STRICT_FAIL_ON_MISSING:
        return "FAIL"
    return "MANUAL_REVIEW"


# --- План gates --------------------------------------------------------------------------------------

Disposition = Literal["run", "not_applicable"]


@dataclass(frozen=True)
class GateRow:
    """Строка `quality_gates` профиля run (snapshot)."""

    gate_id: Any
    name: str
    gate_type: str
    required: bool
    timeout_seconds: int = 600
    config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedGate:
    row: GateRow
    level: Level
    required: bool  # эффективная обязательность (блокирует ли результат завершение)
    disposition: Disposition
    reason: str  # почему выполняется / почему неприменим (not_applicable_reason)
    markers: tuple[str, ...] = ()

    @property
    def advisory(self) -> bool:
        return self.disposition == "run" and not self.required


@dataclass(frozen=True)
class GatePlan:
    profile: str
    gates: tuple[PlannedGate, ...]

    def by_name(self) -> dict[str, PlannedGate]:
        return {g.row.name: g for g in self.gates}

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(g.row.name for g in self.gates if g.required and g.disposition == "run")

    @property
    def advisory_names(self) -> tuple[str, ...]:
        return tuple(g.row.name for g in self.gates if g.advisory)

    def as_report(self) -> dict[str, Any]:
        """applicability_report (artifact): что выполняется, что неприменимо и почему."""
        return {
            "profile": self.profile,
            "gates": [
                {
                    "name": g.row.name,
                    "gate_type": g.row.gate_type,
                    "level": g.level,
                    "required": g.required,
                    "disposition": g.disposition,
                    "reason": g.reason,
                    "markers": list(g.markers),
                }
                for g in self.gates
            ],
        }


def plan_gates(
    profile: str,
    rows: Iterable[GateRow],
    applicability: Mapping[str, Applicability],
    *,
    known_gates: Iterable[str] | None = None,
) -> GatePlan:
    """План для задачи/run. Неизвестный runner-у gate не скрывается: он остаётся в плане с
    disposition=run (движок сам выставит ERROR UNSUPPORTED_GATE для обязательного)."""
    known = set(known_gates) if known_gates is not None else None
    planned: list[PlannedGate] = []
    for row in rows:
        lvl: Level = "R" if row.gate_type == "SCOPE" else level(profile, row.gate_type)
        matrix_required = lvl in ("R", "R*")
        required = row.gate_type == "SCOPE" or row.required or matrix_required
        if lvl == "-" and not row.required:
            planned.append(
                PlannedGate(
                    row,
                    lvl,
                    False,
                    "not_applicable",
                    f"profile {profile}: gate type {row.gate_type} is not part of the profile",
                    (f"profile:{profile}",),
                )
            )
            continue
        app = applicability.get(row.name)
        if known is not None and row.name not in known:
            planned.append(PlannedGate(row, lvl, required, "run", "no runner registered for this gate"))
            continue
        if app is None:
            # Детектор ничего не сказал о gate — fail-closed: выполняем (без runner'а будет ERROR/SKIPPED).
            planned.append(PlannedGate(row, lvl, required, "run", "applicability unknown"))
            continue
        if not app.applicable:
            planned.append(PlannedGate(row, lvl, required, "not_applicable", app.reason, app.markers))
            continue
        planned.append(PlannedGate(row, lvl, required, "run", app.reason, app.markers))
    return GatePlan(profile, tuple(planned))
