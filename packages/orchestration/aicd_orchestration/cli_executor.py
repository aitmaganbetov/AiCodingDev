"""Исполнение задачи через CLI Gateway (P3-06/P3-07; маршрутизация Phase 3 — минимальная, Router — P4-07).

Выбор установки: провайдер из allowed_providers политики run, установка включена, совместимость не
UNSUPPORTED, последний health не UNAVAILABLE/RATE_LIMITED, есть свободный слот. Если подходящих нет —
`ProviderUnavailableError` (AUTH_REQUIRED, если установки есть, но не авторизованы) ⇒ задача BLOCKED.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.gateway import ExecutionGateway
from aicd_cli_adapters.models import ExecutionRequest
from aicd_cli_adapters.subprocess_adapter import SubprocessCLIAdapter
from aicd_orchestration import worktrees as wt
from aicd_orchestration.engine import ProviderUnavailableError, TaskExecution
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.paths import resolve_ref


class AdapterFactory(Protocol):
    def __call__(self, *, provider: str, installation_id: uuid.UUID, gateway: ExecutionGateway,
                 worktree_root: Path) -> SubprocessCLIAdapter: ...


@dataclass
class CliTaskExecutor:
    engine: AsyncEngine
    gateway: ExecutionGateway
    worktrees: wt.WorktreeManager
    repository_roots: dict[str, Path]
    adapter_factory: AdapterFactory
    worker_host_id: uuid.UUID | None = None
    default_timeout_seconds: int = 1800
    _chosen: dict[str, uuid.UUID] = field(default_factory=dict)

    async def select_provider(self, *, workspace_id: uuid.UUID, allowed: list[str], role: str) -> str:
        async with transaction(self.engine, DbContext(workspace_id=workspace_id, actor_type="system")) as conn:
            rows = (await conn.execute(text(
                "SELECT i.id, p.code, i.auth_status, i.max_concurrency, sys_active_executions(i.id) AS active,"
                " (SELECT h.availability FROM provider_health h WHERE h.installation_id = i.id"
                "   ORDER BY h.observed_at DESC LIMIT 1) AS availability"
                " FROM cli_installations i JOIN cli_providers p ON p.id = i.provider_id"
                " WHERE i.enabled AND p.enabled AND i.compatibility <> 'unsupported' AND p.code = ANY(:allowed)"
                " ORDER BY p.code, i.id"), {"allowed": allowed})).mappings().all()
        if not rows:
            raise ProviderUnavailableError("PROVIDER_UNAVAILABLE")
        authed = [r for r in rows if r["auth_status"] == "AUTHENTICATED"]
        if not authed:
            raise ProviderUnavailableError("AUTH_REQUIRED")
        usable = [r for r in authed if r["availability"] not in ("UNAVAILABLE", "RATE_LIMITED")
                  and r["active"] < r["max_concurrency"]]
        if not usable:
            raise ProviderUnavailableError("PROVIDER_UNAVAILABLE")
        # Phase 3: наименее загруженная установка; скоринг по роли/качеству — Router P4-07.
        best = min(usable, key=lambda r: (r["active"] / r["max_concurrency"], r["code"]))
        self._chosen[best["code"]] = best["id"]
        return str(best["code"])

    async def execute(
        self, *, task: dict[str, Any], run: dict[str, Any], attempt_id: uuid.UUID, provider: str,
        register_cancel: Callable[[Callable[[str], Awaitable[None]]], None],
    ) -> TaskExecution:
        ws: uuid.UUID = run["workspace_id"]
        installation_id = self._chosen[provider]
        async with transaction(self.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            ref = (await conn.execute(text("SELECT local_root_ref FROM repositories WHERE id = :id"),
                                      {"id": run["repository_id"]})).scalar_one()
        repo_path = resolve_ref(ref, self.repository_roots)
        worktree_id = uuid7()
        info = self.worktrees.create(repo_path, worktree_id=worktree_id, run_id=run["id"], task_id=task["id"],
                                     base_commit_sha=run["base_commit_sha"])
        async with transaction(self.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            await wt.record(conn, info, workspace_id=ws, repository_id=run["repository_id"], run_id=run["id"],
                            task_id=task["id"], worker_host_id=self.worker_host_id)
        request = ExecutionRequest.model_validate({
            "contract_version": "1.0",
            "execution_id": str(uuid7()),
            "task_id": str(task["id"]),
            "provider": provider,
            "agent_role": task["agent_role"],
            "workspace": {"worktree_id": str(worktree_id), "base_commit": run["base_commit_sha"],
                          "branch_name": info.branch_name},
            "input": {"objective": task["instructions"][:8000], "context_refs": [],
                      "acceptance_criteria": [str(c)[:2000] for c in task["acceptance_criteria"]][:100]},
            "limits": {"timeout_seconds": max(30, min(task["timeout_seconds"], 14400)),
                       "max_attempts": task["max_attempts"]},
            "permissions": {"network": "restricted", "write_scope": "worktree", "production_access": False},
            "run_id": str(run["id"]),
            "task_attempt_id": str(attempt_id),
            "workspace_id": str(ws),
            "command_profile": f"{provider}/agent_task@1",
        })
        adapter = self.adapter_factory(provider=provider, installation_id=installation_id, gateway=self.gateway,
                                       worktree_root=self.worktrees.path_for(worktree_id).parent)

        async def cancel(reason: str) -> None:
            await adapter.cancel(request.execution_id, reason=reason)  # type: ignore[arg-type]

        register_cancel(cancel)
        outcome = await self.gateway.run(request, adapter, installation_id=installation_id)
        result = outcome.result
        head = self.worktrees.commit_snapshot(info.path, f"aicd: task {task['task_key']} attempt {attempt_id}")
        if head is None:
            head = run["base_commit_sha"]  # изменений нет — проверки выполняются на базовом коммите
        async with transaction(self.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            await wt.mark_status(conn, worktree_id, "LOCKED", head)
        return TaskExecution(
            status=result.status,
            failure_category=result.error.code if result.error else None,
            retry_after=result.error.retry_after_seconds if result.error else None,
            provider=provider,
            worktree_path=info.path,
            head_commit_sha=head,
            execution_id=request.execution_id,
        )
