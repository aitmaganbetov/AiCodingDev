"""P4-06 (классификатор/риск), P4-07 (Router), P4-08 (независимость ревью), P4-11 (VCR) — табличные unit-тесты."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aicd_orchestration import classification as cls
from aicd_orchestration import quality, router

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
CAPS = {
    "noninteractive_mode": True,
    "machine_readable_output": "jsonl_stream",
    "resume_session": True,
    "token_usage_exposed": True,
    "tool_events_exposed": True,
}


# ------------------------------------------------------------------ классификация (ROUTING_POLICY §2)
@pytest.mark.parametrize(
    ("signals", "mode", "level", "rules"),
    [
        (cls.Signals(content_class="docs_only", files_estimated=2), "DOCUMENT", 1, {"F1", "F6"}),
        (cls.Signals(content_class="docs_only", files_estimated=5), "DOCUMENT", 1, {"F6"}),
        (cls.Signals(content_class="code", files_estimated=2), "DEVELOP", 2, {"F2", "F6"}),
        (cls.Signals(content_class="tests_only", files_estimated=1), "TEST", 2, {"F2", "F6"}),
        (cls.Signals(files_estimated=11), "DEVELOP", 3, {"F2", "F3", "F6"}),
        (cls.Signals(modules_touched=3), "DEVELOP", 3, {"F2", "F3", "F6"}),
        (cls.Signals(public_api_change=True), "DEVELOP", 3, {"F2", "F3", "F6"}),
        (cls.Signals(migration_kind="additive"), "DEVELOP", 3, {"F2", "F3", "F6"}),
        (cls.Signals(sensitive_categories=frozenset({"auth"})), "DEVELOP", 4, {"F2", "F4", "F6"}),
        (cls.Signals(sensitive_categories=frozenset({"public_api"})), "DEVELOP", 2, {"F2", "F6"}),
        (cls.Signals(migration_kind="destructive"), "DEVELOP", 4, {"F2", "F4", "F6"}),
        (cls.Signals(architecture_change=True), "DEVELOP", 5, {"F2", "F5", "F6"}),
        (cls.Signals(content_class="docs_only"), "SECURITY", 4, {"F1", "F6"}),
        (cls.Signals(content_class="formatting_only"), "MODERNIZE", 4, {"F1", "F6"}),
    ],
)
def test_floor_rules(signals: cls.Signals, mode: str, level: int, rules: set[str]) -> None:
    c = cls.classify(signals, mode)
    assert c.level == level and set(c.floor_rules) == rules


def test_conflicts_resolve_to_maximum_and_user_can_only_raise() -> None:
    assert cls.classify(cls.Signals(), "DEVELOP", planner_estimate=4).level == 4
    assert cls.classify(cls.Signals(files_estimated=20), "DEVELOP", planner_estimate=1).level == 3
    assert cls.classify(cls.Signals(), "DEVELOP", user_minimum=5).level == 5
    assert cls.classify(cls.Signals(files_estimated=20), "DEVELOP", user_minimum=1).level == 3


def test_override_lowers_floor_but_never_below_mode_or_user_minimum() -> None:
    o = cls.Override(max_level=2, justification="генерированный код", policy_version=3)
    c = cls.classify(cls.Signals(files_estimated=50), "DEVELOP", override=o)
    assert c.level == 2 and c.override_applied == o and c.breakdown()["override"]["policy_version"] == 3  # type: ignore[index]
    assert cls.classify(cls.Signals(files_estimated=50), "SECURITY", override=o).level == 4
    assert cls.classify(cls.Signals(files_estimated=50), "DEVELOP", override=o, user_minimum=3).level == 3
    with pytest.raises(ValueError, match="обоснования"):
        cls.classify(cls.Signals(), "DEVELOP", override=cls.Override(1, " ", 1))


def test_invalid_inputs_rejected() -> None:
    with pytest.raises(ValueError, match="режим"):
        cls.classify(cls.Signals(), "HACK")
    with pytest.raises(ValueError, match="категории"):
        cls.Signals(sensitive_categories=frozenset({"weather"}))
    with pytest.raises(ValueError, match="planner_estimate"):
        cls.classify(cls.Signals(), "DEVELOP", planner_estimate=6)


@pytest.mark.parametrize(
    ("signals", "mode", "points", "level"),
    [
        (cls.Signals(content_class="docs_only"), "DOCUMENT", 0, "low"),
        (cls.Signals(coverage_touched=0.9), "DEVELOP", 0, "low"),
        (cls.Signals(), "DEVELOP", 1, "low"),  # покрытие кода неизвестно
        (cls.Signals(coverage_touched=0.9, public_api_change=True), "DEVELOP", 2, "medium"),
        (cls.Signals(coverage_touched=0.9, sensitive_categories=frozenset({"auth"})), "DEVELOP", 3, "medium"),
        (
            cls.Signals(coverage_touched=0.9, sensitive_categories=frozenset({"auth", "crypto", "payments"})),
            "DEVELOP",
            6,
            "high",
        ),
        (cls.Signals(coverage_touched=0.9, migration_kind="destructive"), "DEVELOP", 4, "high"),
        (cls.Signals(coverage_touched=0.9, sensitive_categories=frozenset({"ci_workflow"})), "DEVELOP", 5, "high"),
        (
            cls.Signals(
                coverage_touched=0.9,
                migration_kind="destructive",
                protected_action_planned=True,
                expected_diff_lines=900,
            ),
            "MODERNIZE",
            8,
            "critical",
        ),
    ],
)
def test_risk_scoring(signals: cls.Signals, mode: str, points: int, level: str) -> None:
    got_points, got_level, _ = cls.risk(signals, mode)
    assert (got_points, got_level) == (points, level)


def test_planner_may_only_raise_risk() -> None:
    assert cls.risk(cls.Signals(coverage_touched=0.9), "DEVELOP", planner_risk="high")[1] == "high"
    assert (
        cls.risk(cls.Signals(migration_kind="destructive", coverage_touched=0.9), "DEVELOP", planner_risk="low")[1]
        == "high"
    )


@given(
    files=st.integers(0, 100),
    modules=st.integers(0, 10),
    cats=st.frozensets(st.sampled_from(sorted(cls.SENSITIVE_CATEGORIES))),
    mode=st.sampled_from(sorted(cls.MODE_MINIMUM)),
    planner=st.none() | st.integers(1, 5),
)
def test_level_is_never_below_any_input(
    files: int, modules: int, cats: frozenset[str], mode: str, planner: int | None
) -> None:
    c = cls.classify(
        cls.Signals(files_estimated=files, modules_touched=modules, sensitive_categories=cats),
        mode,
        planner_estimate=planner,
    )
    assert c.level >= max(c.floor, c.mode_minimum, planner or 1) and 1 <= c.level <= 5


# ----------------------------------------------------------------------------- VCR (§7)
def test_quality_neutral_without_history_and_bayesian_smoothing() -> None:
    assert quality.score([], now=NOW).q == 0.5
    q = quality.score([quality.AttemptOutcome(NOW, True)] * 4, now=NOW)
    assert q.q == pytest.approx((4 + 2) / (4 + 4))


def test_quality_decay_window_and_exclusions() -> None:
    month_ago = quality.score([quality.AttemptOutcome(NOW - timedelta(days=30), True)], now=NOW)
    assert month_ago.s_w == pytest.approx(0.5) and month_ago.n_w == pytest.approx(0.5)
    assert quality.score([quality.AttemptOutcome(NOW - timedelta(days=91), False, "GATE_FAILED")], now=NOW).n_w == 0
    for category in quality.EXCLUDED:
        assert quality.score([quality.AttemptOutcome(NOW, False, category)], now=NOW).q == 0.5, category
    failed = quality.score([quality.AttemptOutcome(NOW, False, "REVIEW_REJECTED")] * 2, now=NOW)
    assert failed.q == pytest.approx(2 / 6)


# --------------------------------------------------------------------------- Router (§4–§5)
def cand(provider: str, **kw: Any) -> router.Candidate:
    base: dict[str, Any] = {
        "installation_id": uuid.uuid5(uuid.NAMESPACE_DNS, provider + str(kw.get("tag", ""))),
        "provider": provider,
        "enabled": True,
        "installed": True,
        "compatibility": "supported",
        "auth_status": "AUTHENTICATED",
        "availability": "AVAILABLE",
        "active": 0,
        "max_concurrent": 2,
        "capabilities": CAPS,
    }
    kw.pop("tag", None)
    base.update(kw)
    return router.Candidate(**base)


def req(**kw: Any) -> router.RouteRequest:
    base: dict[str, Any] = {
        "role": "developer",
        "level": 2,
        "now": NOW,
        "allowed_providers": frozenset({"codex", "claude"}),
    }
    base.update(kw)
    return router.RouteRequest(**base)


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"enabled": False}, "disabled"),
        ({"installed": False}, "not_installed"),
        ({"compatibility": "unsupported"}, "unsupported_version"),
        ({"compatibility": "untested"}, "untested_version"),
        ({"auth_status": "AUTH_REQUIRED"}, "auth_required"),
        ({"auth_status": "UNKNOWN"}, "auth_required"),
        ({"availability": "RATE_LIMITED"}, "rate_limited"),
        ({"cooldown_until": NOW + timedelta(minutes=5)}, "rate_limited"),
        ({"availability": "UNAVAILABLE"}, "unavailable"),
        ({"capabilities": {**CAPS, "noninteractive_mode": False}}, "capability_missing:noninteractive_mode"),
        ({"capabilities": {**CAPS, "machine_readable_output": "none"}}, "capability_missing:machine_readable_output"),
    ],
)
def test_hard_filters_record_exclusion_reason(override: dict[str, Any], reason: str) -> None:
    d = router.route(req(), [cand("codex", **override)])
    assert d.outcome == "BLOCKED" and d.selected is None
    assert d.breakdown["excluded"][0]["reason"] == reason


def test_unknown_auth_allowed_after_recent_success_and_untested_by_policy() -> None:
    assert (
        router.route(req(), [cand("codex", auth_status="UNKNOWN", recent_success_within_hour=True)]).outcome
        == "SELECTED"
    )
    assert router.route(req(allow_untested=True), [cand("codex", compatibility="untested")]).outcome == "SELECTED"


def test_policy_and_resume_session_filters() -> None:
    assert (
        router.route(req(allowed_providers=frozenset({"claude"})), [cand("codex")]).breakdown["excluded"][0]["reason"]
        == "policy_disallowed"
    )
    d = router.route(req(requires_resume_session=True), [cand("codex", capabilities={**CAPS, "resume_session": False})])
    assert d.breakdown["excluded"][0]["reason"] == "capability_missing:resume_session"


def test_both_unavailable_blocks_with_right_reason_ac16() -> None:
    both_auth = [cand("codex", auth_status="AUTH_REQUIRED"), cand("claude", auth_status="AUTH_REQUIRED")]
    assert router.route(req(), both_auth).blocked_reason == "AUTH_REQUIRED"
    mixed = [cand("codex", auth_status="AUTH_REQUIRED"), cand("claude", availability="RATE_LIMITED")]
    assert router.route(req(), mixed).blocked_reason == "PROVIDER_UNAVAILABLE"
    assert router.route(req(), []).blocked_reason == "PROVIDER_UNAVAILABLE"
    disabled_and_auth = [cand("codex", enabled=False), cand("claude", auth_status="AUTH_REQUIRED")]
    assert router.route(req(), disabled_and_auth).blocked_reason == "AUTH_REQUIRED"
    assert router.route(req(), [cand("codex", enabled=False)]).blocked_reason == "PROVIDER_UNAVAILABLE"


def test_concurrency_only_means_wait_not_blocked() -> None:
    d = router.route(req(), [cand("codex", active=2), cand("claude", active=2)])
    assert d.outcome == "WAIT" and d.blocked_reason is None
    assert {e["reason"] for e in d.breakdown["excluded"]} == {"concurrency_full"}


def test_quality_drives_selection_and_breakdown_is_complete() -> None:
    good = quality.score([quality.AttemptOutcome(NOW, True)] * 10, now=NOW)
    bad = quality.score([quality.AttemptOutcome(NOW, False, "GATE_FAILED")] * 10, now=NOW)
    d = router.route(req(quality={"codex": bad, "claude": good}), [cand("codex"), cand("claude")])
    assert d.selected is not None and d.selected.provider == "claude" and d.outcome == "SELECTED"
    b = d.breakdown
    assert set(b) >= {
        "weights",
        "candidates",
        "excluded",
        "selected_installation_id",
        "quality_inputs",
        "review_independence",
    }
    assert {c["provider"] for c in b["candidates"]} == {"codex", "claude"}
    assert set(b["candidates"][0]["components"]) == {"Q", "C", "A", "L", "D", "H", "R"}


def test_recent_failures_and_degraded_lower_score() -> None:
    d = router.route(req(), [cand("codex", recent_failure_ages_h=(0.5, 1.0, 2.0)), cand("claude")])
    assert d.selected is not None and d.selected.provider == "claude"
    d = router.route(req(), [cand("codex", availability="DEGRADED"), cand("claude")])
    assert d.selected is not None and d.selected.provider == "claude"


def test_stickiness_bonus_and_its_removal() -> None:
    worse = quality.score([quality.AttemptOutcome(NOW, False, "GATE_FAILED")] * 2, now=NOW)
    q = {"codex": worse, "claude": quality.score([], now=NOW)}
    sticky = router.route(req(quality=q, previous_provider="codex"), [cand("codex"), cand("claude")])
    assert sticky.selected is not None and sticky.selected.provider == "codex"
    dropped = router.route(
        req(quality=q, previous_provider="codex", previous_failed_twice_by_provider=True),
        [cand("codex"), cand("claude")],
    )
    assert dropped.selected is not None and dropped.selected.provider == "claude"


def test_tie_break_order() -> None:
    d = router.route(
        req(),
        [cand("codex", active=1, max_concurrent=4), cand("claude", active=0, max_concurrent=4, capabilities={**CAPS})],
    )
    assert d.breakdown["tie_break"] in (None, "lower_load")
    tie = router.route(req(run_assignments={"codex": 0, "claude": 3}), [cand("claude"), cand("codex")])
    assert (
        tie.breakdown["tie_break"] == "fewer_run_assignments"
        and tie.selected is not None
        and tie.selected.provider == "codex"
    )
    lex = router.route(req(), [cand("codex"), cand("claude")])
    assert (
        lex.breakdown["tie_break"] == "lexicographic" and lex.selected is not None and lex.selected.provider == "claude"
    )


def test_weights_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="весов"):
        req(weights={**router.DEFAULT_WEIGHTS, "w_q": 0.9})


# ---------------------------------------------------------------- независимость ревью (§5, P4-08)
def test_reviewer_gets_non_author_provider() -> None:
    d = router.route(req(role="reviewer", level=3, authors=frozenset({"codex"})), [cand("codex"), cand("claude")])
    assert d.selected is not None and d.selected.provider == "claude" and d.review_independence == "independent"
    assert any(e["reason"] == "independence_conflict" for e in d.breakdown["excluded"])


def test_below_required_level_independence_not_required() -> None:
    d = router.route(req(role="reviewer", level=2, authors=frozenset({"codex"})), [cand("codex")])
    assert d.outcome == "SELECTED" and d.review_independence == "not_required"


@pytest.mark.parametrize(
    ("profile", "fallback", "outcome", "marking", "blocked", "approval"),
    [
        ("basic", None, "FALLBACK", "self_review", None, "SELF_REVIEW_FALLBACK"),
        ("strict", None, "BLOCKED", "human_review", "MANUAL_REVIEW", "MANUAL_REVIEW"),
        ("critical", None, "BLOCKED", "not_required", "PROVIDER_UNAVAILABLE", None),
        ("critical", "SELF_REVIEW_FALLBACK", "BLOCKED", "not_required", "PROVIDER_UNAVAILABLE", None),
        ("strict", "SELF_REVIEW_FALLBACK", "FALLBACK", "self_review", None, "SELF_REVIEW_FALLBACK"),
    ],
)
def test_independence_fallbacks(
    profile: Any, fallback: Any, outcome: str, marking: str, blocked: str | None, approval: str | None
) -> None:
    r = req(
        role="reviewer",
        level=3,
        authors=frozenset({"codex", "claude"}),
        profile=profile,
        independence_fallback=fallback,
    )
    d = router.route(r, [cand("codex"), cand("claude")])
    assert (d.outcome, d.review_independence, d.blocked_reason, d.needs_approval) == (
        outcome,
        marking,
        blocked,
        approval,
    )


@given(
    authors=st.frozensets(st.sampled_from(["codex", "claude"])),
    profile=st.sampled_from(["basic", "strict", "critical"]),
    level=st.integers(1, 5),
    role=st.sampled_from(["reviewer", "security", "developer", "qa"]),
    available=st.frozensets(st.sampled_from(["codex", "claude"])),
)
def test_self_review_is_never_marked_independent(
    authors: frozenset[str], profile: Any, level: int, role: str, available: frozenset[str]
) -> None:
    d = router.route(
        req(role=role, level=level, authors=authors, profile=profile), [cand(p) for p in sorted(available)]
    )
    if d.selected is not None and d.review_independence == "independent":
        assert d.selected.provider not in authors
    if (
        d.selected is not None
        and d.selected.provider in authors
        and router.independence_required(req(role=role, level=level, authors=authors, profile=profile))
    ):
        assert d.review_independence == "self_review" and profile != "critical"


def test_diversity_for_qa_l4() -> None:
    d = router.route(req(role="qa", level=4, implementer_provider="codex"), [cand("codex"), cand("claude")])
    assert d.selected is not None and d.selected.provider == "claude"
    comps = {c["provider"]: c["components"]["D"] for c in d.breakdown["candidates"]}
    assert comps == {"codex": 0.0, "claude": 1.0}
