"""Периодические работы оркестратора: reaper аренд и tick для просроченных RETRY_WAIT / «забытых» READY.

Страховка at-least-once: потерянные сообщения/упавшие воркеры не оставляют run без движения
(DATA_FLOW §5, §7). Межарендаторная выборка — только через SECURITY DEFINER `sys_schedulable_workspaces`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_orchestration import retry, store
from aicd_platform import leases
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_worker.handlers import execution_message
from aicd_worker.runtime import Runtime

log = logging.getLogger("aicd.worker.maintenance")


@dataclass(frozen=True)
class SweepResult:
    reaped: int = 0
    ticked_runs: int = 0
    redispatched: int = 0


async def recover_lost_task(rt: Runtime, task_id: uuid.UUID, ws: uuid.UUID) -> str | None:
    """Задача, чья аренда истекла: LEASED→READY, RUNNING/VERIFYING→RETRY_WAIT(LEASE_LOST) или FAILED по бюджету."""
    async with transaction(rt.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        task = (
            (
                await conn.execute(
                    text(
                        "SELECT status, attempt_count, max_attempts, retry_spent_seconds, retry_budget_seconds"
                        " FROM tasks WHERE id = :id FOR UPDATE"
                    ),
                    {"id": task_id},
                )
            )
            .mappings()
            .first()
        )
        if task is None:
            return None
        status = task["status"]
        if status == "LEASED":
            await store.transition_task(
                conn,
                task_id,
                "READY",
                reason="lease_expired",
                extra={"attempt_count": max(task["attempt_count"] - 1, 0)},
            )
            await _close_attempt(conn, task_id)
            return "READY"
        if status not in ("RUNNING", "VERIFYING"):
            return None
        await _close_attempt(conn, task_id)
        decision = retry.decide(
            failure_category="LEASE_LOST",
            attempt_count=task["attempt_count"],
            max_attempts=task["max_attempts"],
            retry_spent_seconds=task["retry_spent_seconds"],
            retry_budget_seconds=task["retry_budget_seconds"],
            now=utcnow(),
        )
        if decision.action == "retry":
            await store.transition_task(
                conn, task_id, "RETRY_WAIT", reason="LEASE_LOST", extra={"next_attempt_at": decision.next_attempt_at}
            )
            return "RETRY_WAIT"
        if decision.action == "block":
            await store.transition_task(
                conn, task_id, "BLOCKED", reason="LEASE_LOST", extra={"blocked_reason": decision.blocked_reason}
            )
            return "BLOCKED"
        await store.transition_task(conn, task_id, "FAILED", reason="LEASE_LOST")
        return "FAILED"


async def _close_attempt(conn: AsyncConnection, task_id: uuid.UUID) -> None:
    await conn.execute(
        text(
            "UPDATE task_attempts SET status = 'FAILED', failure_category = 'LEASE_LOST', finished_at = now()"
            " WHERE task_id = :t AND status = 'RUNNING'"
        ),
        {"t": task_id},
    )


async def sweep(rt: Runtime, *, workspace_limit: int = 50) -> SweepResult:
    reaped = 0
    for task_id, ws in await leases.reap_expired_leases(rt.engine, "task"):
        if ws is None:
            continue
        try:
            if await recover_lost_task(rt, task_id, ws) is not None:
                reaped += 1
        except Exception:
            log.exception("lease recovery failed", extra={"task_id": str(task_id)})
    async with transaction(rt.engine, DbContext(actor_type="system")) as conn:
        workspaces = (
            (
                await conn.execute(
                    text("SELECT workspace_id FROM sys_schedulable_workspaces(:n)"), {"n": workspace_limit}
                )
            )
            .scalars()
            .all()
        )
    ticked = redispatched = 0
    for ws in workspaces:
        async with transaction(rt.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            runs = (
                (
                    await conn.execute(
                        text(
                            "SELECT DISTINCT t.run_id FROM tasks t JOIN runs r ON r.id = t.run_id WHERE r.status = 'RUNNING'"
                            " AND t.status = 'RETRY_WAIT' AND t.next_attempt_at <= now()"
                        )
                    )
                )
                .scalars()
                .all()
            )
            stale = (
                (
                    await conn.execute(
                        text(
                            "SELECT t.id FROM tasks t JOIN runs r ON r.id = t.run_id WHERE r.status = 'RUNNING'"
                            " AND t.status = 'READY' AND t.updated_at < now() - make_interval(secs => :s)"
                        ),
                        {"s": rt.stale_ready_seconds},
                    )
                )
                .scalars()
                .all()
            )
        for run_id in runs:
            await rt.orchestrator.tick(run_id, ws)  # RETRY_WAIT→READY порождает события ⇒ exec
            ticked += 1
        for task_id in stale:
            await rt.dispatch_execution(execution_message(task_id, ws))
            redispatched += 1
    return SweepResult(reaped, ticked, redispatched)
