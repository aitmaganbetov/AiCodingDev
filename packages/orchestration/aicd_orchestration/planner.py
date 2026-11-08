"""Детерминированный шаблонный планировщик (DATA_FLOW §2, P4-09).

План строится по data-driven шаблону конвейера режима (`pipelines`, AGENT_ROLES §4) с надстройками
уровня L1–L5 (ROUTING_POLICY §8). Для L1 это окончательный план; для L2+ — каркас, который уточняет
роль `planner` (её план проходит ту же валидацию `dag.validate_plan`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from aicd_orchestration.dag import PlannedTask, validate_plan
from aicd_orchestration.pipelines import PipelinePlan, build_pipeline


@dataclass(frozen=True)
class TaskSpec:
    key: str
    role: str
    title: str
    instructions: str
    complexity: int
    risk_level: str
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()


def template_plan(mode: str, objective: str, *, complexity: int = 3, risk_level: str = "medium") -> list[TaskSpec]:
    """Детерминированный план по шаблону режима (P4-09, `pipelines.build_pipeline`).

    Уровень задач = max(complexity, минимум режима); approvals уровня — в `pipeline_plan(...).approvals`.
    """
    return specs_from_pipeline(pipeline_plan(mode, objective, complexity=complexity, risk_level=risk_level))


def pipeline_plan(
    mode: str, objective: str, *, complexity: int = 3, risk_level: str = "medium", signals: Iterable[str] = ()
) -> PipelinePlan:
    return build_pipeline(mode, objective, complexity=complexity, risk_level=risk_level, signals=signals)


def specs_from_pipeline(plan: PipelinePlan) -> list[TaskSpec]:
    specs = [
        TaskSpec(
            key=t.key,
            role=t.role,
            title=t.title,
            instructions=t.instructions,
            complexity=plan.level,
            risk_level=plan.risk_level,
            depends_on=t.depends_on,
            acceptance_criteria=t.acceptance_criteria,
        )
        for t in plan.tasks
    ]
    validate_plan([PlannedTask(t.key, t.role, t.complexity, t.depends_on) for t in specs])
    return specs
