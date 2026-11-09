"""SQL модуля integrations_github (контекст workspace/RLS устанавливает вызывающий через `platform.db`)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_integrations_github.client import Installation, PullInfo
from aicd_platform.ids import uuid7

_INSTALL_COLS = (
    "id, workspace_id, installation_id, account_login, account_type, repository_selection, permissions, status,"
    " installed_by, created_at, updated_at"
)
_PR_COLS = (
    "p.id, p.workspace_id, p.repository_id, p.run_id, p.create_action_id, p.merge_action_id, p.pr_number,"
    " p.head_branch, p.base_branch, p.head_sha, p.state, p.url, p.merged_commit_sha, p.created_at, p.updated_at,"
    " p.merged_at, p.closed_at, r.full_name AS repository_full_name, r.project_id"
)


class InstallationConflictError(Exception):
    """Установка уже подключена к другому workspace (UNIQUE installation_id глобален, §16.1)."""


async def list_installations(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = await conn.execute(text(f"SELECT {_INSTALL_COLS} FROM github_installations ORDER BY created_at, id"))  # noqa: S608
    return [dict(r) for r in rows.mappings().all()]


async def get_installation(conn: AsyncConnection, row_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any] | None:
    sql = f"SELECT {_INSTALL_COLS} FROM github_installations WHERE id = :id" + (" FOR UPDATE" if lock else "")  # noqa: S608
    row = (await conn.execute(text(sql), {"id": row_id})).mappings().first()
    return dict(row) if row else None


async def installation_by_github_id(conn: AsyncConnection, installation_id: int) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                text(f"SELECT {_INSTALL_COLS} FROM github_installations WHERE installation_id = :i"),  # noqa: S608
                {"i": installation_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


PENDING_ACCOUNT_LOGIN = "pending"  # заглушка до проверки установки воркером (status=PENDING, OQ-47 (2))


async def register_pending_installation(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, installation_id: int, actor_id: uuid.UUID
) -> tuple[dict[str, Any], bool]:
    """API (без ключа App): строка установки в статусе PENDING до проверки воркером.

    Установка этого workspace (в т. ч. REVOKED) — повторная проверка (статус PENDING); установка другого
    workspace ⇒ InstallationConflictError (UNIQUE installation_id). Возвращает (строка, создана ли новая).
    """
    existing = await installation_by_github_id(conn, installation_id)
    if existing is not None:
        await set_installation_status(conn, existing["id"], "PENDING")
        row = await get_installation(conn, existing["id"])
        if row is None:  # pragma: no cover — нарушение инварианта
            raise LookupError("installation")
        return row, False
    row_id = uuid7()
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO github_installations (id, workspace_id, installation_id, account_login, account_type,"
                    " repository_selection, permissions, status, installed_by)"
                    " VALUES (:id, :ws, :inst, :login, 'organization', 'selected', '{}'::jsonb, 'PENDING', :uid)"
                ),
                {
                    "id": row_id,
                    "ws": workspace_id,
                    "inst": installation_id,
                    "login": PENDING_ACCOUNT_LOGIN,
                    "uid": actor_id,
                },
            )
    except IntegrityError as exc:
        raise InstallationConflictError(str(installation_id)) from exc
    row = await get_installation(conn, row_id)
    if row is None:  # pragma: no cover — нарушение инварианта
        raise LookupError("installation")
    return row, True


async def connect_installation(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, installation: Installation, actor_id: uuid.UUID
) -> tuple[dict[str, Any], bool]:
    """Подключить установку к workspace (повторное подключение REVOKED/SUSPENDED — реактивация).

    Возвращает (строка, создана ли новая). Установка другого workspace ⇒ InstallationConflictError.
    """
    perms = json.dumps(installation.permissions, sort_keys=True)
    existing = await installation_by_github_id(conn, installation.installation_id)
    status = "SUSPENDED" if installation.suspended else "ACTIVE"
    if existing is not None:
        await conn.execute(
            text(
                "UPDATE github_installations SET status = :s, account_login = :login, account_type = :atype,"
                " repository_selection = :sel, permissions = CAST(:perms AS jsonb), installed_by = :uid WHERE id = :id"
            ),
            {
                "s": status,
                "login": installation.account_login,
                "atype": installation.account_type,
                "sel": installation.repository_selection,
                "perms": perms,
                "uid": actor_id,
                "id": existing["id"],
            },
        )
        row = await get_installation(conn, existing["id"])
        if row is None:  # pragma: no cover — нарушение инварианта
            raise LookupError("installation")
        return row, False
    row_id = uuid7()
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO github_installations (id, workspace_id, installation_id, account_login, account_type,"
                    " repository_selection, permissions, status, installed_by)"
                    " VALUES (:id, :ws, :inst, :login, :atype, :sel, CAST(:perms AS jsonb), :s, :uid)"
                ),
                {
                    "id": row_id,
                    "ws": workspace_id,
                    "inst": installation.installation_id,
                    "login": installation.account_login,
                    "atype": installation.account_type,
                    "sel": installation.repository_selection,
                    "perms": perms,
                    "s": status,
                    "uid": actor_id,
                },
            )
    except IntegrityError as exc:
        raise InstallationConflictError(str(installation.installation_id)) from exc
    row = await get_installation(conn, row_id)
    if row is None:  # pragma: no cover — нарушение инварианта
        raise LookupError("installation")
    return row, True


async def set_installation_status(conn: AsyncConnection, row_id: uuid.UUID, status: str) -> None:
    await conn.execute(text("UPDATE github_installations SET status = :s WHERE id = :id"), {"s": status, "id": row_id})


# --- Pull requests -----------------------------------------------------------------------------------


async def get_pull_request(conn: AsyncConnection, pr_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any] | None:
    sql = (
        f"SELECT {_PR_COLS} FROM pull_requests p JOIN repositories r ON r.id = p.repository_id WHERE p.id = :id"  # noqa: S608
        + (" FOR UPDATE OF p" if lock else "")
    )
    row = (await conn.execute(text(sql), {"id": pr_id})).mappings().first()
    return dict(row) if row else None


async def pull_request_by_number(conn: AsyncConnection, repository_id: uuid.UUID, number: int) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                text(
                    f"SELECT {_PR_COLS} FROM pull_requests p JOIN repositories r ON r.id = p.repository_id"  # noqa: S608
                    " WHERE p.repository_id = :r AND p.pr_number = :n"
                ),
                {"r": repository_id, "n": number},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def list_pull_requests(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID | None,
    state: str | None,
    before: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    where = ["r.project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "lim": limit}
    if run_id is not None:
        where.append("p.run_id = :run")
        params["run"] = run_id
    if state is not None:
        where.append("p.state = :state")
        params["state"] = state
    if before is not None:
        where.append("(p.created_at, p.id) < (:bt, :bid)")
        params["bt"], params["bid"] = before
    sql = (
        f"SELECT {_PR_COLS} FROM pull_requests p JOIN repositories r ON r.id = p.repository_id"  # noqa: S608
        " WHERE " + " AND ".join(where) + " ORDER BY p.created_at DESC, p.id DESC LIMIT :lim"
    )
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


async def record_pull_request(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    repository_id: uuid.UUID,
    run_id: uuid.UUID | None,
    create_action_id: uuid.UUID | None,
    pull: PullInfo,
) -> uuid.UUID:
    """Строка PR по данным GitHub (идемпотентно по (repository_id, pr_number))."""
    existing = await pull_request_by_number(conn, repository_id, pull.number)
    if existing is not None:
        await apply_remote_state(conn, existing["id"], pull)
        if create_action_id is not None and existing["create_action_id"] is None:
            await conn.execute(
                text("UPDATE pull_requests SET create_action_id = :a WHERE id = :id"),
                {"a": create_action_id, "id": existing["id"]},
            )
        return uuid.UUID(str(existing["id"]))
    pr_id = uuid7()
    state, merged_sha, merged_at, closed_at = _state(pull)
    await conn.execute(
        text(
            "INSERT INTO pull_requests (id, workspace_id, repository_id, run_id, create_action_id, pr_number,"
            " head_branch, base_branch, head_sha, state, url, merged_commit_sha, merged_at, closed_at)"
            " VALUES (:id, :ws, :repo, :run, :ca, :n, :head, :base, :sha, :state, :url, :msha,"
            " CAST(:mat AS timestamptz), CAST(:cat AS timestamptz))"
        ),
        {
            "id": pr_id,
            "ws": workspace_id,
            "repo": repository_id,
            "run": run_id,
            "ca": create_action_id,
            "n": pull.number,
            "head": pull.head_ref[:255],
            "base": pull.base_ref[:255],
            "sha": pull.head_sha,
            "state": state,
            "url": pull.url,
            "msha": merged_sha,
            "mat": merged_at,
            "cat": closed_at,
        },
    )
    return pr_id


def _state(pull: PullInfo) -> tuple[str, str | None, str | None, str | None]:
    if pull.merged and pull.merge_commit_sha:
        return "MERGED", pull.merge_commit_sha, pull.merged_at or _now_iso(), pull.closed_at or pull.merged_at
    if pull.state == "closed":
        return "CLOSED", None, None, pull.closed_at or _now_iso()
    return "OPEN", None, None, None


def _now_iso() -> str:
    from aicd_platform.clock import utcnow

    return utcnow().isoformat()


async def apply_remote_state(
    conn: AsyncConnection, pr_id: uuid.UUID, pull: PullInfo, *, merge_action_id: uuid.UUID | None = None
) -> None:
    """Состояние PR — по ответу GitHub API (не по payload вебхука, SC-GH-03). MERGED терминален (триггер)."""
    state, merged_sha, merged_at, closed_at = _state(pull)
    await conn.execute(
        text(
            "UPDATE pull_requests SET state = CASE WHEN state = 'MERGED' THEN state ELSE :state END,"
            " head_sha = CASE WHEN state = 'MERGED' THEN head_sha ELSE :sha END,"
            " merged_commit_sha = COALESCE(merged_commit_sha, :msha),"
            " merged_at = COALESCE(merged_at, CAST(:mat AS timestamptz)),"
            " closed_at = CASE WHEN :state = 'OPEN' AND state <> 'MERGED' THEN NULL"
            "                  ELSE COALESCE(closed_at, CAST(:cat AS timestamptz)) END,"
            " merge_action_id = COALESCE(merge_action_id, :ma) WHERE id = :id"
        ),
        {
            "state": state,
            "sha": pull.head_sha,
            "msha": merged_sha,
            "mat": merged_at,
            "cat": closed_at,
            "ma": merge_action_id,
            "id": pr_id,
        },
    )


# --- Доставки вебхуков (G-таблица; запись приёма — sys_github_webhook_receive) ------------------------


async def receive_delivery(
    conn: AsyncConnection,
    *,
    delivery_guid: uuid.UUID,
    event: str,
    action: str | None,
    installation_id: int | None,
    signature_valid: bool,
    payload_sha256: str,
    status: str,
    error: str | None,
    hint: dict[str, Any],
) -> str:
    """'accepted' | 'ignored' | 'rejected' | 'duplicate' (см. миграцию 0012)."""
    outcome = (
        await conn.execute(
            text(
                "SELECT sys_github_webhook_receive(:id, :eid, :guid, :event, :action, :inst, :valid, :sha, :status,"
                " :error, CAST(:hint AS jsonb))"
            ),
            {
                "id": uuid7(),
                "eid": uuid7(),
                "guid": delivery_guid,
                "event": event,
                "action": action,
                "inst": installation_id,
                "valid": signature_valid,
                "sha": payload_sha256,
                "status": status,
                "error": error[:2000] if error else None,
                "hint": json.dumps(hint, ensure_ascii=False),
            },
        )
    ).scalar_one()
    return str(outcome)


async def get_delivery(conn: AsyncConnection, delivery_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT id, delivery_guid, event, action, installation_id, signature_valid, status, received_at"
                    " FROM github_webhook_deliveries WHERE id = :id FOR UPDATE"
                ),
                {"id": delivery_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def finish_delivery(conn: AsyncConnection, delivery_id: uuid.UUID, status: str, error: str | None = None) -> None:
    if status not in ("PROCESSED", "IGNORED", "FAILED"):
        raise ValueError(status)
    await conn.execute(
        text(
            "UPDATE github_webhook_deliveries SET status = :s, error = :e, processed_at = now()"
            " WHERE id = :id AND status = 'RECEIVED'"
        ),
        {"s": status, "e": error[:2000] if error else None, "id": delivery_id},
    )


async def resolve_installation_workspace(conn: AsyncConnection, installation_id: int) -> uuid.UUID | None:
    ws = (
        await conn.execute(text("SELECT sys_resolve_installation_workspace(:i)"), {"i": installation_id})
    ).scalar_one_or_none()
    return uuid.UUID(str(ws)) if ws else None
