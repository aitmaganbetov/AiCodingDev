"""Решения человека по находкам (OQ-43 (d); QUALITY_GATES §5.3, DATABASE_DESIGN §12.6, §18.3).

`POST /findings/{id}/transitions`: OPEN → ACKNOWLEDGED / FALSE_POSITIVE / ACCEPTED_RISK, ACKNOWLEDGED →
FALSE_POSITIVE / ACCEPTED_RISK, FALSE_POSITIVE / ACCEPTED_RISK → OPEN (повторное открытие). Правила — те же,
что у Verification Engine (`findings.check_transition`): FIXED/VERIFIED_FIXED человек не выставляет; принять
как риск находку выше допустимого профилем уровня нельзя (`critical`-профиль: `critical`). Решение
записывает пользователь сессии (`resolved_by`, триггер 0008 trg_findings_guard), версия находки
(`findings.version`, 0010) защищает от решения по устаревшему виду (If-Match).

Роль БД — aicd_app (UPDATE только status, resolved_by, resolution_reason; RLS по workspace). RBAC по целевому
статусу (`rbac.can_transition_finding`) проверяет API до вызова.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform.errors import FieldError, ProblemError
from aicd_verification.findings import TRANSITIONS, check_transition

HUMAN_TARGETS = frozenset({"OPEN", "ACKNOWLEDGED", "FALSE_POSITIVE", "ACCEPTED_RISK"})


async def human_transition(
    conn: AsyncConnection,
    finding_id: uuid.UUID,
    *,
    to_status: str,
    reason: str,
    actor_id: uuid.UUID,
    expected_version: int,
) -> dict[str, Any]:
    """Переход находки по решению человека. Возвращает {from_status, to_status, severity, run_id, version}.

    404 — находки нет (RLS); 412 — версия не совпала (ETag актуальной); 409 invalid_state_transition —
    переход не из FSM; 422 — правило профиля (принятие риска выше допустимого уровня) или пустая причина."""
    row = (
        (
            await conn.execute(
                text(
                    "SELECT f.status::text AS status, f.severity::text AS severity, f.version, f.run_id,"
                    " r.quality_profile::text AS profile"
                    " FROM findings f JOIN runs r ON r.workspace_id = f.workspace_id AND r.id = f.run_id"
                    " WHERE f.id = :id FOR UPDATE OF f"
                ),
                {"id": finding_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ProblemError("not_found")
    if int(row["version"]) != expected_version:
        raise ProblemError("precondition_failed", headers={"ETag": f'"v{row["version"]}"'})
    current = str(row["status"])
    if to_status not in HUMAN_TARGETS or to_status not in TRANSITIONS.get(current, frozenset()):
        raise ProblemError(
            "invalid_state_transition",
            detail=f"Переход {current} → {to_status} недопустим",
            extensions={"current_status": current},
        )
    check = check_transition(
        current,
        to_status,
        actor_type="user",
        severity=row["severity"],
        profile=row["profile"],
        resolution_reason=reason,
    )
    if not check.allowed:
        raise ProblemError(
            "validation_error",
            detail="Переход запрещён правилами профиля качества",
            errors=[FieldError("/to_status", "not_allowed", check.reason)],
        )
    reopened = to_status == "OPEN"
    version = (
        await conn.execute(
            text(
                "UPDATE findings SET status = CAST(:s AS finding_status_t), resolved_by = :u, resolution_reason = :r"
                " WHERE id = :id RETURNING version"
            ),
            # Повторное открытие снимает решение (resolved_by), причина пересмотра остаётся в resolution_reason.
            {"s": to_status, "u": None if reopened else actor_id, "r": reason, "id": finding_id},
        )
    ).scalar_one()
    return {
        "from_status": current,
        "to_status": to_status,
        "severity": row["severity"],
        "run_id": row["run_id"],
        "version": int(version),
    }
