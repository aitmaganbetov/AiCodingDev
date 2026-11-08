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

from aicd_orchestration import directives, plans, retry, store
from aicd_orchestration.planner import template_plan
from aicd_platform import context, leases
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction

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


class ProviderUnavailableError(Exception):
    def __init__(self, reason: str = "PROVIDER_UNAVAILABLE") -> None:
        super().__init__(reason)
        self.reason = reason


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


class TaskExecutorPort(Protocol):
    async def select_provider(self, *, workspace_id: uuid.UUID, allowed: list[str], role: str) -> str: ...

    async def execute(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        attempt_id: uuid.UUID,
        provider: str,
        register_cancel: Callable[[Callable[[str], Awaitable[None]]], None],
    ) -> TaskExecution: ...


# --- Оркестратор ----------------------------------------------------------------------------------


@dataclass
class Orchestrator:
    engine: AsyncEngine
    approvals: ApprovalsPort
    verifier: VerifierPort
    executor: TaskExecutorPort
    worker_id: str = field(default_factory=lambda: leases.worker_identity("orchestrator"))
    lease_ttl_seconds: int = 120
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
            specs = template_plan(run["mode"], run["objective"])
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
            else:
                await store.transition_run(conn, run_id, "CANCELLED", reason="plan_rejected")
                await _cancel_open_tasks(conn, run_id)
        if approved:
            await self.tick(run_id, ws)

    async def on_run_paused_or_cancelled(self, run_id: uuid.UUID, ws: uuid.UUID, status: str) -> None:
        """Safe point: работающие исполнения run останавливаются штатно (DATA_FLOW §6.2)."""
        async with transaction(self.engine, self._ctx(ws)) as conn:
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

    # ---------- scheduler ----------
    async def tick(self, run_id: uuid.UUID, ws: uuid.UUID) -> list[uuid.UUID]:
        """Продвигает DAG: PENDING→READY, RETRY_WAIT→READY, завершение/блокировка run. Возвращает READY."""
        async with transaction(self.engine, self._ctx(ws)) as conn:
            run = await _run_row(conn, run_id, lock=True)
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
                # v0: run-level gates = сумма task gates; guard БД проверяет PASS/approvals (§18.4).
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
            ready = (
                (
                    await conn.execute(
                        text("SELECT id FROM tasks WHERE run_id = :r AND status = 'READY' ORDER BY created_at"),
                        {"r": run_id},
                    )
                )
                .scalars()
                .all()
            )
            return list(ready)

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
            allowed = list(run["policy_snapshot"].get("routing", {}).get("allowed_providers", ["codex", "claude"]))
            try:
                provider = await self.executor.select_provider(
                    workspace_id=ws, allowed=allowed, role=task["agent_role"]
                )
            except ProviderUnavailableError as exc:
                # Оба CLI недоступны ⇒ BLOCKED без обхода лимитов (ТЗ §5, AC-16).
                await store.transition_task(
                    conn, task_id, "BLOCKED", reason="no_provider", extra={"blocked_reason": exc.reason}
                )
                return
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
            attempt_no = task["attempt_count"] + 1
            attempt_id = uuid.uuid4()
            from aicd_platform.ids import uuid7

            attempt_id = uuid7()
            await store.transition_task(
                conn, task_id, "LEASED", extra={"lease_fencing_token": lease.fencing_token, "attempt_count": attempt_no}
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
        if outcome.status == "completed" and outcome.head_commit_sha:
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
            if task["status"] == "CANCELLED":
                await _finish_attempt_row(conn, attempt_id, "CANCELLED", None)
            elif verification is not None and verification.passed:
                await store.transition_task(conn, task_id, "SUCCEEDED", reason="gates_passed")
                await _finish_attempt_row(conn, attempt_id, "SUCCEEDED", None)
            else:
                category = (
                    "GATE_FAILED"
                    if verification is not None
                    else outcome.failure_category
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
        await _finish_attempt_row(conn, attempt_id, "FAILED", category)
        source = task["status"]
        decision = retry.decide(
            failure_category=category,
            attempt_count=task["attempt_count"],
            max_attempts=task["max_attempts"],
            retry_spent_seconds=task["retry_spent_seconds"] + spent,
            retry_budget_seconds=task["retry_budget_seconds"],
            now=utcnow(),
            retry_after=outcome.retry_after,
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
            del source
            await store.transition_task(
                conn, task_id, "FAILED", reason=category, extra={"retry_spent_seconds": spent_total}
            )

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
                    " quality_profile, approval_policy, policy_snapshot, plan_version, approved_plan_version, version"
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
