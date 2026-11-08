"""Модель ACP 1.0 (AGENT_COMMUNICATION_PROTOCOL §1, §2, §4, §11): участники, типы, коды отказа, результаты.

Модуль не зависит от БД: всё, что здесь описано, — чистые данные.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTOCOL = "acp/1.0"
SCHEMA_VERSION = "1.0"

AGENT_ROLES: tuple[str, ...] = (
    "requirements",
    "architect",
    "planner",
    "developer",
    "reviewer",
    "security",
    "qa",
    "database",
    "performance",
    "devops",
    "maintenance",
    "documentation",
)
ORCHESTRATOR = "orchestrator"
USER = "user"
# acp_participant (§11): agent_role ∪ {orchestrator, user}
PARTICIPANTS: tuple[str, ...] = (*AGENT_ROLES, ORCHESTRATOR, USER)
PROVIDERS: frozenset[str] = frozenset({"codex", "claude"})

MESSAGE_TYPES: tuple[str, ...] = (
    "TASK_ASSIGN",
    "HANDOFF",
    "QUESTION",
    "ANSWER",
    "REVIEW_REQUEST",
    "FINDING",
    "FIX_REQUEST",
    "EVIDENCE_SUBMITTED",
    "DECISION_PROPOSAL",
    "DECISION_RECORDED",
    "BLOCKED",
    "TASK_COMPLETED",
)

# Группы §4.1
IMPL: frozenset[str] = frozenset({"developer", "database", "devops", "maintenance", "documentation", "performance"})
VERIF: frozenset[str] = frozenset({"reviewer", "security", "qa"})
ANALYST: frozenset[str] = frozenset({"requirements", "architect", "planner"})
AGENT: frozenset[str] = frozenset(AGENT_ROLES)

# §3.11: причины BLOCKED, которые может заявить агент (остальные — только orchestrator).
AGENT_BLOCKED_REASONS: frozenset[str] = frozenset(
    {"DOCS_REQUIRED", "MANUAL_REVIEW", "APPROVAL_REQUIRED", "DEPENDENCY_FAILED"}
)

# §5 шаг 4: статусы run, в которых принимаются сообщения агентов и пользователя.
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset({"RUNNING", "DISCOVERY", "PLANNED", "AWAITING_APPROVAL", "VERIFYING"})
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

TRANSPORT_LIMIT_BYTES = 65536  # §5 P0: сырые байты кандидата
MAX_ENVELOPE_BYTES = 32768  # §5 шаг 2: канонический JSON конверта


class RejectionCode(enum.StrEnum):
    """`acp_rejection_code` (§11) — стабильные коды отказа конвейера (§5)."""

    SCHEMA_INVALID = "SCHEMA_INVALID"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    ACL_DENIED = "ACL_DENIED"
    POLICY_DENIED = "POLICY_DENIED"
    CORRELATION_INVALID = "CORRELATION_INVALID"
    LOOP_LIMIT = "LOOP_LIMIT"
    RATE_LIMITED_MESSAGES = "RATE_LIMITED_MESSAGES"


# Шаг конвейера, на котором возникает каждый код (для аудита и тестов порядка).
STEP_OF_CODE: dict[RejectionCode, int] = {
    RejectionCode.SCHEMA_INVALID: 1,
    RejectionCode.PAYLOAD_TOO_LARGE: 2,
    RejectionCode.ACL_DENIED: 3,
    RejectionCode.POLICY_DENIED: 4,
    RejectionCode.CORRELATION_INVALID: 5,
    RejectionCode.LOOP_LIMIT: 6,
    RejectionCode.RATE_LIMITED_MESSAGES: 6,
}


@dataclass(frozen=True)
class Sender:
    """Отправитель, установленный Orchestrator/API (§1): значения `from` из кандидата игнорируются.

    Для агентской роли обязательны `provider` и `task_id` (задача, исполнение которой вернуло сообщение);
    `task_attempt_id` — активная попытка (fencing). Для `user` обязателен `user_id`.
    """

    role: str
    provider: str | None = None
    user_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    task_attempt_id: uuid.UUID | None = None
    source_execution_id: uuid.UUID | None = None

    @property
    def is_agent(self) -> bool:
        return self.role in AGENT

    def as_from(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role}
        if self.is_agent and self.provider is not None:
            data["provider"] = self.provider
        if self.role == USER and self.user_id is not None:
            data["user_id"] = str(self.user_id)
        return data


@dataclass(frozen=True)
class LoopExhausted:
    """§6: принятие FIX_REQUEST превысило лимит review/fix циклов задачи-реализации."""

    task_id: uuid.UUID
    review_loop_count: int
    max_review_loops: int
    correlation_id: uuid.UUID


@dataclass(frozen=True)
class Accepted:
    """Сообщение принято (или уже было принято ранее — `duplicate=True`, идемпотентный no-op)."""

    message_id: uuid.UUID
    envelope: dict[str, Any]
    duplicate: bool = False
    loop_exhausted: LoopExhausted | None = None
    review_task_id: uuid.UUID | None = None  # задача-реализация, чей счётчик циклов изменён FIX_REQUEST
    review_loop_count: int | None = None
    suspected_injection: bool = False
    outbox_event_id: uuid.UUID | None = None
    blocked_message_id: uuid.UUID | None = None  # BLOCKED → user, порождённый исчерпанием циклов
    adjustments: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return True


@dataclass(frozen=True)
class Rejected:
    """Отказ конвейера: `code` — стабильный `acp_rejection_code`, `reason` — стабильный подкод (`acl.sender_type`)."""

    code: RejectionCode
    reason: str
    detail: str = ""
    message_id: uuid.UUID | None = None
    step: int = field(default=0)
    repeated_denials: int = 0  # ACL/POLICY отказов в этой попытке (≥ 3 ⇒ попытка завершается POLICY_DENIED, §5)

    def __post_init__(self) -> None:
        if not self.step:
            object.__setattr__(self, "step", STEP_OF_CODE[self.code])

    @property
    def accepted(self) -> bool:
        return False

    @property
    def attempt_should_fail(self) -> bool:
        return self.repeated_denials >= 3


Outcome = Accepted | Rejected


class AcpRejection(Exception):
    """Внутреннее исключение шага конвейера; превращается в `Rejected`."""

    def __init__(self, code: RejectionCode, reason: str, detail: str = "") -> None:
        super().__init__(f"{code}: {reason}: {detail}")
        self.code = code
        self.reason = reason
        self.detail = detail
