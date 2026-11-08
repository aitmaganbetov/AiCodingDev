"""Исполнение задачи через CLI Gateway (P3-06/P3-07) с вводом-выводом агента (Phase 4).

Установку выбирает Router (`routing_service`, P4-07) до захвата аренды; исполнитель получает готовый выбор.
До запуска CLI собирается промпт роли (inbox + Context Pack), после — разбирается `.aicd/result.json`
из каталога вывода исполнения и ACP-кандидаты отправляются брокеру (подробности и решения — `agent_io`).

Context Pack (P5-11, DATA_FLOW §9): до сборки промпта обеспечивается снимок индекса base_commit run
(`context.snapshot.ensure_snapshot`: нет READY-снимка — индексация в процессе исполнителя с ограничением
`ensure_index_timeout_seconds`), затем retrieval по задаче/роли/режиму документации и inbox
(`context.retrieval.build_context_pack`). Сбой индексации или retrieval **не роняет задачу**: исполнение
продолжается без кода, предупреждение — в manifest pack и в событии `agent.prompt_assembled`.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_cli_adapters import registry
from aicd_cli_adapters.gateway import ExecutionGateway
from aicd_cli_adapters.models import ExecutionRequest
from aicd_cli_adapters.subprocess_adapter import SubprocessCLIAdapter
from aicd_code_intelligence.context.retrieval import ContextPack, RetrievalRequest, build_context_pack
from aicd_code_intelligence.context.snapshot import DEFAULT_ENSURE_TIMEOUT_SECONDS, ensure_snapshot
from aicd_code_intelligence.context.store import StoredPack, record_retrieval, store_context_pack
from aicd_collaboration.acp import AcpBroker
from aicd_orchestration import agent_io, directives
from aicd_orchestration import worktrees as wt
from aicd_orchestration.agent_io import AgentContext, AgentIoSummary, InboxMessage
from aicd_orchestration.agents.prompt import AssembledPrompt
from aicd_orchestration.engine import TaskExecution
from aicd_platform import metrics as prom
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.paths import resolve_ref

log = logging.getLogger(__name__)


class AdapterFactory(Protocol):
    def __call__(
        self, *, provider: str, installation_id: uuid.UUID, gateway: ExecutionGateway, worktree_root: Path
    ) -> SubprocessCLIAdapter: ...


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
    # Context Pack: кэш экспортов снимков для ripgrep и предельное время индексации перед исполнением.
    index_export_cache_root: Path | None = None
    ensure_index_timeout_seconds: float = DEFAULT_ENSURE_TIMEOUT_SECONDS
    context_enabled: bool = True

    async def _context_pack(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        project_id: uuid.UUID,
        inbox: list[InboxMessage],
    ) -> ContextPack | None:
        """Снимок индекса base_commit + retrieval. Никогда не бросает: сбой — pack без кода или None."""
        if not self.context_enabled or not run.get("base_commit_sha"):
            return None
        ws: uuid.UUID = run["workspace_id"]
        warnings: list[str] = []
        try:
            ensured = await ensure_snapshot(
                self.engine,
                workspace_id=ws,
                repository_id=run["repository_id"],
                commit_sha=run["base_commit_sha"],
                repository_roots=self.repository_roots,
                timeout_seconds=self.ensure_index_timeout_seconds,
            )
            if ensured.warning:
                warnings.append(ensured.warning)
            request = RetrievalRequest(
                workspace_id=ws,
                project_id=project_id,
                repository_id=run["repository_id"],
                base_commit=run["base_commit_sha"],
                role=str(task["agent_role"]),
                task_text=agent_io.task_text(task, run),
                documentation_mode=str(run.get("documentation_mode") or "SMART"),
                seed_texts=agent_io.inbox_seed_texts(inbox),
            )
            cache_root = self.index_export_cache_root or Path(tempfile.gettempdir()) / "aicd-index-export"
            return await build_context_pack(
                self.engine,
                request,
                repository_roots=self.repository_roots,
                export_cache_root=cache_root,
                artifacts=self.artifacts or self.gateway.store,
                warnings=warnings,
            )
        except Exception:
            log.exception("context pack build failed")
            return None

    async def execute(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        attempt_id: uuid.UUID,
        provider: str,
        installation_id: uuid.UUID,
        register_cancel: Callable[[Callable[[str], Awaitable[None]]], None],
    ) -> TaskExecution:
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
            # Поручения, доставляемые этой попыткой (ack DELIVERED на старте): раздел directives с directive_id.
            directive_context = await directives.prompt_fragments(conn, task["id"])
        pack = await self._context_pack(task=task, run=run, project_id=repo_row.project_id, inbox=inbox)
        context = [
            *directive_context,
            *(agent_io.pack_fragments(pack.fragments, base_commit=run["base_commit_sha"]) if pack else []),
        ]
        # Промпт собирается до worktree: ошибка сборки (недопустимые поля задачи) не оставляет мусора.
        prompt = agent_io.assemble_prompt(task, run, inbox, context)
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
            stored_pack, pack_metrics = await self._store_pack(
                conn, pack, task=task, run=run, attempt_id=attempt_id, prompt=prompt, prompt_artifact=stored_prompt
            )
            # default_timeout_seconds провайдера в workspace — если у задачи нет явного тайм-аута (§15).
            ws_settings = (await registry.workspace_settings_by_code(conn)).get(
                provider
            ) or registry.default_workspace_settings()
            raw_timeout = task["timeout_seconds"]
            timeout_seconds = registry.effective_timeout(
                None if raw_timeout is None else int(raw_timeout), int(ws_settings["default_timeout_seconds"])
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
                    "context_pack": (
                        {
                            "context_pack_id": str(stored_pack.context_pack_id),
                            "artifact_ref": f"artifact:{stored_pack.artifact_id}",
                            "sha256": stored_pack.sha256,
                        }
                        if stored_pack is not None
                        else None
                    ),
                    "instruction_template_version": template_ref,
                },
                "limits": {
                    "timeout_seconds": timeout_seconds,
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
            context_pack_id=stored_pack.context_pack_id if stored_pack else None,
            context_pack=pack_metrics,
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
        return TaskExecution(
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
                await agent_io.record_prompt(
                    conn,
                    ctx,
                    prompt,
                    artifact_id=summary.prompt_artifact_id,
                    inbox=inbox,
                    context_pack=summary.context_pack,
                )
            if not completed:
                return replace(summary, result_status="skipped", result_error="исполнение CLI не завершилось успешно")
            processor = agent_io.ResultProcessor(engine=self.engine, store=store, broker=self.broker)
            return await processor.process(summary, ctx, io_dir=adapter.agent_io_dir(ctx.execution_id), commit_sha=head)
        except Exception as exc:
            log.exception("agent result processing failed")
            return replace(summary, result_status="error", result_error=type(exc).__name__)

    async def _store_pack(
        self,
        conn: AsyncConnection,
        pack: ContextPack | None,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        attempt_id: uuid.UUID,
        prompt: AssembledPrompt,
        prompt_artifact: agent_io.StoredFile,
    ) -> tuple[StoredPack | None, dict[str, Any]]:
        """Журнал retrieval + строка `context_packs` (savepoint: сбой записи pack не отменяет исполнение)."""
        if pack is None:
            reason = "no_base_commit" if not run.get("base_commit_sha") else "context_pack_unavailable"
            return None, {"context_pack_id": None, "warnings": [reason if self.context_enabled else "disabled"]}
        metrics: dict[str, Any] = {**pack.stats(), "warnings": pack.warnings, "content_hash": pack.content_hash}
        stats = pack.stats()
        prom.observe_context_pack(
            size_bytes=int(stats["size_bytes"]),
            fragments=int(stats["fragment_count"]),
            stale=int(stats["stale_count"]),
            snapshot_id=pack.snapshot_id,
        )
        try:
            async with conn.begin_nested():
                await record_retrieval(conn, pack, run_id=run["id"], task_id=task["id"], task_attempt_id=attempt_id)
                stored = await store_context_pack(
                    conn,
                    self.artifacts or self.gateway.store,
                    pack,
                    task_attempt_id=attempt_id,
                    run_id=run["id"],
                    extra_manifest={
                        "prompt_sha256": prompt.sha256,
                        "prompt_artifact_id": str(prompt_artifact.artifact_id),
                        "instruction_template_version": prompt.manifest.get("instruction_template_version"),
                    },
                )
        except Exception as exc:
            log.exception("context pack store failed")
            return None, {
                **metrics,
                "context_pack_id": None,
                "warnings": [*pack.warnings, f"store_failed:{type(exc).__name__}"],
            }
        if stored is None:
            return None, {**metrics, "context_pack_id": None}
        return stored, {
            **metrics,
            "context_pack_id": str(stored.context_pack_id),
            "artifact_id": str(stored.artifact_id),
            "artifact_bytes": stored.size_bytes,
            "cache_hit": stored.cache_hit,
        }
