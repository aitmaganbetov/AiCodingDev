"""Общая обвязка API-тестов approvals/directives: приложение с роутерами P3-10/P3-13 и хуками оркестратора.

Роутеры регистрирует `main.create_app`; тесты подменяют хуки оркестратора записывающими заглушками.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.hooks import OrchestrationHooks
from aicd_api.main import create_app
from aicd_platform.config import Settings
from tests.conftest import UserSession, login
from tests.support.orchestration import Tenant, drive_run, new_run, seed_tenant


@dataclass
class HookRecorder:
    approvals: list[dict[str, Any]] = field(default_factory=list)
    directives: list[uuid.UUID] = field(default_factory=list)
    on_directive: Callable[[uuid.UUID, uuid.UUID], Awaitable[None]] | None = None

    async def approval_decided(self, approval: dict[str, Any]) -> None:
        self.approvals.append(approval)

    async def directive_changed(self, directive_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        self.directives.append(directive_id)
        if self.on_directive is not None:
            await self.on_directive(directive_id, workspace_id)

    def reset(self) -> None:
        self.approvals.clear()
        self.directives.clear()
        self.on_directive = None


async def orch_app(settings: Settings, recorder: HookRecorder) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    application.state.aicd_hooks = OrchestrationHooks(
        on_approval_decided=recorder.approval_decided, on_directive_changed=recorder.directive_changed
    )
    async with application.router.lifespan_context(application):
        yield application


@dataclass
class Team:
    tenant: Tenant
    users: dict[str, UserSession]

    @property
    def ws(self) -> str:
        return str(self.tenant.workspace_id)


async def make_team(app: FastAPI, engine: AsyncEngine, admin: psycopg.Connection, roles: tuple[str, ...] = ()) -> Team:
    """Владелец (вход через API) + участники с заданными ролями (ключ — `role` или `role2`, …)."""
    owner = await login(app, engine)
    tenant = seed_tenant(admin, owner_id=uuid.UUID(owner.user_id))
    users = {"owner": owner}
    for name in roles:
        session = await login(app, engine)
        role = name.rstrip("0123456789")
        admin.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, %s)",
            (tenant.workspace_id, uuid.UUID(session.user_id), role),
        )
        users[name] = session
    return Team(tenant, users)


async def outsider(app: FastAPI, engine: AsyncEngine) -> UserSession:
    return await login(app, engine)


def running_run(admin: psycopg.Connection, tenant: Tenant) -> uuid.UUID:
    run = new_run(admin, tenant)
    drive_run(admin, run, "DISCOVERY", "PLANNED", "RUNNING")
    return run


def add_task(
    admin: psycopg.Connection, tenant: Tenant, run: uuid.UUID, key: str, *, role: str = "developer",
    complexity: int = 2, risk: str = "low", depends_on: uuid.UUID | None = None,
) -> uuid.UUID:  # fmt: skip
    from aicd_platform.ids import uuid7

    task = uuid7()
    admin.execute(
        "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title, instructions,"
        " complexity, risk_level) VALUES (%s, %s, %s, 1, %s, %s, %s, 'исходные инструкции', %s, %s)",
        (task, tenant.workspace_id, run, key, role, f"task {key}", complexity, risk),
    )
    if depends_on is not None:
        admin.execute(
            "INSERT INTO task_dependencies (workspace_id, run_id, task_id, depends_on_task_id) VALUES (%s, %s, %s, %s)",
            (tenant.workspace_id, run, task, depends_on),
        )
    return task


def key() -> str:
    return "k-" + secrets.token_hex(8)
