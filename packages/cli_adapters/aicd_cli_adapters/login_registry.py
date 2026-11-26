"""Сессии интерактивного входа CLI в БД: создание из API, ведение воркером exec (миграция 0022).

Пишет и читает `cli_login_sessions`. Учётных данных в таблице нет (ТЗ §1, §7.1): только состояние, публичная
часть device-code потока (`verification_url`, `user_code`) и хвост вычищенного вывода CLI — см. `login.scrub()`.

Поток:

1. API (`POST /providers/{id}/login`, роль owner/admin + step-up) создаёт строку `PENDING`, emit outbox
   `provider.login_requested` (очередь exec) и audit в одной транзакции;
2. worker-exec берёт сообщение, запускает `login.run_login()`, обновляет строку: `CHALLENGE` с найденной
   ссылкой и кодом, затем терминальное состояние, и пишет audit `provider.login_completed`;
3. UI опрашивает `GET /providers/{id}/logins/{sid}` (как `status_url` у probe) и показывает ссылку и код;
4. `POST /providers/{id}/logins/{sid}/cancel` переводит активную сессию в `CANCELLED` — воркер видит это при
   очередном опросе и снимает процесс CLI.

Одна активная сессия на провайдера в workspace (`uq_cli_login_sessions_active`): одна подписка — один хост
исполнения (OQ-01/OQ-24).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_cli_adapters.login import (
    LOGIN_TIMEOUT,
    MAX_LINES,
    DeviceChallenge,
    LoginErrorCategory,
    LoginState,
    prompts_for_code,
    scrub,
)
from aicd_platform import audit, outbox
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7

LOGIN_REQUESTED: Final = "provider.login_requested"
LOGIN_COMPLETED: Final = "provider.login_completed"
LOGIN_CANCELLED: Final = "provider.login_cancelled"
#: Оператор передал CLI код авторизации из браузера. В audit попадает только факт и сессия:
#: самого кода нет ни здесь, ни в БД, ни в логах (`aicd_platform.login_input`).
LOGIN_CODE_SUBMITTED: Final = "provider.login_code_submitted"
#: Активные состояния — совпадают с частичным индексом `uq_cli_login_sessions_active`.
ACTIVE_STATES: Final = ("PENDING", "CHALLENGE")
#: Хвост вывода CLI в строке (ограничение `ck` таблицы — 8192 байта).
MAX_OUTPUT_TAIL: Final = 8192
#: Ключ идентификатора сессии в outbox-payload и audit-metadata. Не `login_session_id`: `redaction`
#: затирает любой ключ, содержащий `session` (он рассчитан на идентификаторы сессий пользователей),
#: и потребитель получил бы `[REDACTED]`. В теле HTTP-ответа поле называется `login_session_id`.
PAYLOAD_ID_KEY: Final = "login_id"


async def request_login(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    provider: str,
    actor_id: uuid.UUID,
    ttl: timedelta = LOGIN_TIMEOUT,
) -> tuple[uuid.UUID, datetime]:
    """Создать сессию входа (API, RLS workspace). Активная сессия уже есть ⇒ `conflict`."""
    session_id = uuid7()
    # Гонка двух запросов ловится частичным уникальным индексом; SAVEPOINT не даёт ей оборвать транзакцию.
    savepoint = await conn.begin_nested()
    try:
        created_at = (
            await conn.execute(
                text(
                    "INSERT INTO cli_login_sessions (id, workspace_id, provider_id, requested_by, expires_at)"
                    " VALUES (:id, :ws, :p, :actor, now() + CAST(:ttl AS interval)) RETURNING created_at"
                ),
                {
                    "id": session_id,
                    "ws": workspace_id,
                    "p": provider_id,
                    "actor": actor_id,
                    "ttl": f"{int(ttl.total_seconds())} seconds",
                },
            )
        ).scalar_one()
    except IntegrityError as exc:
        await savepoint.rollback()
        raise ProblemError(
            "conflict", detail="для этого провайдера уже идёт вход: дождитесь его завершения или отмените"
        ) from exc
    await savepoint.commit()
    await outbox.emit(
        conn,
        event_type=LOGIN_REQUESTED,
        aggregate_type="cli_provider",
        aggregate_id=provider_id,
        workspace_id=workspace_id,
        payload={PAYLOAD_ID_KEY: str(session_id), "provider_id": str(provider_id), "provider": provider},
    )
    await audit.record(
        conn,
        action=LOGIN_REQUESTED,
        resource_type="cli_provider",
        resource_id=provider_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={PAYLOAD_ID_KEY: str(session_id), "provider": provider},
    )
    return session_id, created_at


async def claim_session(
    conn: AsyncConnection, *, session_id: uuid.UUID, provider_id: uuid.UUID, host_id: uuid.UUID
) -> bool:
    """Воркер exec берёт сессию себе (`PENDING` → остаётся `PENDING`, но с host_id). Не `PENDING` ⇒ False."""
    row = (
        await conn.execute(
            text(
                "UPDATE cli_login_sessions SET host_id = :host, updated_at = now()"
                " WHERE id = :id AND provider_id = :p AND state = 'PENDING' AND expires_at > now() RETURNING id"
            ),
            {"id": session_id, "p": provider_id, "host": host_id},
        )
    ).scalar_one_or_none()
    return row is not None


async def record_challenge(
    conn: AsyncConnection, *, session_id: uuid.UUID, challenge: DeviceChallenge, output_tail: str
) -> None:
    """Записать ссылку и код устройства (воркер). Только из активного состояния."""
    await conn.execute(
        text(
            "UPDATE cli_login_sessions SET state = 'CHALLENGE', verification_url = :url, user_code = :code,"
            " output_tail = :tail, updated_at = now()"
            " WHERE id = :id AND state IN ('PENDING','CHALLENGE')"
        ),
        {
            "id": session_id,
            "url": challenge.verification_url[:2048],
            "code": challenge.user_code[:64] if challenge.user_code else None,
            "tail": _tail(output_tail),
        },
    )


async def record_output(conn: AsyncConnection, *, session_id: uuid.UUID, output_tail: str) -> None:
    """Обновить хвост вывода активной сессии (воркер)."""
    await conn.execute(
        text(
            "UPDATE cli_login_sessions SET output_tail = :tail, updated_at = now()"
            " WHERE id = :id AND state IN ('PENDING','CHALLENGE')"
        ),
        {"id": session_id, "tail": _tail(output_tail)},
    )


async def finish_session(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    session_id: uuid.UUID,
    state: LoginState,
    error_category: LoginErrorCategory | None,
    output_tail: str,
) -> None:
    """Терминальное состояние сессии плюс audit `provider.login_completed` (воркер).

    Если оператор уже отменил сессию, её состояние не перезаписывается: `CANCELLED` — тоже итог.
    """
    await conn.execute(
        text(
            "UPDATE cli_login_sessions SET state = :state, error_category = :err, output_tail = :tail,"
            " updated_at = now() WHERE id = :id AND state IN ('PENDING','CHALLENGE')"
        ),
        {"id": session_id, "state": state, "err": error_category, "tail": _tail(output_tail)},
    )
    await audit.record(
        conn,
        action=LOGIN_COMPLETED,
        resource_type="cli_provider",
        resource_id=provider_id,
        actor_type="system",
        actor_id=None,
        workspace_id=workspace_id,
        outcome="success" if state == "AUTHENTICATED" else "failure",
        metadata={
            PAYLOAD_ID_KEY: str(session_id),
            "state": state,
            "error_category": error_category,
        },
    )


async def cancel_session(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> bool:
    """Отмена оператором: активная сессия → `CANCELLED`. Уже завершённая ⇒ False (идемпотентно)."""
    row = (
        await conn.execute(
            text(
                "UPDATE cli_login_sessions SET state = 'CANCELLED', error_category = 'CANCELLED',"
                " updated_at = now()"
                " WHERE id = :id AND provider_id = :p AND state IN ('PENDING','CHALLENGE')"
                " RETURNING id"
            ),
            {"id": session_id, "p": provider_id},
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await audit.record(
        conn,
        action=LOGIN_CANCELLED,
        resource_type="cli_provider",
        resource_id=provider_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={PAYLOAD_ID_KEY: str(session_id)},
    )
    return True


async def record_code_submission(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Audit факта передачи кода процессу входа. Значение кода сюда не передаётся и нигде не хранится."""
    await audit.record(
        conn,
        action=LOGIN_CODE_SUBMITTED,
        resource_type="cli_provider",
        resource_id=provider_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={PAYLOAD_ID_KEY: str(session_id)},
    )


