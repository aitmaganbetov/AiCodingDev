"""Адаптеры портов документации (code_intelligence.docs) к approvals и ledger внешних действий (P5-09/P5-10).

Модуль code_intelligence не импортирует оркестрацию (слои): он объявляет `DocsApprovalPort`/`DocsDownloadPort`,
а реализации живут здесь. Загрузка документации — внешнее действие (ADR-0012): план → EXECUTING → CONFIRMED,
только после APPROVED `DOCS_DOWNLOAD` (кроме ручного refresh — согласие человека, API_DESIGN §19).
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_code_intelligence.docs.policy import ApprovalState
from aicd_orchestration import external_actions
from aicd_orchestration.approvals import Approvals
from aicd_platform.clock import utcnow

DOCS_DOWNLOAD_TTL: Final = timedelta(days=7)  # DATABASE_DESIGN §11: approval загрузки документации — 7 дней


class DocsApprovals:
    """`DocsApprovalPort`: approval DOCS_DOWNLOAD (повторный запрос возвращает существующий PENDING)."""

    def __init__(self, approvals: Approvals | None = None) -> None:
        self.approvals = approvals or Approvals()

    async def request_docs_download(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        run_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        subject: dict[str, Any],
        requested_by_role: str | None,
    ) -> uuid.UUID:
        existing = (
            await conn.execute(
                text(
                    "SELECT id FROM approvals WHERE workspace_id = :ws AND action_type = 'DOCS_DOWNLOAD'"
                    " AND status = 'PENDING' AND subject = CAST(:subject AS jsonb)"
                    " AND run_id IS NOT DISTINCT FROM :run AND task_id IS NOT DISTINCT FROM :task LIMIT 1"
                ),
                {"ws": workspace_id, "subject": _json(subject), "run": run_id, "task": task_id},
            )
        ).scalar_one_or_none()
        if existing is not None:
            return uuid.UUID(str(existing))
        return await self.approvals.request(
            conn,
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            action_type="DOCS_DOWNLOAD",
            risk_level="low",
            subject=subject,
            requested_by_type="agent" if requested_by_role else "system",
            requested_by_role=requested_by_role,
            expires_at=utcnow() + DOCS_DOWNLOAD_TTL,
        )

    async def approval_state(self, conn: AsyncConnection, approval_id: uuid.UUID) -> ApprovalState:
        row = (
            await conn.execute(
                text(
                    "SELECT status, expires_at IS NOT NULL AND expires_at <= now() AS expired FROM approvals WHERE id = :a"
                ),
                {"a": approval_id},
            )
        ).first()
        if row is None:
            return ApprovalState.NONE
        status, expired = str(row[0]), bool(row[1])
        if status == "PENDING" and expired:
            return ApprovalState.EXPIRED
        return ApprovalState(status) if status in ApprovalState.__members__ else ApprovalState.NONE


class DocsLedger:
    """`DocsDownloadPort` поверх ledger `external_actions` (ADR-0012)."""

    async def plan(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        target: str,
        request: dict[str, Any],
        approval_id: uuid.UUID | None,
    ) -> uuid.UUID:
        action = await external_actions.plan(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            action_type="docs_download",
            target=target,
            plan_version=None,
            request=request,
            approval_id=approval_id,
        )
        return action.id

    async def begin(self, conn: AsyncConnection, action_id: uuid.UUID, fencing_token: int) -> None:
        await external_actions.begin(conn, action_id, fencing_token)

    async def confirm(
        self,
        conn: AsyncConnection,
        action_id: uuid.UUID,
        *,
        fencing_token: int,
        external_ref: str,
        result: dict[str, Any],
    ) -> None:
        await external_actions.confirm(
            conn, action_id, fencing_token=fencing_token, external_ref=external_ref, result=result
        )

    async def fail(self, conn: AsyncConnection, action_id: uuid.UUID, *, error: str, fencing_token: int | None) -> None:
        await external_actions.fail(conn, action_id, error=error, fencing_token=fencing_token)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
