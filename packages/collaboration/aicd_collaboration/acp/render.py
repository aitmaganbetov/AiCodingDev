"""Шаблонные резюме ACP-сообщений для SSE `agent.message`, чата и ленты активности (EVENT_SCHEMAS §4.3).

Тексты payload недоверенные: попадают в резюме только после редактирования секретов и усечения;
резюме — это данные для отображения, не инструкции.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from aicd_platform.redaction import redact_text

SUMMARY_MAX_CHARS = 1000  # sse agent_message.summary maxLength
BODY_MAX_CHARS = 8000  # sse chat_message.body maxLength
LINKS_MAX = 20

# Вид ссылки ACP (§3.0) → kind ссылки чата (sse chat_message.links[].kind).
LINK_KINDS: Mapping[str, str] = {
    "artifact": "artifact",
    "evidence": "evidence",
    "finding": "finding",
    "task": "task",
    "approval": "approval",
    "gate_execution": "gate_execution",
}


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _clip(text: str, limit: int) -> str:
    text = " ".join(redact_text(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summary(message_type: str, payload: Mapping[str, Any], limit: int = SUMMARY_MAX_CHARS) -> str:
    p = payload
    t = message_type
    if t == "TASK_ASSIGN":
        text = f"Назначение (попытка {_s(p.get('attempt_no'))}): {_s(p.get('title'))}"
    elif t == "HANDOFF":
        text = f"{_s(p.get('handoff_kind'))}: {_s(p.get('summary'))}"
    elif t == "QUESTION":
        prefix = "Блокирующий вопрос" if p.get("blocking") else "Вопрос"
        text = f"{prefix}: {_s(p.get('question'))}"
    elif t == "ANSWER":
        text = f"Ответ: {_s(p.get('answer'))}"
    elif t == "REVIEW_REQUEST":
        text = f"Запрос ревью ({_s(p.get('review_kind'))}, итерация {_s(p.get('loop_iteration'))}): {_s(p.get('scope_summary'))}"
    elif t == "FINDING":
        where = f" — {_s(p.get('file'))}" + (f":{p['line']}" if p.get("line") else "") if p.get("file") else ""
        text = f"[{_s(p.get('severity'))}] {_s(p.get('title'))}{where}"
    elif t == "FIX_REQUEST":
        where = f" — {_s(p.get('file'))}" if p.get("file") else ""
        text = f"Исправить [{_s(p.get('severity'))}] {_s(p.get('finding'))}{where}: {_s(p.get('requested_action'))}"
    elif t == "EVIDENCE_SUBMITTED":
        items = p.get("items") or []
        kinds = sorted({_s(i.get("kind")) for i in items if isinstance(i, Mapping)})
        text = f"Evidence: {len(items)} шт. ({', '.join(kinds)}), статус UNVERIFIED"
    elif t == "DECISION_PROPOSAL":
        text = (
            f"Предложение ({_s(p.get('decision_type'))}): {_s(p.get('title'))} → {_s(p.get('recommended_option'))}"
            + (" (требуется решение человека)" if p.get("requires_human_approval") else "")
        )
    elif t == "DECISION_RECORDED":
        chosen = f" ({_s(p.get('chosen_option'))})" if p.get("chosen_option") else ""
        text = f"Решение ({_s(p.get('decision_type'))}): {_s(p.get('outcome'))}{chosen}"
    elif t == "BLOCKED":
        text = f"BLOCKED {_s(p.get('reason'))}: {_s(p.get('detail'))}"
    elif t == "TASK_COMPLETED":
        verdict = f" [{_s(p.get('review_verdict'))}]" if p.get("review_verdict") else ""
        text = f"Заявлено завершение{verdict}: {_s(p.get('summary'))}"
    else:
        text = t
    return _clip(text, limit)


def severity(message_type: str, payload: Mapping[str, Any]) -> str | None:
    if message_type in ("FINDING", "FIX_REQUEST"):
        value = payload.get("severity")
        return value if isinstance(value, str) else None
    return None


def links(refs: list[tuple[str, uuid.UUID]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, ref_id in refs:
        mapped = LINK_KINDS.get(kind)
        if mapped is None or (mapped, str(ref_id)) in seen:
            continue
        seen.add((mapped, str(ref_id)))
        out.append({"kind": mapped, "id": str(ref_id)})
        if len(out) >= LINKS_MAX:
            break
    return out
