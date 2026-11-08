"""Поручения пользователя (P3-13; DATA_FLOW §3.4/§6, API_DESIGN §5.4/§12, AC-06).

Конвейер: `submit` (API, одна транзакция: чат-сообщение `directive` + `user_directives`
RECEIVED → IMPACT_ANALYSIS_PENDING + outbox) → `analyze` (детерминированный impact analysis,
IMPACT_READY → AWAITING_CONFIRMATION | CONFIRMED) → `confirm`/`cancel` (пользователь) или решение
approval `CRITICAL_DIRECTIVE` → `apply` (CHECKPOINTING: checkpoint + artifact `checkpoint_state`;
REPLANNING: новая plan_version; DISPATCHED: строки `directive_acknowledgements` PENDING).

Phase 3: **DISPATCHED — конечное состояние**. Доставку агентам и подтверждение (ACP TASK_ASSIGN/QUESTION,
`DELIVERED`/`ACKNOWLEDGED`) реализует Phase 4 (P4-04/P4-05), переход в COMPLETED — после завершения и
верификации порождённых задач. UI различает «принято» (202), «доставлено» (DISPATCHED/ACKNOWLEDGED)
и «выполнено» (COMPLETED) — поля `delivery` и `completion` представления (API_DESIGN §12.4).

Работающий CLI-процесс никогда не модифицируется незаметно (ТЗ §8.2): текст поручения не попадает в
stdin; для затронутых RUNNING задач запрашивается safe point через хук `cancel_task(task_id, reason)`,
а изменения вносятся новыми задачами новой plan_version. Текст поручения — недоверенные данные:
в инструкции задач он вставляется только внутри ограждения `fence_untrusted`.

Права (API_DESIGN §5.2/§5.4): тип поручения — `can_submit_directive`; confirm/cancel — автор
поручения либо admin/owner. APPROVE/REJECT — решение approval с теми же правилами, что
`POST /approvals/{id}/decision`; PAUSE/RESUME — команды run (`runs.command`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_identity.rbac import Role, can_submit_directive
from aicd_orchestration import checkpoints, fsm, runs, store
from aicd_orchestration.agents.prompt import fence_untrusted
from aicd_orchestration.approvals import Approvals
from aicd_platform import audit, outbox
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.clock import utcnow
from aicd_platform.db import DbContext, transaction
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text

# --- Правила (чистые функции; unit-тесты) ----------------------------------------------------------

ALLOWED_TARGETS: Final[dict[str, frozenset[str]]] = {
    "ASK": frozenset({"run", "role", "task"}),
    "REVIEW": frozenset({"role", "task"}),
    "NEW_TASK": frozenset({"run", "role"}),
    "CHANGE_REQUEST": frozenset({"run", "role", "task"}),
    "STOP": frozenset({"run", "task"}),
    # API_DESIGN §5.4 допускает task, но FSM задач не имеет паузы — в v1 только run (см. отчёт P3-13).
    "PAUSE": frozenset({"run"}),
    "RESUME": frozenset({"run"}),
    "APPROVE": frozenset({"approval"}),
    "REJECT": frozenset({"approval"}),
}
IMMEDIATE_TYPES: Final = frozenset({"PAUSE", "RESUME", "APPROVE", "REJECT"})
NOT_STARTED: Final = frozenset({"PENDING", "READY", "RETRY_WAIT", "BLOCKED"})
IN_FLIGHT: Final = frozenset({"LEASED", "RUNNING", "VERIFYING"})
PRE_DISPATCH: Final = frozenset(
    {"RECEIVED", "IMPACT_ANALYSIS_PENDING", "IMPACT_READY", "AWAITING_CONFIRMATION", "CONFIRMED"}
)
# Порядок «тяжести» влияния для агрегата user_directives.impact_status.
IMPACT_ORDER: Final = ("NONE", "CONTEXT_ONLY", "TASK_ADD", "REPLAN_REQUIRED", "TASK_CANCEL", "RESTART_REQUIRED")
DISRUPTIVE: Final = frozenset({"REPLAN_REQUIRED", "TASK_CANCEL", "RESTART_REQUIRED"})
CONFIRMER_ROLES: Final = frozenset({Role.OWNER, Role.ADMIN})
MAX_INSTRUCTIONS: Final = 100_000


@dataclass(frozen=True)
class Target:
    type: str
    role: str | None = None
    task_id: uuid.UUID | None = None
    approval_id: uuid.UUID | None = None


@dataclass(frozen=True)
class TaskInfo:
    id: uuid.UUID
    status: str
    agent_role: str
    complexity: int
    risk_level: str


def validate_target(directive_type: str, target: Target) -> None:
    """422 validation_error при недопустимом сочетании типа и цели (API_DESIGN §5.4)."""
    if target.type not in ALLOWED_TARGETS[directive_type]:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/target/type", "not_allowed", f"{directive_type}: цель {target.type} недопустима")],
        )
    required = {
        "role": ("role", target.role),
        "task": ("task_id", target.task_id),
        "approval": ("approval_id", target.approval_id),
    }.get(target.type)
    if required is not None and required[1] is None:
        raise ProblemError(
            "validation_error",
            errors=[FieldError(f"/target/{required[0]}", "required", f"обязательно для target.type={target.type}")],
        )
    extra = {"role": target.role, "task_id": target.task_id, "approval_id": target.approval_id}
    if required is not None:
        extra.pop(required[0])
    for name, value in extra.items():
        if value is not None:
            raise ProblemError(
                "validation_error",
                errors=[FieldError(f"/target/{name}", "not_allowed", f"не допускается для target.type={target.type}")],
            )


def plan_impacts(directive_type: str, target: Target, tasks: Sequence[TaskInfo]) -> list[tuple[uuid.UUID, str]]:
    """Детерминированное влияние на незавершённые задачи цели (task/role/run)."""
    if target.type == "task":
        affected = [t for t in tasks if t.id == target.task_id]
    elif target.type == "role":
        affected = [t for t in tasks if t.agent_role == target.role]
    else:
        affected = list(tasks)
    affected = [t for t in affected if t.status not in fsm.TERMINAL["task"]]
    impact = {
        "ASK": "CONTEXT_ONLY",
        "REVIEW": "CONTEXT_ONLY",
        "CHANGE_REQUEST": "REPLAN_REQUIRED",
        "STOP": "TASK_CANCEL",
    }.get(directive_type)
    if impact is None:  # NEW_TASK (TASK_ADD на REPLANNING), PAUSE/RESUME (NONE)
        return []
    return [(t.id, impact) for t in affected]


def aggregate_impact(directive_type: str, impacts: Sequence[tuple[uuid.UUID, str]]) -> str:
    if directive_type == "NEW_TASK":
        return "TASK_ADD"
    if directive_type in IMMEDIATE_TYPES:
        return "NONE"
    if not impacts:
        # Нечего менять в текущих задачах: изменение оформляется новой задачей, STOP — отменой run/задачи.
        return {"CHANGE_REQUEST": "TASK_ADD", "STOP": "TASK_CANCEL"}.get(directive_type, "NONE")
    return max((i for _, i in impacts), key=IMPACT_ORDER.index)


def is_critical(impacts: Sequence[tuple[uuid.UUID, str]], tasks: Sequence[TaskInfo]) -> bool:
    """Критично: прерывающее влияние на задачи high/critical или L4+, либо отмена исполняющейся задачи."""
    by_id = {t.id: t for t in tasks}
    for task_id, impact in impacts:
        task = by_id.get(task_id)
        if task is None or impact not in DISRUPTIVE:
            continue
        if task.risk_level in ("high", "critical") or task.complexity >= 4:
            return True
        if impact in ("TASK_CANCEL", "RESTART_REQUIRED") and task.status in IN_FLIGHT:
            return True
    return False


def can_manage(actor_id: uuid.UUID, actor_role: Role | str, submitted_by: uuid.UUID) -> bool:
    """confirm/cancel: автор либо admin/owner (reviewer/developer — только своё)."""
    return actor_id == submitted_by or Role(actor_role) in CONFIRMER_ROLES


def delivery_state(status: str, ack_statuses: Sequence[str]) -> str:
    dispatched = status in ("DISPATCHED", "ACKNOWLEDGED", "COMPLETED") or (status == "FAILED" and bool(ack_statuses))
    if not dispatched:
        return "NOT_DISPATCHED"
    if status in ("ACKNOWLEDGED", "COMPLETED"):
        return "ACKNOWLEDGED"
    acked = sum(1 for s in ack_statuses if s == "ACKNOWLEDGED")
    if ack_statuses and acked == len(ack_statuses):
        return "ACKNOWLEDGED"
    return "PARTIALLY_ACKNOWLEDGED" if acked else "DISPATCHED"


def completion_state(status: str) -> str:
    if status == "COMPLETED":
        return "COMPLETED"
    if status == "FAILED":
        return "FAILED"
    if status in ("CANCELLED", "REJECTED"):
        return "CANCELLED"
    if status in ("DISPATCHED", "ACKNOWLEDGED"):
        return "IN_PROGRESS"
    return "NOT_STARTED"


def impact_status_view(status: str, impact_status: str | None) -> str:
    """DirectiveImpactStatus контракта: PENDING | READY | NOT_REQUIRED | FAILED."""
    if status in ("RECEIVED", "IMPACT_ANALYSIS_PENDING"):
        return "PENDING"
    if impact_status is None:
        return "FAILED" if status == "FAILED" else "NOT_REQUIRED"
    return "NOT_REQUIRED" if impact_status == "NONE" else "READY"


# --- Доступ ----------------------------------------------------------------------------------------


async def resolve_run_workspace(conn: AsyncConnection, run_id: uuid.UUID) -> uuid.UUID | None:
    """SECURITY DEFINER резолвер (миграция 0003): workspace run, только если пользователь — член."""
    value = (await conn.execute(text("SELECT sys_resolve_run_workspace(:r)"), {"r": run_id})).scalar_one_or_none()
    return uuid.UUID(str(value)) if value is not None else None


# --- Сервис ----------------------------------------------------------------------------------------

_SELECT = (
    "SELECT d.*, m.body AS message, u.display_name AS submitted_by_name"
    " FROM user_directives d JOIN chat_messages m ON m.workspace_id = d.workspace_id AND m.id = d.chat_message_id"
    " JOIN users u ON u.id = d.submitted_by"
)
_TERMINAL = fsm.TERMINAL["directive"]


async def _load(conn: AsyncConnection, directive_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any] | None:
    sql = _SELECT + " WHERE d.id = :id" + (" FOR UPDATE OF d" if lock else "")
    row = (await conn.execute(text(sql), {"id": directive_id})).mappings().first()
    return dict(row) if row else None


async def _transition(
    conn: AsyncConnection, d: dict[str, Any], target: str, *, reason: str | None = None, **cols: Any
) -> dict[str, Any]:
    fsm.require_transition("directive", d["status"], target)
    allowed = {"impact_status", "approval_id", "checkpoint_id", "resulting_plan_version", "failure_message"}
    unknown = set(cols) - allowed
    if unknown:
        raise ValueError(f"недопустимые колонки: {unknown}")
    sets = "".join(f", {k} = :{k}" for k in cols)
    if target in _TERMINAL:
        sets += ", completed_at = now()"
    result = await conn.execute(
        text(
            f"UPDATE user_directives SET status = :target, version = version + 1{sets}"  # noqa: S608 — allowlist
            " WHERE id = :id AND status = :from AND version = :v"
        ),
        {**cols, "target": target, "id": d["id"], "from": d["status"], "v": d["version"]},
    )
    if result.rowcount != 1:
        raise store.VersionConflictError(None, None)
    await outbox.emit(
        conn,
        event_type="directive.state_changed",
        aggregate_type="directive",
        aggregate_id=d["id"],
        workspace_id=d["workspace_id"],
        run_id=d["run_id"],
        payload={
            "directive_id": str(d["id"]),
            "from": d["status"],
            "to": target,
            "version": d["version"] + 1,
            "reason": reason,
        },
    )
    updated = {**d, **cols, "status": target, "version": d["version"] + 1}
    return updated


async def _active_tasks(conn: AsyncConnection, run_id: uuid.UUID) -> list[TaskInfo]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT id, status, agent_role, complexity, risk_level FROM tasks"
                    " WHERE run_id = :r AND superseded_at IS NULL ORDER BY created_at, id"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .all()
    )
    return [TaskInfo(r["id"], r["status"], r["agent_role"], int(r["complexity"]), r["risk_level"]) for r in rows]


@dataclass
class SubmitResult:
    directive_id: uuid.UUID
    status: str
    chat_message_id: uuid.UUID
    version: int
    decided_approval: dict[str, Any] | None = None


@dataclass
class ApplyResult:
    directive: dict[str, Any] | None
    cancel_task_ids: list[uuid.UUID] = field(default_factory=list)


CancelTask = Callable[[uuid.UUID, str], Awaitable[None]]


class Directives:
    def __init__(self, approvals: Approvals | None = None) -> None:
        self.approvals = approvals or Approvals()

    # ---------- intake ----------
    async def submit(
        self,
        conn: AsyncConnection,
        *,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: Role | str,
        directive_type: str,
        target: Target,
        message: str,
        expected_run_version: int,
        requires_impact_preview: bool = True,
    ) -> SubmitResult:
        if not can_submit_directive(actor_role, directive_type):
            raise ProblemError("forbidden")
        validate_target(directive_type, target)
        run = await store.load_run(conn, run_id, lock=True)
        if run is None:
            raise ProblemError("not_found")
        if run.version != expected_run_version:
            raise ProblemError("version_mismatch", extensions={"current_version": run.version})
        if run.status in fsm.TERMINAL["run"]:
            raise ProblemError(
                "invalid_state_transition", detail="Run завершён", extensions={"current_status": run.status}
            )
        target_task_id: uuid.UUID | None = None
        if target.type == "task":
            found = (
                await conn.execute(
                    text("SELECT id FROM tasks WHERE id = :t AND run_id = :r"), {"t": target.task_id, "r": run_id}
                )
            ).scalar_one_or_none()
            if found is None:
                raise ProblemError(
                    "validation_error",
                    errors=[FieldError("/target/task_id", "not_found", "задача не найдена в этом run")],
                )
            target_task_id = target.task_id
        approval_row: dict[str, Any] | None = None
        if target.type == "approval":
            if target.approval_id is None:  # pragma: no cover — нарушение инварианта
                raise LookupError("target.approval_id")
            approval_row = await self.approvals.get(conn, target.approval_id)
            if approval_row is None or approval_row["run_id"] != run_id:
                raise ProblemError(
                    "validation_error",
                    errors=[FieldError("/target/approval_id", "not_found", "approval не найден в этом run")],
                )

        thread_id = await self._all_agents_thread(conn, workspace_id, run_id)
        message_id = uuid7()
        await conn.execute(
            text(
                "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, sender_id, body,"
                " message_kind, mentions) VALUES (:id, :ws, :run, :th, 'user', :uid, :body, 'directive',"
                " CAST(:mentions AS agent_role_t[]))"
            ),
            {
                "id": message_id,
                "ws": workspace_id,
                "run": run_id,
                "th": thread_id,
                "uid": actor_id,
                "body": redact_text(message),
                "mentions": [target.role] if target.role else [],
            },
        )
        directive_id = uuid7()
        # Цель approval в БД выражается target_type='run' + approval_id (CHECK §9.6 не знает 'approval').
        stored_type = "run" if target.type == "approval" else target.type
        await conn.execute(
            text(
                "INSERT INTO user_directives (id, workspace_id, run_id, chat_message_id, target_type, target_role,"
                " target_task_id, directive_type, requires_impact_preview, expected_run_version, approval_id,"
                " submitted_by) VALUES (:id, :ws, :run, :msg, :tt, :tr, :tt_id, :dt, :prev, :erv, :appr, :uid)"
            ),
            {
                "id": directive_id,
                "ws": workspace_id,
                "run": run_id,
                "msg": message_id,
                "tt": stored_type,
                "tr": target.role,
                "tt_id": target_task_id,
                "dt": directive_type,
                "prev": requires_impact_preview,
                "erv": expected_run_version,
                "appr": target.approval_id,
                "uid": actor_id,
            },
        )
        await outbox.emit(
            conn,
            event_type="chat.message",
            aggregate_type="chat_message",
            aggregate_id=message_id,
            workspace_id=workspace_id,
            run_id=run_id,
            payload={
                "message_id": str(message_id),
                "thread_id": str(thread_id),
                "kind": "directive",
                "directive_id": str(directive_id),
            },
        )
        d = await _load(conn, directive_id, lock=True)
        if d is None:  # pragma: no cover — нарушение инварианта
            raise LookupError("d")
        decided: dict[str, Any] | None = None
        if directive_type in IMMEDIATE_TYPES:
            if directive_type in ("APPROVE", "REJECT"):
                if target.approval_id is None:  # pragma: no cover — нарушение инварианта
                    raise LookupError("target.approval_id")
                decided = await self.decide_approval(
                    conn,
                    target.approval_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    decision="APPROVED" if directive_type == "APPROVE" else "REJECTED",
                    reason=message,
                    expected_version=None,
                )
            else:
                await runs.command(
                    conn,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    command="pause" if directive_type == "PAUSE" else "resume",
                    expected_version=None,
                    reason=f"directive:{directive_id}",
                )
            # Команда исполнена синхронно тем же обработчиком, что REST-эндпоинт: агенты не участвуют.
            d = await _transition(conn, d, "DISPATCHED", impact_status="NONE", reason="command_applied")
            d = await _transition(conn, d, "ACKNOWLEDGED", reason="command_applied")
            d = await _transition(conn, d, "COMPLETED", reason="command_applied")
        else:
            d = await _transition(conn, d, "IMPACT_ANALYSIS_PENDING")
        await audit.record(
            conn,
            action="directive.submit",
            resource_type="directive",
            resource_id=directive_id,
            actor_type="user",
            actor_id=actor_id,
            workspace_id=workspace_id,
            metadata={"directive_type": directive_type, "target_type": target.type, "run_id": str(run_id)},
        )
        return SubmitResult(directive_id, d["status"], message_id, d["version"], decided)

    async def _all_agents_thread(self, conn: AsyncConnection, workspace_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
        await conn.execute(
            text(
                "INSERT INTO chat_threads (id, workspace_id, run_id, scope) VALUES (:id, :ws, :run, 'all_agents')"
                " ON CONFLICT ON CONSTRAINT uq_chat_threads_scope DO NOTHING"
            ),
            {"id": uuid7(), "ws": workspace_id, "run": run_id},
        )
        thread_id: uuid.UUID = (
            await conn.execute(
                text(
                    "SELECT id FROM chat_threads WHERE run_id = :run AND scope = 'all_agents'"
                    " AND target_role IS NULL AND target_provider IS NULL AND target_task_id IS NULL"
                ),
                {"run": run_id},
            )
        ).scalar_one()
        return thread_id

    # ---------- approvals ----------
    async def decide_approval(
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
        """approvals.decide + распространение решения на поручение (CRITICAL_DIRECTIVE) в той же транзакции."""
        decided = await self.approvals.decide(
            conn,
            approval_id,
            actor_id=actor_id,
            actor_role=actor_role,
            decision=decision,
            reason=reason,
            expected_version=expected_version,
        )
        await self.on_approval_resolved(conn, decided)
        return decided

    async def on_approval_resolved(self, conn: AsyncConnection, approval: dict[str, Any]) -> dict[str, Any] | None:
        if approval["action_type"] != "CRITICAL_DIRECTIVE":
            return None
        row = (
            await conn.execute(
                text("SELECT id FROM user_directives WHERE approval_id = :a AND status = 'AWAITING_CONFIRMATION'"),
                {"a": approval["id"]},
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        d = await _load(conn, row, lock=True)
        if d is None:  # pragma: no cover — нарушение инварианта
            raise LookupError("d")
        target = {"APPROVED": "CONFIRMED", "REJECTED": "REJECTED"}.get(approval["status"], "CANCELLED")
        return await _transition(conn, d, target, reason=f"approval_{approval['status'].lower()}")

    async def expire_approvals(self, conn: AsyncConnection) -> list[dict[str, Any]]:
        """Истечение approvals текущего workspace + отмена поручений с истёкшим CRITICAL_DIRECTIVE."""
        expired = await self.approvals.expire_due(conn)
        for approval in expired:
            await self.on_approval_resolved(conn, {**approval, "status": "EXPIRED"})
        return expired

    # ---------- impact analysis ----------
    async def analyze(self, conn: AsyncConnection, directive_id: uuid.UUID) -> dict[str, Any] | None:
        d = await _load(conn, directive_id, lock=True)
        if d is None or d["status"] != "IMPACT_ANALYSIS_PENDING":
            return d  # идемпотентность: уже проанализировано/отменено
        run = await store.load_run(conn, d["run_id"], lock=True)
        if run is None:  # pragma: no cover — нарушение инварианта
            raise LookupError("run")
        if run.status in fsm.TERMINAL["run"]:
            return await _transition(conn, d, "FAILED", failure_message="run завершён", reason="run_terminal")
        tasks = await _active_tasks(conn, d["run_id"])
        target = Target(d["target_type"], d["target_role"], d["target_task_id"])
        impacts = plan_impacts(d["directive_type"], target, tasks)
        by_id = {t.id: t for t in tasks}
        for task_id, impact in impacts:
            await conn.execute(
                text(
                    "INSERT INTO directive_impacts (workspace_id, run_id, directive_id, task_id, impact_type, details)"
                    " VALUES (:ws, :run, :d, :t, :i, CAST(:det AS jsonb)) ON CONFLICT DO NOTHING"
                ),
                {
                    "ws": d["workspace_id"],
                    "run": d["run_id"],
                    "d": directive_id,
                    "t": task_id,
                    "i": impact,
                    "det": json.dumps({"task_status": by_id[task_id].status, "agent_role": by_id[task_id].agent_role}),
                },
            )
        aggregate = aggregate_impact(d["directive_type"], impacts)
        d = await _transition(conn, d, "IMPACT_READY", impact_status=aggregate)
        await outbox.emit(
            conn,
            event_type="directive.impact_ready",
            aggregate_type="directive",
            aggregate_id=directive_id,
            workspace_id=d["workspace_id"],
            run_id=d["run_id"],
            payload={
                "directive_id": str(directive_id),
                "impact_status": aggregate,
                "impacts": [{"task_id": str(t), "impact_type": i} for t, i in impacts],
            },
        )
        if is_critical(impacts, tasks):
            risk = max(
                (by_id[t].risk_level for t, _ in impacts),
                key=["low", "medium", "high", "critical"].index,
                default="high",
            )
            approval_id = await self.approvals.request(
                conn,
                workspace_id=d["workspace_id"],
                project_id=await _project_of(conn, d["run_id"]),
                run_id=d["run_id"],
                task_id=None,
                action_type="CRITICAL_DIRECTIVE",
                risk_level=risk if risk in ("high", "critical") else "high",
                subject={
                    "directive_id": str(directive_id),
                    "run_id": str(d["run_id"]),
                    "directive_type": d["directive_type"],
                    "impact_status": aggregate,
                    "impacts": sorted([str(t), i] for t, i in impacts),
                    "summary": f"Критическое поручение {d['directive_type']}: {d['message'][:200]}",
                },
                requested_by_type="user",
                requested_by=d["submitted_by"],
            )
            return await _transition(conn, d, "AWAITING_CONFIRMATION", approval_id=approval_id, reason="critical")
        if d["requires_impact_preview"]:
            return await _transition(conn, d, "AWAITING_CONFIRMATION", reason="impact_preview")
        return await _transition(conn, d, "CONFIRMED", reason="auto_confirmed")

    # ---------- user commands ----------
    async def confirm(
        self,
        conn: AsyncConnection,
        directive_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        actor_role: Role | str,
        expected_version: int | None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        d = await self._for_command(conn, directive_id, actor_id, actor_role, expected_version)
        if d["status"] != "AWAITING_CONFIRMATION":
            raise ProblemError("invalid_state_transition", extensions={"current_status": d["status"]})
        if d["approval_id"] is not None:
            raise ProblemError(
                "invalid_state_transition",
                detail="Критическое поручение подтверждается решением approval CRITICAL_DIRECTIVE",
                extensions={"current_status": d["status"]},
            )
        d = await _transition(conn, d, "CONFIRMED", reason="user_confirmed")
        await audit.record(
            conn,
            action="directive.confirm",
            resource_type="directive",
            resource_id=directive_id,
            actor_type="user",
            actor_id=actor_id,
            workspace_id=d["workspace_id"],
            metadata={"reason": reason} if reason else None,
        )
        return d

    async def cancel(
        self,
        conn: AsyncConnection,
        directive_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
        actor_role: Role | str,
        expected_version: int | None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        d = await self._for_command(conn, directive_id, actor_id, actor_role, expected_version)
        if d["status"] not in PRE_DISPATCH:
            raise ProblemError("invalid_state_transition", extensions={"current_status": d["status"]})
        if d["approval_id"] is not None and d["directive_type"] not in ("APPROVE", "REJECT"):
            await self.approvals.cancel(conn, d["approval_id"], reason="directive_cancelled")
        d = await _transition(conn, d, "CANCELLED", reason="user_cancelled")
        await conn.execute(
            text(
                "UPDATE directive_impacts SET resolution = 'SKIPPED', resolved_at = now()"
                " WHERE directive_id = :d AND resolution = 'PENDING'"
            ),
            {"d": directive_id},
        )
        await audit.record(
            conn,
            action="directive.cancel",
            resource_type="directive",
            resource_id=directive_id,
            actor_type="user",
            actor_id=actor_id,
            workspace_id=d["workspace_id"],
            metadata={"reason": reason} if reason else None,
        )
        return d

    async def _for_command(
        self,
        conn: AsyncConnection,
        directive_id: uuid.UUID,
        actor_id: uuid.UUID,
        actor_role: Role | str,
        expected_version: int | None,
    ) -> dict[str, Any]:
        d = await _load(conn, directive_id, lock=True)
        if d is None:
            raise ProblemError("not_found")
        if not can_manage(actor_id, actor_role, d["submitted_by"]):
            raise ProblemError("forbidden")
        if expected_version is not None and d["version"] != expected_version:
            raise ProblemError(
                "precondition_failed",
                extensions={"current_version": d["version"]},
                headers={"ETag": f'"v{d["version"]}"'},
            )
        return d

    # ---------- apply: checkpoint → replan → dispatch ----------
    async def apply(self, conn: AsyncConnection, directive_id: uuid.UUID, *, artifacts: ArtifactStore) -> ApplyResult:
        d = await _load(conn, directive_id, lock=True)
        if d is None or d["status"] != "CONFIRMED":
            return ApplyResult(d)
        run = await store.load_run(conn, d["run_id"], lock=True)
        if run is None:  # pragma: no cover — нарушение инварианта
            raise LookupError("run")
        if run.status in fsm.TERMINAL["run"]:
            return ApplyResult(
                await _transition(conn, d, "FAILED", failure_message="run завершён", reason="run_terminal")
            )
        impacts = await self._impacts(conn, directive_id)
        aggregate = d["impact_status"]
        if aggregate in ("NONE", "CONTEXT_ONLY"):
            await self._resolve_impacts(conn, directive_id)
            d = await _transition(conn, d, "DISPATCHED", reason="context_only")
            await self._acknowledgements(conn, d, impacts, [])
            return ApplyResult(d)

        d = await _transition(conn, d, "CHECKPOINTING")
        checkpoint_id = await self._checkpoint(conn, d, run, artifacts)
        cancel_ids: list[uuid.UUID] = []
        if d["directive_type"] == "STOP" and d["target_type"] == "run":
            in_flight = [i["task_id"] for i in impacts if i["task_status"] in IN_FLIGHT]
            await runs.command(
                conn,
                run_id=d["run_id"],
                workspace_id=d["workspace_id"],
                actor_id=d["submitted_by"],
                command="cancel",
                expected_version=None,
                reason=f"directive:{directive_id}",
            )
            cancel_ids.extend(in_flight)
            await self._resolve_impacts(conn, directive_id)
            d = await _transition(conn, d, "DISPATCHED", checkpoint_id=checkpoint_id, reason="run_cancel_requested")
            await self._acknowledgements(conn, d, impacts, [])
            return ApplyResult(d, cancel_ids)

        d = await _transition(conn, d, "REPLANNING", checkpoint_id=checkpoint_id)
        plan_version, new_tasks, cancel_ids = await self._replan(conn, d, run, impacts)
        await self._resolve_impacts(conn, directive_id)
        d = await _transition(conn, d, "DISPATCHED", resulting_plan_version=plan_version, reason="replanned")
        await self._acknowledgements(conn, d, impacts, new_tasks)
        return ApplyResult(d, cancel_ids)

    async def _impacts(self, conn: AsyncConnection, directive_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT i.task_id, i.impact_type, i.resolution, t.status AS task_status, t.agent_role"
                        " FROM directive_impacts i JOIN tasks t ON t.id = i.task_id WHERE i.directive_id = :d"
                        " ORDER BY t.created_at, t.id"
                    ),
                    {"d": directive_id},
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    async def _resolve_impacts(self, conn: AsyncConnection, directive_id: uuid.UUID) -> None:
        await conn.execute(
            text(
                "UPDATE directive_impacts SET resolution = 'APPLIED', resolved_at = now()"
                " WHERE directive_id = :d AND resolution = 'PENDING'"
            ),
            {"d": directive_id},
        )

    async def _checkpoint(
        self, conn: AsyncConnection, d: dict[str, Any], run: store.RunState, artifacts: ArtifactStore
    ) -> uuid.UUID:
        return await checkpoints.create(
            conn, run_id=run.id, artifacts=artifacts, reason="directive", extra={"directive_id": str(d["id"])}
        )

    async def _replan(
        self, conn: AsyncConnection, d: dict[str, Any], run: store.RunState, impacts: list[dict[str, Any]]
    ) -> tuple[int, list[dict[str, Any]], list[uuid.UUID]]:
        """Новая plan_version: затронутые задачи заменяются/отменяются, при необходимости добавляется задача."""
        current = await store.load_run(conn, run.id, lock=True)
        if current is None:  # pragma: no cover — нарушение инварианта
            raise LookupError("current")
        pv = current.plan_version + 1
        fence = fence_untrusted(d["message"], d["id"])
        replacements: dict[uuid.UUID, uuid.UUID] = {}
        new_tasks: list[dict[str, Any]] = []
        cancel_ids: list[uuid.UUID] = []
        now = utcnow()
        for imp in impacts:
            task = (
                (await conn.execute(text("SELECT * FROM tasks WHERE id = :t FOR UPDATE"), {"t": imp["task_id"]}))
                .mappings()
                .one()
            )
            if task["status"] in fsm.TERMINAL["task"] or imp["impact_type"] not in ("REPLAN_REQUIRED", "TASK_CANCEL"):
                continue
            if imp["impact_type"] == "REPLAN_REQUIRED":
                new_id = uuid7()
                instructions = (task["instructions"] + "\n\n" + fence)[-MAX_INSTRUCTIONS:]
                await conn.execute(
                    text(
                        "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, origin_task_id,"
                        " agent_role, title, instructions, complexity, risk_level, acceptance_criteria, max_attempts,"
                        " timeout_seconds, retry_budget_seconds, max_review_loops) VALUES (:id, :ws, :run, :pv, :key,"
                        " :origin, :role, :title, :instr, :cx, :risk, CAST(:ac AS jsonb), :ma, :to, :rb, :mrl)"
                    ),
                    {
                        "id": new_id,
                        "ws": task["workspace_id"],
                        "run": run.id,
                        "pv": pv,
                        "key": task["task_key"],
                        "origin": task["id"],
                        "role": task["agent_role"],
                        "title": task["title"],
                        "instr": instructions,
                        "cx": task["complexity"],
                        "risk": task["risk_level"],
                        "ac": json.dumps(task["acceptance_criteria"], ensure_ascii=False),
                        "ma": task["max_attempts"],
                        "to": task["timeout_seconds"],
                        "rb": task["retry_budget_seconds"],
                        "mrl": task["max_review_loops"],
                    },
                )
                replacements[task["id"]] = new_id
                new_tasks.append({"id": new_id, "agent_role": task["agent_role"], "replaces": task["id"]})
            if task["status"] in NOT_STARTED:
                await store.transition_task(
                    conn, task["id"], "CANCELLED", reason=f"directive:{d['id']}", extra={"superseded_at": now}
                )
            else:
                cancel_ids.append(task["id"])  # safe point: процесс останавливается штатно, без подмены
            if imp["impact_type"] == "TASK_CANCEL":
                # Отменённая задача исключается из DAG: зависимые больше её не ждут.
                await conn.execute(
                    text("DELETE FROM task_dependencies WHERE depends_on_task_id = :t"), {"t": task["id"]}
                )
        for old, new in replacements.items():
            deps = (
                (
                    await conn.execute(
                        text("SELECT depends_on_task_id FROM task_dependencies WHERE task_id = :t"), {"t": old}
                    )
                )
                .scalars()
                .all()
            )
            for dep in deps:
                await conn.execute(
                    text(
                        "INSERT INTO task_dependencies (workspace_id, run_id, task_id, depends_on_task_id)"
                        " VALUES (:ws, :run, :t, :dep) ON CONFLICT DO NOTHING"
                    ),
                    {"ws": d["workspace_id"], "run": run.id, "t": new, "dep": replacements.get(dep, dep)},
                )
            await conn.execute(
                text(
                    "UPDATE task_dependencies SET depends_on_task_id = :new WHERE depends_on_task_id = :old"
                    " AND NOT (task_id = ANY(:replaced))"
                ),
                {"new": new, "old": old, "replaced": list(replacements)},
            )
        if d["impact_status"] == "TASK_ADD" and not replacements:
            new_id = uuid7()
            role = d["target_role"] or "developer"
            origin = None
            if d["target_task_id"] is not None:
                origin = d["target_task_id"]
                role = (
                    d["target_role"]
                    or (
                        await conn.execute(text("SELECT agent_role FROM tasks WHERE id = :t"), {"t": origin})
                    ).scalar_one()
                )
            title = ("Поручение: " + " ".join(d["message"].split()))[:200]
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, origin_task_id, agent_role,"
                    " title, instructions, complexity, risk_level, acceptance_criteria) VALUES (:id, :ws, :run, :pv,"
                    " :key, :origin, :role, :title, :instr, 2, 'medium', CAST(:ac AS jsonb))"
                ),
                {
                    "id": new_id,
                    "ws": d["workspace_id"],
                    "run": run.id,
                    "pv": pv,
                    "key": f"directive-{d['id'].hex[-8:]}",
                    "origin": origin,
                    "role": role,
                    "title": title,
                    "instr": fence,
                    "ac": json.dumps(
                        ["Поручение пользователя выполнено", "Обязательные проверки качества пройдены"],
                        ensure_ascii=False,
                    ),
                },
            )
            new_tasks.append({"id": new_id, "agent_role": role, "replaces": None})
        for t in new_tasks:
            await conn.execute(
                text(
                    "INSERT INTO directive_impacts (workspace_id, run_id, directive_id, task_id, impact_type, resolution,"
                    " details, resolved_at) VALUES (:ws, :run, :d, :t, 'TASK_ADD', 'APPLIED', CAST(:det AS jsonb), now())"
                ),
                {
                    "ws": d["workspace_id"],
                    "run": run.id,
                    "d": d["id"],
                    "t": t["id"],
                    "det": json.dumps({"replaces": str(t["replaces"]) if t["replaces"] else None, "plan_version": pv}),
                },
            )
        approved = pv if current.approved_plan_version is not None else None
        await conn.execute(
            text(
                "UPDATE runs SET plan_version = :pv, approved_plan_version = COALESCE(:apv, approved_plan_version),"
                " version = version + 1 WHERE id = :id"
            ),
            {"pv": pv, "apv": approved, "id": run.id},
        )
        if current.status == "AWAITING_APPROVAL":
            # План изменился до утверждения: старый запрос недействителен, нужен новый (DATA_FLOW §3.5).
            pending = (
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM approvals WHERE run_id = :r AND action_type = 'PLAN_APPROVAL'"
                            " AND status = 'PENDING'"
                        ),
                        {"r": run.id},
                    )
                )
                .scalars()
                .all()
            )
            for approval_id in pending:
                await self.approvals.cancel(conn, approval_id, reason="plan_changed")
            requester = (
                await conn.execute(text("SELECT requested_by FROM runs WHERE id = :r"), {"r": run.id})
            ).scalar_one()
            await self.approvals.request(
                conn,
                workspace_id=d["workspace_id"],
                project_id=await _project_of(conn, run.id),
                run_id=run.id,
                task_id=None,
                action_type="PLAN_APPROVAL",
                risk_level="medium",
                subject={"run_id": str(run.id), "plan_version": pv},
                requested_by_type="user",
                requested_by=requester,
            )
        await outbox.emit(
            conn,
            event_type="run.state_changed",
            aggregate_type="run",
            aggregate_id=run.id,
            workspace_id=d["workspace_id"],
            run_id=run.id,
            payload={
                "from": current.status,
                "to": current.status,
                "reason": "replanned",
                "plan_version": pv,
                "version": current.version + 1,
                "directive_id": str(d["id"]),
            },
        )
        return pv, new_tasks, cancel_ids

    async def _acknowledgements(
        self, conn: AsyncConnection, d: dict[str, Any], impacts: list[dict[str, Any]], new_tasks: list[dict[str, Any]]
    ) -> None:
        replaced = {t["replaces"] for t in new_tasks if t["replaces"]}
        recipients: list[tuple[str, uuid.UUID | None]] = []
        for imp in impacts:
            if imp["task_id"] in replaced:
                continue
            recipients.append((imp["agent_role"], imp["task_id"]))
        recipients.extend((t["agent_role"], t["id"]) for t in new_tasks)
        if not recipients:
            recipients.append((d["target_role"] or "planner", None))
        for role, task_id in dict.fromkeys(recipients):
            await conn.execute(
                text(
                    "INSERT INTO directive_acknowledgements (id, workspace_id, directive_id, recipient_role,"
                    " recipient_task_id) VALUES (:id, :ws, :d, :role, :t) ON CONFLICT DO NOTHING"
                ),
                {"id": uuid7(), "ws": d["workspace_id"], "d": d["id"], "role": role, "t": task_id},
            )

    # ---------- чтение ----------
    async def get(
        self, conn: AsyncConnection, directive_id: uuid.UUID, *, run_id: uuid.UUID | None = None
    ) -> dict[str, Any] | None:
        d = await _load(conn, directive_id)
        if d is None or (run_id is not None and d["run_id"] != run_id):
            return None
        return (await self._enrich(conn, [d]))[0]

    async def list_for_run(
        self,
        conn: AsyncConnection,
        run_id: uuid.UUID,
        *,
        statuses: list[str] | None = None,
        before: tuple[Any, uuid.UUID] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where = ["d.run_id = :run"]
        params: dict[str, Any] = {"run": run_id, "lim": limit}
        if statuses:
            where.append("d.status = ANY(:st)")
            params["st"] = statuses
        if before is not None:
            where.append("(d.created_at, d.id) < (:bt, :bid)")
            params["bt"], params["bid"] = before
        sql = _SELECT + " WHERE " + " AND ".join(where) + " ORDER BY d.created_at DESC, d.id DESC LIMIT :lim"
        rows = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
        return await self._enrich(conn, rows)

    async def _enrich(self, conn: AsyncConnection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return rows
        ids = [r["id"] for r in rows]
        impacts = (
            (
                await conn.execute(
                    text(
                        "SELECT directive_id, task_id, impact_type, resolution FROM directive_impacts"
                        " WHERE directive_id = ANY(:ids) ORDER BY created_at, task_id"
                    ),
                    {"ids": ids},
                )
            )
            .mappings()
            .all()
        )
        acks = (
            (
                await conn.execute(
                    text(
                        "SELECT directive_id, recipient_role, recipient_task_id, status, acknowledged_at"
                        " FROM directive_acknowledgements WHERE directive_id = ANY(:ids) ORDER BY created_at, id"
                    ),
                    {"ids": ids},
                )
            )
            .mappings()
            .all()
        )
        for r in rows:
            r["impacts"] = [dict(i) for i in impacts if i["directive_id"] == r["id"]]
            r["acknowledgements"] = [dict(a) for a in acks if a["directive_id"] == r["id"]]
        return rows


async def _project_of(conn: AsyncConnection, run_id: uuid.UUID) -> uuid.UUID:
    project_id: uuid.UUID = (
        await conn.execute(text("SELECT project_id FROM runs WHERE id = :r"), {"r": run_id})
    ).scalar_one()
    return project_id


async def finalize_superseded(conn: AsyncConnection, run_id: uuid.UUID) -> int:
    """Задачи, отменённые на safe point по поручению (REPLAN_REQUIRED/TASK_CANCEL), помечаются superseded,
    чтобы планировщик не считал их частью текущего плана. Вызывается после отмены исполнения (tick)."""
    result = await conn.execute(
        text(
            "UPDATE tasks t SET superseded_at = now(), version = t.version + 1 WHERE t.run_id = :r"
            " AND t.status = 'CANCELLED' AND t.superseded_at IS NULL AND EXISTS (SELECT 1 FROM directive_impacts i"
            "  WHERE i.task_id = t.id AND i.impact_type IN ('REPLAN_REQUIRED','TASK_CANCEL')"
            "  AND i.resolution = 'APPLIED')"
        ),
        {"r": run_id},
    )
    return int(result.rowcount or 0)


async def process(
    engine: AsyncEngine,
    workspace_id: uuid.UUID,
    directive_id: uuid.UUID,
    *,
    artifacts: ArtifactStore,
    cancel_task: CancelTask | None = None,
    service: Directives | None = None,
) -> dict[str, Any] | None:
    """Шаг воркера оркестрации по событию `directive.state_changed` (роль БД aicd_worker).

    analyze (если ожидает анализа) → apply (если CONFIRMED); каждый шаг — своя транзакция. Хуки safe point
    вызываются после commit. Ошибка apply ⇒ поручение FAILED с сообщением (без деталей SQL).
    """
    svc = service or Directives()
    ctx = DbContext(workspace_id=workspace_id, actor_type="system")
    async with transaction(engine, ctx) as conn:
        d = await svc.analyze(conn, directive_id)
    if d is None or d["status"] != "CONFIRMED":
        return d
    try:
        async with transaction(engine, ctx) as conn:
            result = await svc.apply(conn, directive_id, artifacts=artifacts)
    except Exception as exc:
        async with transaction(engine, ctx) as conn:
            current = await _load(conn, directive_id, lock=True)
            if current is not None and fsm.can_transition("directive", current["status"], "FAILED"):
                await _transition(
                    conn, current, "FAILED", failure_message=f"apply: {type(exc).__name__}", reason="apply_failed"
                )
        raise
    if cancel_task is not None:
        for task_id in result.cancel_task_ids:
            await cancel_task(task_id, "directive")
    return result.directive
