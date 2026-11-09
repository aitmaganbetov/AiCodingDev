"""RBAC как код (FR-IAM-03, API_DESIGN §5). Единственный источник матрицы прав.

Тест P1-10 сверяет каталог с `x-required-permission` каждой операции OpenAPI.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


RANK: Final[dict[Role, int]] = {
    Role.OWNER: 5,
    Role.ADMIN: 4,
    Role.DEVELOPER: 3,
    Role.REVIEWER: 2,
    Role.VIEWER: 1,
}

# Псевдо-права, не требующие членства в workspace.
PUBLIC: Final = "public"
AUTHENTICATED: Final = "authenticated"

_ALL = frozenset(Role)
_ADMIN_UP = frozenset({Role.OWNER, Role.ADMIN})
_DEV_UP = frozenset({Role.OWNER, Role.ADMIN, Role.DEVELOPER})
_REVIEWER_UP = frozenset({Role.OWNER, Role.ADMIN, Role.DEVELOPER, Role.REVIEWER})

# API_DESIGN §5.1. Права с условиями (directive:create по типу, directive:confirm «своё»,
# approval:decide по action_type) здесь задают минимальный допуск; уточнение — в доменных сервисах.
PERMISSIONS: Final[dict[str, frozenset[Role]]] = {
    "workspace:create": _ALL,
    "member:read": _ALL,
    "member:manage": frozenset({Role.OWNER}),
    # OQ-45 (f): название workspace — owner/admin; политики workspace (four-eyes исключение) — только owner
    # (SECURITY_ARCHITECTURE §11, x-permission-depends-on: settings у updateWorkspace).
    "workspace:update": _ADMIN_UP,
    "workspace:policy": frozenset({Role.OWNER}),
    "project:read": _ALL,
    "project:create": _ADMIN_UP,
    "project:update": _ADMIN_UP,
    "repository:attach": _ADMIN_UP,
    "run:read": _ALL,
    "chat:read": _ALL,
    "evidence:read": _ALL,
    "quality:read": _ALL,
    "approval:read": _ALL,
    "provider:read": _ALL,
    "documentation:read": _ALL,
    "artifact:download": _ALL,
    "artifact:download_raw": _ADMIN_UP,
    "run:create": _DEV_UP,
    "run:control": _DEV_UP,
    "chat:write": _REVIEWER_UP,
    "directive:create": _REVIEWER_UP,
    "directive:confirm": _REVIEWER_UP,
    "approval:decide": _REVIEWER_UP,
    "finding:triage": _REVIEWER_UP,
    "compliance:review": _REVIEWER_UP,
    "provider:probe": _ADMIN_UP,
    "provider:settings": _ADMIN_UP,
    "documentation:register": _ADMIN_UP,
    "documentation:refresh": _DEV_UP,
    "audit:read": _ADMIN_UP,
    # Состояние платформы (UI redesign B2, GET /system/health): компоненты, воркеры, очереди — owner/admin.
    "system:health": _ADMIN_UP,
    # GitHub (P8, SECURITY_ARCHITECTURE §4.2 «Управление GitHub-установкой» — owner/admin): просмотр установок
    # и PR — все участники; запрос PR из проверенного run — как управление run (developer+); запрос merge —
    # developer+, само решение — approval MERGE (admin, four-eyes, §5.3).
    "github:read": _ALL,
    "github:manage": _ADMIN_UP,
    "pull_request:create": _DEV_UP,
    "pull_request:merge": _DEV_UP,
    # Inline-комментарии ревью кода (UI redesign B5, ADR-0015 §2): чтение — все; треды, ответы, правка своего,
    # resolve/unresolve — reviewer+ (как chat:write); отправка ревью — reviewer+ с уточнением по вердикту
    # (`can_submit_review`: REQUEST_CHANGES — как поручение CHANGE_REQUEST, APPROVE — правила approval §5.3).
    "review:read": _ALL,
    "review:write": _REVIEWER_UP,
    "review:submit": _REVIEWER_UP,
    # Персональные уведомления (UI redesign B8, API_DESIGN §13.2): каждый участник читает и отмечает прочитанными
    # только свои (RLS по user_id), поэтому право есть у всех ролей.
    "notification:read": _ALL,
    "notification:update": _ALL,
}

# API_DESIGN §5.3 — минимальная роль для решения по approval (иерархия ролей).
APPROVAL_MIN_ROLE: Final[dict[str, Role]] = {
    "MANUAL_REVIEW": Role.REVIEWER,
    "SELF_REVIEW_FALLBACK": Role.REVIEWER,
    "PLAN_APPROVAL": Role.DEVELOPER,
    "DOCS_DOWNLOAD": Role.DEVELOPER,
    "MERGE": Role.ADMIN,
    "DESTRUCTIVE_MIGRATION": Role.ADMIN,
    "CRITICAL_DIRECTIVE": Role.ADMIN,
    "VULN_DB_UPDATE": Role.ADMIN,  # обновление офлайн-БД уязвимостей (egress к реестрам БД), миграция 0016
    "PRODUCTION_DEPLOY": Role.OWNER,
    "SECRET_CHANGE": Role.OWNER,
    "UNRESTRICTED_NETWORK": Role.OWNER,
    "DATA_DELETION": Role.OWNER,
    "LOCAL_EXECUTION_FALLBACK": Role.OWNER,
}

# API_DESIGN §5.5 — минимальная роль для перехода находки по целевому статусу (`finding:triage`,
# x-permission-depends-on: to_status): принятие риска — решение admin, разбор (ACK/ложное срабатывание/
# повторное открытие) — reviewer и выше.
FINDING_TRANSITION_MIN_ROLE: Final[dict[str, Role]] = {
    "ACKNOWLEDGED": Role.REVIEWER,
    "FALSE_POSITIVE": Role.REVIEWER,
    "OPEN": Role.REVIEWER,
    "ACCEPTED_RISK": Role.ADMIN,
}

# Действия, для которых инициатор не может утвердить сам (four-eyes, API_DESIGN §5.3, OQ-10/OQ-18).
FOUR_EYES_ACTIONS: Final = frozenset(
    {
        "MERGE",
        "PRODUCTION_DEPLOY",
        "DESTRUCTIVE_MIGRATION",
        "DATA_DELETION",
        "SECRET_CHANGE",
        "UNRESTRICTED_NETWORK",
        "CRITICAL_DIRECTIVE",
        "LOCAL_EXECUTION_FALLBACK",
        "VULN_DB_UPDATE",
    }
)
# Для этих действий исключение single-member workspace не применяется (OQ-10).
NEVER_SELF_APPROVE: Final = frozenset({"PRODUCTION_DEPLOY", "SECRET_CHANGE"})

# API_DESIGN §5.4 — типы директив, доступные reviewer (остальные — developer и выше).
REVIEWER_DIRECTIVES: Final = frozenset({"ASK", "REVIEW", "APPROVE", "REJECT"})


def has_permission(role: Role | str | None, permission: str) -> bool:
    if permission not in PERMISSIONS:
        raise KeyError(f"неизвестное право: {permission}")
    if role is None:
        return False
    return Role(role) in PERMISSIONS[permission]


def effective_role(workspace_role: Role | str, project_override: Role | str | None) -> Role:
    """Эффективная роль = min(роль workspace, понижающее переопределение проекта)."""
    ws = Role(workspace_role)
    if project_override is None:
        return ws
    override = Role(project_override)
    return override if RANK[override] < RANK[ws] else ws


def can_decide_approval(
    role: Role | str,
    action_type: str,
    *,
    is_requester: bool,
    single_member_self_approval_allowed: bool = False,
) -> tuple[bool, str | None]:
    """Возвращает (разрешено, код отказа). Код — `forbidden` или `four_eyes_violation`."""
    min_role = APPROVAL_MIN_ROLE[action_type]
    if RANK[Role(role)] < RANK[min_role]:
        return False, "forbidden"
    if (
        is_requester
        and action_type in FOUR_EYES_ACTIONS
        and (action_type in NEVER_SELF_APPROVE or not single_member_self_approval_allowed)
    ):
        return False, "four_eyes_violation"
    return True, None


def can_transition_finding(role: Role | str, to_status: str) -> bool:
    """`finding:triage` с учётом целевого статуса (§5.5); неизвестный статус — запрещён."""
    min_role = FINDING_TRANSITION_MIN_ROLE.get(to_status)
    return min_role is not None and RANK[Role(role)] >= RANK[min_role]


def can_submit_review(role: Role | str, verdict: str) -> bool:
    """`review:submit` с учётом вердикта (ADR-0015 §2): COMMENT/APPROVE — reviewer+ (APPROVE дополнительно
    проверяется правилами решения approval §5.3), REQUEST_CHANGES — как поручение CHANGE_REQUEST (§5.4)."""
    if not has_permission(role, "review:submit"):
        return False
    if verdict == "REQUEST_CHANGES":
        return can_submit_directive(role, "CHANGE_REQUEST")
    return verdict in ("COMMENT", "APPROVE")


def can_submit_directive(role: Role | str, directive_type: str) -> bool:
    r = Role(role)
    if r is Role.VIEWER:
        return False
    if r is Role.REVIEWER:
        return directive_type in REVIEWER_DIRECTIVES
    return True