async def expire_due(conn: AsyncConnection) -> int:
    """Просроченные активные сессии → `EXPIRED` (воркер при опросе и API при чтении статуса)."""
    result = await conn.execute(
        text(
            "UPDATE cli_login_sessions SET state = 'EXPIRED', error_category = 'TIMEOUT', updated_at = now()"
            " WHERE state IN ('PENDING','CHALLENGE') AND expires_at <= now()"
        )
    )
    return result.rowcount or 0


async def session_state(conn: AsyncConnection, *, session_id: uuid.UUID) -> str | None:
    value = (
        await conn.execute(text("SELECT state FROM cli_login_sessions WHERE id = :id"), {"id": session_id})
    ).scalar_one_or_none()
    return str(value) if value is not None else None


_SELECT = (
    "SELECT s.id, s.provider_id, s.state, s.verification_url, s.user_code, s.output_tail, s.error_category,"
    " s.created_at, s.updated_at, s.expires_at, h.hostname"
    " FROM cli_login_sessions s LEFT JOIN worker_hosts h ON h.id = s.host_id"
)


async def login_status(
    conn: AsyncConnection, *, provider_id: uuid.UUID, session_id: uuid.UUID
) -> dict[str, Any] | None:
    """`ProviderLoginStatus` сессии workspace текущего RLS-контекста; чужая/несуществующая — None."""
    await expire_due(conn)
    row = (
        (
            await conn.execute(
                text(_SELECT + " WHERE s.id = :id AND s.provider_id = :p"), {"id": session_id, "p": provider_id}
            )
        )
        .mappings()
        .first()
    )
    return _view(row) if row is not None else None


