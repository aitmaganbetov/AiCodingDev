"""Переходы состояний run/task в БД (ADR-0007): условный UPDATE + outbox в одной транзакции.

Каждый переход: проверка таблицы FSM в коде → `UPDATE … WHERE id AND status = :from [AND version = :v]`
→ событие `run.state_changed` / `task.state_changed` в outbox. Триггер `allowed_transitions`
в БД — второй рубеж.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_orchestration import fsm
from aicd_platform import outbox


class VersionConflictError(Exception):
    def __init__(self, current_version: int | None, current_status: str | None) -> None:
        super().__init__("состояние изменилось")
        self.current_version = current_version
        self.current_status = current_status


@dataclass(frozen=True)
class RunState:
    id: uuid.UUID
    workspace_id: uuid.UUID
    status: str
    version: int
    resume_status: str | None
    plan_version: int
    approved_plan_version: int | None


async def load_run(conn: AsyncConnection, run_id: uuid.UUID, *, lock: bool = False) -> RunState | None:
    row = (
        await conn.execute(
            text(
                "SELECT id, workspace_id, status, version, resume_status, plan_version, approved_plan_version"
                " FROM runs WHERE id = :id" + (" FOR UPDATE" if lock else "")
            ),
            {"id": run_id},
        )
    ).mappings().first()
    return RunState(**row) if row else None


async def transition_run(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    target: str,
    *,
    expected_version: int | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> RunState:
    """Переход run с проверкой FSM и optimistic locking; пишет outbox `run.state_changed`.

    `extra` — дополнительные колонки (blocked_reason, resume_status, failure_*, …), имена из allowlist.
    """
    current = await load_run(conn, run_id, lock=True)
    if current is None:
        raise LookupError(str(run_id))
    if expected_version is not None and current.version != expected_version:
        raise VersionConflictError(current.version, current.status)
    fsm.require_transition("run", current.status, target)
    fields: dict[str, Any] = dict(extra or {})
    unknown = set(fields) - _RUN_COLUMNS
    if unknown:
        raise ValueError(f"недопустимые колонки: {unknown}")
    # Ограничения CHECK: blocked_reason только в BLOCKED, resume_status только в PAUSED/BLOCKED.
    fields.setdefault("blocked_reason", fields.get("blocked_reason") if target == "BLOCKED" else None)
    if target in ("PAUSED", "BLOCKED"):
        fields.setdefault("resume_status", current.status if current.status in fsm.RESUMABLE_RUN_STATES
                          else current.resume_status)
    else:
        fields["resume_status"] = None
    if target in fsm.TERMINAL["run"]:
        fields["finished_at"] = _NOW
    if target == "RUNNING" and current.status in ("PLANNED", "AWAITING_APPROVAL"):
        fields.setdefault("started_at", _NOW)
    sets = ", ".join(f"{k} = now()" if v is _NOW else f"{k} = :{k}" for k, v in fields.items())
    params = {k: v for k, v in fields.items() if v is not _NOW}
    result = await conn.execute(
        text(
            f"UPDATE runs SET status = :target, version = version + 1{', ' + sets if sets else ''}"  # noqa: S608 — колонки из allowlist
            " WHERE id = :id AND status = :from AND version = :v"
        ),
        {**params, "target": target, "id": run_id, "from": current.status, "v": current.version},
    )
    if result.rowcount != 1:
        raise VersionConflictError(None, None)
    await outbox.emit(
        conn, event_type="run.state_changed", aggregate_type="run", aggregate_id=run_id,
        workspace_id=current.workspace_id, run_id=run_id,
        payload={"from": current.status, "to": target, "reason": reason, "version": current.version + 1,
                 "blocked_reason": fields.get("blocked_reason")},
    )
    if target in fsm.TERMINAL["run"]:
        await outbox.emit(
            conn, event_type="run.completed", aggregate_type="run", aggregate_id=run_id,
            workspace_id=current.workspace_id, run_id=run_id, payload={"final_status": target},
        )
    new = await load_run(conn, run_id)
    if new is None:  # pragma: no cover
        raise LookupError(str(run_id))
    return new


async def transition_task(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    target: str,
    *,
    expected_from: str | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Переход задачи; возвращает предыдущее состояние. Пишет outbox `task.state_changed`."""
    row = (
        await conn.execute(
            text("SELECT workspace_id, run_id, status, version FROM tasks WHERE id = :id FOR UPDATE"), {"id": task_id}
        )
    ).mappings().first()
    if row is None:
        raise LookupError(str(task_id))
    if expected_from is not None and row["status"] != expected_from:
        raise VersionConflictError(row["version"], row["status"])
    fsm.require_transition("task", row["status"], target)
    fields: dict[str, Any] = dict(extra or {})
    unknown = set(fields) - _TASK_COLUMNS
    if unknown:
        raise ValueError(f"недопустимые колонки: {unknown}")
    fields.setdefault("blocked_reason", None)
    if target != "BLOCKED":
        fields["blocked_reason"] = None
    if target != "RETRY_WAIT":
        fields.setdefault("next_attempt_at", None)
    if target in fsm.TERMINAL["task"]:
        fields["finished_at"] = _NOW
    if target == "RUNNING":
        fields.setdefault("started_at", _NOW)
    sets = ", ".join(f"{k} = now()" if v is _NOW else f"{k} = :{k}" for k, v in fields.items())
    params = {k: v for k, v in fields.items() if v is not _NOW}
    result = await conn.execute(
        text(
            f"UPDATE tasks SET status = :target, version = version + 1, {sets}"  # noqa: S608 — колонки из allowlist
            " WHERE id = :id AND version = :v"
        ),
        {**params, "target": target, "id": task_id, "v": row["version"]},
    )
    if result.rowcount != 1:
        raise VersionConflictError(None, None)
    await outbox.emit(
        conn, event_type="task.state_changed", aggregate_type="task", aggregate_id=task_id,
        workspace_id=row["workspace_id"], run_id=row["run_id"],
        payload={"task_id": str(task_id), "from": row["status"], "to": target, "reason": reason,
                 "blocked_reason": fields.get("blocked_reason")},
    )
    return str(row["status"])


class _Now:
    """Маркер «now() на стороне БД»."""


_NOW: Any = _Now()

_RUN_COLUMNS = frozenset({
    "blocked_reason", "resume_status", "failure_category", "failure_message", "cancel_requested_at",
    "plan_version", "approved_plan_version", "base_commit_sha", "started_at", "finished_at",
})
_TASK_COLUMNS = frozenset({
    "blocked_reason", "next_attempt_at", "attempt_count", "retry_spent_seconds", "review_loop_count",
    "lease_fencing_token", "superseded_at", "started_at", "finished_at",
})
