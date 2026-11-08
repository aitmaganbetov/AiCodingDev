"""Reaper аренд и recovery reconciler (DATA_FLOW §5, §7; P3-04, P3-12).

* `reap` (каждые 15 с): истёкшие аренды задач. Переход задачи выполняется только после того, как
  reconciler убедился, что процесс/контейнер исполнения мёртв (или убил его). Если это невозможно
  установить (процесс на другом хосте, kill не удался) — аренда остаётся, задача не трогается
  (лучше подождать, чем запустить вторую копию CLI в том же worktree).
  LEASED → READY; RUNNING/VERIFYING → попытка FAILED(LEASE_LOST) → RETRY_WAIT/FAILED по политике retry.
* `reconcile` (старт воркера + каждые 60 с), по workspace из `sys_recovery_workspaces`:
  1. `cli_executions` в QUEUED/STARTING/RUNNING/EXITED без живой аренды задачи → kill при необходимости → CRASHED;
  2. `worktrees` ACTIVE без исполняющейся задачи → ORPHANED; diff сохраняется в artifacts, затем каталог удаляется;
  3. `external_actions` в EXECUTING без живой аренды → probe внешней системы (ADR-0012): применено ⇒ CONFIRMED,
     нет probe/неизвестно ⇒ остаётся EXECUTING и попадает в отчёт (слепой повтор запрещён, AC-09);
  4. outbox: повторную публикацию выполняет dispatcher (неопубликованные события он выбирает сам);
  5. run без прогресса дольше порога → предупреждение `stuck_run` (алерт по логу/метрике).
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_orchestration import external_actions, store
from aicd_orchestration import worktrees as wt
from aicd_orchestration.engine import fail_attempt
from aicd_platform import audit, leases, outbox
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.paths import resolve_ref
from aicd_security.sandbox import SandboxRunner

log = logging.getLogger("aicd.recovery")

ACTIVE_EXECUTION = ("QUEUED", "STARTING", "RUNNING", "EXITED")
TERMINAL_RUN = ("COMPLETED", "FAILED", "CANCELLED")
ProbeAction = Callable[[external_actions.ExternalAction], Awaitable[external_actions.Outcome | None]]


@dataclass
class RecoveryReport:
    reaped_tasks: list[uuid.UUID] = field(default_factory=list)
    deferred_tasks: list[uuid.UUID] = field(default_factory=list)  # процесс не подтверждён мёртвым
    crashed_executions: list[uuid.UUID] = field(default_factory=list)
    orphaned_worktrees: list[uuid.UUID] = field(default_factory=list)
    removed_worktrees: list[uuid.UUID] = field(default_factory=list)
    reconciled_actions: list[uuid.UUID] = field(default_factory=list)
    unreconciled_actions: list[uuid.UUID] = field(default_factory=list)
    stuck_runs: list[uuid.UUID] = field(default_factory=list)
    runs_to_tick: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)  # (run_id, workspace_id)

    def merge(self, other: RecoveryReport) -> None:
        for name in self.__dataclass_fields__:
            mine, theirs = getattr(self, name), getattr(other, name)
            if isinstance(mine, set):
                mine |= theirs
            else:
                mine.extend(theirs)


def _ctx(ws: uuid.UUID) -> DbContext:
    return DbContext(workspace_id=ws, actor_type="system")


async def _ensure_dead(runners: tuple[SandboxRunner, ...], ref: str | None) -> bool:
    """True — процесс/контейнер по ссылке точно не работает (не был запущен, умер или убит)."""
    if ref is None:
        return True  # процесс не стартовал (QUEUED/STARTING без ref)
    for runner in runners:
        if runner.owns(ref):
            if not await runner.is_alive(ref):
                return True
            return await runner.kill(ref)
    return False  # чужой хост/неизвестный runner: судить нельзя


async def _crash_executions(
    conn: AsyncConnection, attempt_id: uuid.UUID, runners: tuple[SandboxRunner, ...], report: RecoveryReport
) -> bool:
    """Все незавершённые исполнения попытки → CRASHED; False — какой-то процесс может быть жив."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT id, status, pid_ref, container_ref FROM cli_executions WHERE task_attempt_id = :a"
                    " AND status IN ('QUEUED','STARTING','RUNNING','EXITED') FOR UPDATE"
                ),
                {"a": attempt_id},
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        if not await _ensure_dead(runners, row["container_ref"] or row["pid_ref"]):
            return False
    for row in rows:
        await conn.execute(
            text(
                "UPDATE cli_executions SET status = 'CRASHED', failure_category = 'PROCESS_FAILED',"
                " finished_at = now() WHERE id = :id"
            ),
            {"id": row["id"]},
        )
        report.crashed_executions.append(row["id"])
    return True


