"""Approvals core (P3-10; DATA_FLOW §3.5, API_DESIGN §5.3, DATABASE_DESIGN §14.2/§18.7, AC-13).

* `request` — строка `PENDING` с `required_role` из матрицы `APPROVAL_MIN_ROLE`, предметом и
  `subject_hash` = sha256(canonical JSON предмета). Повторный запрос того же предмета возвращает
  существующий `PENDING` (уникальный индекс `uq_approvals_pending_subject`) — без дублей.
* `decide` — иерархия ролей + four-eyes (`can_decide_approval`), FSM, optimistic locking, outbox
  `approval.decided`, аудит `approval.decide`. Single-member self-approval допускается только при
  настройке workspace `allow_single_member_self_approval=true` и никогда для PRODUCTION_DEPLOY/SECRET_CHANGE;
  такое решение аудируется `self_approved=true`. Триггер БД `trg_approvals_decider_role` и CHECK
  `ck_approvals_four_eyes` — второй рубеж.
* `expire_due` — `PENDING → EXPIRED` по `expires_at` (default 72 ч, PRODUCTION_DEPLOY 24 ч).

Агент никогда не является решающей стороной: `decide` принимает только пользователя.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_identity.rbac import (
    APPROVAL_MIN_ROLE,
    FOUR_EYES_ACTIONS,
    NEVER_SELF_APPROVE,
    RANK,
    Role,
    can_decide_approval,
)
from aicd_orchestration import fsm
from aicd_platform import audit, outbox
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7

DEFAULT_TTL: Final = timedelta(hours=72)
TTL_BY_ACTION: Final[dict[str, timedelta]] = {"PRODUCTION_DEPLOY": timedelta(hours=24)}
DECISIONS: Final = frozenset({"APPROVED", "REJECTED"})

_SUMMARIES: Final[dict[str, str]] = {
    "PLAN_APPROVAL": "Утверждение плана run",
    "MERGE": "Слияние изменений",
    "PRODUCTION_DEPLOY": "Деплой в production",
    "DESTRUCTIVE_MIGRATION": "Деструктивная миграция БД",
    "DATA_DELETION": "Удаление данных",
    "SECRET_CHANGE": "Изменение секретов",
    "UNRESTRICTED_NETWORK": "Неограниченный сетевой доступ",
    "DOCS_DOWNLOAD": "Загрузка документации",
    "CRITICAL_DIRECTIVE": "Критическое поручение",
    "MANUAL_REVIEW": "Ручное ревью",
    "SELF_REVIEW_FALLBACK": "Ревью тем же провайдером",
    "LOCAL_EXECUTION_FALLBACK": "Локальное исполнение без контейнера",
}


# --- Чистые правила (unit-тесты: tests/unit/test_approvals_rules.py) -------------------------------


def canonical_json(value: Any) -> str:
    """Канонический JSON (сортировка ключей, без пробелов) — основа subject_hash / request_hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def subject_hash(subject: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(subject).encode()).hexdigest()


def ttl_for(action_type: str) -> timedelta:
    return TTL_BY_ACTION.get(action_type, DEFAULT_TTL)


def required_role(action_type: str) -> Role:
    if action_type not in APPROVAL_MIN_ROLE:
        raise ValueError(f"неизвестный action_type: {action_type}")
    return APPROVAL_MIN_ROLE[action_type]


@dataclass(frozen=True)
class DecisionCheck:
    allowed: bool
    code: str | None  # forbidden | four_eyes_violation
    self_approval: bool


def evaluate_decider(
    *,
    role: Role | str,
    action_type: str,
    required: Role | str | None,
    actor_id: uuid.UUID,
    requested_by: uuid.UUID | None,
    single_member: bool,
    self_approval_setting: bool,
) -> DecisionCheck:
    """Может ли пользователь решить approval (без учёта статуса/срока).

    `required` — сохранённая `required_role` строки (не ниже матрицы); роль ниже ⇒ forbidden.
    Для действий вне four-eyes инициатор решает сам (например, PLAN_APPROVAL автора run).
    """
    r = Role(role)
    if required is not None and RANK[r] < RANK[Role(required)]:
        return DecisionCheck(False, "forbidden", False)
    is_requester = requested_by is not None and requested_by == actor_id
    allowed, code = can_decide_approval(
        r,
        action_type,
        is_requester=is_requester,
        single_member_self_approval_allowed=single_member and self_approval_setting,
    )
    return DecisionCheck(allowed, code, allowed and is_requester)


