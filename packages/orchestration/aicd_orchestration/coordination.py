"""Согласование работы агентов: повтор задачи продолжает сделанное, а не начинает заново.

Попытка задачи исполняется в свежем worktree (ADR-0006), поэтому без этого модуля каждая следующая попытка —
в том числе у другого провайдера — получала чистое дерево от базового коммита run и ничего не знала о
предыдущей: та же работа выполнялась второй раз, а причина отказа (упавший gate) до агента не доходила.

Модуль даёт исполнителю три вещи:

* `resume_point` — коммит, на котором остановилась предыдущая попытка задачи. Новый worktree создаётся от
  него (база для diff и проверок остаётся базой run), так что следующий агент видит и исправляет уже
  сделанное;
* `previous_attempts` — кто (провайдер), чем закончил и какие gates не прошли;
* `run_plan` — какие задачи run ведут другие агенты, чтобы не выполнять чужую работу.

Всё это — данные раздела `coordination` промпта (в ограждении: заголовки задач пишет планировщик, сообщения
gates приходят из инструментов); правило пользования разделом — в блоке [5] (`agents.prompt`).

Независимость ревью (ROUTING_POLICY §5). Унаследованные коммиты делают автором изменения и провайдера
предыдущей попытки, поэтому `routing_service._authors` считает авторами провайдеров всех попыток задачи,
оставивших коммиты, а не только успешной.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_orchestration.agents.prompt import UntrustedFragment

SECTION: Final = "coordination"
ATTEMPTS_LIMIT: Final = 5
PLAN_LIMIT: Final = 60
MESSAGE_LIMIT: Final = 2000  # сообщение gate несёт упавшие тесты и нарушения — по ним агент исправляет
GATES_LIMIT: Final = 20
_FAILED_GATE_STATUSES: Final = ("FAIL", "ERROR")


@dataclass(frozen=True)
class PreviousAttempt:
    attempt_no: int
    provider: str | None
    status: str
    failure_category: str | None
    failure_message: str | None
    commit_sha: str | None  # HEAD worktree попытки, если она оставила изменения
    failed_gates: tuple[dict[str, str], ...] = ()

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"attempt_no": self.attempt_no, "provider": self.provider, "status": self.status}
        if self.failure_category:
            out["failure"] = self.failure_category
        if self.failure_message:
            out["failure_message"] = self.failure_message[:MESSAGE_LIMIT]
        if self.commit_sha:
            out["commit"] = self.commit_sha
        if self.failed_gates:
            out["failed_gates"] = list(self.failed_gates)
        return out


async def previous_attempts(conn: AsyncConnection, task_id: uuid.UUID) -> list[PreviousAttempt]:
    """Завершённые попытки задачи, от старых к новым (последние `ATTEMPTS_LIMIT`)."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT a.attempt_no, a.provider, a.status, a.failure_category, a.failure_message,"
                    " w.head_commit_sha, w.base_commit_sha FROM task_attempts a"
                    " LEFT JOIN LATERAL (SELECT wt.head_commit_sha, wt.base_commit_sha FROM cli_executions e"
                    "   JOIN worktrees wt ON wt.path_ref = e.worktree_ref AND wt.task_id = a.task_id"
                    "   WHERE e.task_attempt_id = a.id ORDER BY e.created_at DESC LIMIT 1) w ON true"
                    " WHERE a.task_id = :t AND a.status <> 'RUNNING' ORDER BY a.attempt_no DESC LIMIT :lim"
                ),
                {"t": task_id, "lim": ATTEMPTS_LIMIT},
            )
        )
        .mappings()
        .all()
    )
    attempts: list[PreviousAttempt] = []
    for r in reversed(rows):
        head = r["head_commit_sha"]
        changed = head is not None and head != r["base_commit_sha"]
        gates: tuple[dict[str, str], ...] = ()
        if head is not None:
            gates = await _failed_gates(conn, task_id, str(head))
        attempts.append(
            PreviousAttempt(
                attempt_no=int(r["attempt_no"]),
                provider=str(r["provider"]) if r["provider"] else None,
                status=str(r["status"]),
                failure_category=str(r["failure_category"]) if r["failure_category"] else None,
                failure_message=str(r["failure_message"]) if r["failure_message"] else None,
                commit_sha=str(head) if changed else None,
                failed_gates=gates,
            )
        )
    return attempts


async def _failed_gates(conn: AsyncConnection, task_id: uuid.UUID, commit_sha: str) -> tuple[dict[str, str], ...]:
    rows = (
        await conn.execute(
            text(
                "SELECT gate_type, status, error_code, error_message FROM gate_executions"
                " WHERE task_id = :t AND commit_sha = :sha AND status = ANY(:failed) AND superseded_at IS NULL"
                " ORDER BY required DESC, gate_type LIMIT :lim"
            ),
            {"t": task_id, "sha": commit_sha, "failed": list(_FAILED_GATE_STATUSES), "lim": GATES_LIMIT},
        )
    ).all()
    out: list[dict[str, str]] = []
    for gate_type, status, code, message in rows:
        item = {"gate": str(gate_type), "status": str(status)}
        if code:
            item["error_code"] = str(code)
        if message:
            item["message"] = str(message)[:MESSAGE_LIMIT]
        out.append(item)
    return tuple(out)


def resume_point(attempts: list[PreviousAttempt]) -> str | None:
    """Коммит последней попытки, оставившей изменения, — от него продолжает следующая. None — начинать с базы."""
    for attempt in reversed(attempts):
        if attempt.commit_sha:
            return attempt.commit_sha
    return None


async def run_plan(conn: AsyncConnection, run_id: uuid.UUID, task_id: uuid.UUID) -> list[dict[str, Any]]:
    """Остальные задачи действующего плана run: роль, состояние и провайдер, который задачу ведёт или вёл."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT t.task_key, t.agent_role, t.title, t.status,"
                    " (SELECT a.provider FROM task_attempts a WHERE a.task_id = t.id"
                    "   ORDER BY a.attempt_no DESC LIMIT 1) AS provider"
                    " FROM tasks t WHERE t.run_id = :r AND t.id <> :t AND t.superseded_at IS NULL"
                    " ORDER BY t.created_at, t.id LIMIT :lim"
                ),
                {"r": run_id, "t": task_id, "lim": PLAN_LIMIT},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "task_key": str(r["task_key"]),
            "role": str(r["agent_role"]),
            "title": str(r["title"])[:200],
            "status": str(r["status"]),
            "provider": str(r["provider"]) if r["provider"] else None,
        }
        for r in rows
    ]


def fragments(
    attempts: list[PreviousAttempt], plan: list[dict[str, Any]], *, resumed_from: str | None
) -> list[UntrustedFragment]:
    """Фрагменты раздела `coordination`. Пустые части не включаются: первой попытке одиночной задачи раздел не нужен."""
    out: list[UntrustedFragment] = []
    if attempts:
        body: dict[str, Any] = {"kind": "previous_attempts", "attempts": [a.view() for a in attempts]}
        if resumed_from:
            body["resumed_from"] = resumed_from
        out.append(
            UntrustedFragment(
                kind=SECTION, content=json.dumps(body, ensure_ascii=False), ref="coordination:previous_attempts", rank=2
            )
        )
    if plan:
        out.append(
            UntrustedFragment(
                kind=SECTION,
                content=json.dumps({"kind": "run_plan", "other_tasks": plan}, ensure_ascii=False),
                ref="coordination:run_plan",
                rank=1,
            )
        )
    return out
