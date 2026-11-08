"""Порт фактов брокера ACP: всё, что шагам 3–6 конвейера нужно знать о run, задачах и истории сообщений.

Чистое ядро (`pipeline`) обращается к фактам только через этот Protocol; реализации — `sql.SqlFacts`
(PostgreSQL под RLS) и `memory.MemoryFacts` (unit-тесты). Модуль оркестрации не импортируется:
факты о run/задачах читаются из таблиц (MODULE_BOUNDARIES, import-linter).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class RunInfo:
    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    status: str
    mode: str = "DEVELOP"
    policy_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskInfo:
    id: uuid.UUID
    run_id: uuid.UUID
    agent_role: str
    status: str
    blocked_reason: str | None = None
    review_loop_count: int = 0
    max_review_loops: int = 3


@dataclass(frozen=True)
class StoredMessage:
    """Принятое сообщение (строка agent_messages)."""

    id: uuid.UUID
    run_id: uuid.UUID
    message_type: str
    sender_role: str
    recipient_role: str
    correlation_id: uuid.UUID
    reply_to_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    task_attempt_id: uuid.UUID | None = None
    recipient_task_id: uuid.UUID | None = None
    sender_user_id: uuid.UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


class BrokerFacts(Protocol):
    """Факты в RLS-контексте workspace отправителя (строки чужих workspace невидимы ⇒ «не найдено»)."""

    async def run(self, run_id: uuid.UUID) -> RunInfo | None: ...

    async def task(self, task_id: uuid.UUID) -> TaskInfo | None: ...

    async def attempt_active(self, attempt_id: uuid.UUID, task_id: uuid.UUID) -> bool:
        """Попытка принадлежит задаче, в статусе RUNNING, и её fencing_token = tasks.lease_fencing_token."""
        ...

    async def message(self, message_id: uuid.UUID) -> StoredMessage | None: ...

    async def correlation_known(self, run_id: uuid.UUID, correlation_id: uuid.UUID) -> bool:
        """В run есть принятое сообщение с этой корреляцией (или с id = correlation_id)."""
        ...

    async def correlation_messages(
        self, run_id: uuid.UUID, correlation_id: uuid.UUID, message_type: str
    ) -> list[StoredMessage]:
        """Сообщения типа `message_type` корреляции, по времени принятия."""
        ...

    async def answer_exists(self, question_id: uuid.UUID) -> bool: ...

    async def reply_depth(self, message_id: uuid.UUID) -> int:
        """Длина цепочки reply_to от сообщения до корня (корень — 1)."""
        ...

    async def role_has_task(self, run_id: uuid.UUID, role: str) -> bool:
        """У роли есть задача в run (не отменённая, не заменённая)."""
        ...

    async def task_in_run(self, run_id: uuid.UUID, task_id: uuid.UUID) -> TaskInfo | None: ...

    async def dag_connected(self, task_id: uuid.UUID, other_task_id: uuid.UUID) -> bool:
        """Есть ребро task_dependencies между задачами (в любом направлении)."""
        ...

    async def dag_connected_to_role(self, task_id: uuid.UUID, role: str) -> bool:
        """Есть ребро task_dependencies между задачей и какой-либо задачей роли `role`."""
        ...

    async def missing_refs(self, run: RunInfo, refs: list[tuple[str, uuid.UUID]]) -> list[str]:
        """Ссылки `<kind>:<uuid>`, не найденные в workspace/project run (проверяемые виды; см. реализацию)."""
        ...

    async def artifact_sha256(self, project_id: uuid.UUID, artifact_id: uuid.UUID) -> str | None: ...

    async def decision_exists(self, run_id: uuid.UUID, decision_id: uuid.UUID) -> bool: ...

    async def approval_status(self, approval_id: uuid.UUID) -> str | None: ...

    async def messages_from_attempt(self, attempt_id: uuid.UUID) -> int: ...

    async def messages_in_run_last_hour(self, run_id: uuid.UUID) -> int: ...

    async def open_blocking_questions(self, task_id: uuid.UUID) -> int:
        """Принятые QUESTION с blocking=true от задачи, на которые ещё нет ANSWER."""
        ...

    async def denials_in_attempt(self, attempt_id: uuid.UUID) -> int:
        """Число отказов ACL_DENIED/POLICY_DENIED, зафиксированных для попытки."""
        ...
