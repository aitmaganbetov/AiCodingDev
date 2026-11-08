"""Конвейер валидации ACP (AGENT_COMMUNICATION_PROTOCOL §5) — чистое ядро без БД.

Порядок шагов фиксирован: транспортный лимит → 1 schema → 2 size → 3 ACL → 4 policy → 5 correlation →
6 limits. Первый отказ прекращает проверку и возвращает `Rejected` со стабильным кодом. Шаг 7 (persist +
outbox в одной транзакции) выполняет `broker.AcpBroker`.

Все факты о run/задачах/истории берутся через порт `facts.BrokerFacts`; payload — недоверенные данные:
он только валидируется и копируется, никогда не исполняется и не интерпретируется как инструкции.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aicd_collaboration.acp import acl, injection, schema
from aicd_collaboration.acp.facts import BrokerFacts, RunInfo, StoredMessage, TaskInfo
from aicd_collaboration.acp.model import (
    ACTIVE_RUN_STATUSES,
    AGENT,
    AGENT_BLOCKED_REASONS,
    MAX_ENVELOPE_BYTES,
    ORCHESTRATOR,
    PROTOCOL,
    TERMINAL_RUN_STATUSES,
    TRANSPORT_LIMIT_BYTES,
    USER,
    AcpRejection,
    LoopExhausted,
    Rejected,
    RejectionCode,
    Sender,
)

_REF = re.compile(
    r"^(artifact|evidence|finding|message|task|decision|gate_execution|context_pack|adr|requirement|approval|knowledge)"
    r":([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)

# §5 шаг 5: допустимые пары reply_to (тип ответа → типы исходного сообщения). Для прочих типов reply_to
# допускается на любое сообщение того же run и той же корреляции.
REPLY_PAIRS: Mapping[str, frozenset[str]] = {
    "ANSWER": frozenset({"QUESTION"}),
    "FIX_REQUEST": frozenset({"REVIEW_REQUEST"}),
    "FINDING": frozenset({"REVIEW_REQUEST"}),
    "DECISION_RECORDED": frozenset({"DECISION_PROPOSAL"}),
}
TASK_ASSIGN_STATUSES = frozenset({"READY", "LEASED"})
REVIEW_VERDICT_ROLES = frozenset({"reviewer", "security"})


def _section(src: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = src.get(key)
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class AcpLimits:
    """Лимиты §5 шаг 6 и §6; источник — `runs.policy_snapshot` (ключи `review.max_loops`, `acp.limits.*`)."""

    max_review_loops: int = 3
    max_messages_per_attempt: int = 50
    max_open_blocking_questions: int = 5
    max_messages_per_run_hour: int = 500
    max_reply_depth: int = 20
    require_human_approval: frozenset[str] = frozenset()  # decision_type, которые policy поднимает до true

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any] | None) -> AcpLimits:
        snap = snapshot or {}
        review = _section(snap, "review")
        acp = _section(snap, "acp")
        limits = _section(acp, "limits")

        def _int(src: Mapping[str, Any], key: str, default: int, lo: int, hi: int) -> int:
            value = src.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi else default

        approval = acp.get("require_human_approval")
        return cls(
            max_review_loops=_int(review, "max_loops", 3, 1, 10),
            max_messages_per_attempt=_int(limits, "messages_per_attempt", 50, 1, 1000),
            max_open_blocking_questions=_int(limits, "open_blocking_questions", 5, 1, 100),
            max_messages_per_run_hour=_int(limits, "messages_per_run_hour", 500, 1, 100000),
            max_reply_depth=_int(limits, "reply_depth", 20, 1, 100),
            require_human_approval=frozenset(str(t) for t in approval) if isinstance(approval, list) else frozenset(),
        )


@dataclass(frozen=True)
class Evaluation:
    """Результат шагов 0–6: сообщение допустимо к сохранению (шаг 7)."""

    envelope: dict[str, Any]
    run: RunInfo
    sender: Sender
    message_id: uuid.UUID
    correlation_id: uuid.UUID
    reply_to_id: uuid.UUID | None
    review_task: TaskInfo | None = None  # задача-реализация review-цикла (FIX_REQUEST)
    loop_exhausted: LoopExhausted | None = None
    injection_hits: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ()
    review_request: StoredMessage | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------- утилиты ----------


def canonical_json(value: Any) -> bytes:
    """Канонический JSON §5 шаг 2: сортированные ключи, без пробелов, UTF-8."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def iter_refs(payload: Any, depth: int = 0) -> Iterator[tuple[str, uuid.UUID]]:
    """Все типизированные ссылки `<kind>:<uuid>` в payload (§3.0)."""
    if depth > 20:
        return
    if isinstance(payload, str):
        m = _REF.match(payload)
        if m:
            yield m.group(1), uuid.UUID(m.group(2))
    elif isinstance(payload, Mapping):
        for v in payload.values():
            yield from iter_refs(v, depth + 1)
    elif isinstance(payload, list):
        for v in payload:
            yield from iter_refs(v, depth + 1)


