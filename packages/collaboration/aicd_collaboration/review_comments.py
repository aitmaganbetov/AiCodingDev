"""Inline-комментарии ревью кода (ADR-0015; UI_REDESIGN_PLAN §3.10 G-11; миграция 0019).

Треды привязаны к репозиторию + commit + стороне diff + пути + диапазону строк (опционально run/задача/находка).
Комментарий — высказывание человека: агентам НЕ доставляется (нет записи в ACP/Context Pack, у `aicd_worker` нет прав
на таблицы). Воздействие на исполнение — только `submitReview` (`aicd_orchestration.code_review`): APPROVE решает
существующий approval, REQUEST_CHANGES создаёт поручение `CHANGE_REQUEST` с текстом `change_request_message`.

Каждая команда — одна транзакция: изменение + outbox `review.comment` (без тела комментария) + audit (без тела).
Тело редактируется при записи и повторно при чтении (как Live Agent Chat).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform import audit, outbox
from aicd_platform.clock import isoformat_z, utcnow
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text

EVENT_TYPE: Final = "review.comment"
AGGREGATE: Final = "review_thread"
BODY_MAX_CHARS: Final = 8000
MAX_COMMENTS_PER_THREAD: Final = 200
MAX_LINES: Final = 1000
CHANGE_REQUEST_MAX_CHARS: Final = 8000  # = DirectiveCreateRequest.message maxLength
ACTIONS: Final = frozenset(
    {"thread_created", "comment_created", "comment_edited", "thread_resolved", "thread_unresolved", "review_submitted"}
)


@dataclass(frozen=True)
class Anchor:
    commit_sha: str
    file_path: str
    start_line: int
    end_line: int
    side: str = "head"
    base_commit_sha: str | None = None


# --- Чтение ------------------------------------------------------------------------------------------------

# Текущий commit области треда (ADR-0015 §8): задача — последний worktree задачи и её производных (fix-задачи,
# замены по origin_task_id); run — последний worktree run в этом репозитории.
_THREAD_SELECT = """
SELECT t.id, t.workspace_id, t.repository_id, r.project_id, t.run_id, t.task_id, t.finding_id, t.commit_sha,
       t.base_commit_sha, t.side, t.file_path, t.start_line, t.end_line, t.status, t.resolved_by,
       ru.display_name AS resolved_by_name, t.resolved_at, t.created_by, cu.display_name AS created_by_name,
       t.created_at, t.updated_at, t.version,
       (SELECT count(*) FROM review_comments c WHERE c.workspace_id = t.workspace_id AND c.thread_id = t.id)
         AS comment_count,
       CASE
         WHEN t.task_id IS NOT NULL THEN (
           WITH RECURSIVE lineage(id) AS (
             SELECT t.task_id
             UNION
             SELECT x.id FROM tasks x JOIN lineage l ON x.origin_task_id = l.id
           )
           SELECT w.head_commit_sha FROM worktrees w
            WHERE w.workspace_id = t.workspace_id AND w.repository_id = t.repository_id
              AND w.task_id IN (SELECT id FROM lineage) AND w.head_commit_sha IS NOT NULL
            ORDER BY w.updated_at DESC, w.id DESC LIMIT 1)
         WHEN t.run_id IS NOT NULL THEN (
           SELECT w.head_commit_sha FROM worktrees w
            WHERE w.workspace_id = t.workspace_id AND w.run_id = t.run_id AND w.repository_id = t.repository_id
              AND w.head_commit_sha IS NOT NULL
            ORDER BY w.updated_at DESC, w.id DESC LIMIT 1)
       END AS scope_commit_sha
  FROM review_threads t
  JOIN repositories r ON r.workspace_id = t.workspace_id AND r.id = t.repository_id
  JOIN users cu ON cu.id = t.created_by
  LEFT JOIN users ru ON ru.id = t.resolved_by
