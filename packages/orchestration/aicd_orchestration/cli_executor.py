"""Исполнение задачи через CLI Gateway (P3-06/P3-07) с вводом-выводом агента (Phase 4).

Установку выбирает Router (`routing_service`, P4-07) до захвата аренды; исполнитель получает готовый выбор.
До запуска CLI собирается промпт роли (inbox, artifact `context_pack`), после — разбирается `.aicd/result.json`
из каталога вывода исполнения и ACP-кандидаты отправляются брокеру (подробности и решения — `agent_io`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.gateway import ExecutionGateway
from aicd_cli_adapters.models import ExecutionRequest
from aicd_cli_adapters.subprocess_adapter import SubprocessCLIAdapter
from aicd_collaboration.acp import AcpBroker
from aicd_orchestration import agent_io
from aicd_orchestration import worktrees as wt
from aicd_orchestration.agent_io import AgentContext, AgentIoSummary, InboxMessage
from aicd_orchestration.agents.prompt import AssembledPrompt
from aicd_orchestration.engine import TaskExecution
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.paths import resolve_ref

log = logging.getLogger(__name__)


class AdapterFactory(Protocol):
    def __call__(
        self, *, provider: str, installation_id: uuid.UUID, gateway: ExecutionGateway, worktree_root: Path
    ) -> SubprocessCLIAdapter: ...


@dataclass(frozen=True)
class AgentTaskExecution(TaskExecution):
    """TaskExecution + итог ввода-вывода агента (до появления поля `agent_io` в самом `engine.TaskExecution`)."""

    agent_io: AgentIoSummary | None = None


@dataclass
class CliTaskExecutor:
    engine: AsyncEngine
    gateway: ExecutionGateway
    worktrees: wt.WorktreeManager
    repository_roots: dict[str, Path]
    adapter_factory: AdapterFactory
    worker_host_id: uuid.UUID | None = None
    default_timeout_seconds: int = 1800
    artifacts: ArtifactStore | None = None  # по умолчанию — хранилище gateway
    broker: AcpBroker = field(default_factory=AcpBroker)

    async def execute(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        attempt_id: uuid.UUID,
        provider: str,
        installation_id: uuid.UUID,
        register_cancel: Callable[[Callable[[str], Awaitable[None]]], None],
    ) -> AgentTaskExecution:
        ws: uuid.UUID = run["workspace_id"]
        role = str(task["agent_role"])
        async with transaction(self.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            repo_row = (
                await conn.execute(
                    text(
                        "SELECT r.local_root_ref, u.project_id FROM repositories r JOIN runs u ON u.id = :run"
                        " WHERE r.id = :id"
                    ),
                    {"id": run["repository_id"], "run": run["id"]},
                )
            ).one()
            inbox = await agent_io.load_inbox(conn, run_id=run["id"], task_id=task["id"], role=role)
        # Промпт собирается до worktree: ошибка сборки (недопустимые поля задачи) не оставляет мусора.
        prompt = agent_io.assemble_prompt(task, run, inbox)
        template_ref = str(prompt.manifest["instruction_template_version"])
        store = self.artifacts or self.gateway.store
        repo_path = resolve_ref(repo_row.local_root_ref, self.repository_roots)
        worktree_id = uuid7()
        info = self.worktrees.create(
            repo_path,
            worktree_id=worktree_id,
            run_id=run["id"],
            task_id=task["id"],
            base_commit_sha=run["base_commit_sha"],
        )
        async with transaction(self.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            await wt.record(
                conn,
                info,
                workspace_id=ws,
                repository_id=run["repository_id"],
                run_id=run["id"],
                task_id=task["id"],
                worker_host_id=self.worker_host_id,
            )
            stored_prompt = await agent_io.store_artifact(
                conn,
                store,
                workspace_id=ws,
                project_id=repo_row.project_id,
                run_id=run["id"],
                kind="context_pack",
                data=prompt.text.encode("utf-8"),
                mime_type="text/markdown",
                sensitive=True,  # промпт может содержать код клиента (AGENT_ROLES §3.3 п.5)
            )
        permissions = agent_io.role_permissions(role)
        request = ExecutionRequest.model_validate(
            {
                "contract_version": "1.0",
                "execution_id": str(uuid7()),
                "task_id": str(task["id"]),
                "provider": provider,
                "agent_role": role,
                "workspace": {
                    "worktree_id": str(worktree_id),
                    "base_commit": run["base_commit_sha"],
                    "branch_name": info.branch_name,
                },
                "input": {
                    "objective": (task["instructions"] or task["task_key"])[:8000],
                    "context_refs": [],
                    "acceptance_criteria": [str(c)[:2000] for c in task["acceptance_criteria"]][:100],
                    "context_pack": {
                        "context_pack_id": str(stored_prompt.artifact_id),
                        "artifact_ref": f"artifact:{stored_prompt.artifact_id}",
                        "sha256": stored_prompt.sha256,
                    },
                    "instruction_template_version": template_ref,
                },
                "limits": {
                    "timeout_seconds": max(30, min(task["timeout_seconds"], 14400)),
                    "max_attempts": task["max_attempts"],
                },
                "permissions": {
                    "network": "restricted",
                    "write_scope": permissions.write_scope,
                    "production_access": False,
                },
                "run_id": str(run["id"]),
                "task_attempt_id": str(attempt_id),
                "workspace_id": str(ws),
                "command_profile": f"{provider}/agent_task@1",
            }
        )
        adapter = self.adapter_factory(
            provider=provider,
            installation_id=installation_id,
            gateway=self.gateway,
            worktree_root=self.worktrees.path_for(worktree_id).parent,
        )
        adapter.stage_prompt(request.execution_id, prompt.text)

        async def cancel(reason: str) -> None:
            await adapter.cancel(request.execution_id, reason=reason)  # type: ignore[arg-type]

        register_cancel(cancel)
        ctx = AgentContext(
            workspace_id=ws,
            project_id=repo_row.project_id,
            run_id=run["id"],
            task_id=task["id"],
            attempt_id=attempt_id,
            role=role,
            provider=provider,
            execution_id=request.execution_id,
        )
        summary = AgentIoSummary(
            instruction_template_version=template_ref,
            prompt_sha256=prompt.sha256,
            prompt_artifact_id=stored_prompt.artifact_id,
            inbox_message_ids=tuple(m.id for m in inbox),
        )
        try:
            outcome = await self.gateway.run(request, adapter, installation_id=installation_id)
            result = outcome.result
            # Итог агента лежит вне worktree; остатки `.aicd/`/`output/` в дереве — не изменения кода задачи.
            self.worktrees.clear_agent_io(info.path)
            head = self.worktrees.commit_snapshot(info.path, f"aicd: task {task['task_key']} attempt {attempt_id}")
            if head is None:
                head = run["base_commit_sha"]  # изменений нет — проверки выполняются на базовом коммите
            async with transaction(self.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
                await wt.mark_status(conn, worktree_id, "LOCKED", head)
            summary = await self._agent_result(
                summary, ctx, adapter, prompt, inbox, completed=result.status == "completed", head=head
            )
        finally:
            adapter.release_agent_io(request.execution_id)
        return AgentTaskExecution(
            status=result.status,
            failure_category=result.error.code if result.error else None,
            retry_after=result.error.retry_after_seconds if result.error else None,
            provider=provider,
            worktree_path=info.path,
            head_commit_sha=head,
            execution_id=request.execution_id,
            agent_io=summary,
        )

    async def _agent_result(
        self,
        summary: AgentIoSummary,
        ctx: AgentContext,
        adapter: SubprocessCLIAdapter,
        prompt: AssembledPrompt,
        inbox: list[InboxMessage],
        *,
        completed: bool,
        head: str,
    ) -> AgentIoSummary:
        """Промпт фиксируется в ленте активности; итог разбирается только у завершившегося CLI.

        Любой сбой здесь — данные (`result_status=error`), а не исключение: исполнение уже состоялось.
        """
        store = self.artifacts or self.gateway.store
        try:
            async with transaction(self.engine, DbContext(workspace_id=ctx.workspace_id, actor_type="system")) as conn:
                await agent_io.record_prompt(conn, ctx, prompt, artifact_id=summary.prompt_artifact_id, inbox=inbox)
            if not completed:
                return replace(summary, result_status="skipped", result_error="исполнение CLI не завершилось успешно")
            processor = agent_io.ResultProcessor(engine=self.engine, store=store, broker=self.broker)
            return await processor.process(summary, ctx, io_dir=adapter.agent_io_dir(ctx.execution_id), commit_sha=head)
        except Exception as exc:
            log.exception("agent result processing failed")
            return replace(summary, result_status="error", result_error=type(exc).__name__)
