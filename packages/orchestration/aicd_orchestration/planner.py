"""Детерминированный шаблонный планировщик Phase 3 (DATA_FLOW §2).

В Phase 3 план строится по шаблону режима run: это проверяемая заглушка уровня «L1 —
детерминированные инструменты» (ТЗ §5), которую Phase 4 заменяет планом от роли `planner`
(P4-09, AGENT_ROLES). Независимое ревью включается для задач L3+ (ROUTING_POLICY).
"""

from __future__ import annotations

from dataclasses import dataclass

from aicd_orchestration.dag import PlannedTask, validate_plan


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


_PRIMARY_ROLE = {
    "CREATE": "developer", "DEVELOP": "developer", "DEBUG": "maintenance", "MODERNIZE": "maintenance",
    "MAINTAIN": "maintenance", "OPTIMIZE": "performance", "TEST": "qa", "DOCUMENT": "documentation",
    "AUDIT": "reviewer", "REVIEW": "reviewer", "SECURITY": "security",
}
_WRITES_CODE = frozenset({"developer", "maintenance", "performance", "documentation", "qa"})


def template_plan(mode: str, objective: str, *, complexity: int = 3, risk_level: str = "medium") -> list[TaskSpec]:
    role = _PRIMARY_ROLE[mode]
    criteria = ("Изменение выполнено в worktree задачи", "Обязательные проверки качества пройдены")
    main = TaskSpec(
        key="main", role=role, title=f"{mode}: {objective[:200]}", instructions=objective,
        complexity=complexity, risk_level=risk_level, acceptance_criteria=criteria,
    )
    plan = [main]
    if role in _WRITES_CODE and complexity >= 3:
        plan.append(TaskSpec(
            key="review", role="reviewer", title="Независимое ревью изменения",
            instructions="Проверить изменение задачи main на корректность, безопасность и соответствие критериям.",
            complexity=2, risk_level=risk_level, depends_on=("main",),
            acceptance_criteria=("Найденные проблемы зафиксированы как findings",),
        ))
    validate_plan([PlannedTask(t.key, t.role, t.complexity, t.depends_on) for t in plan])
    return plan