"""

_COMMENT_SELECT = (
    "SELECT c.id, c.thread_id, c.author_id, u.display_name AS author_name, c.body, c.revision, c.created_at,"
    " c.edited_at FROM review_comments c JOIN users u ON u.id = c.author_id"
)


def _user(user_id: uuid.UUID | None, name: str | None) -> dict[str, Any] | None:
    return {"id": user_id, "display_name": name or ""} if user_id is not None else None


def comment_view(r: dict[str, Any], viewer_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "id": r["id"],
        "thread_id": r["thread_id"],
        "author": _user(r["author_id"], r["author_name"]),
        # Тело сохранено редактированным; повторная редакция — защита на чтении (ADR-0015 §7).
        "body": redact_text(r["body"]),
        "revision": r["revision"],
        "created_at": r["created_at"],
        "edited_at": r["edited_at"],
        "can_edit": viewer_id is not None and r["author_id"] == viewer_id,
    }


def outdated(commit_sha: str, current: str | None) -> bool | None:
    """ADR-0015 §8: устарел ли якорь относительно текущего commit (None — текущий commit неизвестен)."""
    return None if current is None else commit_sha != current


def thread_view(
    r: dict[str, Any],
    comments: Sequence[dict[str, Any]],
    *,
    viewer_id: uuid.UUID | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    current = head_sha or r["scope_commit_sha"]
    return {
        "id": r["id"],
        "project_id": r["project_id"],
        "repository_id": r["repository_id"],
        "run_id": r["run_id"],
        "task_id": r["task_id"],
        "finding_id": r["finding_id"],
        "anchor": {
            "commit_sha": r["commit_sha"],
            "base_commit_sha": r["base_commit_sha"],
            "side": r["side"],
            "file_path": r["file_path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
        },
        "status": r["status"],
        "outdated": outdated(r["commit_sha"], current),
        "current_commit_sha": current,
        "resolved_by": _user(r["resolved_by"], r["resolved_by_name"]),
        "resolved_at": r["resolved_at"],
        "created_by": _user(r["created_by"], r["created_by_name"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "version": r["version"],
        "comment_count": r["comment_count"],
        "comments": [comment_view(c, viewer_id) for c in comments],
    }


async def resolve_thread_workspace(conn: AsyncConnection, thread_id: uuid.UUID) -> uuid.UUID | None:
    """workspace треда — только для члена этого workspace (SECURITY DEFINER, миграция 0019); иначе None (→ 404)."""
    value = (
        await conn.execute(text("SELECT sys_resolve_review_thread_workspace(:id)"), {"id": thread_id})
    ).scalar_one_or_none()
    return uuid.UUID(str(value)) if value is not None else None


async def resolve_comment_workspace(conn: AsyncConnection, comment_id: uuid.UUID) -> uuid.UUID | None:
    value = (
        await conn.execute(text("SELECT sys_resolve_review_comment_workspace(:id)"), {"id": comment_id})
    ).scalar_one_or_none()
    return uuid.UUID(str(value)) if value is not None else None


async def load_thread(conn: AsyncConnection, thread_id: uuid.UUID) -> dict[str, Any] | None:
    row = (await conn.execute(text(_THREAD_SELECT + " WHERE t.id = :id"), {"id": thread_id})).mappings().first()
    return dict(row) if row else None


async def load_comments(conn: AsyncConnection, thread_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    ids = list(dict.fromkeys(thread_ids))
    out: dict[uuid.UUID, list[dict[str, Any]]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = (
        await conn.execute(
            text(_COMMENT_SELECT + " WHERE c.thread_id = ANY(:ids) ORDER BY c.created_at, c.id"), {"ids": ids}
        )
    ).mappings()
    for r in rows:
        out[r["thread_id"]].append(dict(r))
    return out


async def load_comment(conn: AsyncConnection, comment_id: uuid.UUID) -> dict[str, Any] | None:
    row = (await conn.execute(text(_COMMENT_SELECT + " WHERE c.id = :id"), {"id": comment_id})).mappings().first()
    return dict(row) if row else None


@dataclass(frozen=True)
class ThreadFilters:
    run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    commit_sha: str | None = None
    file_path: str | None = None
    statuses: tuple[str, ...] = ()


async def list_threads(
    conn: AsyncConnection,
    repository_id: uuid.UUID,
    filters: ThreadFilters,
    *,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Треды репозитория по возрастанию (created_at, id) — keyset-курсор."""
    rows = (
        await conn.execute(
            text(
                _THREAD_SELECT + " WHERE t.repository_id = :repo"
                " AND (CAST(:run AS uuid) IS NULL OR t.run_id = :run)"
                " AND (CAST(:task AS uuid) IS NULL OR t.task_id = :task)"
                " AND (CAST(:commit AS text) IS NULL OR t.commit_sha = :commit)"
                " AND (CAST(:path AS text) IS NULL OR t.file_path = :path)"
                " AND (CAST(:statuses AS text[]) IS NULL OR t.status = ANY(CAST(:statuses AS text[])))"
                " AND (CAST(:at AS timestamptz) IS NULL OR (t.created_at, t.id) > (CAST(:at AS timestamptz), CAST(:ai AS uuid)))"
                " ORDER BY t.created_at, t.id LIMIT :lim"
            ),
            {
                "repo": repository_id,
                "run": filters.run_id,
                "task": filters.task_id,
                "commit": filters.commit_sha,
                "path": filters.file_path,
                "statuses": list(filters.statuses) or None,
                "at": after[0] if after else None,
                "ai": after[1] if after else None,
                "lim": limit,
            },
        )
    ).mappings()
    return [dict(r) for r in rows]