# --------------------------------------------------------------------------------------- reaper
async def reap(engine: AsyncEngine, *, runners: tuple[SandboxRunner, ...] = (), limit: int = 100) -> RecoveryReport:
    report = RecoveryReport()
    async with transaction(engine, DbContext(actor_type="system")) as conn:
        expired = await leases.expired(conn, "task", limit)
    for task_id, ws in expired:
        if ws is None:  # pragma: no cover — аренды задач всегда с workspace
            continue
        async with transaction(engine, _ctx(ws)) as conn:
            lease = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, fencing_token FROM worker_leases WHERE resource_type = 'task'"
                            " AND resource_id = :t AND released_at IS NULL AND lease_until < now() FOR UPDATE SKIP LOCKED"
                        ),
                        {"t": task_id},
                    )
                )
                .mappings()
                .first()
            )
            if lease is None:
                continue  # продлена heartbeat'ом или уже обработана другим reaper
            task = (
                (await conn.execute(text("SELECT * FROM tasks WHERE id = :id FOR UPDATE"), {"id": task_id}))
                .mappings()
                .first()
            )
            attempt = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, EXTRACT(EPOCH FROM now() - started_at)::int AS spent FROM task_attempts"
                            " WHERE task_id = :t AND status = 'RUNNING' AND fencing_token = :tok"
                        ),
                        {"t": task_id, "tok": lease["fencing_token"]},
                    )
                )
                .mappings()
                .first()
            )
            if attempt is not None and not await _crash_executions(conn, attempt["id"], runners, report):
                report.deferred_tasks.append(task_id)
                log.warning("lease expired but execution may be alive", extra={"task_id": str(task_id)})
                continue
            await conn.execute(text("UPDATE worker_leases SET released_at = now() WHERE id = :id"), {"id": lease["id"]})
            if task is not None and task["status"] == "LEASED":
                await store.transition_task(conn, task_id, "READY", reason="lease_expired")
            elif task is not None and task["status"] in ("RUNNING", "VERIFYING") and attempt is not None:
                await fail_attempt(conn, dict(task), attempt["id"], "LEASE_LOST", max(int(attempt["spent"]), 0))
            if task is not None:
                report.runs_to_tick.add((task["run_id"], ws))
            report.reaped_tasks.append(task_id)
    return report


