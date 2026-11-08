"""Точки расширения API → orchestrator (`app.state.aicd_hooks`).

Роутеры approvals/directives фиксируют состояние транзакционно (state + outbox), а затем — уже после
commit и только для не повторённых (не replay) запросов — уведомляют оркестратор через эти хуки.
Хуки идемпотентны на стороне оркестратора; по умолчанию — no-op (их подключает сборка приложения,
например `on_approval_decided` → `Orchestrator.on_plan_approval_decided` для PLAN_APPROVAL, а
`on_directive_changed` → `aicd_orchestration.directives.process` на воркере).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request

ApprovalHook = Callable[[dict[str, Any]], Awaitable[None]]
DirectiveHook = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]  # (directive_id, workspace_id)


async def _noop_approval(approval: dict[str, Any]) -> None:
    del approval


async def _noop_directive(directive_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    del directive_id, workspace_id


@dataclass
class OrchestrationHooks:
    # approval: строка approvals после решения (id, workspace_id, run_id, action_type, status, subject, …).
    on_approval_decided: ApprovalHook = _noop_approval
    on_directive_changed: DirectiveHook = _noop_directive


def get_hooks(request: Request) -> OrchestrationHooks:
    hooks = getattr(request.app.state, "aicd_hooks", None)
    return hooks if isinstance(hooks, OrchestrationHooks) else OrchestrationHooks()