def reason_required(decision: str, risk_level: str) -> bool:
    """reason обязателен для REJECTED и для critical-действий (ApprovalDecisionRequest)."""
    return decision == "REJECTED" or risk_level == "critical"


def summary_for(action_type: str, subject: dict[str, Any]) -> str:
    custom = subject.get("summary")
    if isinstance(custom, str) and custom:
        return custom[:500]
    base = _SUMMARIES.get(action_type, action_type)
    if action_type == "PLAN_APPROVAL" and "plan_version" in subject:
        return f"{base} (версия плана {subject['plan_version']})"
    return base


# --- Сервис -------------------------------------------------------------------------------------

_SELECT = (
    "SELECT a.*, a.expires_at IS NOT NULL AND a.expires_at <= now() AS is_expired,"
    " ru.display_name AS requested_by_name, du.display_name AS decided_by_name"
    " FROM approvals a LEFT JOIN users ru ON ru.id = a.requested_by LEFT JOIN users du ON du.id = a.decided_by"
)


@dataclass(frozen=True)
class SelfApprovalContext:
    single_member: bool
    setting: bool


class Approvals:
    """Реализация `engine.ApprovalsPort` + решения пользователей."""

    async def request(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        run_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        action_type: str,
        risk_level: str,
        subject: dict[str, Any],
        requested_by_type: str,
        requested_by: uuid.UUID | None = None,
        requested_by_role: str | None = None,
        expires_at: datetime | None = None,
    ) -> uuid.UUID:
        min_role = required_role(action_type)
        digest = subject_hash(subject)
        approval_id = uuid7()
        params = {
            "id": approval_id,
            "ws": workspace_id,
            "p": project_id,
            "run": run_id,
            "task": task_id,
            "at": action_type,
            "risk": risk_level,
            "rr": min_role.value,
            "rbt": requested_by_type,
            "rb": requested_by,
            "rbr": requested_by_role,
            "h": digest,
            "subj": canonical_json(subject),
            "exp": expires_at,
            "ttl": ttl_for(action_type).total_seconds(),
        }
        inserted = (
            await conn.execute(
                text(
                    "INSERT INTO approvals (id, workspace_id, project_id, run_id, task_id, action_type, risk_level,"
                    " required_role, requested_by_type, requested_by, requested_by_role, subject_hash, subject,"
                    " expires_at) VALUES (:id, :ws, :p, :run, :task, :at, :risk, :rr, :rbt, :rb, :rbr, :h,"
                    " CAST(:subj AS jsonb), COALESCE(CAST(:exp AS timestamptz), now() + make_interval(secs => :ttl)))"
                    " ON CONFLICT (workspace_id, action_type, subject_hash) WHERE status = 'PENDING' DO NOTHING"
                    " RETURNING id"
                ),
                params,
            )
        ).scalar_one_or_none()
        if inserted is None:
            existing: uuid.UUID = (
                await conn.execute(
                    text(
                        "SELECT id FROM approvals WHERE workspace_id = :ws AND action_type = :at"
                        " AND subject_hash = :h AND status = 'PENDING'"
                    ),
                    {"ws": workspace_id, "at": action_type, "h": digest},
                )
            ).scalar_one()
            return existing
        await outbox.emit(
            conn,
            event_type="approval.requested",
            aggregate_type="approval",
            aggregate_id=approval_id,
            workspace_id=workspace_id,
            run_id=run_id,
            payload={
                "approval_id": str(approval_id),
                "action_type": action_type,
                "risk_level": risk_level,
                "required_role": min_role.value,
                "task_id": str(task_id) if task_id else None,
            },
        )
        await audit.record(
            conn,
            action="approval.request",
            resource_type="approval",
            resource_id=approval_id,
            actor_type="user"
            if requested_by_type == "user"
            else ("agent" if requested_by_type == "agent" else "system"),
            actor_id=requested_by,
            workspace_id=workspace_id,
            metadata={"action_type": action_type, "risk_level": risk_level, "subject_hash": digest},
        )
        return approval_id

    async def get(self, conn: AsyncConnection, approval_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any] | None:
        sql = _SELECT + " WHERE a.id = :id"
        if lock:
            sql += " FOR UPDATE OF a"
        row = (await conn.execute(text(sql), {"id": approval_id})).mappings().first()
        return dict(row) if row else None

    async def self_approval_context(self, conn: AsyncConnection, workspace_id: uuid.UUID) -> SelfApprovalContext:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT (SELECT count(*) FROM workspace_members m WHERE m.workspace_id = w.id) AS members,"
                        " COALESCE(w.settings -> 'allow_single_member_self_approval' = 'true'::jsonb, false) AS allowed"
                        " FROM workspaces w WHERE w.id = :ws"
                    ),
                    {"ws": workspace_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return SelfApprovalContext(False, False)
        return SelfApprovalContext(int(row["members"]) == 1, bool(row["allowed"]))

    def check(
        self, row: dict[str, Any], *, actor_id: uuid.UUID, actor_role: Role | str, ctx: SelfApprovalContext
    ) -> DecisionCheck:
        return evaluate_decider(
            role=actor_role,
            action_type=row["action_type"],
            required=row["required_role"],
            actor_id=actor_id,
            requested_by=row["requested_by"],
            single_member=ctx.single_member,
            self_approval_setting=ctx.setting,
        )

    def can_decide(
        self, row: dict[str, Any], *, actor_id: uuid.UUID, actor_role: Role | str, ctx: SelfApprovalContext
    ) -> bool:
        if row["status"] != "PENDING" or row.get("is_expired"):
            return False
        return self.check(row, actor_id=actor_id, actor_role=actor_role, ctx=ctx).allowed

    async def decide(
        self,
        conn: AsyncConnection,
        approval_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        actor_role: Role | str,
        decision: str,
        reason: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        """Решение пользователя. Ошибки — ProblemError (403/412/409/422) в порядке API_DESIGN §6."""
        if decision not in DECISIONS:
            raise ProblemError("validation_error", errors=[FieldError("/decision", "enum", "APPROVED | REJECTED")])
        row = await self.get(conn, approval_id, lock=True)
        if row is None:
            raise ProblemError("not_found")
        ctx = await self.self_approval_context(conn, row["workspace_id"])
        verdict = self.check(row, actor_id=actor_id, actor_role=actor_role, ctx=ctx)
        if not verdict.allowed:
            raise ProblemError(verdict.code or "forbidden")
        if expected_version is not None and row["version"] != expected_version:
            raise ProblemError(
                "precondition_failed",
                extensions={"current_version": row["version"]},
                headers={"ETag": f'"v{row["version"]}"'},
            )
        if row["status"] != "PENDING":
            raise ProblemError("invalid_state_transition", extensions={"current_status": row["status"]})
        if row["is_expired"]:
            # Фактический перевод в EXPIRED выполняет expire_due (эта транзакция откатывается).
            raise ProblemError(
                "invalid_state_transition", detail="Срок подтверждения истёк", extensions={"current_status": "EXPIRED"}
            )
        if reason_required(decision, row["risk_level"]) and not (reason and reason.strip()):
            raise ProblemError(
                "validation_error",
                errors=[FieldError("/reason", "required", "причина обязательна для отклонения и критичных действий")],
            )
        fsm.require_transition("approval", row["status"], decision)
        try:
            result = await conn.execute(
                text(
                    "UPDATE approvals SET status = :d, decided_by = :uid, decided_at = now(), decision_reason = :reason,"
                    " allow_self_approval = allow_self_approval OR :self, version = version + 1"
                    " WHERE id = :id AND status = 'PENDING' AND version = :v"
                ),
                {
                    "d": decision,
                    "uid": actor_id,
                    "reason": reason,
                    "self": verdict.self_approval,
                    "id": approval_id,
                    "v": row["version"],
                },
            )
        except DBAPIError as exc:  # trg_approvals_decider_role: роль ниже required (с учётом overrides проекта)
            if getattr(exc.orig, "sqlstate", None) == "42501":
                raise ProblemError("forbidden") from exc
            raise
        if result.rowcount != 1:
            raise ProblemError("precondition_failed")
        await outbox.emit(
            conn,
            event_type="approval.decided",
            aggregate_type="approval",
            aggregate_id=approval_id,
            workspace_id=row["workspace_id"],
            run_id=row["run_id"],
            payload={
                "approval_id": str(approval_id),
                "action_type": row["action_type"],
                "status": decision,
                "task_id": str(row["task_id"]) if row["task_id"] else None,
                "subject": row["subject"],
            },
        )
        await audit.record(
            conn,
            action="approval.decide",
            resource_type="approval",
            resource_id=approval_id,
            actor_type="user",
            actor_id=actor_id,
            workspace_id=row["workspace_id"],
            metadata={
                "decision": decision,
                "action_type": row["action_type"],
                "self_approved": verdict.self_approval,
                "subject_hash": row["subject_hash"],
            },
        )
        decided = await self.get(conn, approval_id)
        assert decided is not None
        return decided

    async def cancel(self, conn: AsyncConnection, approval_id: uuid.UUID, *, reason: str) -> bool:
        """PENDING → CANCELLED (предмет изменился/инициатор отозвал). False — уже не PENDING."""
        row = (
            (
                await conn.execute(
                    text(
                        "UPDATE approvals SET status = 'CANCELLED', decision_reason = :r, version = version + 1"
                        " WHERE id = :id AND status = 'PENDING' RETURNING workspace_id, run_id, action_type, subject"
                    ),
                    {"id": approval_id, "r": reason[:4000]},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        await outbox.emit(
            conn,
            event_type="approval.decided",
            aggregate_type="approval",
            aggregate_id=approval_id,
            workspace_id=row["workspace_id"],
            run_id=row["run_id"],
            payload={
                "approval_id": str(approval_id),
                "action_type": row["action_type"],
                "status": "CANCELLED",
                "reason": reason,
                "subject": row["subject"],
            },
        )
        return True

    async def expire_due(self, conn: AsyncConnection, *, limit: int = 500) -> list[dict[str, Any]]:
        """PENDING с истёкшим `expires_at` → EXPIRED (в текущем workspace-контексте RLS)."""
        rows = (
            (
                await conn.execute(
                    text(
                        "UPDATE approvals SET status = 'EXPIRED', version = version + 1 WHERE id IN ("
                        " SELECT id FROM approvals WHERE status = 'PENDING' AND expires_at <= now()"
                        " ORDER BY expires_at LIMIT :lim FOR UPDATE SKIP LOCKED)"
                        " RETURNING id, workspace_id, run_id, task_id, action_type, subject, status"
                    ),
                    {"lim": limit},
                )
            )
            .mappings()
            .all()
        )
        expired = [dict(r) for r in rows]
        for r in expired:
            await outbox.emit(
                conn,
                event_type="approval.decided",
                aggregate_type="approval",
                aggregate_id=r["id"],
                workspace_id=r["workspace_id"],
                run_id=r["run_id"],
                payload={
                    "approval_id": str(r["id"]),
                    "action_type": r["action_type"],
                    "status": "EXPIRED",
                    "subject": r["subject"],
                },
            )
            await audit.record(
                conn,
                action="approval.expire",
                resource_type="approval",
                resource_id=r["id"],
                actor_type="system",
                actor_id=None,
                workspace_id=r["workspace_id"],
                metadata={"action_type": r["action_type"]},
            )
        return expired

    async def list_for_viewer(
        self,
        conn: AsyncConnection,
        *,
        viewer_id: uuid.UUID,
        viewer_role: Role | str,
        ctx: SelfApprovalContext,
        statuses: list[str] | None = None,
        run_id: uuid.UUID | None = None,
        action_types: list[str] | None = None,
        decidable_by_me: bool | None = None,
        before: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where = ["a.status = ANY(:statuses)"]
        params: dict[str, Any] = {"statuses": statuses or ["PENDING"], "lim": limit}
        if run_id is not None:
            where.append("a.run_id = :run")
            params["run"] = run_id
        if action_types:
            where.append("a.action_type = ANY(:ats)")
            params["ats"] = action_types
        if before is not None:
            where.append("(a.requested_at, a.id) < (:bt, :bid)")
            params["bt"], params["bid"] = before
        if decidable_by_me is not None:
            # То же правило, что evaluate_decider (тест сверяет): роль ≥ required_role, four-eyes.
            self_ok = ctx.single_member and ctx.setting
            cond = (
                "(a.status = 'PENDING' AND (a.expires_at IS NULL OR a.expires_at > now())"
                " AND " + _RANK_SQL.format(col="a.required_role") + " <= :rank"
                " AND NOT (a.requested_by IS NOT DISTINCT FROM :viewer AND a.action_type = ANY(:four_eyes)"
                " AND (a.action_type = ANY(:never) OR NOT :self_ok)))"
            )
            where.append(cond if decidable_by_me else f"NOT {cond}")
            params.update(
                {
                    "rank": RANK[Role(viewer_role)],
                    "viewer": viewer_id,
                    "four_eyes": sorted(FOUR_EYES_ACTIONS),
                    "never": sorted(NEVER_SELF_APPROVE),
                    "self_ok": self_ok,
                }
            )
        sql = _SELECT + " WHERE " + " AND ".join(where) + " ORDER BY a.requested_at DESC, a.id DESC LIMIT :lim"
        rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]


_RANK_SQL: Final = (
    "(CASE {col} WHEN 'owner' THEN 5 WHEN 'admin' THEN 4 WHEN 'developer' THEN 3 WHEN 'reviewer' THEN 2 ELSE 1 END)"
)