# ----------------------------------------------------------------------------------- reconciler
@dataclass
class Reconciler:
    engine: AsyncEngine
    artifacts: ArtifactStore
    worktrees: wt.WorktreeManager
    repository_roots: dict[str, Path]
    # worktrees принадлежат хосту исполнения: reconciler хоста видит и чистит только свои каталоги.
    worker_host_id: uuid.UUID | None
    runners: tuple[SandboxRunner, ...] = ()
    probes: Mapping[str, ProbeAction] = field(default_factory=dict)
    stuck_after_seconds: int = 3600
    workspace_limit: int = 200

    async def reconcile(self) -> RecoveryReport:
        report = await reap(self.engine, runners=self.runners)
        async with transaction(self.engine, DbContext(actor_type="system")) as conn:
            workspaces = (
                (
                    await conn.execute(
                        text("SELECT workspace_id FROM sys_recovery_workspaces(:n)"), {"n": self.workspace_limit}
                    )
                )
                .scalars()
                .all()
            )
        for ws in workspaces:
            try:
                report.merge(await self._workspace(ws))
            except Exception:  # одна «битая» запись не должна останавливать восстановление остальных
                log.exception("recovery failed for workspace", extra={"workspace_id": str(ws)})
        return report

    async def _workspace(self, ws: uuid.UUID) -> RecoveryReport:
        report = RecoveryReport()
        await self._executions(ws, report)
        await self._worktrees(ws, report)
        await self._actions(ws, report)
        await self._stuck(ws, report)
        return report

    # 1. исполнения без живой аренды задачи
    async def _executions(self, ws: uuid.UUID, report: RecoveryReport) -> None:
        async with transaction(self.engine, _ctx(ws)) as conn:
            attempts = (
                (
                    await conn.execute(
                        text(
                            "SELECT DISTINCT e.task_attempt_id FROM cli_executions e JOIN task_attempts a ON a.id = e.task_attempt_id"
                            " WHERE e.status IN ('QUEUED','STARTING','RUNNING','EXITED') AND NOT EXISTS ("
                            "  SELECT 1 FROM worker_leases l WHERE l.resource_type = 'task' AND l.resource_id = a.task_id"
                            "  AND l.fencing_token = a.fencing_token AND l.released_at IS NULL AND l.lease_until >= now())"
                        )
                    )
                )
                .scalars()
                .all()
            )
        for attempt_id in attempts:
            async with transaction(self.engine, _ctx(ws)) as conn:
                await _crash_executions(conn, attempt_id, self.runners, report)

    # 2. worktree без исполняющейся задачи
    async def _worktrees(self, ws: uuid.UUID, report: RecoveryReport) -> None:
        async with transaction(self.engine, _ctx(ws)) as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT w.id, w.run_id, w.task_id, w.base_commit_sha, w.path_ref, r.local_root_ref, rn.project_id"
                            " FROM worktrees w JOIN repositories r ON r.id = w.repository_id JOIN runs rn ON rn.id = w.run_id"
                            " LEFT JOIN tasks t ON t.id = w.task_id"
                            " WHERE w.worker_host_id IS NOT DISTINCT FROM :host AND w.status IN ('ACTIVE','ORPHANED')"
                            " AND (w.status = 'ORPHANED' OR t.id IS NULL OR t.status NOT IN ('LEASED','RUNNING','VERIFYING'))"
                        ),
                        {"host": self.worker_host_id},
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                await wt.mark_status(conn, row["id"], "ORPHANED")
                report.orphaned_worktrees.append(row["id"])
        for row in rows:
            await self._preserve_and_remove(ws, dict(row), report)

    async def _preserve_and_remove(self, ws: uuid.UUID, row: dict[str, Any], report: RecoveryReport) -> None:
        path = self.worktrees.path_for(row["id"])
        artifact_id: uuid.UUID | None = None
        if path.is_dir():
            try:
                diff = await asyncio.to_thread(self.worktrees.diff, path, row["base_commit_sha"])
            except (wt.WorktreeError, OSError, ValueError):
                log.exception("orphan worktree diff failed", extra={"worktree_id": str(row["id"])})
                return  # без сохранённого diff не удаляем (DATA_FLOW §7.2)
            if diff.strip():
                artifact_id = uuid7()
                stored = self.artifacts.put(ws, artifact_id, io.BytesIO(diff))
                async with transaction(self.engine, _ctx(ws)) as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO artifacts (id, workspace_id, project_id, run_id, kind, storage_ref, sha256,"
                            " size_bytes, mime_type, is_sensitive, redaction_applied)"
                            " VALUES (:id, :ws, :p, :r, 'git_diff', :ref, :sha, :size, 'text/x-diff', true, false)"
                        ),
                        {
                            "id": artifact_id,
                            "ws": ws,
                            "p": row["project_id"],
                            "r": row["run_id"],
                            "ref": stored.storage_ref,
                            "sha": stored.sha256,
                            "size": stored.size_bytes,
                        },
                    )
            try:
                repo = resolve_ref(row["local_root_ref"], self.repository_roots)
                await asyncio.to_thread(self.worktrees.remove, repo, path)
            except Exception:
                log.exception("orphan worktree removal failed", extra={"worktree_id": str(row["id"])})
                return
        async with transaction(self.engine, _ctx(ws)) as conn:
            await wt.mark_status(conn, row["id"], "REMOVED")
            await outbox.emit(
                conn,
                event_type="worktree.orphan_removed",
                aggregate_type="worktree",
                aggregate_id=row["id"],
                payload={"diff_artifact_id": str(artifact_id) if artifact_id else None, "task_id": str(row["task_id"])},
                workspace_id=ws,
                run_id=row["run_id"],
            )
            await audit.record(
                conn,
                action="worktree.orphan_removed",
                resource_type="worktree",
                resource_id=row["id"],
                actor_type="system",
                actor_id=None,
                workspace_id=ws,
                metadata={"diff_artifact_id": str(artifact_id) if artifact_id else None},
            )
        report.removed_worktrees.append(row["id"])

    # 3. внешние действия, прерванные во время вызова
    async def _actions(self, ws: uuid.UUID, report: RecoveryReport) -> None:
        async with transaction(self.engine, _ctx(ws)) as conn:
            ids = (
                (
                    await conn.execute(
                        text(
                            "SELECT x.id FROM external_actions x WHERE x.status = 'EXECUTING' AND NOT EXISTS ("
                            " SELECT 1 FROM worker_leases l WHERE l.fencing_token = x.fencing_token AND l.released_at IS NULL"
                            " AND l.lease_until >= now())"
                        )
                    )
                )
                .scalars()
                .all()
            )
        for action_id in ids:
            async with transaction(self.engine, _ctx(ws)) as conn:
                action = await external_actions.get(conn, action_id, lock=True)
                if action is None or action.status != "EXECUTING" or action.fencing_token is None:
                    continue
                probe = self.probes.get(action.action_type)
                observed = await probe(action) if probe is not None else None
                if observed is None:
                    report.unreconciled_actions.append(action_id)
                    log.warning(
                        "external action needs reconciliation",
                        extra={"action_id": str(action_id), "action_type": action.action_type},
                    )
                    continue
                await external_actions.confirm(
                    conn,
                    action_id,
                    fencing_token=action.fencing_token,
                    external_ref=observed.external_ref,
                    result={**observed.result, "reconciled": True},
                )
                report.reconciled_actions.append(action_id)

    # 5. «зависшие» run
    async def _stuck(self, ws: uuid.UUID, report: RecoveryReport) -> None:
        async with transaction(self.engine, _ctx(ws)) as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT r.id FROM runs r WHERE r.status NOT IN ('COMPLETED','FAILED','CANCELLED','AWAITING_APPROVAL',"
                            " 'PAUSED','BLOCKED') AND r.updated_at < now() - make_interval(secs => :s) AND NOT EXISTS ("
                            " SELECT 1 FROM tasks t WHERE t.run_id = r.id AND t.updated_at >= now() - make_interval(secs => :s))"
                        ),
                        {"s": self.stuck_after_seconds},
                    )
                )
                .scalars()
                .all()
            )
        for run_id in rows:
            log.warning("stuck_run", extra={"run_id": str(run_id), "workspace_id": str(ws)})
            report.stuck_runs.append(run_id)
            report.runs_to_tick.add((run_id, ws))
