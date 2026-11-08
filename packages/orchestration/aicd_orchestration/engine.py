"""Durable orchestrator (ADR-0007, DATA_FLOW §2–§5, P3-06/P3-07).

Обработчики идемпотентны и вызываются по доменным событиям (outbox → Dramatiq) или циклом `drive`
(тесты, E2E). Состояние живёт только в PostgreSQL; каждый шаг — короткая транзакция с outbox.
Исполнение CLI идёт вне транзакции под арендой задачи с fencing token.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_code_intelligence.docs.policy import DocumentationGate
from aicd_collaboration.acp import AcpBroker
from aicd_orchestration import checkpoints, directives, docs_flow, plans, retry, review_flow, routing_service, store
from aicd_orchestration.agent_io import AgentIoSummary
from aicd_orchestration.agents.profiles import profile as role_profile
from aicd_orchestration.classification import Signals, classify
from aicd_orchestration.dag import PlannedTask, critical_path_lengths
from aicd_orchestration.docs_ports import DocsApprovals, DocsLedger
from aicd_orchestration.planner import template_plan
from aicd_platform import context, leases
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_standards.store import evaluate_run as evaluate_compliance
from aicd_verification.verdict import run_completion_verdict

log = logging.getLogger("aicd.orchestrator")


# --- Порты (реализации: approvals, verification, cli gateway) ----------------------------------


class ApprovalsPort(Protocol):
    async def request(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        task_id: uuid.UUID | None,
        action_type: str,
        risk_level: str,
        subject: dict[str, Any],
        requested_by_type: str,
        requested_by: uuid.UUID | None = None,
        requested_by_role: str | None = None,
    ) -> uuid.UUID: ...


class VerificationOutcome(Protocol):
    """Структурный тип результата верификации: модуль verification не импортирует оркестратор."""

    @property
    def passed(self) -> bool: ...

    @property
    def gate_execution_ids(self) -> tuple[uuid.UUID, ...]: ...

    @property
    def summary(self) -> str: ...


class VerifierPort(Protocol):
    async def verify_task(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        task_id: uuid.UUID,
        worktree_path: Any,
        commit_sha: str,
        quality_profile: str,
    ) -> VerificationOutcome: ...


@dataclass(frozen=True)
class TaskExecution:
    """Итог исполнения задачи CLI: нормализованный результат + где искать изменения."""

    status: str  # completed | failed | cancelled
    failure_category: str | None
    retry_after: int | None
    provider: str
    worktree_path: Any
    head_commit_sha: str | None
    execution_id: uuid.UUID
    agent_io: AgentIoSummary | None = None  # итог ввода-вывода агента (промпт, result.json, ACP), P4-03/P4-04


class TaskExecutorPort(Protocol):
    async def execute(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        attempt_id: uuid.UUID,
        provider: str,
        installation_id: uuid.UUID,
        register_cancel: Callable[[Callable[[str], Awaitable[None]]], None],
    ) -> TaskExecution: ...


# --- Оркестратор ----------------------------------------------------------------------------------


@dataclass
class Orchestrator:
    engine: AsyncEngine
    approvals: ApprovalsPort
    verifier: VerifierPort
    executor: TaskExecutorPort
    # Хранилище снимков checkpoint (DATA_FLOW §7): plan_approved, pause. Без него снимки не создаются.
    artifacts: ArtifactStore | None = None
    worker_id: str = field(default_factory=lambda: leases.worker_identity("orchestrator"))
    lease_ttl_seconds: int = 120
    # Документация по запросу агента (BLOCKED DOCS_REQUIRED, ACP §9.4): approvals, ledger загрузок, решения ACP.
    docs_approvals: DocsApprovals = field(default_factory=DocsApprovals)
    docs_ledger: DocsLedger = field(default_factory=DocsLedger)
    acp: AcpBroker = field(default_factory=AcpBroker)
    _cancel_hooks: dict[uuid.UUID, Callable[[str], Awaitable[None]]] = field(default_factory=dict)

    def _ctx(self, ws: uuid.UUID) -> DbContext:
        return DbContext(workspace_id=ws, actor_type="system")

    # ---------- run lifecycle ----------
    async def on_run_created(self, run_id: uuid.UUID, ws: uuid.UUID) -> None:
        async with transaction(self.engine, self._ctx(ws)) as conn:
            run = await _run_row(conn, run_id)
            if run is None or run["status"] != "CREATED":
                return  # идемпотентность: событие уже обработано
            if run["base_commit_sha"] is None:
                await store.transition_run(
                    conn,
                    run_id,
                    "FAILED",
                    reason="no_base_commit",
                    extra={"failure_category": "INTERNAL_ERROR", "failure_message": "в репозитории нет коммитов"},
                )
                return
            await store.transition_run(conn, run_id, "DISCOVERY", reason="start")
            # Классификация L1–L5/риска (ROUTING_POLICY §2–§3). До сигналов Code Intelligence (Phase 5) оценка
            # planner консервативна — L3; уровень не бывает ниже минимума режима и floor-правил.
            classified = classify(Signals(), run["mode"], planner_estimate=DEFAULT_PLANNER_ESTIMATE)
            specs = template_plan(
                run["mode"], run["objective"], complexity=classified.level, risk_level=classified.risk_level
            )
            plan_version, _ = await plans.install_plan(conn, run_id, specs)
            await store.transition_run(conn, run_id, "PLANNED", reason="plan_validated")
            needs_approval = _plan_needs_approval(run["approval_policy"], specs)
            if needs_approval:
                await self.approvals.request(
                    conn,
                    workspace_id=ws,
                    project_id=run["project_id"],
                    run_id=run_id,
                    task_id=None,
                    action_type="PLAN_APPROVAL",
                    risk_level=max((s.risk_level for s in specs), key=_RISK.index),
                    subject={"run_id": str(run_id), "plan_version": plan_version},
                    requested_by_type="user",
                    requested_by=run["requested_by"],
                )
                await store.transition_run(conn, run_id, "AWAITING_APPROVAL", reason="approval_required")
                return
            await plans.mark_plan_approved(conn, run_id, plan_version)
            await store.transition_run(conn, run_id, "RUNNING", reason="no_approval_required")
            await self._checkpoint(conn, run_id, "plan_approved")
        await self.tick(run_id, ws)

    async def on_plan_approval_decided(
        self, run_id: uuid.UUID, ws: uuid.UUID, *, approved: bool, plan_version: int
    ) -> None:
        async with transaction(self.engine, self._ctx(ws)) as conn:
            run = await store.load_run(conn, run_id, lock=True)
            if run is None or run.status != "AWAITING_APPROVAL" or run.plan_version != plan_version:
                return
            if approved:
                await plans.mark_plan_approved(conn, run_id, plan_version)
                await store.transition_run(conn, run_id, "RUNNING", reason="plan_approved")
                await self._checkpoint(conn, run_id, "plan_approved")
            else:
                await store.transition_run(conn, run_id, "CANCELLED", reason="plan_rejected")
                await _cancel_open_tasks(conn, run_id)
        if approved:
            await self.tick(run_id, ws)

    async def on_task_approval_decided(self, approval_id: uuid.UUID, ws: uuid.UUID) -> None:
        """Решение по approval уровня задачи из маршрутизации (ROUTING_POLICY §5, P4-08) и документации (ACP §9.4).

        * SELF_REVIEW_FALLBACK одобрен → задача снова READY; Router выберет провайдера-автора (маркировка self_review);
        * MANUAL_REVIEW одобрен → ревью выполнено человеком: задача SUCCEEDED (маркировка human_review);
          отклонён → задача FAILED (REVIEW_REJECTED). То же — после исчерпания review/fix циклов (ACP §6).
        * DOCS_DOWNLOAD и MANUAL_REVIEW потока документации — `_on_docs_approval_decided`.
        Идемпотентно: действует только на задачу в ожидаемом состоянии BLOCKED.
        """
        async with transaction(self.engine, self._ctx(ws)) as conn:
            head = (
                (
                    await conn.execute(
                        text("SELECT action_type, subject FROM approvals WHERE id = :a"), {"a": approval_id}
                    )
                )
                .mappings()
                .first()
            )
        if head is None:
            return
        subject = head["subject"] if isinstance(head["subject"], dict) else {}
        if head["action_type"] == "DOCS_DOWNLOAD" or (
            head["action_type"] == "MANUAL_REVIEW" and subject.get("reason") == docs_flow.MANUAL_REVIEW_REASON
        ):
            await self._on_docs_approval_decided(approval_id, ws)
            return
        async with transaction(self.engine, self._ctx(ws)) as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT a.action_type, a.status, a.task_id, t.status AS task_status, t.blocked_reason"
                            " FROM approvals a JOIN tasks t ON t.id = a.task_id WHERE a.id = :a FOR UPDATE OF t"
                        ),
                        {"a": approval_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["task_status"] != "BLOCKED":
                return
            action, status, task_id = row["action_type"], row["status"], row["task_id"]
            if (
                action == "SELF_REVIEW_FALLBACK"
                and status == "APPROVED"
                and row["blocked_reason"] == "APPROVAL_REQUIRED"
            ):
                await store.transition_task(conn, task_id, "READY", reason="self_review_approved")
            elif action == "MANUAL_REVIEW" and row["blocked_reason"] in ("MANUAL_REVIEW", "REVIEW_LOOP_EXHAUSTED"):
                if status == "APPROVED":
                    await store.transition_task(conn, task_id, "SUCCEEDED", reason="manual_review_approved")
                elif status == "REJECTED":
                    await store.transition_task(conn, task_id, "FAILED", reason="REVIEW_REJECTED")
            else:
                return
            run_id = (await conn.execute(text("SELECT run_id FROM tasks WHERE id = :t"), {"t": task_id})).scalar_one()
        await self.tick(run_id, ws)

    # ---------- документация по запросу агента (ACP §9.4, AC-10) ----------
    async def _on_docs_approval_decided(self, approval_id: uuid.UUID, ws: uuid.UUID) -> None:
        """DOCS_DOWNLOAD решён (одобрен/отклонён/истёк) → задачи BLOCKED(DOCS_REQUIRED) workspace оцениваются заново:
        одобрен — загрузка в ledger + событие импортёру (задача ждёт снимка); отклонён/истёк — SMART: новая попытка
        по локальному индексу (DECISION_RECORDED), STRICT: MANUAL_REVIEW. PENDING по предмету в workspace один,
        поэтому решение касается всех ожидающих задач, а не только задачи-инициатора.

        MANUAL_REVIEW потока документации: одобрен — новая попытка без документации (решение человека в
        DECISION_RECORDED), отклонён — задача FAILED.
        """
        runs: set[uuid.UUID] = set()
        async with transaction(self.engine, self._ctx(ws)) as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT a.action_type, a.status, a.task_id, a.decided_by, t.run_id, t.status AS task_status,"
                            " t.blocked_reason FROM approvals a JOIN tasks t ON t.id = a.task_id WHERE a.id = :a"
                            " FOR UPDATE OF t"
                        ),
                        {"a": approval_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] == "PENDING":
                return
            if row["action_type"] == "DOCS_DOWNLOAD":
                runs.update(await self._reevaluate_docs_blocked(conn, ws))
            elif row["task_status"] == "BLOCKED" and row["blocked_reason"] == "MANUAL_REVIEW":
                task = await _task_row(conn, row["task_id"])
                run = await _run_row(conn, row["run_id"])
                if task is None or run is None:  # pragma: no cover — FK
                    return
                if row["status"] == "APPROVED":
                    await store.transition_task(conn, task["id"], "READY", reason="docs_manual_review_approved")
                    evaluation = docs_flow.DocsEvaluation(
                        docs_flow.mode_of(run), "proceed", "решение человека: продолжить без документации"
                    )
                    await docs_flow.record_decision(
                        conn, self.acp, run=run, task=task, evaluation=evaluation,
                        decided_by_user=row["decided_by"], approval_id=approval_id,
                    )  # fmt: skip
                else:
                    await store.transition_task(conn, task["id"], "FAILED", reason="docs_manual_review_rejected")
                runs.add(row["run_id"])
        for run_id in sorted(runs):
            await self.tick(run_id, ws)

    async def on_docs_available(self, ws: uuid.UUID, source_id: uuid.UUID) -> list[uuid.UUID]:
        """Импорт источника завершён (снимок READY или загрузка не удалась): задачи BLOCKED(DOCS_REQUIRED) активных
        run проектов этого источника оцениваются заново (снимок есть ⇒ новая попытка с разделом docs; загрузка
        не удалась ⇒ SMART — по локальному индексу, STRICT — MANUAL_REVIEW). Возвращает READY-задачи после tick.
        Идемпотентно: повторный вызов не находит заблокированных задач."""
        async with transaction(self.engine, self._ctx(ws)) as conn:
            runs = await self._reevaluate_docs_blocked(conn, ws, source_id=source_id)
        ready: list[uuid.UUID] = []
        for run_id in sorted(runs):
            ready += await self.tick(run_id, ws)
        return ready

    async def _reevaluate_docs_blocked(
        self, conn: AsyncConnection, ws: uuid.UUID, *, source_id: uuid.UUID | None = None
    ) -> set[uuid.UUID]:
        del ws  # RLS-контекст транзакции уже ограничен workspace
        params: dict[str, Any] = {"s": source_id}
        relevant = (
            ""
            if source_id is None
            else " AND EXISTS (SELECT 1 FROM documentation_sources s WHERE s.id = :s AND (s.technology_version_id IS NULL"
            " OR s.technology_version_id IN (SELECT pt.technology_version_id FROM project_technologies pt"
            "   WHERE pt.project_id = r.project_id)))"
        )
        task_ids = (
            (
                await conn.execute(
                    text(
                        "SELECT t.id FROM tasks t JOIN runs r ON r.id = t.run_id WHERE t.status = 'BLOCKED'"  # noqa: S608
                        " AND t.blocked_reason = 'DOCS_REQUIRED' AND t.superseded_at IS NULL"
                        " AND r.status IN ('RUNNING','BLOCKED')" + relevant + " ORDER BY t.created_at, t.id LIMIT 100"
                    ),
                    params,
                )
            )
            .scalars()
            .all()
        )
        runs: set[uuid.UUID] = set()
        for task_id in task_ids:
            task = await _task_row(conn, task_id)
            if task is None or task["status"] != "BLOCKED" or task["blocked_reason"] != "DOCS_REQUIRED":
                continue
            run = await _run_row(conn, task["run_id"])
            if run is None:  # pragma: no cover — FK
                continue
            evaluation = await docs_flow.evaluate(
                conn, DocumentationGate(self.docs_approvals), self.docs_approvals, run=run, task=task
            )
            await self._apply_docs(conn, task, run, evaluation)
            runs.add(run["id"])
        return runs

    async def _on_docs_required(
        self, conn: AsyncConnection, task: dict[str, Any], run: dict[str, Any], attempt_id: uuid.UUID, spent: int
    ) -> None:
        """Попытка завершилась BLOCKED(DOCS_REQUIRED) агента. Попытка закрывается без расхода max_attempts."""
        if await docs_flow.decision_sent(conn, run["id"], task["id"]):
            # Защита от цикла: решение уже сообщено, агент его оспаривает (см. docs_flow).
            await fail_attempt(conn, task, attempt_id, "POLICY_DENIED", spent)
            return
        evaluation = await docs_flow.evaluate(
            conn, DocumentationGate(self.docs_approvals), self.docs_approvals, run=run, task=task
        )
        await _finish_attempt_row(conn, attempt_id, "CANCELLED", None)
        await self._apply_docs(conn, task, run, evaluation)

    async def _apply_docs(
        self, conn: AsyncConnection, task: dict[str, Any], run: dict[str, Any], ev: docs_flow.DocsEvaluation
    ) -> None:
        running = task["status"] != "BLOCKED"  # RUNNING (итог попытки) или BLOCKED (повторная оценка)
        free = {"attempt_count": max(task["attempt_count"] - 1, 0)} if running else {}
        for download in ev.downloads:
            await docs_flow.plan_download(
                conn, self.docs_ledger, workspace_id=run["workspace_id"], run_id=run["id"], task_id=task["id"],
                download=download,
            )  # fmt: skip
        if ev.outcome == "proceed":
            if running:
                # RUNNING → READY не разрешён FSM: немедленный RETRY_WAIT (tick переводит в READY), попытка не расходуется.
                await store.transition_task(
                    conn,
                    task["id"],
                    "RETRY_WAIT",
                    reason="docs_unavailable",
                    extra={"next_attempt_at": utcnow(), **free},
                )
            else:
                await store.transition_task(conn, task["id"], "READY", reason="docs_resolved")
            await docs_flow.record_decision(conn, self.acp, run=run, task=task, evaluation=ev)
            return
        reason = "MANUAL_REVIEW" if ev.outcome == "manual_review" else "DOCS_REQUIRED"
        if running:
            await store.transition_task(
                conn, task["id"], "BLOCKED", reason="docs_required", extra={"blocked_reason": reason, **free}
            )
        elif task["blocked_reason"] != reason:
            await store.reblock_task(conn, task["id"], reason, why="docs_required")
        if reason == "MANUAL_REVIEW" and not await _approved(conn, task["id"], "MANUAL_REVIEW", statuses=("PENDING",)):
            await self.approvals.request(
                conn,
                workspace_id=run["workspace_id"],
                project_id=run["project_id"],
                run_id=run["id"],
                task_id=task["id"],
                action_type="MANUAL_REVIEW",
                risk_level=task["risk_level"],
                subject={"task_id": str(task["id"]), "reason": docs_flow.MANUAL_REVIEW_REASON, "detail": ev.reason},
                requested_by_type="system",
            )

    async def on_run_paused_or_cancelled(self, run_id: uuid.UUID, ws: uuid.UUID, status: str) -> None:
        """Safe point: работающие исполнения run останавливаются штатно (DATA_FLOW §6.2)."""
        async with transaction(self.engine, self._ctx(ws)) as conn:
            if status == "PAUSED":
                current = await store.load_run(conn, run_id)
                if current is not None and current.status == "PAUSED":
                    await self._checkpoint(conn, run_id, "pause")
            running = (
                (
                    await conn.execute(
                        text("SELECT id FROM tasks WHERE run_id = :r AND status IN ('LEASED','RUNNING')"), {"r": run_id}
                    )
                )
                .scalars()
                .all()
            )
        reason = "pause" if status == "PAUSED" else "run_cancel"
        for task_id in running:
            hook = self._cancel_hooks.get(task_id)
            if hook is not None:
                await hook(reason)

    async def on_run_resumed(self, run_id: uuid.UUID, ws: uuid.UUID) -> None:
        async with transaction(self.engine, self._ctx(ws)) as conn:
            paused = (
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM tasks WHERE run_id = :r AND status = 'BLOCKED' AND blocked_reason = 'RUN_PAUSED'"
                        ),
                        {"r": run_id},
                    )
                )
                .scalars()
                .all()
            )
            for task_id in paused:
                await store.transition_task(conn, task_id, "READY", reason="run_resumed")
        await self.tick(run_id, ws)

    async def _request_task_approval(
        self, conn: AsyncConnection, task: dict[str, Any], run: dict[str, Any], action: str, decision: Any
    ) -> None:
        if await _approved(conn, task["id"], action, statuses=("PENDING", "APPROVED")):
            return  # уже запрошено (повторный роутинг той же задачи)
        await self.approvals.request(
            conn,
            workspace_id=run["workspace_id"],
            project_id=run["project_id"],
            run_id=run["id"],
            task_id=task["id"],
            action_type=action,
            risk_level=task["risk_level"],
            subject={
                "task_id": str(task["id"]),
                "reason": decision.reason,
                "review_independence": decision.review_independence,
            },
            requested_by_type="system",
        )

    async def _checkpoint(self, conn: AsyncConnection, run_id: uuid.UUID, reason: checkpoints.Reason) -> None:
        if self.artifacts is not None:
            await checkpoints.create(conn, run_id=run_id, artifacts=self.artifacts, reason=reason)

    # ---------- scheduler ----------
    async def tick(self, run_id: uuid.UUID, ws: uuid.UUID) -> list[uuid.UUID]:
        """Продвигает DAG: PENDING→READY, RETRY_WAIT→READY, завершение/блокировка run. Возвращает READY."""
        async with transaction(self.engine, self._ctx(ws)) as conn:
            run = await _run_row(conn, run_id, lock=True)
            if run is not None and run["status"] == "BLOCKED":
                # Run блокируется только сводно по задачам (tasks_blocked); когда заблокированных задач не осталось
                # (провайдер вернулся, approval решён), run возобновляется сам (ROUTING_POLICY §4.1).
                still_blocked = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM tasks WHERE run_id = :r AND status = 'BLOCKED' AND superseded_at IS NULL LIMIT 1"
                        ),
                        {"r": run_id},
                    )
                ).first()
                if still_blocked is None:
                    await store.transition_run(conn, run_id, "RUNNING", reason="tasks_unblocked")
                    run = await _run_row(conn, run_id)
            if run is None or run["status"] != "RUNNING":
                return []
            # Задачи, отменённые на safe point по поручению, выходят из текущего плана (иначе run не завершится).
            await directives.finalize_superseded(conn, run_id)
            ready_now = (
                (
                    await conn.execute(
                        text(
                            "SELECT t.id FROM tasks t WHERE t.run_id = :r AND t.superseded_at IS NULL AND t.status = 'PENDING'"
                            " AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on_task_id"
                            "   WHERE d.task_id = t.id AND p.status <> 'SUCCEEDED')"
                            # зависимость завершена, только когда завершены и её производные по origin_task_id
                            # (fix/повторное ревью цикла) — кроме самой задачи t и её собственных предков
                            " AND NOT EXISTS (WITH RECURSIVE der(id) AS ("
                            "   SELECT c.id FROM task_dependencies d JOIN tasks c ON c.origin_task_id = d.depends_on_task_id"
                            "   WHERE d.task_id = t.id"
                            "   UNION SELECT c.id FROM tasks c JOIN der ON c.origin_task_id = der.id)"
                            "   SELECT 1 FROM der JOIN tasks x ON x.id = der.id"
                            "   WHERE x.status <> 'SUCCEEDED' AND x.superseded_at IS NULL AND x.id <> t.id"
                            "   AND x.origin_task_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM task_dependencies dd"
                            "     WHERE dd.task_id = x.id AND dd.depends_on_task_id = t.id))"
                        ),
                        {"r": run_id},
                    )
                )
                .scalars()
                .all()
            )
            for task_id in ready_now:
                await store.transition_task(conn, task_id, "READY", reason="dependencies_succeeded")
            due = (
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM tasks WHERE run_id = :r AND status = 'RETRY_WAIT' AND next_attempt_at <= now()"
                        ),
                        {"r": run_id},
                    )
                )
                .scalars()
                .all()
            )
            for task_id in due:
                await store.transition_task(conn, task_id, "READY", reason="backoff_elapsed")
            tasks = await _tasks(conn, run_id)
            statuses = [t["status"] for t in tasks]
            failed = [t for t in tasks if t["status"] == "FAILED"]
            if failed:
                await _cancel_open_tasks(conn, run_id)
                await store.transition_run(
                    conn,
                    run_id,
                    "FAILED",
                    reason="task_failed",
                    extra={
                        "failure_category": failed[0]["failure_category"] or "INTERNAL_ERROR",
                        "failure_message": f"задача {failed[0]['task_key']} завершилась неуспешно",
                    },
                )
                return []
            if statuses and all(s == "SUCCEEDED" for s in statuses):
                await store.transition_run(conn, run_id, "VERIFYING", reason="all_tasks_succeeded")
                # Guard завершения (P6-09, QUALITY_GATES §5.3): обязательные gates PASS на финальном commit с
                # полным evidence, нет открытых блокирующих findings; guard БД (§18.4) — второй рубеж.
                # Сначала compliance.evaluate(run): вердикты контролей на финальном commit пишутся в
                # compliance_results, иначе блокирующие контроли (§5.3 п.6) guard'ом не учитываются.
                await evaluate_compliance(conn, run_id)
                verdict = await run_completion_verdict(conn, run_id, store=self.artifacts)
                if not verdict.ok:
                    await store.transition_run(
                        conn,
                        run_id,
                        "FAILED",
                        reason="completion_guard_rejected",
                        extra={"failure_category": "GATE_FAILED", "failure_message": verdict.summary()[:2000]},
                    )
                    return []
                await store.transition_run(conn, run_id, "COMPLETED", reason="required_gates_passed")
                return []
            # PENDING с выполненными зависимостями уже переведены в READY выше; оставшиеся PENDING ждут
            # незавершённых предков и сами не продвинутся, если в работе ничего нет (иначе run «висит» в RUNNING).
            active = {"READY", "LEASED", "RUNNING", "VERIFYING", "RETRY_WAIT"}
            blocked = [t for t in tasks if t["status"] == "BLOCKED"]
            if blocked and not any(s in active for s in statuses):
                await store.transition_run(
                    conn,
                    run_id,
                    "BLOCKED",
                    reason="tasks_blocked",
                    extra={"blocked_reason": blocked[0]["blocked_reason"]},
                )
                return []
            return await _dispatchable(conn, run)

    # ---------- task execution ----------
    async def execute_task(self, task_id: uuid.UUID, ws: uuid.UUID) -> None:
        context.bind(task_id=task_id)
        async with transaction(self.engine, self._ctx(ws)) as conn:
            task = await _task_row(conn, task_id)
            if task is None or task["status"] != "READY":
                return
            run = await _run_row(conn, task["run_id"])
            if run is None:  # pragma: no cover — FK гарантирует существование run
                return
            if run["status"] != "RUNNING":
                return
            # Лимит параллелизма и приоритет критического пути (DATA_FLOW §4.2) — под блокировкой run,
            # т.к. сообщения exec приходят на любые READY-задачи; лишние остаются READY до освобождения слота.
            await _run_row(conn, task["run_id"], lock=True)
            if task_id not in await _dispatchable(conn, run):
                return
            # Router (ROUTING_POLICY §4–§5): фильтры, независимость ревью, скоринг; решение — в routing_decisions.
            decision = await routing_service.select(conn, task=task, run=run, now=utcnow())
            if decision.outcome == "WAIT":
                return  # все подходящие установки заняты — задача остаётся READY до освобождения слота
            if decision.outcome == "BLOCKED":
                # Нет допустимого провайдера ⇒ BLOCKED без обхода лимитов (ТЗ §5, AC-16); MANUAL_REVIEW — approval.
                await store.transition_task(
                    conn, task_id, "BLOCKED", reason="routing", extra={"blocked_reason": decision.blocked_reason}
                )
                if decision.needs_approval == "MANUAL_REVIEW":
                    await self._request_task_approval(conn, task, run, "MANUAL_REVIEW", decision)
                return
            if decision.needs_approval == "SELF_REVIEW_FALLBACK" and not await _approved(
                conn, task_id, "SELF_REVIEW_FALLBACK"
            ):
                # Самопроверка только после явного одобрения человеком; маркировка self_review (ТЗ §4).
                await store.transition_task(
                    conn, task_id, "BLOCKED", reason="routing", extra={"blocked_reason": "APPROVAL_REQUIRED"}
                )
                await self._request_task_approval(conn, task, run, "SELF_REVIEW_FALLBACK", decision)
                return
            if decision.selected is None:  # pragma: no cover — SELECTED/FALLBACK всегда несут выбор
                raise RuntimeError("router: решение без выбранной установки")
            provider = decision.selected.provider
            installation_id = decision.selected.installation_id
            lease = await leases.acquire(
                conn,
                resource_type="task",
                resource_id=task_id,
                worker_id=self.worker_id,
                workspace_id=ws,
                ttl_seconds=self.lease_ttl_seconds,
            )
            if lease is None:
                return  # задачей уже владеет другой воркер
            # attempt_count — расход бюджета (RATE_LIMITED/LEASE_LOST не считаются), attempt_no — сквозной номер
            # попытки: после «бесплатного» повтора они расходятся, поэтому номер берётся из истории попыток.
            attempt_count = task["attempt_count"] + 1
            attempt_no = (
                1
                + (
                    await conn.execute(
                        text("SELECT COALESCE(max(attempt_no), 0) FROM task_attempts WHERE task_id = :t"),
                        {"t": task_id},
                    )
                ).scalar_one()
            )
            attempt_id = uuid.uuid4()
            from aicd_platform.ids import uuid7

            attempt_id = uuid7()
            await store.transition_task(
                conn,
                task_id,
                "LEASED",
                extra={"lease_fencing_token": lease.fencing_token, "attempt_count": attempt_count},
            )
            await conn.execute(
                text(
                    "INSERT INTO task_attempts (id, workspace_id, task_id, attempt_no, provider, fencing_token)"
                    " VALUES (:id, :ws, :t, :n, :p, :tok)"
                ),
                {"id": attempt_id, "ws": ws, "t": task_id, "n": attempt_no, "p": provider, "tok": lease.fencing_token},
            )
            await store.transition_task(conn, task_id, "RUNNING", reason="execution_started")
        heartbeat = asyncio.create_task(self._heartbeat(lease, ws))
        started = utcnow()
        try:
            outcome = await self.executor.execute(
                task=dict(task),
                run=dict(run),
                attempt_id=attempt_id,
                provider=provider,
                installation_id=installation_id,
                register_cancel=lambda hook: self._cancel_hooks.__setitem__(task_id, hook),
            )
        except Exception as exc:
            log.exception("executor failure")
            outcome = TaskExecution("failed", "WORKER_CRASH", None, provider, None, None, uuid.uuid4())
            del exc
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self._cancel_hooks.pop(task_id, None)
        spent = int((utcnow() - started).total_seconds())
        await self._finish_attempt(task_id, ws, lease, attempt_id, outcome, spent, run)
        await self.tick(task["run_id"], ws)

    async def _heartbeat(self, lease: leases.Lease, ws: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self.lease_ttl_seconds / 3)
            async with transaction(self.engine, self._ctx(ws)) as conn:
                if not await leases.heartbeat(conn, lease, self.lease_ttl_seconds):
                    return

    async def _finish_attempt(
        self,
        task_id: uuid.UUID,
        ws: uuid.UUID,
        lease: leases.Lease,
        attempt_id: uuid.UUID,
        outcome: TaskExecution,
        spent: int,
        run: dict[str, Any],
    ) -> None:
        verification: VerificationOutcome | None = None
        async with transaction(self.engine, self._ctx(ws)) as conn:
            current = await _task_row(conn, task_id)
            if (
                current is not None
                and current["status"] == "BLOCKED"
                and await leases.is_current(conn, "task", task_id, lease.fencing_token)
            ):
                # Задачу заблокировали во время исполнения (исчерпан лимит review/fix циклов, ACP §6): попытка
                # закрывается без перехода к проверке, решение — за человеком (approval MANUAL_REVIEW).
                await _finish_attempt_row(conn, attempt_id, "CANCELLED", None)
                await leases.release(conn, lease)
                return
        rejected_category = _agent_result_failure(outcome)
        io = outcome.agent_io
        if outcome.status == "completed" and rejected_category is None and io is not None and io.accepted_message_ids:
            # Агент заявил BLOCKED(DOCS_REQUIRED): до проверок — решение политики документации (ACP §9.4).
            async with transaction(self.engine, self._ctx(ws)) as conn:
                if await docs_flow.docs_required(conn, io.accepted_message_ids):
                    if not await leases.is_current(conn, "task", task_id, lease.fencing_token):
                        return
                    task = await _task_row(conn, task_id)
                    if task is not None and task["status"] == "RUNNING":
                        await self._on_docs_required(conn, task, run, attempt_id, spent)
                    await leases.release(conn, lease)
                    return
        read_only = role_profile(current["agent_role"]).permissions.write_scope == "none" if current else False
        if outcome.status == "completed" and rejected_category is None and outcome.head_commit_sha:
            async with transaction(self.engine, self._ctx(ws)) as conn:
                if not await leases.is_current(conn, "task", task_id, lease.fencing_token):
                    return  # аренда потеряна: результат «зомби»-воркера не записывается
                await store.transition_task(conn, task_id, "VERIFYING", reason="execution_collected")
            verification = await self.verifier.verify_task(
                workspace_id=ws,
                run_id=run["id"],
                task_id=task_id,
                worktree_path=outcome.worktree_path,
                commit_sha=outcome.head_commit_sha,
                quality_profile=run["quality_profile"],
            )
            if not verification.passed:
                log.warning("verification failed", extra={"task_id": str(task_id), "summary": verification.summary})
        async with transaction(self.engine, self._ctx(ws)) as conn:
            if not await leases.is_current(conn, "task", task_id, lease.fencing_token):
                return
            task = await _task_row(conn, task_id)
            if task is None:  # pragma: no cover
                return
            analysis_done = (
                read_only
                and outcome.status == "completed"
                and rejected_category is None
                and not outcome.head_commit_sha
            )
            if task["status"] == "CANCELLED":
                await _finish_attempt_row(conn, attempt_id, "CANCELLED", None)
            elif (verification is not None and verification.passed) or analysis_done:
                # Роли без права записи (reviewer, security-анализ…) не меняют код: их итог — валидный result.json
                # и принятые ACP-сообщения; гейты проверяют изменения реализующих задач (QUALITY_GATES).
                reason = "gates_passed" if verification is not None else "analysis_completed"
                await store.transition_task(conn, task_id, "SUCCEEDED", reason=reason)
                await _finish_attempt_row(conn, attempt_id, "SUCCEEDED", None)
                if outcome.agent_io is not None and outcome.agent_io.review_verdict == "changes_requested":
                    # Ревью вернуло замечания: fix + повторное ревью — в той же транзакции (ACP §9.2).
                    await review_flow.open_fix_loop(conn, review_task_id=task_id, fix_request_id=None)
            else:
                category = (
                    rejected_category
                    or ("GATE_FAILED" if verification is not None else None)
                    or outcome.failure_category
                    or ("CANCELLED" if outcome.status == "cancelled" else "PROCESS_FAILED")
                )
                await self._after_failure(conn, task, attempt_id, category, outcome, spent, run)
            await leases.release(conn, lease)

    async def _after_failure(
        self,
        conn: AsyncConnection,
        task: dict[str, Any],
        attempt_id: uuid.UUID,
        category: str,
        outcome: TaskExecution,
        spent: int,
        run: dict[str, Any],
    ) -> None:
        task_id = task["id"]
        if category == "CANCELLED":
            current_run = await store.load_run(conn, task["run_id"])
            await _finish_attempt_row(conn, attempt_id, "CANCELLED", None)
            if current_run is not None and current_run.status == "PAUSED":
                # Отмена из-за паузы не расходует бюджет попыток (DATA_FLOW §6.2).
                await store.transition_task(
                    conn,
                    task_id,
                    "BLOCKED",
                    reason="run_paused",
                    extra={"blocked_reason": "RUN_PAUSED", "attempt_count": max(task["attempt_count"] - 1, 0)},
                )
            else:
                await store.transition_task(conn, task_id, "CANCELLED", reason="cancelled")
            return
        await fail_attempt(conn, task, attempt_id, category, spent, retry_after=outcome.retry_after)

    # ---------- drive (тесты / E2E без брокера) ----------
    async def drive(
        self, run_id: uuid.UUID, ws: uuid.UUID, *, max_steps: int = 50, fast_forward_backoff: bool = False
    ) -> str:
        """Синхронный прогон run до терминального/ожидающего состояния (без очереди сообщений)."""
        for _ in range(max_steps):
            ready = await self.tick(run_id, ws)
            if not ready and fast_forward_backoff:
                async with transaction(self.engine, self._ctx(ws)) as conn:
                    moved = await conn.execute(
                        text(
                            "UPDATE tasks SET next_attempt_at = now() - interval '1 second', version = version + 1"
                            " WHERE run_id = :r AND status = 'RETRY_WAIT'"
                        ),
                        {"r": run_id},
                    )
                if moved.rowcount:
                    continue
            if not ready:
                break
            for task_id in ready:
                await self.execute_task(task_id, ws)
        async with transaction(self.engine, self._ctx(ws)) as conn:
            final = await store.load_run(conn, run_id)
        return final.status if final else "UNKNOWN"


_RISK = ["low", "medium", "high", "critical"]


def _plan_needs_approval(policy: str, specs: list[Any]) -> bool:
    if policy == "always":
        return True
    high = any(s.complexity >= 4 or s.risk_level in ("high", "critical") for s in specs)
    # L4/L5 всегда требуют human approval независимо от политики (ТЗ §5).
    return high


async def _run_row(conn: AsyncConnection, run_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT id, workspace_id, project_id, repository_id, requested_by, mode, objective, status, base_commit_sha,"  # noqa: S608 — константа
                    " quality_profile, approval_policy, documentation_mode, policy_snapshot, plan_version, approved_plan_version,"
                    " version"
                    " FROM runs WHERE id = :id" + (" FOR UPDATE" if lock else "")
                ),
                {"id": run_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def _task_row(conn: AsyncConnection, task_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT t.*, (SELECT a.failure_category FROM task_attempts a WHERE a.task_id = t.id"
                    "  ORDER BY a.attempt_no DESC LIMIT 1) AS failure_category FROM tasks t WHERE t.id = :id"
                ),
                {"id": task_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def _tasks(conn: AsyncConnection, run_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT t.id, t.task_key, t.status, t.blocked_reason, (SELECT a.failure_category FROM task_attempts a"
                    "  WHERE a.task_id = t.id ORDER BY a.attempt_no DESC LIMIT 1) AS failure_category"
                    " FROM tasks t WHERE t.run_id = :r AND t.superseded_at IS NULL ORDER BY t.created_at"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def _cancel_open_tasks(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    open_ids = (
        (
            await conn.execute(
                text("SELECT id FROM tasks WHERE run_id = :r AND status IN ('PENDING','READY','RETRY_WAIT','BLOCKED')"),
                {"r": run_id},
            )
        )
        .scalars()
        .all()
    )
    for task_id in open_ids:
        await store.transition_task(conn, task_id, "CANCELLED", reason="run_terminated")


DEFAULT_MAX_PARALLEL_TASKS = 4
DEFAULT_PLANNER_ESTIMATE = 3


async def _dispatchable(conn: AsyncConnection, run: dict[str, Any]) -> list[uuid.UUID]:
    """READY-задачи, которые можно запускать сейчас: по убыванию длины критического пути, не больше свободных
    слотов `limits.max_parallel_tasks` из policy snapshot run (DATA_FLOW §4.2)."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT id, task_key, agent_role, complexity, status, created_at FROM tasks"
                    " WHERE run_id = :r AND superseded_at IS NULL"
                ),
                {"r": run["id"]},
            )
        )
        .mappings()
        .all()
    )
    edges = (
        await conn.execute(
            text(
                "SELECT t.task_key, p.task_key FROM task_dependencies d JOIN tasks t ON t.id = d.task_id"
                " JOIN tasks p ON p.id = d.depends_on_task_id WHERE d.run_id = :r AND t.superseded_at IS NULL"
                " AND p.superseded_at IS NULL"
            ),
            {"r": run["id"]},
        )
    ).all()
    deps: dict[str, list[str]] = {}
    for child, parent in edges:
        deps.setdefault(child, []).append(parent)
    priority = critical_path_lengths(
        [PlannedTask(r["task_key"], r["agent_role"], r["complexity"], tuple(deps.get(r["task_key"], ()))) for r in rows]
    )
    limit = int(
        (run["policy_snapshot"] or {}).get("limits", {}).get("max_parallel_tasks") or DEFAULT_MAX_PARALLEL_TASKS
    )
    active = sum(1 for r in rows if r["status"] in ("LEASED", "RUNNING", "VERIFYING"))
    ready = sorted(
        (r for r in rows if r["status"] == "READY"), key=lambda r: (-priority[r["task_key"]], r["created_at"], r["id"])
    )
    return [r["id"] for r in ready[: max(limit - active, 0)]]


