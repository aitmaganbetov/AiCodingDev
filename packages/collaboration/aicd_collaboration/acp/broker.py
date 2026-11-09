"""ACP Broker (AGENT_COMMUNICATION_PROTOCOL §1, §5 шаг 7, §6): конвейер + сохранение в одной транзакции.

`AcpBroker.submit(conn, candidate, sender=…, run_id=…)` выполняется в транзакции вызывающего (RLS-контекст
workspace отправителя, роль БД `aicd_worker`: только ей выданы INSERT agent_messages/message_deliveries):

- принятое сообщение: `agent_messages` + `message_deliveries` на каждого потребителя + outbox `agent.message`
  (проекция SSE через outbox_dispatcher) — атомарно; дубликат `message_id` — идемпотентный no-op;
- FIX_REQUEST: инкремент `tasks.review_loop_count`; превышение лимита — сообщение сохраняется с пометкой
  `loop_limit_exceeded` без доставки `orchestrator.acp_handler` (новая попытка не порождается), задача
  блокируется через порт `ReviewLoopEscalation`, `orchestrator` отправляет `BLOCKED` → `user`;
- отказ: в `agent_messages` не пишется; факт (хеш payload + 1 KiB санитизированного текста) — в
  `agent_activity_events` (`acp.rejected`) и `audit_events`, плюс outbox `acp.message_rejected`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_collaboration.acp import pipeline, render
from aicd_collaboration.acp.facts import RunInfo
from aicd_collaboration.acp.model import (
    AGENT,
    ORCHESTRATOR,
    USER,
    Accepted,
    LoopExhausted,
    Outcome,
    Rejected,
    RejectionCode,
    Sender,
)
from aicd_collaboration.acp.sql import SqlFacts
from aicd_platform import audit, outbox
from aicd_platform.clock import utcnow
from aicd_platform.db import ActorType
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text

# Потребители §7 (строка message_deliveries на каждого). SSE-проекцию выполняет outbox_dispatcher по
# событию `agent.message` (идемпотентно по event_id), отдельная доставка ей не нужна.
CONSUMER_ACP_HANDLER = "orchestrator.acp_handler"
CONSUMER_CHAT = "chat.projector"
CONSUMER_ACTIVITY = "activity.projector"
DEFAULT_CONSUMERS: tuple[str, ...] = (CONSUMER_ACP_HANDLER, CONSUMER_CHAT, CONSUMER_ACTIVITY)

OUTBOX_ACCEPTED = "agent.message"  # тип SSE (sse.PERSISTED_TYPES) и каталога EVENT_SCHEMAS §2.2
OUTBOX_REJECTED = "acp.message_rejected"
ACTIVITY_REJECTED = "acp.rejected"
EXCERPT_BYTES = 1024


class ReviewLoopEscalation(Protocol):
    """Порт оркестрации (§6): задача → BLOCKED(REVIEW_LOOP_EXHAUSTED) + approval MANUAL_REVIEW.

    Вызывается в транзакции брокера; возвращает id созданного approval (или None).
    """

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
    ) -> uuid.UUID | None: ...


def _uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _actor(sender: Sender) -> tuple[ActorType, uuid.UUID | None]:
    if sender.role == USER:
        return "user", sender.user_id
    return ("agent", None) if sender.role in AGENT else ("system", None)


class AcpBroker:
    def __init__(
        self,
        *,
        consumers: Sequence[str] = DEFAULT_CONSUMERS,
        escalation: ReviewLoopEscalation | None = None,
    ) -> None:
        self.consumers = tuple(consumers)
        self.escalation = escalation

    async def submit(
        self,
        conn: AsyncConnection,
        candidate: Any,
        *,
        sender: Sender,
        run_id: uuid.UUID,
        message_id: uuid.UUID | None = None,
        raw_size: int | None = None,
    ) -> Outcome:
        """Кандидат `{to, type, payload, reply_to_id?, correlation_id?}` → `Accepted` | `Rejected`.

        `message_id` задаёт вызывающий для идемпотентной повторной обработки (например, детерминированно из
        исполнения CLI); для `orchestrator`/`user` допускается `candidate["message_id"]`.
        """
        facts = SqlFacts(conn)
        c: Mapping[str, Any] = candidate if isinstance(candidate, Mapping) else {}
        if message_id is None and sender.role not in AGENT:
            message_id = _uuid(c.get("message_id"))
        message_id = message_id or uuid7()
        if raw_size is None:
            try:
                raw_size = len(json.dumps(candidate, ensure_ascii=False, default=str).encode())
            except (TypeError, ValueError):
                raw_size = None

        existing = await facts.message(message_id)
        if existing is not None:
            if existing.run_id != run_id:
                return await self._reject(
                    conn,
                    facts,
                    Rejected(
                        RejectionCode.CORRELATION_INVALID, "correlation.message_id_conflict", message_id=message_id
                    ),
                    sender=sender,
                    run_id=run_id,
                    envelope={"type": c.get("type"), "to": c.get("to"), "payload": c.get("payload")},
                )
            return Accepted(message_id=message_id, envelope=_envelope_of(existing), duplicate=True)

        correlation_id = _uuid(c.get("correlation_id"))
        reply_to_id = _uuid(c.get("reply_to_id"))
        if "correlation_id" not in c and reply_to_id is not None:
            parent = await facts.message(reply_to_id)
            if parent is not None and parent.run_id == run_id:
                correlation_id = parent.correlation_id  # ответы наследуют корреляцию (§2)
        envelope = pipeline.build_envelope(
            candidate,
            sender=sender,
            run_id=run_id,
            message_id=message_id,
            created_at=utcnow(),
            correlation_id=correlation_id,
        )
        result = await pipeline.evaluate(envelope, sender=sender, facts=facts, raw_size=raw_size)
        if isinstance(result, Rejected):
            return await self._reject(conn, facts, result, sender=sender, run_id=run_id, envelope=envelope)
        return await self._persist(conn, facts, result)

    # ---------- шаг 7 ----------

    async def _persist(self, conn: AsyncConnection, facts: SqlFacts, ev: pipeline.Evaluation) -> Accepted:
        env = ev.envelope
        run = ev.run
        exhausted = ev.loop_exhausted
        review_loop_count: int | None = None
        if env["type"] == "FIX_REQUEST" and ev.review_task is not None and exhausted is None:
            review_loop_count = await facts.increment_review_loop(ev.review_task.id)
        flags = {"loop_limit_exceeded": exhausted is not None, "suspected_injection": bool(ev.injection_hits)}
        consumers = [c for c in self.consumers if not (exhausted is not None and c == CONSUMER_ACP_HANDLER)]
        event_id = await insert_message(conn, run, env, ev.sender, consumers=consumers, flags=flags)
        blocked_id: uuid.UUID | None = None
        if exhausted is not None:
            blocked_id = await self._escalate(conn, run, ev.message_id, exhausted, env)
        return Accepted(
            message_id=ev.message_id,
            envelope=env,
            loop_exhausted=exhausted,
            review_task_id=ev.review_task.id if ev.review_task else None,
            review_loop_count=review_loop_count,
            suspected_injection=bool(ev.injection_hits),
            outbox_event_id=event_id,
            blocked_message_id=blocked_id,
            adjustments=ev.adjustments,
        )

    async def _escalate(
        self,
        conn: AsyncConnection,
        run: RunInfo,
        fix_request_id: uuid.UUID,
        exhausted: LoopExhausted,
        fix_envelope: Mapping[str, Any],
    ) -> uuid.UUID:
        approval_id: uuid.UUID | None = None
        if self.escalation is not None:
            approval_id = await self.escalation.review_loop_exhausted(
                conn,
                workspace_id=run.workspace_id,
                run_id=run.id,
                task_id=exhausted.task_id,
                fix_request_id=fix_request_id,
                correlation_id=exhausted.correlation_id,
                review_loop_count=exhausted.review_loop_count,
                max_review_loops=exhausted.max_review_loops,
            )
        refs = [f"message:{fix_request_id}"]
        for ref in fix_envelope["payload"].get("finding_refs") or []:
            if ref not in refs and ref.startswith("finding:"):
                refs.append(ref)
        payload: dict[str, Any] = {
            "reason": "REVIEW_LOOP_EXHAUSTED",
            "detail": (
                f"Исчерпан лимит review/fix циклов ({exhausted.review_loop_count} из {exhausted.max_review_loops}). "
                "Требуется решение: принять риск, отправить CHANGE_REQUEST, увеличить лимит или отменить задачу."
            ),
            "needed_from": "user",
            "unblock_condition": "решение пользователя по approval MANUAL_REVIEW",
            "evidence_refs": refs[:100],
        }
        if approval_id is not None:
            payload["approval_id"] = str(approval_id)
        message_id = uuid7()
        sender = Sender(role=ORCHESTRATOR)
        envelope = pipeline.build_envelope(
            {"to": {"role": USER}, "type": "BLOCKED", "payload": payload, "task_id": str(exhausted.task_id)},
            sender=sender,
            run_id=run.id,
            message_id=message_id,
            created_at=utcnow(),
            correlation_id=exhausted.correlation_id,
        )
        # Системное сообщение строит сам брокер: проверяются схема и размер (инварианты контракта).
        pipeline.check_schema(envelope, sender)
        pipeline.check_size(envelope)
        await insert_message(conn, run, envelope, sender, consumers=self.consumers, flags={})
        return message_id

    # ---------- отказ ----------

    async def _reject(
        self,
        conn: AsyncConnection,
        facts: SqlFacts,
        rejected: Rejected,
        *,
        sender: Sender,
        run_id: uuid.UUID,
        envelope: Mapping[str, Any],
    ) -> Rejected:
        payload_obj = envelope.get("payload")
        try:
            canonical = pipeline.canonical_json(payload_obj)
        except (TypeError, ValueError):
            canonical = repr(payload_obj).encode()
        excerpt = redact_text(canonical[:EXCERPT_BYTES].decode(errors="ignore"))
        to = envelope.get("to")
        mtype = envelope.get("type")
        details: dict[str, Any] = {
            "rejection_code": str(rejected.code),
            "reason": rejected.reason,
            "detail": redact_text(rejected.detail)[:1000],
            "message_id": str(rejected.message_id) if rejected.message_id else None,
            "message_type": mtype if isinstance(mtype, str) else None,
            "from_role": sender.role,
            "to_role": to.get("role") if isinstance(to, Mapping) and isinstance(to.get("role"), str) else None,
            "task_attempt_id": str(sender.task_attempt_id) if sender.task_attempt_id else None,
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
            "excerpt": excerpt,
        }
        run = await facts.run(run_id)
        workspace_id = run.workspace_id if run else None
        if run is not None:
            task_id = sender.task_id if sender.task_id and await facts.task_in_run(run.id, sender.task_id) else None
            await insert_activity(
                conn,
                workspace_id=run.workspace_id,
                run_id=run.id,
                task_id=task_id,
                execution_id=None,
                event_type=ACTIVITY_REJECTED,
                payload=details,
                correlation_id=None,
            )
        actor_type, actor_id = _actor(sender)
        resource_id = rejected.message_id or uuid7()
        if workspace_id is not None:
            await audit.record(
                conn,
                action="acp.message_rejected",
                resource_type="agent_message",
                resource_id=resource_id,
                actor_type=actor_type,
                actor_id=actor_id,
                workspace_id=workspace_id,
                outcome="denied",
                metadata={k: v for k, v in details.items() if k != "excerpt"},
            )
            await outbox.emit(
                conn,
                event_type=OUTBOX_REJECTED,
                aggregate_type="agent_message",
                aggregate_id=resource_id,
                workspace_id=workspace_id,
                run_id=run_id,
                payload={
                    "rejection_code": str(rejected.code),
                    "reason": rejected.reason,
                    "type": details["message_type"],
                    "from_role": sender.role,
                    "payload_sha256": details["payload_sha256"],
                },
            )
        repeated = 0
        if sender.task_attempt_id and rejected.code in (RejectionCode.ACL_DENIED, RejectionCode.POLICY_DENIED):
            repeated = await facts.denials_in_attempt(sender.task_attempt_id)
        return Rejected(
            rejected.code,
            rejected.reason,
            rejected.detail,
            message_id=rejected.message_id,
            repeated_denials=repeated,
        )


def _envelope_of(msg: Any) -> dict[str, Any]:
    """Конверт из сохранённой строки (для идемпотентного повтора)."""
    sender: dict[str, Any] = {"role": msg.sender_role}
    if msg.sender_user_id:
        sender["user_id"] = str(msg.sender_user_id)
    to: dict[str, Any] = {"role": msg.recipient_role}
    if msg.recipient_task_id:
        to["task_id"] = str(msg.recipient_task_id)
    return {
        "message_id": str(msg.id),
        "run_id": str(msg.run_id),
        "task_id": str(msg.task_id) if msg.task_id else None,
        "task_attempt_id": str(msg.task_attempt_id) if msg.task_attempt_id else None,
        "from": sender,
        "to": to,
        "type": msg.message_type,
        "correlation_id": str(msg.correlation_id),
        "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
        "payload": msg.payload,
    }


async def insert_message(
    conn: AsyncConnection,
    run: RunInfo,
    env: Mapping[str, Any],
    sender: Sender,
    *,
    consumers: Sequence[str],
    flags: Mapping[str, bool],
) -> uuid.UUID:
    """INSERT agent_messages + message_deliveries + outbox `agent.message` (вызывать в одной транзакции)."""
    message_id = uuid.UUID(env["message_id"])
    to = env["to"]
    await conn.execute(
        text(
            "INSERT INTO agent_messages (id, workspace_id, run_id, task_id, task_attempt_id, protocol, sender_role,"
            " sender_provider, sender_user_id, recipient_role, recipient_task_id, message_type, correlation_id,"
            " reply_to_id, schema_version, payload, source_execution_id, created_at)"
            " VALUES (:id, :ws, :run, :task, :attempt, :protocol, :srole, :sprov, :suser, :rrole, :rtask, :mtype,"
            " :corr, :reply, :sv, CAST(:payload AS jsonb), :exec, :created)"
        ),
        {
            "id": message_id,
            "ws": run.workspace_id,
            "run": run.id,
            "task": _uuid(env.get("task_id")),
            "attempt": _uuid(env.get("task_attempt_id")),
            "protocol": env["protocol"],
            "srole": sender.role,
            "sprov": sender.provider if sender.role in AGENT else None,
            "suser": sender.user_id if sender.role == USER else None,
            "rrole": to["role"],
            "rtask": _uuid(to.get("task_id")),
            "mtype": env["type"],
            "corr": uuid.UUID(env["correlation_id"]),
            "reply": _uuid(env.get("reply_to_id")),
            "sv": env["protocol"].split("/", 1)[1],
            "payload": json.dumps(env["payload"], ensure_ascii=False),
            "exec": sender.source_execution_id if sender.role in AGENT else None,
            "created": env["created_at"],
        },
    )
    for consumer in consumers:
        await conn.execute(
            text(
                "INSERT INTO message_deliveries (workspace_id, message_id, consumer_id) VALUES (:ws, :m, :c)"
                " ON CONFLICT DO NOTHING"
            ),
            {"ws": run.workspace_id, "m": message_id, "c": consumer},
        )
    payload = env["payload"]
    sse_from: dict[str, Any] = {"role": sender.role}
    if sender.role in AGENT and sender.provider:
        sse_from["provider"] = sender.provider
    event: dict[str, Any] = {
        # Поля SSE agent_message (sse.py _ALLOWED / sse-event.schema.json).
        "message_id": str(message_id),
        "message_type": env["type"],
        "task_id": env.get("task_id"),
        "from": sse_from,
        "to": {"role": to["role"]},
        "correlation_id": env["correlation_id"],
        "reply_to_id": env.get("reply_to_id"),
        "summary": render.summary(env["type"], payload),
        "suspected_injection": bool(flags.get("suspected_injection")),
    }
    sev = render.severity(env["type"], payload)
    if sev is not None:
        event["severity"] = sev
    # Поля только для потребителей Dramatiq (в SSE не проецируются).
    event["recipient_task_id"] = to.get("task_id")
    event["loop_limit_exceeded"] = bool(flags.get("loop_limit_exceeded"))
    event["consumers"] = list(consumers)
    return await outbox.emit(
        conn,
        event_type=OUTBOX_ACCEPTED,
        aggregate_type="agent_message",
        aggregate_id=message_id,
        workspace_id=run.workspace_id,
        run_id=run.id,
        correlation_id=uuid.UUID(env["correlation_id"]),
        payload=event,
    )


async def insert_activity(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    execution_id: uuid.UUID | None,
    event_type: str,
    payload: Mapping[str, Any],
    correlation_id: uuid.UUID | None,
    occurred_at: Any = None,
) -> uuid.UUID:
    activity_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO agent_activity_events (id, workspace_id, run_id, task_id, execution_id, event_type,"
            " payload, correlation_id, occurred_at)"
            " VALUES (:id, :ws, :run, :task, :exec, :et, CAST(:p AS jsonb), :corr, :at)"
        ),
        {
            "id": activity_id,
            "ws": workspace_id,
            "run": run_id,
            "task": task_id,
            "exec": execution_id,
            "et": event_type,
            "p": json.dumps(payload, ensure_ascii=False, default=str),
            "corr": correlation_id,
            "at": occurred_at or utcnow(),
        },
    )
    return activity_id
