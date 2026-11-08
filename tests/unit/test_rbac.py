"""Матрица прав из API_DESIGN §5.1 (эталон закодирован здесь независимо от rbac.py)."""

import pytest

from aicd_identity.rbac import (
    PERMISSIONS,
    Role,
    can_decide_approval,
    can_submit_directive,
    can_transition_finding,
    effective_role,
    has_permission,
)

O, A, D, R, V = "owner", "admin", "developer", "reviewer", "viewer"
EXPECTED = {
    "workspace:create": {O, A, D, R, V},
    "member:read": {O, A, D, R, V},
    "member:manage": {O},
    "project:read": {O, A, D, R, V},
    "project:create": {O, A},
    "project:update": {O, A},
    "repository:attach": {O, A},
    "run:read": {O, A, D, R, V},
    "chat:read": {O, A, D, R, V},
    "evidence:read": {O, A, D, R, V},
    "quality:read": {O, A, D, R, V},
    "approval:read": {O, A, D, R, V},
    "provider:read": {O, A, D, R, V},
    "documentation:read": {O, A, D, R, V},
    "artifact:download": {O, A, D, R, V},
    "artifact:download_raw": {O, A},
    "run:create": {O, A, D},
    "run:control": {O, A, D},
    "chat:write": {O, A, D, R},
    "directive:create": {O, A, D, R},
    "directive:confirm": {O, A, D, R},
    "approval:decide": {O, A, D, R},
    "finding:triage": {O, A, D, R},
    "compliance:review": {O, A, D, R},
    "provider:probe": {O, A},
    "provider:settings": {O, A},
    "documentation:register": {O, A},
    "documentation:refresh": {O, A, D},
    "audit:read": {O, A},
}


def test_catalog_matches_api_design() -> None:
    assert set(PERMISSIONS) == set(EXPECTED)


@pytest.mark.parametrize("permission", sorted(EXPECTED))
@pytest.mark.parametrize("role", [O, A, D, R, V])
def test_matrix_cell(permission: str, role: str) -> None:
    assert has_permission(role, permission) is (role in EXPECTED[permission])


def test_unknown_permission_is_error() -> None:
    with pytest.raises(KeyError):
        has_permission("owner", "everything")


def test_project_override_only_downgrades() -> None:
    assert effective_role(Role.ADMIN, Role.VIEWER) is Role.VIEWER
    assert effective_role(Role.VIEWER, Role.DEVELOPER) is Role.VIEWER


@pytest.mark.parametrize(
    ("role", "action", "requester", "single", "expected"),
    [
        (R, "MANUAL_REVIEW", False, False, (True, None)),
        (R, "PLAN_APPROVAL", False, False, (False, "forbidden")),
        (D, "PLAN_APPROVAL", True, False, (True, None)),
        (A, "MERGE", True, False, (False, "four_eyes_violation")),
        (A, "MERGE", True, True, (True, None)),
        (A, "DATA_DELETION", False, False, (False, "forbidden")),
        (O, "LOCAL_EXECUTION_FALLBACK", False, False, (True, None)),
        (O, "PRODUCTION_DEPLOY", True, True, (False, "four_eyes_violation")),
        (A, "CRITICAL_DIRECTIVE", False, False, (True, None)),
    ],
)
def test_approval_rules(role: str, action: str, requester: bool, single: bool, expected: tuple) -> None:  # type: ignore[type-arg]
    assert (
        can_decide_approval(role, action, is_requester=requester, single_member_self_approval_allowed=single)
        == expected
    )


def test_directive_rules() -> None:
    assert can_submit_directive(R, "ASK") and not can_submit_directive(R, "CHANGE_REQUEST")
    assert can_submit_directive(D, "STOP") and not can_submit_directive(V, "ASK")


@pytest.mark.parametrize(
    ("role", "to_status", "expected"),
    [
        (R, "ACKNOWLEDGED", True),
        (R, "FALSE_POSITIVE", True),
        (R, "OPEN", True),
        (R, "ACCEPTED_RISK", False),
        (D, "ACCEPTED_RISK", False),
        (A, "ACCEPTED_RISK", True),
        (O, "ACCEPTED_RISK", True),
        (V, "ACKNOWLEDGED", False),
        (O, "VERIFIED_FIXED", False),  # подтверждение исправления — только Verification Engine
    ],
)
def test_finding_transition_rules(role: str, to_status: str, expected: bool) -> None:
    assert can_transition_finding(role, to_status) is expected
