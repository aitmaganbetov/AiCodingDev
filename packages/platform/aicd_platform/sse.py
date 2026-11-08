"""SSE-события run: проекция outbox → `run_event_log` и формат кадров (ADR-0011, EVENT_SCHEMAS §3).

Нормативные схемы: `packages/contracts/schemas/events/1.0/sse-event.schema.json`. Payload доменного
события (outbox) приводится к payload SSE-схемы; недостающие обязательные поля дочитываются из БД
в транзакции диспетчера (RLS-контекст workspace события).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform.clock import isoformat_z
from aicd_platform.redaction import redact_text

SCHEMA_VERSION = "1.0"

# Домен sse_event_type_persisted_t (миграция 0001): типы, попадающие в run_event_log.
PERSISTED_TYPES: frozenset[str] = frozenset(
    {
        "run.state_changed",
        "task.state_changed",
        "agent.message",
        "cli.output",
        "directive.impact_ready",
        "directive.state_changed",
        "approval.requested",
        "approval.decided",
        "gate.completed",
        "run.completed",
        "chat.message",
    }
)
# Доменные события, проецируемые в SSE под другим типом (EVENT_SCHEMAS §2): изменение ack поручения —
# SSE `directive.state_changed` с полем `recipients` (статус поручения не меняется).
PROJECTED_ALIASES: dict[str, str] = {"directive.ack_changed": "directive.state_changed"}
PROJECTED_TYPES: frozenset[str] = PERSISTED_TYPES | frozenset(PROJECTED_ALIASES)
TERMINAL_TYPE = "run.completed"
CLI_OUTPUT_MAX_BYTES = 8192  # ADR-0011 п.5
CLI_OUTPUT_MAX_CHARS = 4096  # схема cli_output.text maxLength

_RUN_STATUSES = frozenset(
    {
        "CREATED",
        "DISCOVERY",
        "PLANNED",
        "AWAITING_APPROVAL",
        "RUNNING",
        "VERIFYING",
        "COMPLETED",
        "PAUSED",
        "BLOCKED",
        "FAILED",
        "CANCELLED",
    }
)
_BLOCKED = frozenset(
    {
        "APPROVAL_REQUIRED",
        "PROVIDER_UNAVAILABLE",
        "AUTH_REQUIRED",
        "DIRECTIVE_REPLAN",
        "RUN_PAUSED",
        "DOCS_REQUIRED",
        "MANUAL_REVIEW",
        "REVIEW_LOOP_EXHAUSTED",
        "DEPENDENCY_FAILED",
    }
)
_FAILURE = frozenset(
    {
        "CLI_NOT_INSTALLED",
        "AUTH_REQUIRED",
        "RATE_LIMITED",
        "TIMEOUT",
        "SANDBOX_DENIED",
        "PROCESS_FAILED",
        "OUTPUT_PARSE_ERROR",
        "UNSUPPORTED_VERSION",
        "CANCELLED",
        "GATE_FAILED",
        "REVIEW_REJECTED",
        "LEASE_LOST",
        "WORKER_CRASH",
        "POLICY_DENIED",
        "INTERNAL_ERROR",
    }
)
_APPROVER_ROLES = frozenset({"owner", "admin", "developer", "reviewer"})
_DIRECTIVE_STATUSES = frozenset(
    {
        "RECEIVED",
        "IMPACT_ANALYSIS_PENDING",
        "IMPACT_READY",
        "AWAITING_CONFIRMATION",
        "CONFIRMED",
        "CHECKPOINTING",
        "REPLANNING",
        "DISPATCHED",
        "ACKNOWLEDGED",
        "COMPLETED",
        "REJECTED",
        "CANCELLED",
        "FAILED",
    }
)
_ACK_STATUSES = frozenset({"PENDING", "DELIVERED", "ACKNOWLEDGED", "REJECTED", "EXPIRED"})

# Разрешённые поля payload по типу (additionalProperties: false в схеме).
_ALLOWED: dict[str, frozenset[str]] = {
    "agent.message": frozenset(
        {
            "message_id",
            "message_type",
            "task_id",
            "from",
            "to",
            "correlation_id",
            "reply_to_id",
            "summary",
            "severity",
            "suspected_injection",
        }
    ),
    "cli.output": frozenset(
        {
            "execution_id",
            "task_id",
            "provider",
            "agent_role",
            "cli_seq",
            "cli_event_type",
            "stream",
            "text",
            "redactions",
            "truncated",
            "coalesced_count",
            "tool_kind",
            "tool_phase",
            "target",
            "artifact_id",
            "warning_code",
            "execution_status",
        }
    ),
    "directive.impact_ready": frozenset(
        {"directive_id", "summary", "impacts", "requires_confirmation", "approval_id", "expected_run_version"}
    ),
    "directive.state_changed": frozenset(
        {"directive_id", "directive_type", "from_status", "to_status", "reason", "recipients"}
    ),
    "gate.completed": frozenset(
        {
            "gate_execution_id",
            "gate_type",
            "task_id",
            "status",
            "required",
            "commit_sha",
            "evidence_id",
            "findings",
            "duration_ms",
            "tool",
        }
    ),
    "chat.message": frozenset(
        {
            "chat_message_id",
            "thread_id",
            "scope",
            "message_kind",
            "sender_type",
            "sender_id",
            "sender_role",
            "provider",
            "task_id",
            "target_role",
            "mentions",
            "body",
            "acp_type",
            "source_event_id",
            "directive_id",
            "approval_id",
            "links",
            "created_at",
        }
    ),
}


@dataclass(frozen=True)
class OutboxRecord:
    id: uuid.UUID
    created_at: datetime
    workspace_id: uuid.UUID | None
    run_id: uuid.UUID | None
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    schema_version: str
    payload: dict[str, Any]
    correlation_id: uuid.UUID | None
    causation_id: uuid.UUID | None
    publish_attempts: int = 0

    def message(self) -> dict[str, Any]:
        """Тело Dramatiq-сообщения (JSON-совместимое; потребитель перечитывает состояние из PG)."""
        return {
            "event_id": str(self.id),
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": str(self.aggregate_id),
            "workspace_id": str(self.workspace_id) if self.workspace_id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "occurred_at": isoformat_z(self.created_at),
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "causation_id": str(self.causation_id) if self.causation_id else None,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class Projection:
    event_type: str
    payload: dict[str, Any]
    task_id: uuid.UUID | None


def run_channel(run_id: uuid.UUID | str) -> str:
    """Redis pub/sub канал пробуждения SSE (только сигнал, не данные)."""
    return f"aicd:run:{run_id}"


def _str(value: Any, limit: int) -> str:
    return redact_text(str(value))[:limit]


def _enum(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _uuid_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _truncate_utf8(value: str, max_bytes: int, max_chars: int) -> tuple[str, bool]:
    cut = value[:max_chars]
    raw = cut.encode()
    if len(raw) > max_bytes:
        cut = raw[:max_bytes].decode(errors="ignore")
    return cut, cut != value


async def project(conn: AsyncConnection, event: OutboxRecord) -> Projection | None:
    """Payload SSE для outbox-события или None (событие не проецируется в run_event_log)."""
    if event.run_id is None or event.event_type not in PROJECTED_TYPES:
        return None
    p = event.payload
    etype = event.event_type
    if etype in ("directive.state_changed", "directive.ack_changed"):
        return await _project_directive(conn, event)
    if etype == "run.state_changed":
        to_status = _enum(p.get("to_status", p.get("to")), _RUN_STATUSES)
        if to_status is None:
            return None
        payload: dict[str, Any] = {
            "from_status": _enum(p.get("from_status", p.get("from")), _RUN_STATUSES),
            "to_status": to_status,
            "run_version": max(int(p.get("run_version", p.get("version", 0)) or 0), 0),
            "blocked_reason": _enum(p.get("blocked_reason"), _BLOCKED),
        }
        if isinstance(p.get("reason"), str):
            payload["reason"] = _str(p["reason"], 1000)
        return Projection(etype, payload, None)
    if etype == "task.state_changed":
        return await _project_task(conn, event)
    if etype == "run.completed":
        return await _project_run_completed(conn, event)
    if etype in ("approval.requested", "approval.decided"):
        return await _project_approval(conn, event)
    allowed = _ALLOWED[etype]
    payload = {k: v for k, v in p.items() if k in allowed}
    if etype == "cli.output" and isinstance(payload.get("text"), str):
        payload["text"], cut = _truncate_utf8(payload["text"], CLI_OUTPUT_MAX_BYTES, CLI_OUTPUT_MAX_CHARS)
        payload["truncated"] = bool(payload.get("truncated")) or cut
    task_id = _uuid_or_none(p.get("task_id"))
    return Projection(etype, payload, uuid.UUID(task_id) if task_id else None)


async def _project_directive(conn: AsyncConnection, event: OutboxRecord) -> Projection | None:
    """`directive.state_changed` / `directive.ack_changed` → SSE `directive.state_changed` (схема directive_state_changed)."""
    p = event.payload
    directive_id = _uuid_or_none(p.get("directive_id")) or str(event.aggregate_id)
    directive_type = p.get("directive_type")
    if not isinstance(directive_type, str):  # события до добавления поля — тип дочитывается из БД
        directive_type = (
            await conn.execute(text("SELECT directive_type FROM user_directives WHERE id = :id"), {"id": directive_id})
        ).scalar_one_or_none()
    if event.event_type == "directive.ack_changed":
        to_status = _enum(p.get("status"), _DIRECTIVE_STATUSES)
        from_status = to_status
    else:
        to_status = _enum(p.get("to_status", p.get("to")), _DIRECTIVE_STATUSES)
        from_status = _enum(p.get("from_status", p.get("from")), _DIRECTIVE_STATUSES)
    if to_status is None or not isinstance(directive_type, str):
        return None
    payload: dict[str, Any] = {
        "directive_id": directive_id,
        "directive_type": directive_type,
        "from_status": from_status,
        "to_status": to_status,
    }
    if isinstance(p.get("reason"), str):
        payload["reason"] = _str(p["reason"], 1000)
    if event.event_type == "directive.ack_changed":
        ack_status = _enum(p.get("ack_status"), _ACK_STATUSES)
        if ack_status is None or not isinstance(p.get("recipient_role"), str):
            return None
        payload["recipients"] = [
            {
                "role": p["recipient_role"],
                "ack_status": ack_status,
                "execution_id": _uuid_or_none(p.get("execution_id")),
                "at": p.get("at") if isinstance(p.get("at"), str) else None,
            }
        ]
    task_id = _uuid_or_none(p.get("recipient_task_id"))
    return Projection("directive.state_changed", payload, uuid.UUID(task_id) if task_id else None)


async def _project_task(conn: AsyncConnection, event: OutboxRecord) -> Projection | None:
    p = event.payload
    task_id = _uuid_or_none(p.get("task_id")) or str(event.aggregate_id)
    row = (
        (
            await conn.execute(
                text("SELECT agent_role, title, version, complexity, attempt_count FROM tasks WHERE id = :id"),
                {"id": task_id},
            )
        )
        .mappings()
        .first()
    )
    to_status = p.get("to_status", p.get("to"))
    if row is None or not isinstance(to_status, str):
        return None
    payload: dict[str, Any] = {
        "task_id": task_id,
        "title": _str(row["title"], 500),
        "agent_role": row["agent_role"],
        "from_status": p.get("from_status", p.get("from")),
        "to_status": to_status,
        "blocked_reason": _enum(p.get("blocked_reason"), _BLOCKED),
        # Версия задачи на момент проекции (outbox-payload её не несёт): монотонна, но может опережать событие.
        "task_version": int(p.get("task_version", row["version"])),
        "attempt_no": int(row["attempt_count"]) if row["attempt_count"] else None,
        "complexity": f"L{int(row['complexity'])}",
    }
    failure = _enum(p.get("failure_category"), _FAILURE)
    if failure is not None:
        payload["failure_category"] = failure
    return Projection("task.state_changed", payload, uuid.UUID(task_id))


async def _project_run_completed(conn: AsyncConnection, event: OutboxRecord) -> Projection | None:
    p = event.payload
    final = _enum(p.get("final_status"), frozenset({"COMPLETED", "FAILED", "CANCELLED"}))
    if final is None:
        return None
    gates = (
        (
            await conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE required) AS total,"
                    " count(*) FILTER (WHERE required AND status = 'PASS') AS passed"
                    " FROM gate_executions WHERE run_id = :r"
                ),
                {"r": event.run_id},
            )
        )
        .mappings()
        .one()
    )
    payload: dict[str, Any] = {
        "final_status": final,
        "required_gates_total": int(p.get("required_gates_total", gates["total"])),
        "required_gates_passed": int(p.get("required_gates_passed", gates["passed"])),
    }
    if isinstance(p.get("reason"), str):
        payload["reason"] = _str(p["reason"], 1000)
    return Projection("run.completed", payload, None)


async def _project_approval(conn: AsyncConnection, event: OutboxRecord) -> Projection | None:
    p = event.payload
    approval_id = _uuid_or_none(p.get("approval_id")) or str(event.aggregate_id)
    row = (
        (
            await conn.execute(
                text(
                    "SELECT action_type, status, task_id, required_role, requested_by_type, requested_by, subject,"
                    " decided_by, decision_reason, requested_at, expires_at FROM approvals WHERE id = :id"
                ),
                {"id": approval_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    task_id = row["task_id"]
    if event.event_type == "approval.decided":
        status = p.get("status", row["status"])
        if status not in ("APPROVED", "REJECTED", "EXPIRED", "CANCELLED"):
            return None
        reason = p.get("decision_reason", row["decision_reason"])
        decided: dict[str, Any] = {
            "approval_id": approval_id,
            "action_type": row["action_type"],
            "status": status,
            "decided_by": _uuid_or_none(p.get("decided_by", row["decided_by"])),
            "decision_reason": _str(reason, 2000) if reason is not None else None,
        }
        return Projection("approval.decided", decided, task_id)
    subject = row["subject"] if isinstance(row["subject"], dict) else {}
    summary = p.get("summary") or subject.get("summary") or row["action_type"]
    expires = row["expires_at"] or row["requested_at"] + timedelta(days=7)  # без срока в БД — ориентир для UI
    requested: dict[str, Any] = {
        "approval_id": approval_id,
        "action_type": row["action_type"],
        "task_id": str(task_id) if task_id else None,
        "summary": _str(summary, 2000),
        "required_role": row["required_role"] if row["required_role"] in _APPROVER_ROLES else "admin",
        "requested_by": {
            "type": row["requested_by_type"],
            "id": str(row["requested_by"]) if row["requested_by"] else None,
        },
        "expires_at": isoformat_z(expires),
    }
    return Projection("approval.requested", requested, task_id)


def envelope(row: dict[str, Any]) -> dict[str, Any]:
    """Конверт SseEnvelope из строки run_event_log (event_id, run_id, seq, event_type, occurred_at, payload)."""
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {
        "event_id": str(row["event_id"]),
        "run_id": str(row["run_id"]),
        "seq": int(row["seq"]),
        "type": row["event_type"],
        "timestamp": isoformat_z(row["occurred_at"]),
        "schema_version": row.get("schema_version") or SCHEMA_VERSION,
        "payload": payload,
    }


def frame(env: dict[str, Any]) -> str:
    data = json.dumps(env, ensure_ascii=False, separators=(",", ":"))
    return f"id: {env['seq']}\nevent: {env['type']}\ndata: {data}\n\n"


def heartbeat_frame(run_id: uuid.UUID, last_seq: int, now: datetime) -> str:
    """Heartbeat без `id:` — не сдвигает Last-Event-ID и не пишется в run_event_log."""
    ts = isoformat_z(now)
    env = {
        "event_id": str(uuid.uuid4()),
        "run_id": str(run_id),
        "seq": None,
        "type": "heartbeat",
        "timestamp": ts,
        "schema_version": SCHEMA_VERSION,
        "payload": {"server_time": ts, "last_seq": last_seq},
    }
    return f"event: heartbeat\ndata: {json.dumps(env, separators=(',', ':'))}\n\n"