async def active_sessions(conn: AsyncConnection) -> dict[uuid.UUID, dict[str, Any]]:
    """Активные сессии входа workspace по provider_id — для `GET /providers`."""
    await expire_due(conn)
    rows = (await conn.execute(text(_SELECT + " WHERE s.state IN ('PENDING','CHALLENGE')"))).mappings().all()
    return {uuid.UUID(str(r["provider_id"])): _view(r) for r in rows}


def _view(row: Any) -> dict[str, Any]:
    tail = row["output_tail"] or ""
    return {
        "login_session_id": row["id"],
        "provider_id": row["provider_id"],
        "state": row["state"],
        "verification_url": row["verification_url"],
        "user_code": row["user_code"],
        "output_tail": tail,
        # CLI попросил ввести код авторизации из браузера (Claude Code) — UI показывает поле ввода.
        "awaiting_code": row["state"] in ACTIVE_STATES and prompts_for_code(tail),
        "error_category": row["error_category"],
        "host": row["hostname"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }


def _tail(output: str) -> str:
    """Последние строки вывода, вычищенные и обрезанные под ограничение столбца."""
    lines = [scrub(line) for line in output.splitlines() if line.strip()][-MAX_LINES:]
    tail = "\n".join(lines)
    return tail[-MAX_OUTPUT_TAIL:]
