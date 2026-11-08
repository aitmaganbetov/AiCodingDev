"""Фикстуры ACP: валидные payload каждого из 12 типов и сборка кандидатов (тесты P4-04/P4-05)."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from aicd_platform.ids import uuid7


def u() -> str:
    return str(uuid7())


def ref(kind: str, value: uuid.UUID | str | None = None) -> str:
    return f"{kind}:{value or uuid7()}"


SHA1 = "a" * 40
SHA256 = "b" * 64


def valid_payload(message_type: str, **overrides: Any) -> dict[str, Any]:
    """Минимально-валидный payload по схеме `packages/contracts/schemas/acp/1.0/payloads/<type>`."""
    base: dict[str, dict[str, Any]] = {
        "TASK_ASSIGN": {
            "task_id": u(),
            "attempt_no": 1,
            "title": "Реализовать эндпоинт",
            "objective": "Добавить проверку авторизации",
            "acceptance_criteria": ["тест проходит"],
            "complexity": "L2",
            "risk_level": "low",
            "provider": "codex",
            "context_pack_ref": ref("context_pack"),
            "instruction_template_version": "developer@1.0.0",
            "permissions": {"write_scope": "worktree", "network": "none"},
        },
        "HANDOFF": {"handoff_kind": "implementation_ready", "summary": "Готово к ревью", "artifact_refs": [ref("artifact")]},
        "QUESTION": {"question": "Какой формат даты?", "question_kind": "clarification", "blocking": True},
        "ANSWER": {"answer": "ISO 8601"},
        "REVIEW_REQUEST": {
            "review_kind": "code",
            "diff_ref": ref("artifact"),
            "commit_sha": SHA1,
            "scope_summary": "Авторизация сервиса",
            "acceptance_criteria": [],
            "loop_iteration": 1,
        },
        "FINDING": {
            "title": "Нет проверки прав",
            "severity": "high",
            "category": "security",
            "description": "Эндпоинт доступен без роли",
            "blocking": True,
            "evidence_refs": [ref("evidence")],
        },
        "FIX_REQUEST": {
            "finding": "Missing authorization check",
            "severity": "high",
            "file": "app/auth/service.py",
            "evidence_refs": [ref("finding")],
            "requested_action": "Add authorization check and regression test",
            "loop_iteration": 1,
        },
        "EVIDENCE_SUBMITTED": {"items": [{"kind": "git_diff", "artifact_ref": ref("artifact"), "sha256": SHA256}]},
        "DECISION_PROPOSAL": {
            "decision_type": "architecture",
            "title": "Выбор очереди",
            "options": [{"option_id": "redis", "summary": "Redis Streams"}],
            "recommended_option": "redis",
            "rationale": "Уже в стеке",
            "requires_human_approval": False,
        },
        "DECISION_RECORDED": {
            "decision_id": u(),
            "decision_type": "architecture",
            "outcome": "accepted",
            "decided_by": {"type": "policy", "policy_version": "1"},
            "rationale": "Согласно policy",
        },
        "BLOCKED": {"reason": "MANUAL_REVIEW", "detail": "Нужен человек", "needed_from": "user"},
        "TASK_COMPLETED": {
            "summary": "Сделано",
            "claims": [{"type": "implementation_completed", "evidence_refs": []}],
            "artifact_refs": [],
        },
    }
    payload = copy.deepcopy(base[message_type])
    payload.update(overrides)
    return payload


# Негативные варианты по каждому типу: (описание, функция порчи payload).
def _drop(key: str) -> Any:
    def f(p: dict[str, Any]) -> None:
        p.pop(key)

    return f


def _set(key: str, value: Any) -> Any:
    def f(p: dict[str, Any]) -> None:
        p[key] = value

    return f


NEGATIVE: dict[str, list[tuple[str, Any]]] = {
    "TASK_ASSIGN": [
        ("no_task_id", _drop("task_id")),
        ("attempt_no_zero", _set("attempt_no", 0)),
        ("bad_template_version", _set("instruction_template_version", "developer@1")),
        ("permission_escalation_field", _set("permissions", {"write_scope": "repo", "network": "none"})),
    ],
    "HANDOFF": [
        ("plan_ready_without_plan_version", _set("handoff_kind", "plan_ready")),
        ("empty_artifact_refs", _set("artifact_refs", [])),
        ("free_form_ref", _set("artifact_refs", ["finding-44"])),
    ],
    "QUESTION": [
        ("no_blocking", _drop("blocking")),
        ("unknown_kind", _set("question_kind", "chat")),
        ("too_many_options", _set("options", ["o"] * 11)),
    ],
    "ANSWER": [("no_answer", _drop("answer")), ("empty_answer", _set("answer", ""))],
    "REVIEW_REQUEST": [
        ("short_sha", _set("commit_sha", "abc")),
        ("path_traversal", _set("changed_files", ["../etc/passwd"])),
        ("absolute_path", _set("changed_files", ["/etc/passwd"])),
        ("loop_iteration_11", _set("loop_iteration", 11)),
    ],
    "FINDING": [
        ("no_evidence", _set("evidence_refs", [])),
        ("bad_severity", _set("severity", "urgent")),
        ("line_zero", _set("line", 0)),
    ],
    "FIX_REQUEST": [
        ("no_loop_iteration", _drop("loop_iteration")),
        ("no_file_key", _drop("file")),
        ("traversal_file", _set("file", "a/../../b")),
    ],
    "EVIDENCE_SUBMITTED": [
        ("empty_items", _set("items", [])),
        ("bad_sha256", _set("items", [{"kind": "git_diff", "artifact_ref": ref("artifact"), "sha256": "x"}])),
        ("verified_claim_field", _set("verification_status", "VERIFIED")),
    ],
    "DECISION_PROPOSAL": [
        ("no_options", _set("options", [])),
        ("bad_option_id", _set("recommended_option", "Bad Option")),
        ("no_requires_human_approval", _drop("requires_human_approval")),
    ],
    "DECISION_RECORDED": [
        ("decided_by_agent", _set("decided_by", {"type": "agent"})),
        ("user_without_user_id", _set("decided_by", {"type": "user"})),
        ("bad_outcome", _set("outcome", "maybe")),
    ],
    "BLOCKED": [
        ("unknown_reason", _set("reason", "BORED")),
        ("no_needed_from", _drop("needed_from")),
    ],
    "TASK_COMPLETED": [
        ("no_claims", _set("claims", [])),
        ("claim_without_evidence_refs", _set("claims", [{"type": "tests_passed"}])),
        ("self_verified", _set("verified", True)),
    ],
}
