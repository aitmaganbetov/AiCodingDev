"""Отправка ревью кода (ADR-0015 §3): единственная точка воздействия inline-комментариев на исполнение.

* `APPROVE` — решение существующего `PENDING` approval этого run тем же путём, что поручение APPROVE и
  `POST /approvals/{id}/decision` (роль по action_type, four-eyes; step-up проверяет API) — без обхода approvals/gates;
* `REQUEST_CHANGES` — поручение `CHANGE_REQUEST` (impact analysis → подтверждение → checkpoint → новая plan_version
  → задачи замены → доставка/ack). Текст замечаний — недоверенные данные (`change_request_message`), в инструкции
  задач он попадает только внутри `fence_untrusted`;
* `COMMENT` — только запись ревью.

Всё — в одной транзакции вызывающего (API: `run_idempotent`): ревью, поручение/решение, outbox, audit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_collaboration import review_comments
from aicd_identity.rbac import Role, can_submit_review
from aicd_orchestration import fsm
from aicd_orchestration.directives import Directives, SubmitResult, Target
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text

VERDICTS: Final = frozenset({"APPROVE", "REQUEST_CHANGES", "COMMENT"})
MAX_THREADS: Final = 50


@dataclass
class ReviewResult:
    review_id: uuid.UUID
    directive: SubmitResult | None


def _invalid(pointer: str, code: str, message: str) -> ProblemError:
    return ProblemError("validation_error", errors=[FieldError(pointer, code, message)])


async def _threads(conn: AsyncConnection, run_id: uuid.UUID, thread_ids: Sequence[uuid.UUID]) -> list[dict[str, Any]]:
    """Треды тела запроса: не видны под RLS ⇒ 404 (API_DESIGN §4); не этого run ⇒ 422."""
    threads: list[dict[str, Any]] = []
    for thread_id in thread_ids:
        thread = await review_comments.load_thread(conn, thread_id)
        if thread is None:
            raise ProblemError("not_found")
        if thread["run_id"] != run_id:
            raise _invalid("/thread_ids", "mismatch", f"тред {thread_id} не относится к этому run")
        threads.append(thread)
    return threads


async def submit_review(
    conn: AsyncConnection,
    directives: Directives,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_role: Role | str,
    verdict: str,
    body: str | None,
    thread_ids: Sequence[uuid.UUID],
    task_id: uuid.UUID | None,
    approval_id: uuid.UUID | None,
    expected_run_version: int,
    requires_impact_preview: bool = True,
) -> ReviewResult:
    if verdict not in VERDICTS:  # pragma: no cover — схема запроса
        raise _invalid("/verdict", "enum", "APPROVE, REQUEST_CHANGES или COMMENT")
    if not can_submit_review(actor_role, verdict):
        raise ProblemError("forbidden")
    ids = list(thread_ids)
    if len(set(ids)) != len(ids) or len(ids) > MAX_THREADS:
        raise _invalid("/thread_ids", "invalid", f"уникальные id, не более {MAX_THREADS}")
    summary = redact_text(body) if body is not None else None
    if summary is not None and (not summary.strip() or "\x00" in summary):
        raise _invalid("/body", "invalid", "пустой текст или NUL-символ")
    if verdict == "APPROVE" and approval_id is None:
        raise _invalid("/approval_id", "required", "APPROVE решает существующий approval этого run")
    if verdict != "APPROVE" and approval_id is not None:
        raise _invalid("/approval_id", "not_allowed", "только для APPROVE")
    if verdict == "REQUEST_CHANGES" and not ids and summary is None:
        raise _invalid("/thread_ids", "required", "нужны треды или общее замечание")

    run = (
        (await conn.execute(text("SELECT status, version FROM runs WHERE id = :id"), {"id": run_id})).mappings().first()
    )
    if run is None:
        raise ProblemError("not_found")
    if run["status"] in fsm.TERMINAL["run"]:
        raise ProblemError("invalid_state_transition", detail="Run завершён", extensions={"current_status": run["status"]})
    if task_id is not None:
        task_run = (await conn.execute(text("SELECT run_id FROM tasks WHERE id = :id"), {"id": task_id})).first()
        if task_run is None:
            raise ProblemError("not_found")
        if task_run[0] != run_id:
            raise _invalid("/task_id", "mismatch", "задача не относится к этому run")
    threads = await _threads(conn, run_id, ids)
    if verdict == "REQUEST_CHANGES":
        closed = [str(t["id"]) for t in threads if t["status"] != "OPEN"]
        if closed:
            raise _invalid("/thread_ids", "resolved", "решённые треды не отправляются: " + ", ".join(closed))

    review_id = uuid7()
    result: SubmitResult | None = None
    if verdict == "APPROVE":
        result = await directives.submit(
            conn, run_id=run_id, workspace_id=workspace_id, actor_id=actor_id, actor_role=actor_role,
            directive_type="APPROVE", target=Target("approval", approval_id=approval_id),
            message=summary or f"Одобрено ревью кода {review_id}", expected_run_version=expected_run_version,
            requires_impact_preview=False,
        )  # fmt: skip
    elif verdict == "REQUEST_CHANGES":
        comments = await review_comments.load_comments(conn, [t["id"] for t in threads])
        message = review_comments.change_request_message(review_id, threads, comments, summary)
        target = Target("task", task_id=task_id) if task_id is not None else Target("run")
        result = await directives.submit(
            conn, run_id=run_id, workspace_id=workspace_id, actor_id=actor_id, actor_role=actor_role,
            directive_type="CHANGE_REQUEST", target=target, message=message,
            expected_run_version=expected_run_version, requires_impact_preview=requires_impact_preview,
        )  # fmt: skip
    elif run["version"] != expected_run_version:
        raise ProblemError("version_mismatch", extensions={"current_version": run["version"]})

    await review_comments.record_submission(
        conn, review_id=review_id, workspace_id=workspace_id, run_id=run_id, task_id=task_id, verdict=verdict,
        body=summary, approval_id=approval_id if verdict == "APPROVE" else None,
        directive_id=result.directive_id if verdict == "REQUEST_CHANGES" and result else None,
        user_id=actor_id, threads=threads,
    )  # fmt: skip
    return ReviewResult(review_id, result)