def _agent_result_failure(outcome: TaskExecution) -> str | None:
    """Категория отказа по итогу агента (AGENT_ROLES §3.4, ACP §5); None — итог приемлем или не применим."""
    io = outcome.agent_io
    if outcome.status != "completed" or io is None:
        return None
    if io.attempt_should_fail:
        return "POLICY_DENIED"  # ≥ 3 отказов ACL/POLICY в попытке
    if io.result_status in ("missing", "invalid"):
        return "OUTPUT_PARSE_ERROR"  # обязательный AgentResult отсутствует или отклонён целиком
    if io.result_status == "error":
        return "WORKER_CRASH"  # сбой обработки на стороне платформы — повторяемый
    return None


async def _approved(
    conn: AsyncConnection, task_id: uuid.UUID, action: str, *, statuses: tuple[str, ...] = ("APPROVED",)
) -> bool:
    found = (
        await conn.execute(
            text("SELECT 1 FROM approvals WHERE task_id = :t AND action_type = :a AND status = ANY(:s) LIMIT 1"),
            {"t": task_id, "a": action, "s": list(statuses)},
        )
    ).first()
    return found is not None


async def fail_attempt(
    conn: AsyncConnection,
    task: dict[str, Any],
    attempt_id: uuid.UUID,
    category: str,
    spent: int,
    *,
    retry_after: int | None = None,
) -> None:
    """Неуспешная попытка → RETRY_WAIT / BLOCKED / FAILED по политике retry (DATA_FLOW §3.2).

    Общая для исполнителя и reaper (LEASE_LOST/WORKER_CRASH): один источник правил бюджета попыток.
    """
    task_id = task["id"]
    await _finish_attempt_row(conn, attempt_id, "FAILED", category)
    decision = retry.decide(
        failure_category=category,
        attempt_count=task["attempt_count"],
        max_attempts=task["max_attempts"],
        retry_spent_seconds=task["retry_spent_seconds"] + spent,
        retry_budget_seconds=task["retry_budget_seconds"],
        now=utcnow(),
        retry_after=retry_after,
    )
    spent_total = min(task["retry_spent_seconds"] + spent, 2_000_000_000)
    if decision.action == "retry":
        extra: dict[str, Any] = {"next_attempt_at": decision.next_attempt_at, "retry_spent_seconds": spent_total}
        if not decision.counts_attempt:
            extra["attempt_count"] = max(task["attempt_count"] - 1, 0)
        await store.transition_task(conn, task_id, "RETRY_WAIT", reason=category, extra=extra)
    elif decision.action == "block":
        await store.transition_task(
            conn,
            task_id,
            "BLOCKED",
            reason=category,
            extra={
                "blocked_reason": decision.blocked_reason,
                "retry_spent_seconds": spent_total,
                "attempt_count": max(task["attempt_count"] - 1, 0),
            },
        )
    else:
        await store.transition_task(
            conn, task_id, "FAILED", reason=category, extra={"retry_spent_seconds": spent_total}
        )


async def _finish_attempt_row(conn: AsyncConnection, attempt_id: uuid.UUID, status: str, category: str | None) -> None:
    await conn.execute(
        text(
            "UPDATE task_attempts SET status = :s, failure_category = :c, finished_at = now()"
            " WHERE id = :id AND status = 'RUNNING'"
        ),
        {"s": status, "c": category, "id": attempt_id},
    )


def backoff_for_tests() -> timedelta:  # pragma: no cover — справочно
    return timedelta(seconds=retry.BASE_SECONDS)
