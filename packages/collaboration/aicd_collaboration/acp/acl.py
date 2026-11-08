"""Шаг 3 конвейера ACP: статический ACL (AGENT_COMMUNICATION_PROTOCOL §4).

- `SEND_MATRIX` — §4.2 «роль × тип» (отправка), дословно;
- `SENDERS_BY_TYPE` / `recipient_denial` — §4.1 (отправители и получатели по типу, условия по payload).

Условия §4.1, требующие фактов БД (ребро DAG, задача получателя в run, автор REVIEW_REQUEST, отправитель
QUESTION), проверяет `pipeline` через порт фактов. Policy проекта может только **сужать** матрицу
(`policy_snapshot.acp_acl.send = {role: [types]}`), но не расширять.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aicd_collaboration.acp.model import AGENT, IMPL, MESSAGE_TYPES, ORCHESTRATOR, USER, VERIF

_ALL_AGENT_TYPES = frozenset(
    {"HANDOFF", "QUESTION", "ANSWER", "EVIDENCE_SUBMITTED", "DECISION_PROPOSAL", "BLOCKED", "TASK_COMPLETED"}
)

# §4.2: ✓ — разрешено. Ключ — отправитель (acp_participant).
SEND_MATRIX: Mapping[str, frozenset[str]] = {
    "orchestrator": frozenset({"TASK_ASSIGN", "DECISION_RECORDED", "BLOCKED"}),
    "user": frozenset({"QUESTION", "ANSWER"}),
    "requirements": _ALL_AGENT_TYPES,
    "architect": _ALL_AGENT_TYPES | {"REVIEW_REQUEST", "FINDING", "FIX_REQUEST"},
    "planner": _ALL_AGENT_TYPES,
    "developer": _ALL_AGENT_TYPES | {"REVIEW_REQUEST"},
    "reviewer": (_ALL_AGENT_TYPES - {"HANDOFF"}) | {"FINDING", "FIX_REQUEST"},
    "security": (_ALL_AGENT_TYPES - {"HANDOFF"}) | {"FINDING", "FIX_REQUEST"},
    "qa": _ALL_AGENT_TYPES | {"FINDING", "FIX_REQUEST"},  # HANDOFF только test_plan_ready (сноска ¹)
    "database": _ALL_AGENT_TYPES | {"REVIEW_REQUEST", "FINDING", "FIX_REQUEST"},
    "performance": _ALL_AGENT_TYPES | {"REVIEW_REQUEST", "FINDING"},
    "devops": _ALL_AGENT_TYPES | {"REVIEW_REQUEST"},
    "maintenance": _ALL_AGENT_TYPES | {"REVIEW_REQUEST"},
    "documentation": _ALL_AGENT_TYPES | {"REVIEW_REQUEST"},
}

# §4.1 «Разрешённые отправители» (сверяется с SEND_MATRIX тестом согласованности).
SENDERS_BY_TYPE: Mapping[str, frozenset[str]] = {
    "TASK_ASSIGN": frozenset({ORCHESTRATOR}),
    "HANDOFF": frozenset({"requirements", "architect", "planner", "qa"}) | IMPL,
    "QUESTION": AGENT | {USER},
    "ANSWER": AGENT | {USER},
    "REVIEW_REQUEST": IMPL | {"architect"},
    "FINDING": VERIF | {"architect", "database", "performance"},
    "FIX_REQUEST": VERIF | {"architect", "database"},
    "EVIDENCE_SUBMITTED": AGENT,
    "DECISION_PROPOSAL": AGENT,
    "DECISION_RECORDED": frozenset({ORCHESTRATOR}),
    "BLOCKED": AGENT | {ORCHESTRATOR},
    "TASK_COMPLETED": AGENT,
}

# §4.1 HANDOFF: отправитель → допустимые получатели.
HANDOFF_ROUTES: Mapping[str, frozenset[str]] = {
    "requirements": frozenset({"architect", "planner"}),
    "architect": frozenset({"planner", "database", "developer"}),
    "planner": frozenset({ORCHESTRATOR}),
    "qa": frozenset({"developer", ORCHESTRATOR}),
    **{role: IMPL | {"qa", "documentation"} for role in IMPL},
}

# Автор проверяемой работы — отправитель REVIEW_REQUEST.
REVIEW_AUTHORS: frozenset[str] = IMPL | {"architect"}
REVIEWERS: frozenset[str] = VERIF | {"architect", "database"}


def allowed_types(role: str, narrow: Mapping[str, Iterable[str]] | None = None) -> frozenset[str]:
    """Типы, которые `role` может отправлять: §4.2 ∩ сужение policy (если задано для роли)."""
    base = SEND_MATRIX.get(role, frozenset())
    if narrow is not None and role in narrow:
        return base & frozenset(narrow[role])
    return base


def sender_denial(role: str, message_type: str, narrow: Mapping[str, Iterable[str]] | None = None) -> str | None:
    if message_type not in MESSAGE_TYPES:
        return "acl.unknown_type"
    if message_type not in SEND_MATRIX.get(role, frozenset()):
        return "acl.sender_type"
    if message_type not in allowed_types(role, narrow):
        return "acl.policy_narrowed"
    return None


def recipient_denial(sender: str, message_type: str, recipient: str, payload: Mapping[str, Any]) -> str | None:
    """Статические условия §4.1 на получателя (без фактов БД); None — разрешено."""
    t = message_type
    if t == "TASK_ASSIGN":
        return None if recipient in AGENT else "acl.recipient"
    if t == "DECISION_RECORDED":
        return None if recipient in AGENT or recipient == USER else "acl.recipient"
    if t == "HANDOFF":
        kind = payload.get("handoff_kind")
        if kind == "plan_ready" and not (sender == "planner" and recipient == ORCHESTRATOR):
            return "acl.handoff_plan_ready"
        if sender == "qa" and kind != "test_plan_ready":
            return "acl.handoff_qa_kind"
        return None if recipient in HANDOFF_ROUTES.get(sender, frozenset()) else "acl.handoff_route"
    if t == "QUESTION":
        if sender == USER:
            return None if recipient in AGENT else "acl.recipient"
        return None if recipient in AGENT or recipient == USER else "acl.recipient"
    if t == "ANSWER":
        return None if recipient in AGENT or recipient == USER else "acl.recipient"
    if t == "REVIEW_REQUEST":
        if recipient == sender:
            return "acl.self_review"
        return None if recipient in REVIEWERS else "acl.recipient"
    if t == "FINDING":
        return None if recipient in REVIEW_AUTHORS or recipient == ORCHESTRATOR else "acl.recipient"
    if t == "FIX_REQUEST":
        return None if recipient in IMPL else "acl.recipient"
    if t in ("EVIDENCE_SUBMITTED", "DECISION_PROPOSAL", "TASK_COMPLETED"):
        return None if recipient == ORCHESTRATOR else "acl.recipient"
    if t == "BLOCKED":
        expected = USER if sender == ORCHESTRATOR else ORCHESTRATOR
        return None if recipient == expected else "acl.recipient"
    return "acl.unknown_type"


def static_denial(
    sender: str,
    message_type: str,
    recipient: str,
    payload: Mapping[str, Any],
    narrow: Mapping[str, Iterable[str]] | None = None,
) -> str | None:
    """Полная статическая проверка ACL: отправитель × тип × получатель; None — разрешено."""
    return sender_denial(sender, message_type, narrow) or recipient_denial(sender, message_type, recipient, payload)


def narrowing_from_snapshot(snapshot: Mapping[str, Any] | None) -> Mapping[str, Iterable[str]] | None:
    """`policy_snapshot.acp_acl.send` → сужение; некорректная форма игнорируется только целиком (fail-closed)."""
    if not snapshot:
        return None
    acl = snapshot.get("acp_acl")
    if not isinstance(acl, Mapping):
        return None
    send = acl.get("send")
    if not isinstance(send, Mapping):
        return None
    result: dict[str, frozenset[str]] = {}
    for role, types in send.items():
        if isinstance(types, list | tuple):
            result[str(role)] = frozenset(str(t) for t in types)
        else:  # некорректная запись роли ⇒ роль не может отправлять ничего (только сужение)
            result[str(role)] = frozenset()
    return result
