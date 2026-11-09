"""Персональные уведомления пользователя в workspace (UI redesign B8, G-15; ADR-0016; миграция 0020).

Таблица `notifications` — проекция доменных событий: строку пишет только потребитель outbox
`notifications.project` (`aicd_orchestration.notifications`, роль `aicd_worker`) — по одной на получателя, вид и
исходное событие (`UNIQUE (workspace_id, user_id, source_event_id, kind)`: повторная доставка не создаёт дублей).
Каждая новая строка сопровождается outbox-событием `notification.created` (в той же транзакции): worker-outbox после
commit подаёт сигнал потоку workspace (`aicd:ws:{id}`), а поток `GET /workspaces/{id}/events` читает строки
`notifications` текущего пользователя (RLS) и отдаёт кадры `notification.created`.

Содержимое — только шаблонный заголовок, редактированная краткая выжимка (`redact_text`, ≤ 300 символов) и
идентификаторы для ссылок (run, задача, проект, ресурс). Ни payload событий, ни тексты CLI/агентов, ни секреты
не копируются.

API (`aicd_api.routers.notifications`, роль `aicd_app`): список (keyset `(created_at, id)` desc, фильтр
непрочитанных), счётчик непрочитанных, отметка прочтения — по id, «до уведомления включительно» или всех. RLS:
`aicd_app` видит только строки `user_id = app_current_user()` текущего workspace; меняет только `read_at`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform import outbox
from aicd_platform.clock import isoformat_z
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text, redact_value

CREATED_EVENT: Final = "notification.created"
AGGREGATE_TYPE: Final = "notification"

# Виды (CHECK notifications.kind, миграция 0020; API NotificationKind). Источник и получатели — API_DESIGN §13.2.
KINDS: Final = (
    "approval.requested",
    "approval.decided",
    "run.completed",
    "run.failed",
    "run.blocked",
    "directive.awaiting_confirmation",
    "directive.acknowledged",
    "directive.completed",
    "directive.failed",
    "pull_request.merged",
    "pull_request.failed",
    "finding.critical",
)
SEVERITIES: Final = ("info", "success", "warning", "error")
RESOURCE_TYPES: Final = ("approval", "run", "directive", "gate_execution", "external_action")

TITLE_MAX: Final = 200
BODY_MAX: Final = 300  # выжимка; CHECK БД допускает до 1000
DATA_MAX_KEYS: Final = 12
MARK_MAX_IDS: Final = 100


@dataclass(frozen=True)
class Draft:
    """Уведомление одному получателю (до записи)."""

    user_id: uuid.UUID
    kind: str
    severity: str
    title: str
    resource_type: str
    resource_id: uuid.UUID
    body: str = ""
    project_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"неизвестный вид уведомления: {self.kind}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"неизвестная важность: {self.severity}")
        if self.resource_type not in RESOURCE_TYPES:
            raise ValueError(f"неизвестный тип ресурса: {self.resource_type}")


def summary(value: Any, limit: int = BODY_MAX) -> str:
    """Безопасная выжимка: редактирование секретов, одна строка, усечение."""
    if value is None:
        return ""
    flat = " ".join(str(value).split())
    clean = redact_text(flat)
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _data(value: Mapping[str, Any]) -> dict[str, Any]:
    """Только скаляры (строки ≤ 128), не больше DATA_MAX_KEYS ключей, редактированы."""
    out: dict[str, Any] = {}
    for key, item in list(value.items())[:DATA_MAX_KEYS]:
        if isinstance(item, bool | int | float) or item is None:
            out[str(key)] = item
        else:
            out[str(key)] = summary(item, 128)
    redacted: dict[str, Any] = redact_value(out)
    return redacted


_INSERT = text(
    "INSERT INTO notifications (id, workspace_id, user_id, kind, severity, title, body, project_id, run_id, task_id,"
    " resource_type, resource_id, data, source_event_id)"
    " SELECT :id, :ws, :uid, :kind, :sev, :title, :body, :project, :run, :task, :rtype, :rid, CAST(:data AS jsonb), :src"
    " WHERE EXISTS (SELECT 1 FROM workspace_members m WHERE m.workspace_id = :ws AND m.user_id = :uid)"
    " ON CONFLICT ON CONSTRAINT uq_notifications_source DO NOTHING RETURNING id"
)


async def record(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, source_event_id: uuid.UUID, drafts: Iterable[Draft]
) -> list[uuid.UUID]:
    """Записывает уведомления (контекст: workspace_id, роль aicd_worker); возвращает id созданных строк.

    Получатель, уже не являющийся участником workspace, пропускается; повтор того же события — no-op (уникальный
    ключ). Для каждой новой строки — outbox `notification.created` (сигнал потоку workspace)."""
    created: list[uuid.UUID] = []
    seen: set[tuple[uuid.UUID, str]] = set()
    for draft in drafts:
        if (draft.user_id, draft.kind) in seen:
            continue
        seen.add((draft.user_id, draft.kind))
        notification_id = uuid7()
        inserted = (
            await conn.execute(
                _INSERT,
                {
                    "id": notification_id,
                    "ws": workspace_id,
                    "uid": draft.user_id,
                    "kind": draft.kind,
                    "sev": draft.severity,
                    "title": summary(draft.title, TITLE_MAX) or draft.kind,
                    "body": summary(draft.body),
                    "project": draft.project_id,
                    "run": draft.run_id,
                    "task": draft.task_id,
                    "rtype": draft.resource_type,
                    "rid": draft.resource_id,
                    "data": json.dumps(_data(draft.data), ensure_ascii=False, default=str),
                    "src": source_event_id,
                },
            )
        ).scalar_one_or_none()
        if inserted is None:
            continue
        created.append(inserted)
        await outbox.emit(
            conn,
            event_type=CREATED_EVENT,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=inserted,
            workspace_id=workspace_id,
            payload={"notification_id": str(inserted), "kind": draft.kind},
            causation_id=source_event_id,
        )
    return created


# --- Чтение и отметка прочтения (роль aicd_app; RLS: только свои строки текущего workspace) -------------------

COLUMNS: Final = "id, kind, severity, title, body, project_id, run_id, task_id, resource_type, resource_id, data, created_at, read_at"


def view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Notification (контракт): строка таблицы без workspace/user/source_event_id."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "severity": row["severity"],
        "title": row["title"],
        "body": row["body"],
        "project_id": row["project_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "data": dict(row["data"] or {}),
        "created_at": row["created_at"],
        "read_at": row["read_at"],
        "read": row["read_at"] is not None,
    }


async def list_for_user(
    conn: AsyncConnection,
    *,
    unread_only: bool,
    kinds: Sequence[str],
    before: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    clauses = ["workspace_id = app_current_workspace()", "user_id = app_current_user()"]
    params: dict[str, Any] = {"lim": limit}
    if unread_only:
        clauses.append("read_at IS NULL")
    if kinds:
        clauses.append("kind = ANY(:kinds)")
        params["kinds"] = list(kinds)
    if before is not None:
        clauses.append("(created_at, id) < (:bt, :bid)")
        params["bt"], params["bid"] = before
    sql = f"SELECT {COLUMNS} FROM notifications WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT :lim"  # noqa: S608 — фиксированные фрагменты
    rows = (await conn.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def unread_count(conn: AsyncConnection) -> tuple[int, datetime | None]:
    """(число непрочитанных, время самого нового непрочитанного)."""
    row = (
        await conn.execute(
            text(
                "SELECT count(*), max(created_at) FROM notifications"
                " WHERE workspace_id = app_current_workspace() AND user_id = app_current_user() AND read_at IS NULL"
            )
        )
    ).one()
    return int(row[0]), row[1]


async def locate(conn: AsyncConnection, notification_id: uuid.UUID) -> tuple[datetime, uuid.UUID] | None:
    row = (
        await conn.execute(
            text(
                "SELECT created_at, id FROM notifications WHERE id = :id"
                " AND workspace_id = app_current_workspace() AND user_id = app_current_user()"
            ),
            {"id": notification_id},
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def mark_read(
    conn: AsyncConnection,
    *,
    ids: Sequence[uuid.UUID] | None = None,
    up_to: tuple[datetime, uuid.UUID] | None = None,
) -> int:
    """Отмечает прочитанными: по списку id (чужие/несуществующие игнорируются RLS), до `(created_at, id)`
    включительно или все (оба аргумента None). Уже прочитанные не меняются (read_at сохраняет первое прочтение)."""
    clauses = ["workspace_id = app_current_workspace()", "user_id = app_current_user()", "read_at IS NULL"]
    params: dict[str, Any] = {}
    if ids is not None:
        clauses.append("id = ANY(:ids)")
        params["ids"] = list(ids)
    elif up_to is not None:
        clauses.append("(created_at, id) <= (:ut, :uid)")
        params["ut"], params["uid"] = up_to
    sql = f"UPDATE notifications SET read_at = greatest(now(), created_at) WHERE {' AND '.join(clauses)}"  # noqa: S608
    result = await conn.execute(text(sql), params)
    return int(result.rowcount or 0)


# Поток workspace (aicd_api.routers.events): строки уведомлений пользователя в форме кадра `notification.created`.
SSE_SELECT: Final = (
    "SELECT 'notification' AS source, run_id, NULL::bigint AS seq, id AS event_id,"
    " 'notification.created'::text AS event_type, '1.0'::text AS schema_version,"
    " jsonb_build_object('id', id, 'kind', kind, 'severity', severity, 'title', title, 'body', body,"
    " 'project_id', project_id, 'run_id', run_id, 'task_id', task_id, 'resource_type', resource_type,"
    " 'resource_id', resource_id, 'data', data, 'created_at', created_at, 'read_at', read_at,"
    " 'read', read_at IS NOT NULL) AS payload,"
    " created_at AS occurred_at, created_at"
    " FROM notifications WHERE workspace_id = :ws AND user_id = app_current_user()"
)


def sse_payload(payload: Any, created_at: datetime) -> dict[str, Any]:
    """Payload кадра `notification.created` (= Notification): времена — UTC ISO 8601 с Z, как в REST."""
    body = json.loads(payload) if isinstance(payload, str) else dict(payload)
    body["created_at"] = isoformat_z(created_at)
    read_at = body.get("read_at")
    if isinstance(read_at, str):
        body["read_at"] = isoformat_z(datetime.fromisoformat(read_at))
    body["data"] = dict(body.get("data") or {})
    return body
