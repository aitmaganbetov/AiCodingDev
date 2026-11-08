"""P4-09: шаблоны конвейеров 11 режимов × уровни L1–L5 дают валидный DAG (AGENT_ROLES §4, ROUTING_POLICY §8)."""

from __future__ import annotations

import pytest

from aicd_orchestration.agents.profiles import PROFILES
from aicd_orchestration.classification import MODE_MINIMUM
from aicd_orchestration.dag import PlannedTask, PlanValidationError, validate_plan
from aicd_orchestration.pipelines import PIPELINES, SIGNALS, PipelineError, build_pipeline, effective_level
from aicd_orchestration.planner import TaskSpec, pipeline_plan, template_plan

MODES = ["CREATE", "DEVELOP", "DEBUG", "MODERNIZE", "MAINTAIN", "OPTIMIZE", "TEST", "DOCUMENT", "AUDIT", "REVIEW", "SECURITY"]
NO_CODE = {"AUDIT", "REVIEW"}
VERIFIERS = {"reviewer", "qa", "security"}


def _ancestors(tasks: list[TaskSpec], key: str) -> set[str]:
    by = {t.key: t for t in tasks}
    out: set[str] = set()
    stack = list(by[key].depends_on)
    while stack:
        k = stack.pop()
        if k not in out:
            out.add(k)
            stack.extend(by[k].depends_on)
    return out


def test_all_canonical_modes_have_pipelines() -> None:
    assert set(PIPELINES) == set(MODES) == set(MODE_MINIMUM)
    assert {m for m, p in PIPELINES.items() if not p.modifies_code} == NO_CODE


@pytest.mark.parametrize("complexity", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("mode", MODES)
def test_mode_level_generates_valid_dag(mode: str, complexity: int) -> None:
    plan = pipeline_plan(mode, "Цель пользователя", complexity=complexity)
    specs = template_plan(mode, "Цель пользователя", complexity=complexity)
    level = max(complexity, MODE_MINIMUM[mode])
    assert plan.level == level == effective_level(mode, complexity)
    validate_plan([PlannedTask(t.key, t.role, t.complexity, t.depends_on) for t in specs])  # вкл. правило L3+
    assert all(t.role in PROFILES for t in specs) and all(t.complexity == level for t in specs)
    assert [t.key for t in specs] == [t.key for t in plan.tasks]
    assert all(t.acceptance_criteria for t in specs) and all("Цель пользователя" in t.instructions for t in specs)
    roles = [t.role for t in specs]
    assert "planner" not in roles[:1], "planner фазы DISCOVERY не входит в DAG исполнения"

    # Изменения кода L3+ проверяются независимым ревьюером.
    if level >= 3 and plan.modifies_code:
        for t in plan.tasks:
            if t.writes_code:
                dependents = [d for d in specs if t.key in _ancestors(specs, d.key)]
                assert any(d.role in VERIFIERS for d in dependents), f"{mode} L{level}: {t.key} без проверки"
        assert "reviewer" in roles

    # Approvals по уровню (ROUTING_POLICY §8).
    actions = [a.action_type for a in plan.approvals]
    if level <= 3:
        assert not plan.approvals and not plan.requires_plan_approval
    else:
        assert plan.requires_plan_approval and "PLAN_APPROVAL" in actions
        assert any(t.complexity >= 4 for t in specs), "engine._plan_needs_approval видит L4+"
        assert {"security", "qa"} <= set(roles), "L4+: security и qa обязательны"
        if plan.modifies_code:
            assert "MERGE" in actions
    if level == 4 and plan.modifies_code:
        analyses = [t for t in specs if t.key.startswith("analysis_")]
        assert len(analyses) == 3 and analyses[-1].role == "planner"
        merge = analyses[-1].key
        assert all(merge in _ancestors(specs, t.key) for t in plan.tasks if t.writes_code)
    if level == 5:
        keys = {t.key for t in specs}
        assert {"arch_analysis_a", "arch_analysis_b", "arch_merge"} <= keys
        if plan.modifies_code:
            assert {"adr", "prototype", "prototype_review"} <= keys and "MANUAL_REVIEW" in actions
            impl = [t for t in plan.tasks if t.writes_code and t.key != "prototype"]
            assert all("prototype_review" in _ancestors(specs, t.key) for t in impl)
            assert next(t for t in specs if t.key == "prototype").role == "architect"


def test_develop_l3_keeps_stable_keys() -> None:
    """E2E Phase 3 опирается на DEVELOP L3: developer `main` → reviewer `review`."""
    specs = template_plan("DEVELOP", "Добавить эндпоинт")
    assert [(t.key, t.role, t.depends_on) for t in specs] == [("main", "developer", ()), ("review", "reviewer", ("main",))]
    assert specs[0].title == "DEVELOP: Добавить эндпоинт" and "Добавить эндпоинт" in specs[0].instructions


@pytest.mark.parametrize(
    ("mode", "signals", "risk", "expected"),
    [
        ("DEVELOP", {"criteria_missing", "needs_database", "public_api_change"}, "high",
         ["requirements", "database", "main", "review", "security", "docs"]),
        ("DEVELOP", set(), "low", ["main", "review"]),
        ("CREATE", {"needs_database"}, "medium",
         ["requirements", "architecture", "database", "main", "test_plan", "review", "security", "pipeline", "docs"]),
        ("TEST", {"needs_test_hooks"}, "medium", ["main", "hooks", "review"]),
        ("MAINTAIN", {"ci_change"}, "medium", ["impact", "main", "tests", "sca_triage", "review", "pipeline"]),
        ("DOCUMENT", {"adr_needed"}, "medium", ["main", "adr", "review"]),
        ("REVIEW", {"tests_review"}, "medium", ["main", "security", "tests", "report"]),
    ],
)  # fmt: skip
def test_conditional_steps(mode: str, signals: set[str], risk: str, expected: list[str]) -> None:
    plan = build_pipeline(mode, "x", complexity=3, risk_level=risk, signals=signals)
    assert [t.key for t in plan.tasks] == expected


def test_skipped_dependencies_are_closed_transitively() -> None:
    plan = build_pipeline("CREATE", "x", complexity=3)
    by = {t.key: t for t in plan.tasks}
    assert by["main"].depends_on == ("architecture",), "database исключён — зависимость перенесена на architecture"
    assert "database:signal:needs_database" in plan.skipped


def test_merge_approval_required_for_high_risk() -> None:
    low = build_pipeline("DEVELOP", "x", complexity=4, risk_level="low")
    high = build_pipeline("DEVELOP", "x", complexity=4, risk_level="high")
    assert next(a for a in low.approvals if a.action_type == "MERGE").required is False
    assert next(a for a in high.approvals if a.action_type == "MERGE").required is True


def test_invalid_inputs() -> None:
    with pytest.raises(PipelineError):
        build_pipeline("HACK", "x")
    with pytest.raises(PipelineError):
        build_pipeline("DEVELOP", "x", complexity=0)
    with pytest.raises(PipelineError):
        build_pipeline("DEVELOP", "x", risk_level="extreme")
    with pytest.raises(PipelineError):
        build_pipeline("DEVELOP", "x", signals={"unknown"})
    assert "needs_database" in SIGNALS


def test_long_objective_title_bounded() -> None:
    specs = template_plan("DEVELOP", "я" * 5000)
    assert all(1 <= len(t.title) <= 500 for t in specs)


def test_validation_still_rejects_unverified_developer() -> None:
    with pytest.raises(PlanValidationError):
        validate_plan([PlannedTask("main", "developer", 3)])
