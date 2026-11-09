"""Генерация персональных уведомлений из outbox-событий (UI redesign B8, G-15; API_DESIGN §13.2).

Потребитель `notifications.project` (очередь `notifications`, воркер orchestrator) получает от worker-outbox
события из `SOURCE_EVENT_TYPES`, перечитывает состояние агрегата из PostgreSQL (контекст workspace события, роль
`aicd_worker`), определяет получателей и пишет строки `notifications` через `aicd_collaboration.notifications`.

Набор (минимальный и согласованный; источник → получатели):

| Событие | Условие | Вид | Получатели |
|---|---|---|---|
| `approval.requested` | approval ещё `PENDING` | `approval.requested` | участники, которые могут его решить (`evaluate_decider`: роль ≥ `required_role`, four-eyes) |
| `approval.decided` | `APPROVED`/`REJECTED`/`EXPIRED` | `approval.decided` | инициатор-пользователь, если решал не он сам |
| `run.completed` | `final_status` `COMPLETED`/`FAILED` | `run.completed`/`run.failed` | автор run (`runs.requested_by`) |
| `run.state_changed` | переход в `BLOCKED` (не `BLOCKED → BLOCKED`) | `run.blocked` | автор run |
| `directive.state_changed` | `AWAITING_CONFIRMATION`/`ACKNOWLEDGED`/`COMPLETED`/`FAILED`/`REJECTED` | `directive.*` | автор поручения (`submitted_by`) |
| `external_action.state_changed` | `pr_merge → CONFIRMED`; `pr_create`/`pr_merge → FAILED` | `pull_request.merged`/`.failed` | автор run |
| `gate.completed` | `findings.critical > 0` | `finding.critical` | автор run |

Отмена run (`CANCELLED`) уведомления не создаёт (инициатор — сам пользователь или каскад, о котором он уже
знает); наблюдателей run и упоминаний пользователей в v1 нет (упоминания чата адресованы ролям агентов).

Идемпотентность (at-least-once): отметка `processed_events(notifications.project, event_id)` и запись уведомлений —
в одной транзакции (сбой ⇒ откат обеих ⇒ повтор Dramatiq); второй барьер — `UNIQUE (workspace_id, user_id,
source_event_id, kind)` таблицы. Повтор после commit ⇒ `duplicate`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_collaboration import notifications as store
from aicd_identity.rbac import Role
from aicd_orchestration.approvals import evaluate_decider, summary_for
from aicd_platform import consumer
from aicd_platform.db import DbContext, transaction

CONSUMER: Final = "notifications.project"
SOURCE_EVENT_TYPES: Final = frozenset(
    {
        "approval.requested",
        "approval.decided",
        "run.completed",
        "run.state_changed",
        "directive.state_changed",
        "external_action.state_changed",
        "gate.completed",
    }
)
_DIRECTIVE_KINDS: Final = {
    "AWAITING_CONFIRMATION": ("directive.awaiting_confirmation", "warning", "Поручение ждёт вашего подтверждения"),
    "ACKNOWLEDGED": ("directive.acknowledged", "info", "Поручение принято агентами"),
    "COMPLETED": ("directive.completed", "success", "Поручение выполнено"),
    "FAILED": ("directive.failed", "error", "Поручение не выполнено"),
    "REJECTED": ("directive.failed", "error", "Поручение отклонено"),
}
_DECIDED: Final = {
    "APPROVED": ("success", "Запрос одобрен"),
    "REJECTED": ("warning", "Запрос отклонён"),
    "EXPIRED": ("warning", "Срок запроса истёк"),
}


def is_source(event: Mapping[str, Any]) -> bool:
    """Нужно ли событие потребителю (решение диспетчера о постановке в очередь `notifications`)."""
    return event.get("event_type") in SOURCE_EVENT_TYPES and event.get("workspace_id") is not None


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def _row(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    row = (await conn.execute(text(sql), params)).mappings().first()
    return dict(row) if row is not None else None


async def _run(conn: AsyncConnection, run_id: uuid.UUID | None) -> dict[str, Any] | None:
    if run_id is None:
        return None
    return await _row(
        conn,
        "SELECT id, project_id, requested_by, objective, status, blocked_reason FROM runs WHERE id = :r",
        {"r": run_id},
    )


def _run_draft(
    run: Mapping[str, Any],
    kind: str,
    severity: str,
    title: str,
    *,
    task_id: uuid.UUID | None = None,
    resource: tuple[str, uuid.UUID] | None = None,
    data: Mapping[str, Any] | None = None,
) -> store.Draft:
    rtype, rid = resource or ("run", run["id"])
    return store.Draft(
        user_id=run["requested_by"],
        kind=kind,
        severity=severity,
        title=title,
        body=store.summary(run["objective"]),
        project_id=run["project_id"],
        run_id=run["id"],
        task_id=task_id,
        resource_type=rtype,
        resource_id=rid,
        data=data or {},
    )


async def _approval_requested(conn: AsyncConnection, approval_id: uuid.UUID) -> list[store.Draft]:
    approval = await _row(
        conn,
        "SELECT a.id, a.workspace_id, a.project_id, a.run_id, a.task_id, a.action_type, a.status, a.risk_level,"
        " a.required_role, a.requested_by, a.subject, (a.expires_at IS NOT NULL AND a.expires_at <= now()) AS expired,"
        " (SELECT count(*) FROM workspace_members m WHERE m.workspace_id = a.workspace_id) AS members,"
        " COALESCE(w.settings -> 'allow_single_member_self_approval' = 'true'::jsonb, false) AS self_ok"
        " FROM approvals a JOIN workspaces w ON w.id = a.workspace_id WHERE a.id = :a",
        {"a": approval_id},
    )
    if approval is None or approval["status"] != "PENDING" or approval["expired"]:
        return []  # уже решён/истёк к моменту обработки — уведомление «нужно решение» неактуально
    members = (
        await conn.execute(
            text("SELECT user_id, role FROM workspace_members WHERE workspace_id = :ws ORDER BY user_id"),
            {"ws": approval["workspace_id"]},
        )
    ).all()
    subject = approval["subject"] if isinstance(approval["subject"], dict) else {}
    severity = "error" if approval["risk_level"] == "critical" else "warning"
    drafts = []
    for user_id, role in members:
        verdict = evaluate_decider(
            role=Role(role),
            action_type=approval["action_type"],
            required=approval["required_role"],
            actor_id=user_id,
            requested_by=approval["requested_by"],
            single_member=int(approval["members"]) == 1,
            self_approval_setting=bool(approval["self_ok"]),
        )
        if not verdict.allowed:
            continue
        drafts.append(
            store.Draft(
                user_id=user_id,
                kind="approval.requested",
                severity=severity,
                title="Требуется ваше решение",
                body=summary_for(approval["action_type"], subject),
                project_id=approval["project_id"],
                run_id=approval["run_id"],
                task_id=approval["task_id"],
                resource_type="approval",
                resource_id=approval["id"],
                data={"action_type": approval["action_type"], "risk_level": approval["risk_level"]},
            )
        )
    return drafts


async def _approval_decided(conn: AsyncConnection, approval_id: uuid.UUID) -> list[store.Draft]:
    approval = await _row(
        conn,
        "SELECT id, project_id, run_id, task_id, action_type, status, requested_by_type, requested_by, decided_by,"
        " subject FROM approvals WHERE id = :a",
        {"a": approval_id},
    )
    if approval is None or approval["status"] not in _DECIDED:
        return []
    requester = approval["requested_by"]
    if approval["requested_by_type"] != "user" or requester is None or requester == approval["decided_by"]:
        return []
    severity, title = _DECIDED[approval["status"]]
    subject = approval["subject"] if isinstance(approval["subject"], dict) else {}
    return [
        store.Draft(
            user_id=requester,
            kind="approval.decided",
            severity=severity,
            title=title,
            body=summary_for(approval["action_type"], subject),
            project_id=approval["project_id"],
            run_id=approval["run_id"],
            task_id=approval["task_id"],
            resource_type="approval",
            resource_id=approval["id"],
            data={"action_type": approval["action_type"], "status": approval["status"]},
        )
    ]


async def derive(conn: AsyncConnection, event: Mapping[str, Any]) -> list[store.Draft]:
    """Уведомления по событию (без записи). Состояние перечитывается из БД; payload — только подсказка."""
    etype = event.get("event_type")
    payload: Mapping[str, Any] = event.get("payload") or {}
    aggregate_id = _uuid(event.get("aggregate_id"))
    run_id = _uuid(event.get("run_id"))
    to_status = payload.get("to_status", payload.get("to"))
    if etype == "approval.requested":
        approval_id = _uuid(payload.get("approval_id")) or aggregate_id
        return await _approval_requested(conn, approval_id) if approval_id else []
    if etype == "approval.decided":
        approval_id = _uuid(payload.get("approval_id")) or aggregate_id
        return await _approval_decided(conn, approval_id) if approval_id else []
    if etype == "directive.state_changed":
        spec = _DIRECTIVE_KINDS.get(str(to_status))
        directive_id = _uuid(payload.get("directive_id")) or aggregate_id
        if spec is None or directive_id is None:
            return []
        directive = await _row(
            conn,
            "SELECT d.id, d.run_id, d.status, d.directive_type, d.submitted_by, d.target_task_id, r.project_id,"
            " r.objective FROM user_directives d JOIN runs r ON r.id = d.run_id WHERE d.id = :d",
            {"d": directive_id},
        )
        if directive is None:
            return []
        kind, severity, title = spec
        return [
            store.Draft(
                user_id=directive["submitted_by"],
                kind=kind,
                severity=severity,
                title=title,
                body=store.summary(directive["objective"]),
                project_id=directive["project_id"],
                run_id=directive["run_id"],
                task_id=directive["target_task_id"],
                resource_type="directive",
                resource_id=directive["id"],
                data={"directive_type": directive["directive_type"], "status": str(to_status)},
            )
        ]
    run = await _run(conn, run_id)
    if run is None:
        return []
    if etype == "run.completed":
        final = payload.get("final_status")
        if final == "COMPLETED":
            return [_run_draft(run, "run.completed", "success", "Run завершён", data={"final_status": final})]
        if final == "FAILED":
            return [_run_draft(run, "run.failed", "error", "Run завершился ошибкой", data={"final_status": final})]
        return []
    if etype == "run.state_changed":
        from_status = payload.get("from_status", payload.get("from"))
        if to_status != "BLOCKED" or from_status == "BLOCKED":
            return []
        reason = payload.get("blocked_reason") or run["blocked_reason"]
        return [_run_draft(run, "run.blocked", "warning", "Run заблокирован", data={"blocked_reason": reason})]
    if etype == "external_action.state_changed":
        action_type = payload.get("action_type")
        action_id = _uuid(payload.get("action_id")) or aggregate_id
        if action_id is None:
            return []
        data = {"action_type": action_type, "external_ref": payload.get("external_ref")}
        if action_type == "pr_merge" and to_status == "CONFIRMED":
            return [
                _run_draft(
                    run,
                    "pull_request.merged",
                    "success",
                    "Pull request слит",
                    resource=("external_action", action_id),
                    data=data,
                )
            ]
        if action_type in ("pr_create", "pr_merge") and to_status == "FAILED":
            title = "Не удалось слить pull request" if action_type == "pr_merge" else "Не удалось открыть pull request"
            return [
                _run_draft(
                    run, "pull_request.failed", "error", title, resource=("external_action", action_id), data=data
                )
            ]
        return []
    if etype == "gate.completed":
        findings = payload.get("findings")
        critical = findings.get("critical", 0) if isinstance(findings, Mapping) else 0
        gate_id = _uuid(payload.get("gate_execution_id")) or aggregate_id
        if not isinstance(critical, int) or critical <= 0 or gate_id is None:
            return []
        return [
            _run_draft(
                run,
                "finding.critical",
                "error",
                "Критические находки",
                task_id=_uuid(payload.get("task_id")),
                resource=("gate_execution", gate_id),
                data={"critical": critical, "gate_type": payload.get("gate_type"), "status": payload.get("status")},
            )
        ]
    return []


async def project(engine: AsyncEngine, event: Mapping[str, Any]) -> str:
    """Обработка события потребителем: `notified` / `no_recipients` / `duplicate` / `ignored`."""
    event_id = _uuid(event.get("event_id"))
    ws = _uuid(event.get("workspace_id"))
    if event_id is None or ws is None or not is_source(event):
        return "ignored"
    async with transaction(engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        if not await consumer.claim(conn, CONSUMER, event_id):
            return "duplicate"
        drafts = await derive(conn, event)
        created = await store.record(conn, workspace_id=ws, source_event_id=event_id, drafts=drafts)
    return "notified" if created else "no_recipients"
