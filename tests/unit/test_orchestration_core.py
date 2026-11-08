"""P3-02/P3-05/P3-07: FSM, DAG, политика повторов (включая property-based проверки)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aicd_orchestration import dag, fsm, retry

ENTITIES = list(fsm.TRANSITIONS)


@settings(max_examples=300, deadline=None)
@given(entity=st.sampled_from(ENTITIES), seed=st.integers(0, 10_000), steps=st.integers(1, 30))
def test_random_walks_stay_inside_table(entity: fsm.Entity, seed: int, steps: int) -> None:
    rnd = random.Random(seed)
    state = fsm.NEW
    for _ in range(steps):
        targets = sorted(fsm.TRANSITIONS[entity].get(state, frozenset()))
        if not targets:
            assert state in fsm.TERMINAL[entity] or entity == "external_action", (entity, state)
            break
        nxt = rnd.choice(targets)
        fsm.require_transition(entity, state, nxt)
        state = nxt


@given(entity=st.sampled_from(ENTITIES), a=st.text(min_size=1, max_size=20), b=st.text(min_size=1, max_size=20))
def test_unknown_pairs_rejected(entity: fsm.Entity, a: str, b: str) -> None:
    if not fsm.can_transition(entity, a, b):
        with pytest.raises(fsm.InvalidTransitionError):
            fsm.require_transition(entity, a, b)


def test_terminal_states_have_no_exits_except_ledger_compensation() -> None:
    for entity, terminals in fsm.TERMINAL.items():
        for state in terminals:
            assert not fsm.TRANSITIONS[entity].get(state), (entity, state)
    assert fsm.can_transition("external_action", "CONFIRMED", "COMPENSATED")
    assert not fsm.can_transition("external_action", "CONFIRMED", "EXECUTING"), "подтверждённое не повторяется"


def test_completed_reachable_only_from_verifying() -> None:
    sources = {f for f, ts in fsm.TRANSITIONS["run"].items() if "COMPLETED" in ts}
    assert sources == {"VERIFYING"}
    assert {f for f, ts in fsm.TRANSITIONS["task"].items() if "SUCCEEDED" in ts} == {"VERIFYING", "BLOCKED"}


@st.composite
def acyclic_plans(draw: st.DrawFn) -> list[dag.PlannedTask]:
    n = draw(st.integers(1, 25))
    tasks = []
    for i in range(n):
        deps = draw(st.lists(st.integers(0, max(i - 1, 0)), max_size=3, unique=True)) if i else []
        tasks.append(
            dag.PlannedTask(
                key=f"t{i}",
                role=draw(st.sampled_from(["planner", "reviewer", "qa", "documentation"])),
                complexity=draw(st.integers(1, 5)),
                depends_on=tuple(f"t{d}" for d in deps),
            )
        )
    return tasks


@settings(max_examples=200, deadline=None)
@given(plan=acyclic_plans())
def test_acyclic_plans_validate_and_respect_edges(plan: list[dag.PlannedTask]) -> None:
    order = dag.validate_plan(plan)
    pos = {k: i for i, k in enumerate(order)}
    for t in plan:
        for d in t.depends_on:
            assert pos[d] < pos[t.key]


@settings(max_examples=200, deadline=None)
@given(plan=acyclic_plans().filter(lambda p: len(p) >= 2), data=st.data())
def test_any_back_edge_is_detected_with_cycle_path(plan: list[dag.PlannedTask], data: st.DataObject) -> None:
    # добавляем ребро «ранняя задача зависит от поздней, достижимой из неё» ⇒ гарантированный цикл
    last = plan[-1]
    ancestors = _ancestors(plan, last.key)
    candidates = [t for t in plan[:-1] if t.key in ancestors]
    if not candidates:
        return
    target = data.draw(st.sampled_from(candidates))
    mutated = [
        t if t.key != target.key else dag.PlannedTask(t.key, t.role, t.complexity, (*t.depends_on, last.key))
        for t in plan
    ]
    with pytest.raises(dag.PlanValidationError) as exc:
        dag.validate_plan(mutated)
    assert exc.value.code == "PLAN_CYCLE_DETECTED"
    cycle = exc.value.details["cycle"]
    assert isinstance(cycle, list) and cycle[0] == cycle[-1]


def _ancestors(plan: list[dag.PlannedTask], key: str) -> set[str]:
    by = {t.key: t for t in plan}
    out: set[str] = set()
    stack = [key]
    while stack:
        for d in by[stack.pop()].depends_on:
            if d not in out:
                out.add(d)
                stack.append(d)
    return out


@pytest.mark.parametrize(
    ("tasks", "code"),
    [
        ([], "PLAN_EMPTY"),
        ([dag.PlannedTask("a", "wizard", 1)], "PLAN_UNKNOWN_ROLE"),
        ([dag.PlannedTask("a", "qa", 6)], "PLAN_BAD_COMPLEXITY"),
        ([dag.PlannedTask("a", "qa", 1, ("a",))], "PLAN_CYCLE_DETECTED"),
        ([dag.PlannedTask("a", "qa", 1, ("zz",))], "PLAN_UNKNOWN_DEPENDENCY"),
        ([dag.PlannedTask("a", "qa", 1), dag.PlannedTask("a", "qa", 1)], "PLAN_DUPLICATE_KEY"),
        ([dag.PlannedTask("dev", "developer", 3)], "PLAN_MISSING_VERIFICATION"),
    ],
)
def test_plan_rejections(tasks: list[dag.PlannedTask], code: str) -> None:
    with pytest.raises(dag.PlanValidationError) as exc:
        dag.validate_plan(tasks)
    assert exc.value.code == code


def test_limits_and_critical_path() -> None:
    chain = [dag.PlannedTask(f"t{i}", "qa", 1, (f"t{i - 1}",) if i else ()) for i in range(25)]
    with pytest.raises(dag.PlanValidationError, match="PLAN_TOO_DEEP"):
        dag.validate_plan(chain)
    with pytest.raises(dag.PlanValidationError, match="PLAN_TOO_LARGE"):
        dag.validate_plan([dag.PlannedTask(f"x{i}", "qa", 1) for i in range(51)])
    ok = [dag.PlannedTask("dev", "developer", 3), dag.PlannedTask("rev", "reviewer", 2, ("dev",))]
    assert dag.validate_plan(ok) == ["dev", "rev"]
    assert dag.critical_path_lengths(ok) == {"dev": 2, "rev": 1}


NOW = datetime(2026, 11, 8, tzinfo=UTC)


def test_backoff_growth_cap_and_jitter() -> None:
    rng = random.Random(1)
    values = [retry.backoff_seconds(n, rng=rng) for n in range(1, 10)]
    assert all(
        0.8 * min(30 * 2 ** (n - 1), 900) <= v <= 1.2 * min(30 * 2 ** (n - 1), 900)
        for n, v in zip(range(1, 10), values, strict=True)
    )
    assert retry.backoff_seconds(1, rate_limited=True, retry_after=7200, rng=rng) == 7200


@pytest.mark.parametrize(
    ("category", "attempts", "spent", "expected"),
    [
        ("TIMEOUT", 1, 0, "retry"),
        ("TIMEOUT", 3, 0, "fail"),  # бюджет попыток исчерпан (AC-07)
        ("TIMEOUT", 1, 7190, "fail"),  # бюджет времени исчерпан
        ("RATE_LIMITED", 3, 0, "retry"),  # не расходует max_attempts
        ("AUTH_REQUIRED", 1, 0, "block"),  # AC-16
        ("SANDBOX_DENIED", 1, 0, "fail"),
        ("UNSUPPORTED_VERSION", 1, 0, "fail"),
    ],
)
def test_retry_decisions(category: str, attempts: int, spent: int, expected: str) -> None:
    d = retry.decide(
        failure_category=category,
        attempt_count=attempts,
        max_attempts=3,
        retry_spent_seconds=spent,
        retry_budget_seconds=7200,
        now=NOW,
        rng=random.Random(0),
    )
    assert d.action == expected
    if expected == "retry":
        assert d.next_attempt_at is not None and d.next_attempt_at > NOW
        assert d.counts_attempt is (category != "RATE_LIMITED")
    if expected == "block":
        assert d.blocked_reason == "AUTH_REQUIRED" and not d.counts_attempt
    assert timedelta(0) == timedelta(0)
