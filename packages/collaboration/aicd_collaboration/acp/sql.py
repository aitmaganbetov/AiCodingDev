"""`BrokerFacts` поверх PostgreSQL (миграции 0003, 0006): запросы в RLS-контексте соединения.

Таблицы оркестрации (runs, tasks, task_attempts, task_dependencies, approvals, agent_decisions, evidence,
gate_executions) только читаются; единственная запись в них — `increment_review_loop` (§6, счётчик
`tasks.review_loop_count`, с инкрементом version по ADR-0007).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_collaboration.acp.facts import RunInfo, StoredMessage, TaskInfo

MESSAGE_COLUMNS = (
    "id, run_id, message_type, sender_role, recipient_role, correlation_id, reply_to_id, task_id, task_attempt_id,"
    " recipient_task_id, sender_user_id, payload, created_at"
)

# Виды ссылок §3.0, для которых в схеме есть таблица: (таблица, область проверки).
# finding, context_pack, adr, requirement, knowledge — таблиц ещё нет (Phase 5–6): не проверяются.
REF_TABLES: dict[str, tuple[str, str]] = {
    "artifact": ("artifacts", "project"),
    "message": ("agent_messages", "run"),
    "task": ("tasks", "run"),
    "decision": ("agent_decisions", "run"),
    "evidence": ("evidence", "run"),
    "gate_execution": ("gate_executions", "run"),
    "approval": ("approvals", "project"),
}


def stored_message(row: Any) -> StoredMessage:
    m = row._mapping
    return StoredMessage(
        id=m["id"],
        run_id=m["run_id"],
        message_type=m["message_type"],
        sender_role=m["sender_role"],
        recipient_role=m["recipient_role"],
        correlation_id=m["correlation_id"],
        reply_to_id=m["reply_to_id"],
        task_id=m["task_id"],
        task_attempt_id=m["task_attempt_id"],
        recipient_task_id=m["recipient_task_id"],
        sender_user_id=m["sender_user_id"],
        payload=dict(m["payload"] or {}),
        created_at=m["created_at"],
    )


def _task(row: Any) -> TaskInfo:
    m = row._mapping
    return TaskInfo(
        id=m["id"],
        run_id=m["run_id"],
        agent_role=m["agent_role"],
        status=m["status"],
        blocked_reason=m["blocked_reason"],
        review_loop_count=int(m["review_loop_count"]),
        max_review_loops=int(m["max_review_loops"]),
    )


_TASK_COLUMNS = "id, run_id, agent_role, status, blocked_reason, review_loop_count, max_review_loops"


class SqlFacts:
    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn

    async def _scalar(self, sql: str, params: dict[str, Any]) -> Any:
        return (await self.conn.execute(text(sql), params)).scalar()

    async def run(self, run_id: uuid.UUID) -> RunInfo | None:
        row = (
            await self.conn.execute(
                text("SELECT id, workspace_id, project_id, status, mode, policy_snapshot FROM runs WHERE id = :r"),
                {"r": run_id},
            )
        ).first()
        if row is None:
            return None
        m = row._mapping
        return RunInfo(
            m["id"], m["workspace_id"], m["project_id"], m["status"], m["mode"], dict(m["policy_snapshot"] or {})
        )

    async def task(self, task_id: uuid.UUID) -> TaskInfo | None:
        row = (
            await self.conn.execute(
                text(f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = :t"),  # noqa: S608
                {"t": task_id},
            )
        ).first()
        return _task(row) if row else None

    async def task_in_run(self, run_id: uuid.UUID, task_id: uuid.UUID) -> TaskInfo | None:
        task = await self.task(task_id)
        return task if task is not None and task.run_id == run_id else None

    async def attempt_active(self, attempt_id: uuid.UUID, task_id: uuid.UUID) -> bool:
        return bool(
            await self._scalar(
                "SELECT EXISTS (SELECT 1 FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
                " WHERE a.id = :a AND a.task_id = :t AND a.status = 'RUNNING'"
                " AND t.lease_fencing_token = a.fencing_token)",
                {"a": attempt_id, "t": task_id},
            )
        )

    async def message(self, message_id: uuid.UUID) -> StoredMessage | None:
        row = (
            await self.conn.execute(
                text(f"SELECT {MESSAGE_COLUMNS} FROM agent_messages WHERE id = :id"),  # noqa: S608
                {"id": message_id},
            )
        ).first()
        return stored_message(row) if row else None

    async def correlation_known(self, run_id: uuid.UUID, correlation_id: uuid.UUID) -> bool:
        return bool(
            await self._scalar(
                "SELECT EXISTS (SELECT 1 FROM agent_messages WHERE run_id = :r AND (correlation_id = :c OR id = :c))",
                {"r": run_id, "c": correlation_id},
            )
        )

    async def correlation_messages(
        self, run_id: uuid.UUID, correlation_id: uuid.UUID, message_type: str
    ) -> list[StoredMessage]:
        rows = (
            await self.conn.execute(
                text(
                    f"SELECT {MESSAGE_COLUMNS} FROM agent_messages WHERE correlation_id = :c AND run_id = :r"  # noqa: S608
                    " AND message_type = :t ORDER BY created_at, id"
                ),
                {"c": correlation_id, "r": run_id, "t": message_type},
            )
        ).all()
        return [stored_message(r) for r in rows]

    async def answer_exists(self, question_id: uuid.UUID) -> bool:
        return bool(
            await self._scalar(
                "SELECT EXISTS (SELECT 1 FROM agent_messages WHERE reply_to_id = :q AND message_type = 'ANSWER')",
                {"q": question_id},
            )
        )

    async def reply_depth(self, message_id: uuid.UUID) -> int:
        depth = await self._scalar(
            "WITH RECURSIVE chain(id, reply_to_id, depth) AS ("
            " SELECT id, reply_to_id, 1 FROM agent_messages WHERE id = :m"
            " UNION ALL SELECT a.id, a.reply_to_id, c.depth + 1 FROM agent_messages a"
            " JOIN chain c ON a.id = c.reply_to_id WHERE c.depth < 1000)"
            " SELECT coalesce(max(depth), 0) FROM chain",
            {"m": message_id},
        )
        return int(depth or 0)

    async def role_has_task(self, run_id: uuid.UUID, role: str) -> bool:
        return bool(
            await self._scalar(
                "SELECT EXISTS (SELECT 1 FROM tasks WHERE run_id = :r AND agent_role = :role"
                " AND status <> 'CANCELLED' AND superseded_at IS NULL)",
                {"r": run_id, "role": role},
            )
        )

    async def dag_connected(self, task_id: uuid.UUID, other_task_id: uuid.UUID) -> bool:
        return bool(
            await self._scalar(
                "SELECT EXISTS (SELECT 1 FROM task_dependencies WHERE (task_id = :a AND depends_on_task_id = :b)"
                " OR (task_id = :b AND depends_on_task_id = :a))",
                {"a": task_id, "b": other_task_id},
            )
        )

    async def dag_connected_to_role(self, task_id: uuid.UUID, role: str) -> bool:
        return bool(
            await self._scalar(
                "SELECT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks t"
                " ON t.id = CASE WHEN d.task_id = :a THEN d.depends_on_task_id ELSE d.task_id END"
                " WHERE (d.task_id = :a OR d.depends_on_task_id = :a) AND t.agent_role = :role)",
                {"a": task_id, "role": role},
            )
        )

    async def missing_refs(self, run: RunInfo, refs: list[tuple[str, uuid.UUID]]) -> list[str]:
        missing: list[str] = []
        for kind, ref_id in refs:
            spec = REF_TABLES.get(kind)
            if spec is None:
                continue
            table, scope = spec
            column, value = ("project_id", run.project_id) if scope == "project" else ("run_id", run.id)
            found = await self._scalar(
                f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id = :id AND {column} = :v)",  # noqa: S608 — таблица из словаря
                {"id": ref_id, "v": value},
            )
            if not found:
                missing.append(f"{kind}:{ref_id}")
        return missing

    async def artifact_sha256(self, project_id: uuid.UUID, artifact_id: uuid.UUID) -> str | None:
        value = await self._scalar(
            "SELECT sha256 FROM artifacts WHERE id = :a AND project_id = :p", {"a": artifact_id, "p": project_id}
        )
        return str(value) if value is not None else None

    async def decision_exists(self, run_id: uuid.UUID, decision_id: uuid.UUID) -> bool:
        return bool(
            await self._scalar(
                "SELECT EXISTS (SELECT 1 FROM agent_decisions WHERE id = :d AND run_id = :r)",
                {"d": decision_id, "r": run_id},
            )
        )

    async def approval_status(self, approval_id: uuid.UUID) -> str | None:
        value = await self._scalar("SELECT status FROM approvals WHERE id = :a", {"a": approval_id})
        return str(value) if value is not None else None

    async def messages_from_attempt(self, attempt_id: uuid.UUID) -> int:
        return int(
            await self._scalar("SELECT count(*) FROM agent_messages WHERE task_attempt_id = :a", {"a": attempt_id}) or 0
        )

    async def messages_in_run_last_hour(self, run_id: uuid.UUID) -> int:
        return int(
            await self._scalar(
                "SELECT count(*) FROM agent_messages WHERE run_id = :r AND created_at > now() - interval '1 hour'",
                {"r": run_id},
            )
            or 0
        )

    async def open_blocking_questions(self, task_id: uuid.UUID) -> int:
        return int(
            await self._scalar(
                "SELECT count(*) FROM agent_messages q WHERE q.task_id = :t AND q.message_type = 'QUESTION'"
                " AND (q.payload ->> 'blocking')::boolean"
                " AND NOT EXISTS (SELECT 1 FROM agent_messages a WHERE a.reply_to_id = q.id"
                " AND a.message_type = 'ANSWER')",
                {"t": task_id},
            )
            or 0
        )

    async def denials_in_attempt(self, attempt_id: uuid.UUID) -> int:
        return int(
            await self._scalar(
                "SELECT count(*) FROM agent_activity_events WHERE event_type = 'acp.rejected'"
                " AND payload ->> 'task_attempt_id' = :a"
                " AND payload ->> 'rejection_code' IN ('ACL_DENIED', 'POLICY_DENIED')",
                {"a": str(attempt_id)},
            )
            or 0
        )

    async def increment_review_loop(self, task_id: uuid.UUID) -> int:
        value = await self._scalar(
            "UPDATE tasks SET review_loop_count = review_loop_count + 1, version = version + 1"
            " WHERE id = :t RETURNING review_loop_count",
            {"t": task_id},
        )
        if value is None:
            raise LookupError(f"задача {task_id} не найдена")
        return int(value)