def build_envelope(
    candidate: Any,
    *,
    sender: Sender,
    run_id: uuid.UUID,
    message_id: uuid.UUID,
    created_at: datetime,
    correlation_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Конверт формирует Orchestrator (§1): `from`, `run_id`, `task_id`, попытка, время — из контекста отправителя.

    Значения `from`/`run_id`/`task_id`/`created_at` кандидата игнорируются (подмена отправителя невозможна).
    Неструктурный кандидат даёт заведомо невалидный конверт (отказ на шаге 1).
    """
    c: Mapping[str, Any] = candidate if isinstance(candidate, Mapping) else {}
    if sender.role in AGENT:
        task_id: Any = str(sender.task_id) if sender.task_id else None
    else:
        task_id = c.get("task_id")
        if task_id is None and c.get("type") == "TASK_ASSIGN" and isinstance(c.get("payload"), Mapping):
            task_id = c["payload"].get("task_id")
    env: dict[str, Any] = {
        "protocol": PROTOCOL,
        "message_id": str(message_id),
        "run_id": str(run_id),
        "task_id": task_id,
        "task_attempt_id": str(sender.task_attempt_id) if sender.role in AGENT and sender.task_attempt_id else None,
        "from": sender.as_from(),
        "to": c.get("to"),
        "type": c.get("type"),
        "correlation_id": str(correlation_id) if correlation_id else c.get("correlation_id", str(message_id)),
        "reply_to_id": c.get("reply_to_id"),
        "payload": c.get("payload"),
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }
    if not isinstance(candidate, Mapping):
        env["payload"] = None
    return env


# ---------- конвейер ----------


async def evaluate(
    envelope: dict[str, Any],
    *,
    sender: Sender,
    facts: BrokerFacts,
    raw_size: int | None = None,
) -> Evaluation | Rejected:
    """Шаги 0–6 §5. Возвращает `Evaluation` (можно сохранять) или `Rejected`."""
    message_id = _uuid(envelope.get("message_id"))
    try:
        return await _evaluate(envelope, sender=sender, facts=facts, raw_size=raw_size)
    except AcpRejection as rej:
        return Rejected(rej.code, rej.reason, rej.detail, message_id=message_id)


def _deny(code: RejectionCode, reason: str, detail: str = "") -> AcpRejection:
    return AcpRejection(code, reason, detail)


def check_schema(envelope: Mapping[str, Any], sender: Sender) -> None:
    """Шаг 1 (+ транспорт не входит): JSON Schema конверта и payload, семантика схемы, не выразимая в JSON Schema."""
    errors = schema.envelope_errors(envelope)
    if errors:
        reason = "schema.payload" if errors[0].startswith("/payload") else "schema.envelope"
        raise _deny(RejectionCode.SCHEMA_INVALID, reason, "; ".join(errors))
    # §3.12: review_verdict обязателен для задач reviewer/security.
    if (
        envelope["type"] == "TASK_COMPLETED"
        and sender.role in REVIEW_VERDICT_ROLES
        and "review_verdict" not in envelope["payload"]
    ):
        raise _deny(RejectionCode.SCHEMA_INVALID, "schema.review_verdict_required", "/payload/review_verdict")


def check_size(envelope: Mapping[str, Any], raw_size: int | None = None) -> None:
    if raw_size is not None and raw_size > TRANSPORT_LIMIT_BYTES:
        raise _deny(RejectionCode.PAYLOAD_TOO_LARGE, "size.transport", f"{raw_size} > {TRANSPORT_LIMIT_BYTES}")
    size = len(canonical_json(envelope))
    if size > MAX_ENVELOPE_BYTES:
        raise _deny(RejectionCode.PAYLOAD_TOO_LARGE, "size.envelope", f"{size} > {MAX_ENVELOPE_BYTES}")


async def _evaluate(
    envelope: dict[str, Any], *, sender: Sender, facts: BrokerFacts, raw_size: int | None
) -> Evaluation | Rejected:
    # Транспортный лимит (P0) проверяется до разбора конверта, размер канонического JSON — после схемы.
    if raw_size is not None and raw_size > TRANSPORT_LIMIT_BYTES:
        raise _deny(RejectionCode.PAYLOAD_TOO_LARGE, "size.transport", f"{raw_size} > {TRANSPORT_LIMIT_BYTES}")

    # 1. Schema
    check_schema(envelope, sender)
    env = envelope
    mtype: str = env["type"]
    payload: dict[str, Any] = env["payload"]
    to_role: str = env["to"]["role"]
    message_id = uuid.UUID(env["message_id"])
    run_id = uuid.UUID(env["run_id"])
    correlation_id = uuid.UUID(env["correlation_id"])
    reply_to_id = _uuid(env.get("reply_to_id"))
    to_task_id = _uuid(env["to"].get("task_id"))
    task_id = _uuid(env.get("task_id"))
    attempt_id = _uuid(env.get("task_attempt_id"))

    # 2. Size
    check_size(env)

    # 3. ACL (статический + условия §4.1, требующие фактов)
    run = await facts.run(run_id)
    narrow = acl.narrowing_from_snapshot(run.policy_snapshot) if run else None
    denial = acl.static_denial(sender.role, mtype, to_role, payload, narrow)
    if denial:
        raise _deny(RejectionCode.ACL_DENIED, denial, f"{sender.role} → {mtype} → {to_role}")
    reply_to = await facts.message(reply_to_id) if reply_to_id else None
    if reply_to is not None and reply_to.run_id != run_id:
        reply_to = None  # чужой run — не раскрываем, отказ на шаге 5
    review_request = await _review_request(facts, run_id, correlation_id, reply_to) if run else None
    if run is not None:
        await _check_dynamic_acl(env, sender, facts, run_id, task_id, to_task_id, reply_to, review_request)

    # 4. Policy
    if run is None:
        raise _deny(RejectionCode.POLICY_DENIED, "policy.run_not_found")
    adjustments = await _check_policy(env, sender, facts, run, task_id, attempt_id, to_task_id)
    limits = AcpLimits.from_snapshot(run.policy_snapshot)
    raise_approval = mtype == "DECISION_PROPOSAL" and payload["decision_type"] in limits.require_human_approval
    if raise_approval and not payload["requires_human_approval"]:
        payload["requires_human_approval"] = True  # Orchestrator может только повысить (§3.9)
        adjustments.append("requires_human_approval_raised")

    # 5. Correlation
    await _check_correlation(env, facts, run_id, message_id, correlation_id, reply_to_id, reply_to, limits)
    if mtype == "FIX_REQUEST" and review_request is None:
        raise _deny(RejectionCode.CORRELATION_INVALID, "correlation.no_review_request", str(correlation_id))

    # 6. Limits
    review_task, exhausted = await _check_limits(
        env, sender, facts, run_id, task_id, attempt_id, to_task_id, review_request, limits
    )

    return Evaluation(
        envelope=env,
        run=run,
        sender=sender,
        message_id=message_id,
        correlation_id=correlation_id,
        reply_to_id=reply_to_id,
        review_task=review_task,
        loop_exhausted=exhausted,
        injection_hits=tuple(injection.scan(payload)),
        adjustments=tuple(adjustments),
        review_request=review_request,
    )


async def _review_request(
    facts: BrokerFacts, run_id: uuid.UUID, correlation_id: uuid.UUID, reply_to: StoredMessage | None
) -> StoredMessage | None:
    """REVIEW_REQUEST review-цикла: цель reply_to либо первый REVIEW_REQUEST корреляции."""
    if reply_to is not None and reply_to.message_type == "REVIEW_REQUEST":
        return reply_to
    found = await facts.correlation_messages(run_id, correlation_id, "REVIEW_REQUEST")
    return found[0] if found else None


async def _check_dynamic_acl(
    env: Mapping[str, Any],
    sender: Sender,
    facts: BrokerFacts,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    to_task_id: uuid.UUID | None,
    reply_to: StoredMessage | None,
    review_request: StoredMessage | None,
) -> None:
    mtype = env["type"]
    to_role = env["to"]["role"]
    acl_denied = RejectionCode.ACL_DENIED
    if mtype == "QUESTION" and sender.role in AGENT and to_role in AGENT:
        if not await facts.role_has_task(run_id, to_role):
            raise _deny(acl_denied, "acl.question_recipient_no_task", to_role)
    elif mtype == "HANDOFF" and to_role != ORCHESTRATOR and task_id is not None:
        connected = (
            await facts.dag_connected(task_id, to_task_id)
            if to_task_id
            else await facts.dag_connected_to_role(task_id, to_role)
        )
        if not connected:
            raise _deny(acl_denied, "acl.handoff_no_dag_edge", to_role)
    elif mtype == "ANSWER" and reply_to is not None and reply_to.message_type == "QUESTION":
        if to_role != reply_to.sender_role:
            raise _deny(acl_denied, "acl.answer_recipient", f"ожидался {reply_to.sender_role}")
        if sender.role != reply_to.recipient_role and sender.role != USER:
            raise _deny(acl_denied, "acl.answer_sender", f"вопрос адресован {reply_to.recipient_role}")
    elif mtype == "FINDING":
        allowed = {ORCHESTRATOR} | ({review_request.sender_role} if review_request else set())
        if to_role not in allowed:
            raise _deny(acl_denied, "acl.finding_recipient", f"ожидался один из {sorted(allowed)}")
    elif mtype == "FIX_REQUEST" and review_request is not None and to_role != review_request.sender_role:
        raise _deny(acl_denied, "acl.fix_recipient", f"ожидался {review_request.sender_role}")


async def _check_policy(
    env: Mapping[str, Any],
    sender: Sender,
    facts: BrokerFacts,
    run: RunInfo,
    task_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None,
    to_task_id: uuid.UUID | None,
) -> list[str]:
    mtype = env["type"]
    payload: Mapping[str, Any] = env["payload"]
    to_role = env["to"]["role"]
    denied = RejectionCode.POLICY_DENIED
    adjustments: list[str] = []

    # Статус run: агенты/пользователь — §5 шаг 4; orchestrator — любой нетерминальный статус.
    if sender.role == ORCHESTRATOR:
        if run.status in TERMINAL_RUN_STATUSES:
            raise _deny(denied, "policy.run_status", run.status)
    elif run.status not in ACTIVE_RUN_STATUSES:
        raise _deny(denied, "policy.run_status", run.status)

    # Отправитель-агент: роль задачи и активная попытка (fencing).
    if sender.role in AGENT:
        if task_id is None or attempt_id is None:
            raise _deny(denied, "policy.agent_without_task")
        task = await facts.task_in_run(run.id, task_id)
        if task is None or task.agent_role != sender.role:
            raise _deny(denied, "policy.sender_task", "отправитель — не роль задачи task_id")
        if not await facts.attempt_active(attempt_id, task_id):
            raise _deny(denied, "policy.attempt_inactive")
    elif task_id is not None and await facts.task_in_run(run.id, task_id) is None:
        raise _deny(denied, "policy.task_not_in_run")

    if to_task_id is not None:
        target = await facts.task_in_run(run.id, to_task_id)
        if target is None or target.agent_role != to_role:
            raise _deny(denied, "policy.recipient_task", "to.task_id не задача роли-получателя в этом run")

    if mtype == "BLOCKED" and sender.role in AGENT and payload["reason"] not in AGENT_BLOCKED_REASONS:
        raise _deny(denied, "policy.blocked_reason", payload["reason"])

    refs = list(dict.fromkeys(iter_refs(payload)))
    if refs:
        missing = await facts.missing_refs(run, refs)
        if missing:
            raise _deny(denied, "policy.foreign_ref", ", ".join(missing[:5]))

    if mtype == "TASK_ASSIGN":
        assigned_id = uuid.UUID(payload["task_id"])
        if task_id is not None and task_id != assigned_id:
            raise _deny(denied, "policy.assign_task_mismatch")
        assigned = await facts.task_in_run(run.id, assigned_id)
        if assigned is None or assigned.status not in TASK_ASSIGN_STATUSES:
            raise _deny(denied, "policy.assign_task_status", assigned.status if assigned else "not_found")
        if assigned.agent_role != to_role:
            raise _deny(denied, "policy.assign_role", f"роль задачи {assigned.agent_role}")
    elif mtype == "EVIDENCE_SUBMITTED":
        for item in payload["items"]:
            kind, _, raw = item["artifact_ref"].partition(":")
            if kind != "artifact":
                raise _deny(denied, "policy.evidence_ref_kind", kind)
            sha = await facts.artifact_sha256(run.project_id, uuid.UUID(raw))
            if sha != item["sha256"]:
                raise _deny(denied, "policy.evidence_sha256", item["artifact_ref"])
    elif mtype == "DECISION_RECORDED":
        if not await facts.decision_exists(run.id, uuid.UUID(payload["decision_id"])):
            raise _deny(denied, "policy.decision_missing")
        approval = payload.get("approval_id")
        if approval is not None and await facts.approval_status(uuid.UUID(approval)) != "APPROVED":
            raise _deny(denied, "policy.approval_not_approved")
    return adjustments


async def _check_correlation(
    env: Mapping[str, Any],
    facts: BrokerFacts,
    run_id: uuid.UUID,
    message_id: uuid.UUID,
    correlation_id: uuid.UUID,
    reply_to_id: uuid.UUID | None,
    reply_to: StoredMessage | None,
    limits: AcpLimits,
) -> None:
    invalid = RejectionCode.CORRELATION_INVALID
    mtype = env["type"]
    if reply_to_id is not None:
        if reply_to is None:
            raise _deny(invalid, "correlation.reply_to_missing", str(reply_to_id))
        pairs = REPLY_PAIRS.get(mtype)
        if pairs is not None and reply_to.message_type not in pairs:
            raise _deny(invalid, "correlation.reply_to_type", f"{mtype} → {reply_to.message_type}")
        if reply_to.correlation_id != correlation_id:
            raise _deny(invalid, "correlation.mismatch", "correlation_id ≠ корреляции reply_to")
        if await facts.reply_depth(reply_to_id) + 1 > limits.max_reply_depth:
            raise _deny(invalid, "correlation.depth", f"> {limits.max_reply_depth}")
        if mtype == "ANSWER" and await facts.answer_exists(reply_to_id):
            raise _deny(invalid, "correlation.already_answered", str(reply_to_id))
    elif correlation_id != message_id and not await facts.correlation_known(run_id, correlation_id):
        raise _deny(invalid, "correlation.unknown", str(correlation_id))


async def _check_limits(
    env: Mapping[str, Any],
    sender: Sender,
    facts: BrokerFacts,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None,
    to_task_id: uuid.UUID | None,
    review_request: StoredMessage | None,
    limits: AcpLimits,
) -> tuple[TaskInfo | None, LoopExhausted | None]:
    rate = RejectionCode.RATE_LIMITED_MESSAGES
    mtype = env["type"]
    if attempt_id is not None and await facts.messages_from_attempt(attempt_id) >= limits.max_messages_per_attempt:
        raise _deny(rate, "limit.attempt_messages", f"≥ {limits.max_messages_per_attempt}")
    if await facts.messages_in_run_last_hour(run_id) >= limits.max_messages_per_run_hour:
        raise _deny(rate, "limit.run_hourly", f"≥ {limits.max_messages_per_run_hour}")
    if (
        mtype == "QUESTION"
        and env["payload"]["blocking"]
        and task_id is not None
        and await facts.open_blocking_questions(task_id) >= limits.max_open_blocking_questions
    ):
        raise _deny(rate, "limit.open_blocking_questions", f"≥ {limits.max_open_blocking_questions}")

    if mtype != "FIX_REQUEST":
        return None, None
    # §6: счётчик — tasks.review_loop_count задачи-реализации (получатель FIX_REQUEST / автор REVIEW_REQUEST).
    impl_task_id = to_task_id or (review_request.task_id if review_request else None)
    if impl_task_id is None:
        return None, None
    task = await facts.task_in_run(run_id, impl_task_id)
    if task is None:
        return None, None
    # Fix-задачи циклов (origin_task_id) не обнуляют счётчик: считаем по исходной реализации цепочки.
    seen = {task.id}
    while task.origin_task_id is not None and task.origin_task_id not in seen:
        origin = await facts.task_in_run(run_id, task.origin_task_id)
        if origin is None:
            break
        seen.add(origin.id)
        task = origin
    if task.status == "BLOCKED" and task.blocked_reason == "REVIEW_LOOP_EXHAUSTED":
        raise _deny(RejectionCode.LOOP_LIMIT, "limit.review_loop_exhausted", f"{task.review_loop_count} циклов")
    max_loops = task.max_review_loops if task.max_review_loops >= 1 else limits.max_review_loops
    if task.review_loop_count + 1 > max_loops:
        corr = uuid.UUID(env["correlation_id"])
        return task, LoopExhausted(task.id, task.review_loop_count, max_loops, corr)
    return task, None
