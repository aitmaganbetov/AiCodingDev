"""Права GitHub App (SC-GH-04, SECURITY_ARCHITECTURE §10) и минимальные права installation token на операцию.

App запрашивает ровно `APP_PERMISSIONS`. Установка, у которой есть хоть одно право из `FORBIDDEN_PERMISSIONS`
(или право шире, чем у App), не подключается: платформа не должна иметь возможности менять CI-workflow,
branch protection, секреты и окружения репозитория. Installation token запрашивается на одно действие,
для одного репозитория и с подмножеством прав (`TOKEN_SCOPES`).

Отклонение от SC-GH-04 (зафиксировано в отчёте P8): `statuses: write` вместо `statuses: read` — публикация
итогов gates как commit status (P8-04 «статусы»); `checks` остаётся `read` (check runs не создаются).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

Level = Literal["read", "write"]

APP_PERMISSIONS: Final[Mapping[str, Level]] = {
    "contents": "write",  # push веток aicd/* и merge PR
    "pull_requests": "write",  # создание PR, комментарии ревью
    "checks": "read",  # required checks перед merge
    "statuses": "write",  # итог gates как commit status (отклонение от SC-GH-04: было read)
    "metadata": "read",
}

# Права, наличие которых у установки — отказ подключения (SC-GH-04).
FORBIDDEN_PERMISSIONS: Final = frozenset(
    {"workflows", "administration", "secrets", "environments", "actions", "organization_administration"}
)

Operation = Literal["read", "push", "pr_create", "pr_comment", "status", "merge"]

TOKEN_SCOPES: Final[Mapping[Operation, Mapping[str, Level]]] = {
    "read": {"metadata": "read", "pull_requests": "read", "checks": "read", "statuses": "read", "contents": "read"},
    "push": {"contents": "write", "metadata": "read"},
    "pr_create": {"pull_requests": "write", "metadata": "read"},
    "pr_comment": {"pull_requests": "write", "metadata": "read"},
    "status": {"statuses": "write", "metadata": "read"},
    "merge": {"contents": "write", "pull_requests": "read", "checks": "read", "statuses": "read", "metadata": "read"},
}

_RANK: Final = {"read": 1, "write": 2}


def excess_permissions(granted: Mapping[str, str]) -> list[str]:
    """Права установки сверх `APP_PERMISSIONS` или из запрещённого списка (пустой список — всё в порядке)."""
    problems: list[str] = []
    for name, level in sorted(granted.items()):
        if name in FORBIDDEN_PERMISSIONS:
            problems.append(f"{name}:{level}")
            continue
        allowed = APP_PERMISSIONS.get(name)
        if allowed is None or _RANK.get(str(level), 3) > _RANK[allowed]:
            problems.append(f"{name}:{level}")
    return problems


def missing_permissions(granted: Mapping[str, str]) -> list[str]:
    """Права App, которых нет у установки (владелец ещё не принял обновлённые права App)."""
    return [
        f"{name}:{level}"
        for name, level in sorted(APP_PERMISSIONS.items())
        if _RANK.get(str(granted.get(name)), 0) < _RANK[level]
    ]
