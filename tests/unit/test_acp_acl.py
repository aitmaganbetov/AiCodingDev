"""P4-04: ACL ACP исчерпывающе — роль × тип (§4.2) и получатели по типу (§4.1)."""

from __future__ import annotations

import itertools

import pytest

from aicd_collaboration.acp import acl
from aicd_collaboration.acp.model import AGENT_ROLES, IMPL, MESSAGE_TYPES, PARTICIPANTS, VERIF
from tests.support.acp import valid_payload

# Таблица §4.2, переписанная дословно (✓ = "x"); порядок столбцов — MESSAGE_TYPES.
# TA HO QU AN RR FI FX EV DP DR BL TC
MATRIX_4_2 = """
orchestrator  x . . . . . . . . x x .
user          . . x x . . . . . . . .
requirements  . x x x . . . x x . x x
architect     . x x x x x x x x . x x
planner       . x x x . . . x x . x x
developer     . x x x x . . x x . x x
reviewer      . . x x . x x x x . x x
security      . . x x . x x x x . x x
qa            . x x x . x x x x . x x
database      . x x x x x x x x . x x
performance   . x x x x x . x x . x x
devops        . x x x x . . x x . x x
maintenance   . x x x x . . x x . x x
documentation . x x x x . . x x . x x
"""


def _expected() -> dict[tuple[str, str], bool]:
    table: dict[tuple[str, str], bool] = {}
    for line in MATRIX_4_2.strip().splitlines():
        role, *cells = line.split()
        assert len(cells) == 12
        for t, cell in zip(MESSAGE_TYPES, cells, strict=True):
            table[(role, t)] = cell == "x"
    return table


EXPECTED = _expected()


def test_matrix_covers_all_participants() -> None:
    assert {r for r, _ in EXPECTED} == set(PARTICIPANTS)
    assert set(acl.SEND_MATRIX) == set(PARTICIPANTS)


@pytest.mark.parametrize(("role", "message_type"), list(itertools.product(PARTICIPANTS, MESSAGE_TYPES)))
def test_send_matrix_role_by_type(role: str, message_type: str) -> None:
    denial = acl.sender_denial(role, message_type)
    assert (denial is None) == EXPECTED[(role, message_type)], (role, message_type, denial)


def test_type_matrix_4_1_consistent_with_4_2() -> None:
    for t in MESSAGE_TYPES:
        senders = {r for r in PARTICIPANTS if EXPECTED[(r, t)]}
        assert senders == set(acl.SENDERS_BY_TYPE[t]), t


# Получатели §4.1 для разрешённых отправителей (без условий, требующих фактов БД).
AGENTS = set(AGENT_ROLES)


def expected_recipients(sender: str, t: str, handoff_kind: str = "implementation_ready") -> set[str]:
    if t == "TASK_ASSIGN":
        return AGENTS
    if t == "HANDOFF":
        if sender == "qa":
            return set()  # только test_plan_ready
        routes = {
            "requirements": {"architect", "planner"},
            "architect": {"planner", "database", "developer"},
            "planner": {"orchestrator"},
        }
        return routes.get(sender, set(IMPL) | {"qa", "documentation"})
    if t == "QUESTION":
        return AGENTS if sender == "user" else AGENTS | {"user"}
    if t == "ANSWER":
        return AGENTS | {"user"}
    if t == "REVIEW_REQUEST":
        return (set(VERIF) | {"architect", "database"}) - {sender}
    if t == "FINDING":
        return set(IMPL) | {"architect", "orchestrator"}
    if t == "FIX_REQUEST":
        return set(IMPL)
    if t in ("EVIDENCE_SUBMITTED", "DECISION_PROPOSAL", "TASK_COMPLETED"):
        return {"orchestrator"}
    if t == "DECISION_RECORDED":
        return AGENTS | {"user"}
    if t == "BLOCKED":
        return {"user"} if sender == "orchestrator" else {"orchestrator"}
    raise AssertionError(t)


@pytest.mark.parametrize(
    ("sender", "message_type"),
    [(r, t) for r, t in itertools.product(PARTICIPANTS, MESSAGE_TYPES) if EXPECTED[(r, t)]],
)
def test_recipients_by_type(sender: str, message_type: str) -> None:
    payload = valid_payload(message_type)
    expected = expected_recipients(sender, message_type)
    for recipient in PARTICIPANTS:
        denial = acl.static_denial(sender, message_type, recipient, payload)
        assert (denial is None) == (recipient in expected), (sender, message_type, recipient, denial)


def test_qa_handoff_only_test_plan_ready() -> None:
    plan = valid_payload("HANDOFF", handoff_kind="test_plan_ready")
    assert acl.static_denial("qa", "HANDOFF", "developer", plan) is None
    assert acl.static_denial("qa", "HANDOFF", "orchestrator", plan) is None
    assert acl.static_denial("qa", "HANDOFF", "devops", plan) == "acl.handoff_route"
    assert acl.static_denial("qa", "HANDOFF", "developer", valid_payload("HANDOFF")) == "acl.handoff_qa_kind"


def test_plan_ready_only_planner_to_orchestrator() -> None:
    plan = valid_payload("HANDOFF", handoff_kind="plan_ready", plan_version=1)
    assert acl.static_denial("planner", "HANDOFF", "orchestrator", plan) is None
    assert acl.static_denial("architect", "HANDOFF", "planner", plan) == "acl.handoff_plan_ready"
    assert acl.static_denial("developer", "HANDOFF", "qa", plan) == "acl.handoff_plan_ready"


def test_self_review_denied() -> None:
    assert acl.static_denial("architect", "REVIEW_REQUEST", "architect", valid_payload("REVIEW_REQUEST")) == (
        "acl.self_review"
    )
    assert acl.static_denial("database", "REVIEW_REQUEST", "database", valid_payload("REVIEW_REQUEST")) == (
        "acl.self_review"
    )


def test_policy_can_only_narrow() -> None:
    narrow = acl.narrowing_from_snapshot({"acp_acl": {"send": {"developer": ["QUESTION", "TASK_ASSIGN"]}}})
    assert narrow is not None
    # Сужение работает…
    assert acl.sender_denial("developer", "HANDOFF", narrow) == "acl.policy_narrowed"
    assert acl.sender_denial("developer", "QUESTION", narrow) is None
    # …но не расширяет матрицу (TASK_ASSIGN для developer запрещён §4.2).
    assert acl.sender_denial("developer", "TASK_ASSIGN", narrow) == "acl.sender_type"
    # Роли вне сужения — по базовой матрице.
    assert acl.sender_denial("reviewer", "FINDING", narrow) is None


def test_malformed_narrowing_fails_closed_for_role() -> None:
    narrow = acl.narrowing_from_snapshot({"acp_acl": {"send": {"developer": "ALL"}}})
    assert narrow is not None
    assert acl.sender_denial("developer", "QUESTION", narrow) == "acl.policy_narrowed"
    assert acl.narrowing_from_snapshot({"acp_acl": "bad"}) is None
    assert acl.narrowing_from_snapshot(None) is None


def test_unknown_role_and_type_denied() -> None:
    assert acl.sender_denial("root", "QUESTION") == "acl.sender_type"
    assert acl.sender_denial("developer", "PING") == "acl.unknown_type"
