"""P4-04: contract + negative тесты ACP по каждому из 12 типов (схемы packages/contracts/schemas/acp/1.0)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from aicd_collaboration.acp import schema
from aicd_collaboration.acp.model import MESSAGE_TYPES, ORCHESTRATOR
from tests.support.acp import NEGATIVE, u, valid_payload

AGENT_SENDER_OF = {
    "TASK_ASSIGN": {"role": ORCHESTRATOR},
    "DECISION_RECORDED": {"role": ORCHESTRATOR},
}


def envelope(message_type: str, payload: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    mid = u()
    env: dict[str, Any] = {
        "protocol": "acp/1.0",
        "message_id": mid,
        "run_id": u(),
        "task_id": u(),
        "from": AGENT_SENDER_OF.get(message_type, {"role": "developer", "provider": "codex"}),
        "to": {"role": "orchestrator"},
        "type": message_type,
        "correlation_id": mid,
        "reply_to_id": u() if message_type == "ANSWER" else None,
        "payload": payload if payload is not None else valid_payload(message_type),
        "created_at": "2026-11-08T09:00:00Z",
    }
    env.update(overrides)
    return env


def test_all_twelve_types_have_schemas() -> None:
    assert len(MESSAGE_TYPES) == 12
    schema.warm_up()


@pytest.mark.parametrize("message_type", MESSAGE_TYPES)
def test_valid_payload_and_envelope_pass(message_type: str) -> None:
    assert schema.payload_errors(message_type, valid_payload(message_type)) == []
    assert schema.envelope_errors(envelope(message_type)) == []


@pytest.mark.parametrize(
    ("message_type", "case"),
    [(t, name) for t, cases in NEGATIVE.items() for name, _ in cases],
)
def test_negative_payload_rejected(message_type: str, case: str) -> None:
    mutate = dict(NEGATIVE[message_type])[case]
    payload = valid_payload(message_type)
    mutate(payload)
    assert schema.payload_errors(message_type, payload), f"{message_type}/{case} должен быть отклонён"
    errors = schema.envelope_errors(envelope(message_type, payload))
    assert errors and errors[0].startswith("/payload")


@pytest.mark.parametrize("message_type", MESSAGE_TYPES)
def test_additional_payload_property_rejected(message_type: str) -> None:
    payload = valid_payload(message_type, write_scope="unrestricted")  # «расширить права» — поля нет by design
    assert schema.payload_errors(message_type, payload)


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("unknown_type", {"type": "PING"}),
        ("bad_protocol", {"protocol": "acp/2.0"}),
        ("bad_uuid", {"message_id": "not-a-uuid"}),
        ("extra_field", {"priority": "high"}),
        ("missing_correlation", {"correlation_id": None}),
        ("bad_created_at", {"created_at": "вчера"}),
        ("to_unknown_role", {"to": {"role": "root"}}),
        ("orchestrator_with_provider", {"from": {"role": "orchestrator", "provider": "codex"}}),
        ("agent_without_provider", {"from": {"role": "developer"}}),
        ("agent_with_user_id", {"from": {"role": "developer", "provider": "codex", "user_id": u()}}),
        ("user_without_user_id", {"from": {"role": "user"}}),
        ("non_object_payload", {"payload": "rm -rf /"}),
    ],
)
def test_negative_envelope_rejected(case: str, overrides: dict[str, Any]) -> None:
    env = envelope("QUESTION", **overrides)
    if overrides.get("correlation_id", "") is None:
        env.pop("correlation_id")
    assert schema.envelope_errors(env), case


@pytest.mark.parametrize("message_type", ["TASK_ASSIGN", "DECISION_RECORDED"])
def test_orchestrator_only_types_rejected_from_agent(message_type: str) -> None:
    env = envelope(message_type, **{"from": {"role": "architect", "provider": "claude"}})
    assert schema.envelope_errors(env)


def test_answer_requires_reply_to() -> None:
    env = envelope("ANSWER", reply_to_id=None)
    assert schema.envelope_errors(env)


def test_unknown_payload_type() -> None:
    assert schema.payload_errors("PING", {})


def test_schema_errors_do_not_leak_long_values() -> None:
    env = envelope("ANSWER", payload={"answer": "x" * 9000})
    errors = schema.envelope_errors(copy.deepcopy(env))
    assert errors and all(len(e) < 300 for e in errors)
