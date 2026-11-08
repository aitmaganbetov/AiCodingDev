"""Исчерпание review/fix циклов (ACP §6, P4-10): реализация порта `ReviewLoopEscalation` брокера ACP.

Брокер вызывает порт в своей транзакции, когда FIX_REQUEST превысил `max_review_loops` (по умолчанию 3):
задача → BLOCKED(REVIEW_LOOP_EXHAUSTED) и approval MANUAL_REVIEW — дальше решает человек (одобрить текущее
состояние или отклонить). Новых циклов агентов не запускается: лимит не обходится повтором.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_orchestration import store
from aicd_orchestration.approvals import Approvals

_BLOCKABLE = frozenset({"PENDING", "READY", "LEASED", "RUNNING", "VERIFYING", "RETRY_WAIT"})


class ReviewLoopEscalator:
    def __init__(self, approvals: Approvals | None = None) -> None:
        self.approvals = approvals or Approvals()

    async def review_loop_exhausted(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        task_id: uuid.UUID,
        fix_request_id: uuid.UUID,
        correlation_id: uuid.UUID,
        review_loop_count: int,
        max_review_loops: int,
    ) -> uuid.UUID | None:
        async def load(tid: uuid.UUID) -> dict[str, Any] | None:
            found = (
                (
                    await conn.execute(
                        text(
                            "SELECT t.id, t.status, t.risk_level, r.project_id FROM tasks t JOIN runs r ON r.id = t.run_id"
                            " WHERE t.id = :t AND t.run_id = :r FOR UPDATE OF t"
                        ),
                        {"t": tid, "r": run_id},
                    )
                )
                .mappings()
                .first()
            )
            return dict(found) if found else None

        row = await load(task_id)
        if row is not None and row["status"] not in _BLOCKABLE:
            # Реализация уже завершена (цикл идёт новыми fix-задачами): блокируется задача ревью, отправившая
            # FIX_REQUEST сверх лимита — run не продолжится без решения человека.
            sender = (
                await conn.execute(text("SELECT task_id FROM agent_messages WHERE id = :m"), {"m": fix_request_id})
            ).scalar_one_or_none()
            if sender is not None:
                row = await load(sender) or row
        if row is None:
            return None
        task_id = row["id"]
        if row["status"] in _BLOCKABLE:
            await store.transition_task(
                conn,
                task_id,
                "BLOCKED",
                reason="review_loop_exhausted",
                extra={"blocked_reason": "REVIEW_LOOP_EXHAUSTED"},
            )
        existing = (
            await conn.execute(
                text(
                    "SELECT id FROM approvals WHERE task_id = :t AND action_type = 'MANUAL_REVIEW' AND status = 'PENDING'"
                ),
                {"t": task_id},
            )
        ).scalar_one_or_none()
        if existing is not None:
            return uuid.UUID(str(existing))
        return await self.approvals.request(
            conn,
            workspace_id=workspace_id,
            project_id=row["project_id"],
            run_id=run_id,
            task_id=task_id,
            action_type="MANUAL_REVIEW",
            risk_level=row["risk_level"],
            subject={
                "task_id": str(task_id),
                "reason": "REVIEW_LOOP_EXHAUSTED",
                "review_loop_count": review_loop_count,
                "max_review_loops": max_review_loops,
                "fix_request_id": str(fix_request_id),
                "correlation_id": str(correlation_id),
            },
            requested_by_type="system",
        )
