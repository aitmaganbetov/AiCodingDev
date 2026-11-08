"""In-memory реализация `BrokerFacts` — для unit-тестов чистого ядра и прототипирования без БД."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from aicd_collaboration.acp.facts import RunInfo, StoredMessage, TaskInfo


@dataclass
class MemoryFacts:
    runs: dict[uuid.UUID, RunInfo] = field(default_factory=dict)
    tasks: dict[uuid.UUID, TaskInfo] = field(default_factory=dict)
    active_attempts: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)  # attempt → task
    messages: dict[uuid.UUID, StoredMessage] = field(default_factory=dict)
    edges: set[frozenset[uuid.UUID]] = field(default_factory=set)
    known_refs: set[tuple[str, uuid.UUID]] = field(default_factory=set)
    artifacts: dict[uuid.UUID, tuple[uuid.UUID, str]] = field(default_factory=dict)  # id → (project, sha256)
    decisions: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)  # decision → run
    approvals: dict[uuid.UUID, str] = field(default_factory=dict)
    run_hour_counts: dict[uuid.UUID, int] = field(default_factory=dict)
    denials: dict[uuid.UUID, int] = field(default_factory=dict)
    # Виды ссылок, которые проверяются (как в SqlFacts: виды с таблицами в схеме).
    checked_ref_kinds: frozenset[str] = frozenset({"artifact", "message", "task", "approval", "decision"})

    # ---------- наполнение ----------
    def add_message(self, msg: StoredMessage) -> None:
        self.messages[msg.id] = msg

    def bump_review_loop(self, task_id: uuid.UUID) -> int:
        task = self.tasks[task_id]
        self.tasks[task_id] = replace(task, review_loop_count=task.review_loop_count + 1)
        return task.review_loop_count + 1

    # ---------- BrokerFacts ----------
    async def run(self, run_id: uuid.UUID) -> RunInfo | None:
        return self.runs.get(run_id)

    async def task(self, task_id: uuid.UUID) -> TaskInfo | None:
        return self.tasks.get(task_id)

    async def attempt_active(self, attempt_id: uuid.UUID, task_id: uuid.UUID) -> bool:
        return self.active_attempts.get(attempt_id) == task_id

    async def message(self, message_id: uuid.UUID) -> StoredMessage | None:
        return self.messages.get(message_id)

    async def correlation_known(self, run_id: uuid.UUID, correlation_id: uuid.UUID) -> bool:
        return any(m.run_id == run_id and correlation_id in (m.correlation_id, m.id) for m in self.messages.values())

    async def correlation_messages(
        self, run_id: uuid.UUID, correlation_id: uuid.UUID, message_type: str
    ) -> list[StoredMessage]:
        return [
            m
            for m in self.messages.values()
            if m.run_id == run_id and m.correlation_id == correlation_id and m.message_type == message_type
        ]

    async def answer_exists(self, question_id: uuid.UUID) -> bool:
        return any(m.message_type == "ANSWER" and m.reply_to_id == question_id for m in self.messages.values())

    async def reply_depth(self, message_id: uuid.UUID) -> int:
        depth, current = 0, self.messages.get(message_id)
        while current is not None and depth < 1000:
            depth += 1
            current = self.messages.get(current.reply_to_id) if current.reply_to_id else None
        return depth

    async def role_has_task(self, run_id: uuid.UUID, role: str) -> bool:
        return any(t.run_id == run_id and t.agent_role == role for t in self.tasks.values())

    async def task_in_run(self, run_id: uuid.UUID, task_id: uuid.UUID) -> TaskInfo | None:
        task = self.tasks.get(task_id)
        return task if task is not None and task.run_id == run_id else None

    async def dag_connected(self, task_id: uuid.UUID, other_task_id: uuid.UUID) -> bool:
        return frozenset({task_id, other_task_id}) in self.edges

    async def dag_connected_to_role(self, task_id: uuid.UUID, role: str) -> bool:
        return any(frozenset({task_id, t.id}) in self.edges for t in self.tasks.values() if t.agent_role == role)

    async def missing_refs(self, run: RunInfo, refs: list[tuple[str, uuid.UUID]]) -> list[str]:
        known = self.known_refs | {("message", m) for m in self.messages} | {("task", t) for t in self.tasks}
        known |= {("artifact", a) for a, (p, _) in self.artifacts.items() if p == run.project_id}
        return [f"{k}:{v}" for k, v in refs if k in self.checked_ref_kinds and (k, v) not in known]

    async def artifact_sha256(self, project_id: uuid.UUID, artifact_id: uuid.UUID) -> str | None:
        found = self.artifacts.get(artifact_id)
        return found[1] if found and found[0] == project_id else None

    async def decision_exists(self, run_id: uuid.UUID, decision_id: uuid.UUID) -> bool:
        return self.decisions.get(decision_id) == run_id

    async def approval_status(self, approval_id: uuid.UUID) -> str | None:
        return self.approvals.get(approval_id)

    async def messages_from_attempt(self, attempt_id: uuid.UUID) -> int:
        return sum(1 for m in self.messages.values() if m.task_attempt_id == attempt_id)

    async def messages_in_run_last_hour(self, run_id: uuid.UUID) -> int:
        return self.run_hour_counts.get(run_id, 0) + sum(1 for m in self.messages.values() if m.run_id == run_id)

    async def open_blocking_questions(self, task_id: uuid.UUID) -> int:
        answered = {m.reply_to_id for m in self.messages.values() if m.message_type == "ANSWER"}
        return sum(
            1
            for m in self.messages.values()
            if m.message_type == "QUESTION"
            and m.task_id == task_id
            and m.payload.get("blocking") is True
            and m.id not in answered
        )

    async def denials_in_attempt(self, attempt_id: uuid.UUID) -> int:
        return self.denials.get(attempt_id, 0)