async def list_revisions(conn: AsyncConnection, comment_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(
            text(
                "SELECT revision, body, authored_at, superseded_at FROM review_comment_revisions"
                " WHERE comment_id = :id ORDER BY revision"
            ),
            {"id": comment_id},
        )
    ).mappings()
    return [
        {
            "revision": r["revision"],
            "body": redact_text(r["body"]),
            "authored_at": r["authored_at"],
            "superseded_at": r["superseded_at"],
        }
        for r in rows
    ]


# --- Команды -----------------------------------------------------------------------------------------------


def _clean_body(body: str, pointer: str = "/body") -> str:
    if "\x00" in body:
        raise ProblemError("validation_error", errors=[FieldError(pointer, "invalid", "NUL-символ недопустим")])
    clean = redact_text(body)
    if not clean.strip() or len(clean) > BODY_MAX_CHARS:
        raise ProblemError(
            "validation_error", errors=[FieldError(pointer, "length", f"от 1 до {BODY_MAX_CHARS} символов")]
        )
    return clean


def event_payload(
    thread: dict[str, Any],
    action: str,
    *,
    actor_id: uuid.UUID,
    comment_id: uuid.UUID | None = None,
    review_id: uuid.UUID | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Payload `review.comment` в форме SSE (sse-event.schema.json#/$defs/review_comment). Без тела комментария."""
    if action not in ACTIONS:  # pragma: no cover — нарушение инварианта
        raise ValueError(action)
    return {
        "action": action,
        "thread_id": str(thread["id"]),
        "comment_id": str(comment_id) if comment_id else None,
        "review_id": str(review_id) if review_id else None,
        "repository_id": str(thread["repository_id"]),
        "task_id": str(thread["task_id"]) if thread["task_id"] else None,
        "commit_sha": thread["commit_sha"],
        "side": thread["side"],
        "file_path": thread["file_path"],
        "start_line": thread["start_line"],
        "end_line": thread["end_line"],
        "status": status or thread["status"],
        "actor_id": str(actor_id),
        "occurred_at": isoformat_z(utcnow()),
    }


async def emit(
    conn: AsyncConnection,
    thread: dict[str, Any],
    action: str,
    *,
    actor_id: uuid.UUID,
    comment_id: uuid.UUID | None = None,
    review_id: uuid.UUID | None = None,
    status: str | None = None,
) -> None:
    await outbox.emit(
        conn,
        event_type=EVENT_TYPE,
        aggregate_type=AGGREGATE,
        aggregate_id=thread["id"],
        workspace_id=thread["workspace_id"],
        run_id=thread["run_id"],
        payload=event_payload(thread, action, actor_id=actor_id, comment_id=comment_id, review_id=review_id,
                              status=status),
    )  # fmt: skip


def _anchor_meta(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository_id": str(thread["repository_id"]),
        "run_id": str(thread["run_id"]) if thread["run_id"] else None,
        "task_id": str(thread["task_id"]) if thread["task_id"] else None,
        "commit_sha": thread["commit_sha"],
        "file_path": thread["file_path"],
        "start_line": thread["start_line"],
        "end_line": thread["end_line"],
    }


async def _validate_links(
    conn: AsyncConnection,
    repository_id: uuid.UUID,
    run_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    finding_id: uuid.UUID | None,
) -> None:
    """Ссылки тела на ресурсы: не видны под RLS (чужой workspace/нет) ⇒ 404 (API_DESIGN §4); видны, но не
    согласованы с репозиторием/run ⇒ 422."""
    if task_id is not None and run_id is None:
        raise ProblemError("validation_error", errors=[FieldError("/run_id", "required", "обязателен при task_id")])
    if run_id is not None:
        run_repo = (
            await conn.execute(text("SELECT repository_id FROM runs WHERE id = :id"), {"id": run_id})
        ).first()
        if run_repo is None:
            raise ProblemError("not_found")
        if run_repo[0] != repository_id:
            raise ProblemError(
                "validation_error", errors=[FieldError("/run_id", "mismatch", "run не относится к этому репозиторию")]
            )
    if task_id is not None:
        task_run = (await conn.execute(text("SELECT run_id FROM tasks WHERE id = :id"), {"id": task_id})).first()
        if task_run is None:
            raise ProblemError("not_found")
        if task_run[0] != run_id:
            raise ProblemError(
                "validation_error", errors=[FieldError("/task_id", "mismatch", "задача не относится к этому run")]
            )
    if finding_id is not None:
        finding_run = (
            await conn.execute(text("SELECT run_id FROM findings WHERE id = :id"), {"id": finding_id})
        ).first()
        if finding_run is None:
            raise ProblemError("not_found")
        if run_id is not None and finding_run[0] != run_id:
            raise ProblemError(
                "validation_error",
                errors=[FieldError("/finding_id", "mismatch", "находка не относится к этому run")],
            )


async def create_thread(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    repository_id: uuid.UUID,
    user_id: uuid.UUID,
    anchor: Anchor,
    body: str,
    run_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    finding_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Тред + первый комментарий + outbox `review.comment` (thread_created) + audit — одна транзакция."""
    if anchor.end_line < anchor.start_line or anchor.end_line - anchor.start_line >= MAX_LINES:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/anchor/end_line", "range", f"end_line ≥ start_line, не более {MAX_LINES} строк")],
        )
    if anchor.side == "base" and anchor.base_commit_sha is None:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/anchor/base_commit_sha", "required", "обязателен для стороны base")],
        )
    clean = _clean_body(body)
    await _validate_links(conn, repository_id, run_id, task_id, finding_id)
    thread_id, comment_id = uuid7(), uuid7()
    await conn.execute(
        text(
            "INSERT INTO review_threads (id, workspace_id, repository_id, run_id, task_id, finding_id, commit_sha,"
            " base_commit_sha, side, file_path, start_line, end_line, created_by)"
            " VALUES (:id, :ws, :repo, :run, :task, :finding, :sha, :base, :side, :path, :sl, :el, :uid)"
        ),
        {
            "id": thread_id,
            "ws": workspace_id,
            "repo": repository_id,
            "run": run_id,
            "task": task_id,
            "finding": finding_id,
            "sha": anchor.commit_sha,
            "base": anchor.base_commit_sha,
            "side": anchor.side,
            "path": anchor.file_path,
            "sl": anchor.start_line,
            "el": anchor.end_line,
            "uid": user_id,
        },
    )
    await _insert_comment(conn, workspace_id, thread_id, comment_id, user_id, clean)
    thread = await load_thread(conn, thread_id)
    if thread is None:  # pragma: no cover — нарушение инварианта
        raise LookupError(thread_id)
    await emit(conn, thread, "thread_created", actor_id=user_id, comment_id=comment_id)
    await audit.record(
        conn,
        action="review.thread.create",
        resource_type="review_thread",
        resource_id=thread_id,
        actor_type="user",
        actor_id=user_id,
        workspace_id=workspace_id,
        metadata={**_anchor_meta(thread), "comment_id": str(comment_id)},
    )
    return thread_id


async def _insert_comment(
    conn: AsyncConnection, workspace_id: uuid.UUID, thread_id: uuid.UUID, comment_id: uuid.UUID, user_id: uuid.UUID,
    body: str,
) -> None:  # fmt: skip
    await conn.execute(
        text(
            "INSERT INTO review_comments (id, workspace_id, thread_id, author_id, body)"
            " VALUES (:id, :ws, :th, :uid, :body)"
        ),
        {"id": comment_id, "ws": workspace_id, "th": thread_id, "uid": user_id, "body": body},
    )


async def reply(conn: AsyncConnection, *, thread_id: uuid.UUID, user_id: uuid.UUID, body: str) -> uuid.UUID:
    """Ответ в треде (в т.ч. в RESOLVED — обсуждение можно продолжить, статус не меняется)."""
    clean = _clean_body(body)
    locked = (
        await conn.execute(text("SELECT id FROM review_threads WHERE id = :id FOR UPDATE"), {"id": thread_id})
    ).first()
    thread = await load_thread(conn, thread_id) if locked else None
    if thread is None:
        raise ProblemError("not_found")
    if int(thread["comment_count"]) >= MAX_COMMENTS_PER_THREAD:
        raise ProblemError("conflict", detail=f"в треде не более {MAX_COMMENTS_PER_THREAD} комментариев")
    comment_id = uuid7()
    await _insert_comment(conn, thread["workspace_id"], thread_id, comment_id, user_id, clean)
    await emit(conn, thread, "comment_created", actor_id=user_id, comment_id=comment_id)
    await audit.record(
        conn,
        action="review.comment.create",
        resource_type="review_comment",
        resource_id=comment_id,
        actor_type="user",
        actor_id=user_id,
        workspace_id=thread["workspace_id"],
        metadata={"thread_id": str(thread_id), **_anchor_meta(thread)},
    )
    return comment_id


async def edit_comment(
    conn: AsyncConnection, *, comment_id: uuid.UUID, user_id: uuid.UUID, body: str, expected_revision: int
) -> None:
    """Правка своего комментария (If-Match = revision). Прежнее тело сохраняет триггер БД (история правок)."""
    clean = _clean_body(body)
    row = (
        (
            await conn.execute(
                text("SELECT id, thread_id, author_id, body, revision FROM review_comments WHERE id = :id FOR UPDATE"),
                {"id": comment_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ProblemError("not_found")
    if row["author_id"] != user_id:
        raise ProblemError("forbidden")
    if row["revision"] != expected_revision:
        raise ProblemError(
            "precondition_failed", extensions={"current_version": row["revision"]},
            headers={"ETag": f'"v{row["revision"]}"'},
        )  # fmt: skip
    if row["body"] == clean:
        return  # без изменений — новая редакция не создаётся
    await conn.execute(text("UPDATE review_comments SET body = :b WHERE id = :id"), {"b": clean, "id": comment_id})
    thread = await load_thread(conn, row["thread_id"])
    if thread is None:  # pragma: no cover — нарушение инварианта
        raise LookupError(row["thread_id"])
    await emit(conn, thread, "comment_edited", actor_id=user_id, comment_id=comment_id)
    await audit.record(
        conn,
        action="review.comment.edit",
        resource_type="review_comment",
        resource_id=comment_id,
        actor_type="user",
        actor_id=user_id,
        workspace_id=thread["workspace_id"],
        metadata={"thread_id": str(thread["id"]), "revision": int(row["revision"]) + 1},
    )


async def set_status(
    conn: AsyncConnection, *, thread_id: uuid.UUID, user_id: uuid.UUID, resolve: bool, expected_version: int
) -> None:
    """resolve/unresolve (If-Match = version треда). Повтор достигнутого состояния — 409 (FSM без петель)."""
    row = (
        (
            await conn.execute(
                text("SELECT status, version FROM review_threads WHERE id = :id FOR UPDATE"), {"id": thread_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ProblemError("not_found")
    if row["version"] != expected_version:
        raise ProblemError(
            "precondition_failed", extensions={"current_version": row["version"]},
            headers={"ETag": f'"v{row["version"]}"'},
        )  # fmt: skip
    target = "RESOLVED" if resolve else "OPEN"
    if row["status"] == target:
        raise ProblemError(
            "invalid_state_transition",
            detail="Тред уже " + ("решён" if resolve else "открыт"),
            extensions={"current_status": row["status"]},
        )
    if resolve:
        await conn.execute(
            text("UPDATE review_threads SET status = 'RESOLVED', resolved_by = :u, resolved_at = now() WHERE id = :id"),
            {"u": user_id, "id": thread_id},
        )
    else:
        await conn.execute(
            text("UPDATE review_threads SET status = 'OPEN', resolved_by = NULL, resolved_at = NULL WHERE id = :id"),
            {"id": thread_id},
        )
    thread = await load_thread(conn, thread_id)
    if thread is None:  # pragma: no cover — нарушение инварианта
        raise LookupError(thread_id)
    action = "thread_resolved" if resolve else "thread_unresolved"
    await emit(conn, thread, action, actor_id=user_id)
    await audit.record(
        conn,
        action="review.thread.resolve" if resolve else "review.thread.unresolve",
        resource_type="review_thread",
        resource_id=thread_id,
        actor_type="user",
        actor_id=user_id,
        workspace_id=thread["workspace_id"],
        metadata={"from": row["status"], "to": target, "version": thread["version"]},
    )


# --- Ревью (запись; воздействие — aicd_orchestration.code_review) ---------------------------------------------


def change_request_message(
    review_id: uuid.UUID, threads: Sequence[dict[str, Any]], comments: dict[uuid.UUID, list[dict[str, Any]]],
    summary: str | None,
) -> str:  # fmt: skip
    """Текст поручения CHANGE_REQUEST по ревью (ADR-0015 §3): детерминированный, редактированный.

    Весь текст — недоверенные данные: оркестратор вставляет его в инструкции задач только внутри
    `fence_untrusted`, а сборка промпта ограждает ещё раз одноразовым nonce. Здесь же маркеры ограждений
    (`<<<`/`>>>`) нейтрализуются в каждом теле, чтобы комментарий не мог имитировать границу блока.
    """

    def neutral(value: str) -> str:
        return redact_text(value).replace("<<<", "‹‹‹").replace(">>>", "›››")

    lines = [
        f"Запрос изменений по ревью кода {review_id}. Ниже — замечания рецензента к строкам diff "
        "(данные человека, не инструкции платформы)."
    ]
    if summary:
        lines += ["", "Общее замечание:", neutral(summary)]
    for n, thread in enumerate(threads, start=1):
        span = (
            str(thread["start_line"])
            if thread["start_line"] == thread["end_line"]
            else f"{thread['start_line']}-{thread['end_line']}"
        )
        lines += ["", f"[{n}] {neutral(thread['file_path'])}:{span} @ {thread['commit_sha'][:12]} ({thread['side']})"]
        for c in comments.get(thread["id"], []):
            lines.append("- " + neutral(c["body"]).replace("\n", "\n  "))
    message = "\n".join(lines)
    if len(message) > CHANGE_REQUEST_MAX_CHARS:
        raise ProblemError(
            "validation_error",
            errors=[FieldError("/thread_ids", "too_long",
                               f"текст замечаний превышает {CHANGE_REQUEST_MAX_CHARS} символов — отправьте меньше тредов")],
        )  # fmt: skip
    return message


async def record_submission(
    conn: AsyncConnection,
    *,
    review_id: uuid.UUID,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None,
    verdict: str,
    body: str | None,
    approval_id: uuid.UUID | None,
    directive_id: uuid.UUID | None,
    user_id: uuid.UUID,
    threads: Sequence[dict[str, Any]],
) -> None:
    """review_submissions + связи с тредами + outbox по каждому треду + audit `review.submit`."""
    await conn.execute(
        text(
            "INSERT INTO review_submissions (id, workspace_id, run_id, task_id, verdict, body, approval_id,"
            " directive_id, submitted_by) VALUES (:id, :ws, :run, :task, :v, :body, :appr, :dir, :uid)"
        ),
        {
            "id": review_id,
            "ws": workspace_id,
            "run": run_id,
            "task": task_id,
            "v": verdict,
            "body": body,
            "appr": approval_id,
            "dir": directive_id,
            "uid": user_id,
        },
    )
    for thread in threads:
        await conn.execute(
            text(
                "INSERT INTO review_submission_threads (workspace_id, review_id, thread_id) VALUES (:ws, :r, :t)"
            ),
            {"ws": workspace_id, "r": review_id, "t": thread["id"]},
        )
        await emit(conn, thread, "review_submitted", actor_id=user_id, review_id=review_id)
    await audit.record(
        conn,
        action="review.submit",
        resource_type="review_submission",
        resource_id=review_id,
        actor_type="user",
        actor_id=user_id,
        workspace_id=workspace_id,
        metadata={
            "run_id": str(run_id),
            "verdict": verdict,
            "thread_ids": [str(t["id"]) for t in threads],
            "approval_id": str(approval_id) if approval_id else None,
            "directive_id": str(directive_id) if directive_id else None,
        },
    )


_SUBMISSION_SELECT = (
    "SELECT s.id, s.run_id, s.task_id, s.verdict, s.body, s.approval_id, s.directive_id, d.status AS directive_status,"
    " a.status AS approval_status, s.submitted_by, u.display_name AS submitted_by_name, s.created_at,"
    " ARRAY(SELECT st.thread_id FROM review_submission_threads st WHERE st.review_id = s.id ORDER BY st.thread_id)"
    "   AS thread_ids"
    " FROM review_submissions s JOIN users u ON u.id = s.submitted_by"
    " LEFT JOIN user_directives d ON d.id = s.directive_id"
    " LEFT JOIN approvals a ON a.id = s.approval_id"
)


def submission_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "task_id": r["task_id"],
        "verdict": r["verdict"],
        "body": redact_text(r["body"]) if r["body"] is not None else None,
        "thread_ids": list(r["thread_ids"] or []),
        "approval_id": r["approval_id"],
        "approval_status": r["approval_status"],
        "directive_id": r["directive_id"],
        "directive_status": r["directive_status"],
        "submitted_by": _user(r["submitted_by"], r["submitted_by_name"]),
        "created_at": r["created_at"],
    }


async def load_submission(conn: AsyncConnection, review_id: uuid.UUID) -> dict[str, Any] | None:
    row = (await conn.execute(text(_SUBMISSION_SELECT + " WHERE s.id = :id"), {"id": review_id})).mappings().first()
    return dict(row) if row else None


async def list_submissions(
    conn: AsyncConnection, run_id: uuid.UUID, *, before: tuple[datetime, uuid.UUID] | None, limit: int
) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(
            text(
                _SUBMISSION_SELECT + " WHERE s.run_id = :run"
                " AND (CAST(:bt AS timestamptz) IS NULL OR (s.created_at, s.id) < (CAST(:bt AS timestamptz), CAST(:bi AS uuid)))"
                " ORDER BY s.created_at DESC, s.id DESC LIMIT :lim"
            ),
            {"run": run_id, "bt": before[0] if before else None, "bi": before[1] if before else None, "lim": limit},
        )
    ).mappings()
    return [dict(r) for r in rows]
