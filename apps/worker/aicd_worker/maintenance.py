"""Периодические работы воркера-оркестратора (DATA_FLOW §5, §7).

* reaper аренд (каждые 15 с) и reconciler (при старте и каждые 60 с) — доменная логика восстановления живёт
  в `aicd_orchestration.recovery` (P3-12); выполняются на воркере exec (процессы CLI и worktrees — его), после
  чего затронутые run продвигаются tick'ом;
* tick для run с просроченными RETRY_WAIT и повторная постановка «забытых» READY в очередь exec
  (потерянное сообщение не оставляет задачу без исполнения). Межарендаторная выборка — только через
  SECURITY DEFINER `sys_schedulable_workspaces`;
* сверка прерванных GitHub-действий ledger (OQ-47 (7), `aicd_orchestration.github_recovery`) — на воркере
  orchestrator (там GitHub App), при старте и каждые 60 с: probe → CONFIRMED и продолжение workflow без
  ожидания повторной доставки Dramatiq.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import text

from aicd_orchestration import github_recovery
from aicd_orchestration.recovery import RecoveryReport, reap
from aicd_platform.db import DbContext, transaction
from aicd_worker.handlers import execution_message
from aicd_worker.runtime import Runtime

log = logging.getLogger("aicd.worker.maintenance")

REAP_INTERVAL_SECONDS = 15.0
RECONCILE_INTERVAL_SECONDS = 60.0
GITHUB_RECONCILE_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class SweepResult:
    reaped: int = 0
    reconciled: bool = False
    ticked_runs: int = 0
    redispatched: int = 0


@dataclass
class Scheduler:
    """Расписание reap/reconcile по монотонным часам; оба — сразу при первом вызове (старт воркера)."""

    reap_interval: float = REAP_INTERVAL_SECONDS
    reconcile_interval: float = RECONCILE_INTERVAL_SECONDS
    next_reap: float = 0.0
    next_reconcile: float = 0.0

    async def run_due(self, rt: Runtime) -> tuple[int, bool]:
        if rt.reconciler is None:
            return 0, False
        now = time.monotonic()
        report: RecoveryReport | None = None
        reconciled = now >= self.next_reconcile
        if reconciled:  # reconcile включает reap
            self.next_reconcile = now + self.reconcile_interval
            self.next_reap = now + self.reap_interval
            report = await rt.reconciler.reconcile()
        elif now >= self.next_reap:
            self.next_reap = now + self.reap_interval
            report = await reap(rt.engine, runners=rt.reconciler.runners)
        if report is None:
            return 0, False
        for run_id, ws in sorted(report.runs_to_tick):
            await rt.orchestrator.tick(run_id, ws)
        return len(report.reaped_tasks), reconciled


@dataclass
class GitHubRecoverySchedule:
    """Сверка GitHub-действий: сразу при первом вызове (старт воркера), далее по интервалу."""

    interval: float = GITHUB_RECONCILE_INTERVAL_SECONDS
    next_run: float = 0.0

    async def run_due(self, rt: Runtime) -> github_recovery.GitHubRecoveryReport | None:
        if rt.github is None:
            return None
        now = time.monotonic()
        if now < self.next_run:
            return None
        self.next_run = now + self.interval
        return await github_recovery.reconcile(rt.github)


async def sweep(rt: Runtime, scheduler: Scheduler | None = None, *, workspace_limit: int = 50) -> SweepResult:
    reaped, reconciled = await (scheduler or Scheduler()).run_due(rt)
    async with transaction(rt.engine, DbContext(actor_type="system")) as conn:
        workspaces = (
            await conn.execute(text("SELECT workspace_id FROM sys_schedulable_workspaces(:n)"), {"n": workspace_limit})
        ).scalars().all()  # fmt: skip
    ticked = redispatched = 0
    for ws in workspaces:
        async with transaction(rt.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            runs = (await conn.execute(text(
                "SELECT DISTINCT t.run_id FROM tasks t JOIN runs r ON r.id = t.run_id WHERE r.status = 'RUNNING'"
                " AND t.status = 'RETRY_WAIT' AND t.next_attempt_at <= now()"))).scalars().all()  # fmt: skip
            stale = (await conn.execute(text(
                "SELECT t.id FROM tasks t JOIN runs r ON r.id = t.run_id WHERE r.status = 'RUNNING'"
                " AND t.status = 'READY' AND t.updated_at < now() - make_interval(secs => :s)"),
                {"s": rt.stale_ready_seconds})).scalars().all()  # fmt: skip
        for run_id in runs:
            await rt.orchestrator.tick(run_id, ws)  # RETRY_WAIT→READY порождает события ⇒ exec
            ticked += 1
        for task_id in stale:
            await rt.dispatch_execution(execution_message(task_id, ws))
            redispatched += 1
    return SweepResult(reaped, reconciled, ticked, redispatched)
