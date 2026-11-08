"""Журнал внешних действий (side-effect ledger, ADR-0012, P3-11; AC-09, AC-13).

Протокол:
1. `plan` — в транзакции FSM вставить `PLANNED` с детерминированным ключом
   `sha256(run_id, task_id, action_type, target, plan_version)`; повтор возвращает существующую строку.
2. `begin` — `PLANNED|FAILED → EXECUTING` с fencing token аренды. Триггер БД
   `trg_external_actions_approval_gate` запрещает EXECUTING для pr_merge / production_deploy /
   деструктивной migration_apply без APPROVED, не истёкшего approval с `subject_hash = request_hash`.
3. Вызов внешней системы — вне транзакции.
4. `confirm` — `EXECUTING → CONFIRMED` + `external_ref` (только с актуальным fencing token).

`execute_once` — исполнитель протокола: `CONFIRMED` никогда не исполняется повторно; для `EXECUTING`
(крах после начала вызова) и `FAILED` после начала вызова сначала вызывается `probe()` — запрос
состояния внешней системы; если действие уже применено, оно подтверждается без повторного вызова.
Reconciler (P3-12) использует тот же `execute_once` для строк `EXECUTING`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_orchestration import fsm
from aicd_orchestration.approvals import canonical_json, subject_hash
from aicd_platform import outbox
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text, redact_value

ACTION_TYPES: Final = frozenset({
    "git_push", "branch_delete", "pr_create", "pr_update", "pr_merge", "pr_comment", "migration_apply",
    "docs_download", "production_deploy",
})
# Требуют approval (ck_external_actions_approval_required): деструктивная миграция — по флагу.
APPROVAL_REQUIRED: Final = frozenset({"pr_merge", "production_deploy"})


class LedgerError(Exception):
    pass


class ApprovalRequiredError(LedgerError):
    """EXECUTING запрещён: нет APPROVED, не истёкшего approval на этот request_hash (AC-13)."""


class StaleFencingTokenError(LedgerError):
    """Запись от воркера с устаревшим fencing token (аренда перехвачена)."""


class AlreadyConfirmedError(LedgerError):
    """Подтверждённое действие не исполняется повторно (AC-09)."""


@dataclass(frozen=True)
class ExternalAction:
    id: uuid.UUID
    workspace_id: uuid.UUID
    run_id: uuid.UUID | None
    task_id: uuid.UUID | None
    action_type: str
    idempotency_key: str
    target: str
    status: str
    approval_id: uuid.UUID | None
    request: dict[str, Any]
    request_hash: str
    external_ref: str | None
    result: dict[str, Any] | None
    fencing_token: int | None
    attempt_count: int
    executing_at: Any
    version: int


@dataclass(frozen=True)
class Outcome:
    """Результат вызова/запроса состояния внешней системы."""

    external_ref: str | None
    result: dict[str, Any] = field(default_factory=dict)


def idempotency_key(
    run_id: uuid.UUID | None, task_id: uuid.UUID | None, action_type: str, target: str, plan_version: int | None
) -> str:
    material = json.dumps(
        [str(run_id) if run_id else None, str(task_id) if task_id else None, action_type, target, plan_version],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def request_hash(request: dict[str, Any]) -> str:
    """Совпадает с approvals.subject_hash того же предмета — привязка approval к действию."""
    return subject_hash(request)


_COLS = (
    "id, workspace_id, run_id, task_id, action_type, idempotency_key, target, status, approval_id, request,"
    " request_hash, external_ref, result, fencing_token, attempt_count, executing_at, version"
)


async def get(conn: AsyncConnection, action_id: uuid.UUID, *, lock: bool = False) -> ExternalAction | None:
    row = (
        await conn.execute(
            text(f"SELECT {_COLS} FROM external_actions WHERE id = :id" + (" FOR UPDATE" if lock else "")),  # noqa: S608
            {"id": action_id},
        )
    ).mappings().first()
    return ExternalAction(**row) if row else None


async def plan(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, run_id: uuid.UUID | None, task_id: uuid.UUID | None,
    action_type: str, target: str, plan_version: int | None, request: dict[str, Any], is_destructive: bool = False,
    approval_id: uuid.UUID | None = None,
) -> ExternalAction:
    if action_type not in ACTION_TYPES:
        raise ValueError(f"неизвестный action_type: {action_type}")
    key = idempotency_key(run_id, task_id, action_type, target, plan_version)
    action_id = uuid7()
    inserted = (
        await conn.execute(
            text(
                "INSERT INTO external_actions (id, workspace_id, run_id, task_id, action_type, idempotency_key, target,"
                " is_destructive, approval_id, request, request_hash)"
                " VALUES (:id, :ws, :run, :task, :at, :key, :target, :destr, :appr, CAST(:req AS jsonb), :rh)"
                " ON CONFLICT ON CONSTRAINT uq_external_actions_idempotency_key DO NOTHING RETURNING id"
            ),
            {"id": action_id, "ws": workspace_id, "run": run_id, "task": task_id, "at": action_type, "key": key,
             "target": target, "destr": is_destructive, "appr": approval_id, "req": canonical_json(request),
             "rh": request_hash(request)},
        )
    ).scalar_one_or_none()
    if inserted is None:
        existing = (
            await conn.execute(text("SELECT id FROM external_actions WHERE idempotency_key = :k"), {"k": key})
        ).scalar_one()
        found = await get(conn, existing)
        assert found is not None
        return found
    await _emit(conn, action_id, workspace_id, run_id, None, "PLANNED", action_type)
    found = await get(conn, action_id)
    assert found is not None
    return found


async def begin(conn: AsyncConnection, action_id: uuid.UUID, fencing_token: int) -> ExternalAction:
    """PLANNED|FAILED → EXECUTING; EXECUTING с более новым токеном — перехват после краха воркера."""
    current = await get(conn, action_id, lock=True)
    if current is None:
        raise LookupError(str(action_id))
    if current.status in ("CONFIRMED", "COMPENSATED"):
        raise AlreadyConfirmedError(str(action_id))
    if current.status == "EXECUTING":
        if current.fencing_token is not None and fencing_token <= current.fencing_token:
            if fencing_token == current.fencing_token:
                return current
            raise StaleFencingTokenError(str(action_id))
        await conn.execute(
            text("UPDATE external_actions SET fencing_token = :tok, version = version + 1 WHERE id = :id"),
            {"tok": fencing_token, "id": action_id},
        )
    else:
        fsm.require_transition("external_action", current.status, "EXECUTING")
        try:
            await conn.execute(
                text(
                    "UPDATE external_actions SET status = 'EXECUTING', fencing_token = :tok,"
                    " executing_at = COALESCE(executing_at, now()), failed_at = NULL, attempt_count = attempt_count + 1,"
                    " version = version + 1 WHERE id = :id AND version = :v"
                ),
                {"tok": fencing_token, "id": action_id, "v": current.version},
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23514":
                raise ApprovalRequiredError(str(action_id)) from exc
            raise
        await _emit(conn, action_id, current.workspace_id, current.run_id, current.status, "EXECUTING",
                    current.action_type)
    updated = await get(conn, action_id)
    assert updated is not None
    return updated


async def confirm(
    conn: AsyncConnection, action_id: uuid.UUID, *, fencing_token: int, external_ref: str | None,
    result: dict[str, Any] | None = None,
) -> ExternalAction:
    current = await get(conn, action_id, lock=True)
    if current is None:
        raise LookupError(str(action_id))
    if current.status == "CONFIRMED":
        return current  # идемпотентно
    if current.status != "EXECUTING" or current.fencing_token != fencing_token:
        raise StaleFencingTokenError(str(action_id))
    await conn.execute(
        text(
            "UPDATE external_actions SET status = 'CONFIRMED', confirmed_at = now(), external_ref = :ref,"
            " result = CAST(:res AS jsonb), last_error = NULL, version = version + 1 WHERE id = :id"
        ),
        {"ref": external_ref[:1024] if external_ref else None,
         "res": json.dumps(redact_value(result or {}), ensure_ascii=False, default=str), "id": action_id},
    )
    await _emit(conn, action_id, current.workspace_id, current.run_id, "EXECUTING", "CONFIRMED", current.action_type,
                external_ref=external_ref)
    updated = await get(conn, action_id)
    assert updated is not None
    return updated


async def fail(
    conn: AsyncConnection, action_id: uuid.UUID, *, error: str, fencing_token: int | None = None
) -> ExternalAction:
    """PLANNED|EXECUTING → FAILED (EXECUTING — только владелец актуального токена)."""
    current = await get(conn, action_id, lock=True)
    if current is None:
        raise LookupError(str(action_id))
    if current.status == "FAILED":
        return current
    if current.status == "EXECUTING" and fencing_token is not None and current.fencing_token != fencing_token:
        raise StaleFencingTokenError(str(action_id))
    fsm.require_transition("external_action", current.status, "FAILED")
    await conn.execute(
        text(
            "UPDATE external_actions SET status = 'FAILED', failed_at = now(), last_error = :err,"
            " version = version + 1 WHERE id = :id"
        ),
        {"err": redact_text(error)[:4000], "id": action_id},
    )
    await _emit(conn, action_id, current.workspace_id, current.run_id, current.status, "FAILED", current.action_type)
    updated = await get(conn, action_id)
    assert updated is not None
    return updated


Do = Callable[[], Awaitable[Outcome]]
Probe = Callable[[], Awaitable[Outcome | None]]


async def execute_once(
    engine: AsyncEngine, workspace_id: uuid.UUID, action_id: uuid.UUID, fencing_token: int, *, do: Do, probe: Probe,
) -> ExternalAction:
    """Выполнить действие ровно один раз (с точки зрения внешней системы).

    * CONFIRMED/COMPENSATED → вернуть как есть, `do` не вызывается (AC-09);
    * EXECUTING (крах во время вызова) или FAILED после начала вызова → сначала `probe()`;
      применено ⇒ CONFIRMED без повтора; не применено ⇒ `do()`;
    * PLANNED → EXECUTING → `do()` → CONFIRMED; исключение `do` ⇒ FAILED и проброс исключения.
    """
    ctx = DbContext(workspace_id=workspace_id, actor_type="system")
    async with transaction(engine, ctx) as conn:
        current = await get(conn, action_id, lock=True)
        if current is None:
            raise LookupError(str(action_id))
        if current.status in ("CONFIRMED", "COMPENSATED"):
            return current
        maybe_applied = current.status == "EXECUTING" or (current.status == "FAILED" and current.executing_at is not None)
    async with transaction(engine, ctx) as conn:
        await begin(conn, action_id, fencing_token)
    if maybe_applied:
        observed = await probe()
        if observed is not None:
            async with transaction(engine, ctx) as conn:
                return await confirm(conn, action_id, fencing_token=fencing_token, external_ref=observed.external_ref,
                                     result={**observed.result, "reconciled": True})
    try:
        outcome = await do()
    except Exception as exc:
        async with transaction(engine, ctx) as conn:
            await fail(conn, action_id, error=f"{type(exc).__name__}: {exc}", fencing_token=fencing_token)
        raise
    async with transaction(engine, ctx) as conn:
        return await confirm(conn, action_id, fencing_token=fencing_token, external_ref=outcome.external_ref,
                             result=outcome.result)


async def _emit(
    conn: AsyncConnection, action_id: uuid.UUID, workspace_id: uuid.UUID, run_id: uuid.UUID | None,
    previous: str | None, status: str, action_type: str, *, external_ref: str | None = None,
) -> None:
    await outbox.emit(
        conn, event_type="external_action.state_changed", aggregate_type="external_action", aggregate_id=action_id,
        workspace_id=workspace_id, run_id=run_id,
        payload={"action_id": str(action_id), "action_type": action_type, "from": previous, "to": status,
                 "external_ref": external_ref},
    )
