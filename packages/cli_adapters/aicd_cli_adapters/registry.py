"""Реестр хостов и установок CLI: запись результатов probe/check_auth и health (ТЗ §9, P2-12).

Пишет воркер (`aicd_worker`); читает API (`GET /providers`). В БД — только статусы и capabilities,
никаких учётных данных, путей к credential store или токенов (ТЗ §7.1).
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_cli_adapters.models import ProbeResult
from aicd_platform import audit, metrics, outbox
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_text

ALL_ROLES = [
    "requirements",
    "architect",
    "planner",
    "developer",
    "reviewer",
    "security",
    "qa",
    "database",
    "performance",
    "devops",
    "maintenance",
    "documentation",
]
# Значения до появления policies (P3): одна сессия на установку до подтверждения условий подписок (OQ-01).
DEFAULT_WORKSPACE_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "max_parallel_sessions": 1,
    "default_timeout_seconds": 1800,
    "allowed_roles": ALL_ROLES,
    "settings_version": 0,
}


async def register_host(conn: AsyncConnection, kind: str = "exec", hostname: str | None = None) -> uuid.UUID:
    row = (
        await conn.execute(
            text(
                "INSERT INTO worker_hosts (id, hostname, kind) VALUES (:id, :h, :k)"
                " ON CONFLICT ON CONSTRAINT uq_worker_hosts_hostname_kind"
                " DO UPDATE SET last_heartbeat_at = now(), status = 'ONLINE' RETURNING id"
            ),
            {"id": uuid7(), "h": (hostname or socket.gethostname())[:253], "k": kind},
        )
    ).scalar_one()
    return uuid.UUID(str(row))


def availability_of(probe: ProbeResult) -> tuple[str, str | None]:
    if not probe.installed:
        return "UNAVAILABLE", "CLI_NOT_INSTALLED"
    if probe.compatibility == "UNSUPPORTED":
        return "UNAVAILABLE", "UNSUPPORTED_VERSION"
    if probe.auth_status != "AUTHENTICATED":
        return "UNAVAILABLE", "AUTH_REQUIRED"
    return ("DEGRADED", None) if probe.compatibility == "UNTESTED" else ("AVAILABLE", None)


async def record_probe(
    conn: AsyncConnection, *, host_id: uuid.UUID, probe: ProbeResult, duration_ms: int | None = None
) -> uuid.UUID:
    """Upsert установки по (provider, host, binary) и наблюдение provider_health."""
    if probe.binary_path is None:
        raise ValueError("probe без binary_path не может быть записан как установка")
    installation_id = (
        await conn.execute(
            text(
                "INSERT INTO cli_installations (id, provider_id, host_id, binary_path, detected_version, compatibility,"
                " auth_status, capabilities, checked_at)"
                " SELECT :id, p.id, :host, :bin, :ver, :compat, :auth, CAST(:caps AS jsonb), now()"
                " FROM cli_providers p WHERE p.code = :prov"
                " ON CONFLICT ON CONSTRAINT uq_cli_installations_host_binary DO UPDATE SET"
                " detected_version = EXCLUDED.detected_version, compatibility = EXCLUDED.compatibility,"
                " auth_status = EXCLUDED.auth_status, capabilities = EXCLUDED.capabilities, checked_at = now()"
                " RETURNING id"
            ),
            {
                "id": probe.installation_id,
                "host": host_id,
                "bin": probe.binary_path,
                "ver": probe.detected_version,
                "compat": probe.compatibility.lower(),
                "auth": probe.auth_status or "UNKNOWN",
                "caps": probe.capabilities.model_dump_json(),
                "prov": probe.provider,
            },
        )
    ).scalar_one()
    availability, error = availability_of(probe)
    metrics.PROVIDER_PROBES.labels(metrics.label(probe.provider, metrics.PROVIDERS), availability).inc()
    detail = "; ".join(e.message for e in probe.errors or [])
    await conn.execute(
        text(
            "INSERT INTO provider_health (id, installation_id, auth_status, availability, error_category, detail,"
            " probe_duration_ms) VALUES (:id, :inst, :auth, :avail, :err, :detail, :dur)"
        ),
        {
            "id": uuid7(),
            "inst": installation_id,
            "auth": probe.auth_status or "UNKNOWN",
            "avail": availability,
            "err": error,
            "detail": redact_text(detail)[:2000] or None,
            "dur": duration_ms,
        },
    )
    return uuid.UUID(str(installation_id))


async def sample_availability(conn: AsyncConnection) -> dict[str, bool]:
    """Есть ли у провайдера доступная установка (последнее наблюдение AVAILABLE/DEGRADED) → gauge P8-06."""
    rows = (
        await conn.execute(
            text(
                "SELECT p.code, COALESCE(bool_or(ph.availability IN ('AVAILABLE','DEGRADED')), false)"
                " FROM cli_providers p"
                " LEFT JOIN cli_installations i ON i.provider_id = p.id AND i.enabled"
                " LEFT JOIN LATERAL (SELECT availability FROM provider_health WHERE installation_id = i.id"
                "   ORDER BY observed_at DESC LIMIT 1) ph ON true GROUP BY p.code"
            )
        )
    ).all()
    result = {str(code): bool(ok) for code, ok in rows}
    for code, ok in result.items():
        metrics.PROVIDER_AVAILABLE.labels(metrics.label(code, metrics.PROVIDERS)).set(1 if ok else 0)
    return result


async def list_providers(conn: AsyncConnection) -> list[dict[str, Any]]:
    providers = (
        (await conn.execute(text("SELECT id, code, display_name, enabled FROM cli_providers ORDER BY code")))
        .mappings()
        .all()
    )
    installs = (
        (await conn.execute(text(_INSTALLATION_SELECT + " WHERE i.enabled ORDER BY h.hostname, i.id"))).mappings().all()
    )
    settings = await workspace_settings_by_code(conn)
    result = []
    for p in providers:
        result.append(
            {
                "provider_id": p["id"],
                "code": p["code"],
                "display_name": p["display_name"],
                "enabled_globally": p["enabled"],
                "installations": [_installation_view(i) for i in installs if i["provider_id"] == p["id"]],
                "workspace_settings": settings.get(p["code"], dict(DEFAULT_WORKSPACE_SETTINGS)),
            }
        )
    return result


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    loaded: dict[str, Any] = json.loads(value)
    return loaded


# --- Запросы проверки провайдера (POST /providers/{id}/probe, OQ-45 (c)) ------------------------------
#
# Без отдельной таблицы: запрос — outbox-событие `provider.probe_requested` (очередь exec) плюс audit
# `provider.probe_requested` с `metadata.probe_request_id`; результат worker-exec пишет audit
# `provider.probe_completed` (тот же probe_request_id, installation_id, error_category). Статус запроса
# выводится из этих записей workspace (RLS), установка — из cli_installations/provider_health.

PROBE_REQUESTED = "provider.probe_requested"
PROBE_COMPLETED = "provider.probe_completed"
PROBE_TIMEOUT = timedelta(minutes=15)


async def provider_code(conn: AsyncConnection, provider_id: uuid.UUID) -> str | None:
    value = (
        await conn.execute(text("SELECT code FROM cli_providers WHERE id = :id"), {"id": provider_id})
    ).scalar_one_or_none()
    return str(value) if value is not None else None


async def request_probe(
    conn: AsyncConnection, *, workspace_id: uuid.UUID, provider_id: uuid.UUID, provider: str, actor_id: uuid.UUID
) -> tuple[uuid.UUID, datetime]:
    """Запрос проверки (API, RLS workspace): outbox для worker-exec и audit в одной транзакции."""
    probe_request_id = uuid7()
    await outbox.emit(
        conn,
        event_type=PROBE_REQUESTED,
        aggregate_type="cli_provider",
        aggregate_id=provider_id,
        workspace_id=workspace_id,
        payload={"probe_request_id": str(probe_request_id), "provider_id": str(provider_id), "provider": provider},
    )
    await audit.record(
        conn,
        action=PROBE_REQUESTED,
        resource_type="cli_provider",
        resource_id=provider_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"probe_request_id": str(probe_request_id), "provider": provider},
    )
    accepted_at = (await conn.execute(text("SELECT now()"))).scalar_one()
    return probe_request_id, accepted_at


async def record_probe_outcome(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    probe_request_id: uuid.UUID,
    installation_id: uuid.UUID | None,
    error_category: str | None,
) -> None:
    """Итог проверки (worker-exec, RLS workspace запроса): installation_id — записанная установка или None."""
    await audit.record(
        conn,
        action=PROBE_COMPLETED,
        resource_type="cli_provider",
        resource_id=provider_id,
        actor_type="system",
        actor_id=None,
        workspace_id=workspace_id,
        outcome="success" if installation_id is not None else "failure",
        metadata={
            "probe_request_id": str(probe_request_id),
            "installation_id": str(installation_id) if installation_id else None,
            "error_category": error_category,
        },
    )


_INSTALLATION_SELECT = (
    "SELECT i.id, i.provider_id, h.hostname, i.detected_version, i.compatibility, i.auth_status,"
    " i.capabilities, i.checked_at, ph.availability, ph.error_category"
    " FROM cli_installations i JOIN worker_hosts h ON h.id = i.host_id"
    " LEFT JOIN LATERAL (SELECT availability, error_category FROM provider_health"
    "   WHERE installation_id = i.id ORDER BY observed_at DESC LIMIT 1) ph ON true"
)


def _installation_view(i: Any) -> dict[str, Any]:
    return {
        "installation_id": i["id"],
        "host": i["hostname"],
        "detected_version": i["detected_version"],
        "compatibility": str(i["compatibility"]).upper(),
        "auth_status": i["auth_status"],
        "availability": i["availability"] or "UNAVAILABLE",
        "capabilities": _as_dict(i["capabilities"]),
        "last_error_category": i["error_category"],
        "checked_at": i["checked_at"],
    }


_CLI_ERRORS = frozenset(
    {
        "CLI_NOT_INSTALLED",
        "AUTH_REQUIRED",
        "RATE_LIMITED",
        "TIMEOUT",
        "SANDBOX_DENIED",
        "PROCESS_FAILED",
        "OUTPUT_PARSE_ERROR",
        "UNSUPPORTED_VERSION",
        "CANCELLED",
    }
)


async def probe_status(
    conn: AsyncConnection, *, provider_id: uuid.UUID, probe_request_id: uuid.UUID, now: datetime
) -> dict[str, Any] | None:
    """`ProviderProbeStatus` запроса workspace текущего RLS-контекста; чужой/несуществующий — None."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT action, created_at, outcome, metadata FROM audit_events"
                    " WHERE resource_type = 'cli_provider' AND resource_id = :p AND action = ANY(:actions)"
                    " AND metadata ->> 'probe_request_id' = :rid ORDER BY created_at, id"
                ),
                {"p": provider_id, "actions": [PROBE_REQUESTED, PROBE_COMPLETED], "rid": str(probe_request_id)},
            )
        )
        .mappings()
        .all()
    )
    requested = next((r for r in rows if r["action"] == PROBE_REQUESTED), None)
    if requested is None:
        return None
    done = next((r for r in reversed(rows) if r["action"] == PROBE_COMPLETED), None)
    status = "PENDING"
    completed_at = None
    installation = None
    error_category = None
    if done is not None:
        meta = _as_dict(done["metadata"] or {})
        completed_at = done["created_at"]
        raw_error = meta.get("error_category")
        error_category = raw_error if raw_error in _CLI_ERRORS else None
        inst_id = meta.get("installation_id")
        if inst_id:
            row = (
                (await conn.execute(text(_INSTALLATION_SELECT + " WHERE i.id = :id"), {"id": uuid.UUID(str(inst_id))}))
                .mappings()
                .first()
            )
            installation = _installation_view(row) if row is not None else None
        status = "COMPLETED" if installation is not None else "FAILED"
        if status == "FAILED" and error_category is None:
            error_category = "PROCESS_FAILED"
    elif now - requested["created_at"] > PROBE_TIMEOUT:
        status = "EXPIRED"
    return {
        "probe_request_id": probe_request_id,
        "provider_id": provider_id,
        "status": status,
        "requested_at": requested["created_at"],
        "completed_at": completed_at,
        "installation": installation,
        "error_category": error_category,
    }


# --- Безопасные настройки провайдера в workspace (PATCH /providers/{id}/settings, API_DESIGN §15) --------
#
# Хранение — `policies(policy_type='provider_settings', name=<код провайдера>, project_id NULL)` workspace
# текущего RLS-контекста. Строки неизменяемы (кроме `active`): изменение = деактивация активной версии и новая
# строка `version + 1` (DATABASE_DESIGN §14.1). `settings_version` = `policies.version` активной строки;
# 0 — строки нет, действуют значения по умолчанию (`DEFAULT_WORKSPACE_SETTINGS`).

PROVIDER_SETTINGS_POLICY = "provider_settings"
SETTINGS_FIELDS = ("enabled", "max_parallel_sessions", "default_timeout_seconds", "allowed_roles")


def _settings_view(definition: dict[str, Any] | None, version: int) -> dict[str, Any]:
    view = {k: v for k, v in DEFAULT_WORKSPACE_SETTINGS.items() if k in SETTINGS_FIELDS}
    for key in SETTINGS_FIELDS:
        if definition and key in definition:
            view[key] = definition[key]
    view["allowed_roles"] = list(view["allowed_roles"])
    view["settings_version"] = version
    return view


def default_workspace_settings() -> dict[str, Any]:
    """Настройки провайдера в workspace без строки политики (settings_version 0)."""
    return _settings_view(None, 0)


TASK_TIMEOUT_COLUMN_DEFAULT = 1800  # tasks.timeout_seconds DEFAULT (миграция 0003)


def effective_timeout(task_timeout: int, workspace_default: int) -> int:
    """Тайм-аут исполнения CLI (API_DESIGN §15, ROUTING_POLICY §4.1a): явный тайм-аут задачи важнее
    `default_timeout_seconds` workspace; значение столбца по умолчанию (1800, план его не задавал) считается
    «не задано». Результат ограничен 30…14400 с (ExecutionRequest.limits)."""
    chosen = workspace_default if task_timeout == TASK_TIMEOUT_COLUMN_DEFAULT else task_timeout
    return max(30, min(int(chosen), 14400))


async def workspace_settings_by_code(conn: AsyncConnection) -> dict[str, dict[str, Any]]:
    """Активные настройки провайдеров workspace текущего RLS-контекста (без контекста — пусто)."""
    rows = (
        await conn.execute(
            text(
                "SELECT name, version, definition FROM policies WHERE policy_type = :t AND active"
                " AND project_id IS NULL AND workspace_id = app_current_workspace()"
            ),
            {"t": PROVIDER_SETTINGS_POLICY},
        )
    ).all()
    return {r[0]: _settings_view(_as_dict(r[2]), int(r[1])) for r in rows}


async def update_workspace_settings(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    provider_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_version: int,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Merge-patch настроек провайдера с optimistic locking (If-Match = `settings_version`) и аудитом.

    Неизвестный провайдер — 404; несовпадение версии (в т.ч. гонка двух вставок — уникальный индекс активной
    версии) — 412 с актуальным ETag. Изменение без отличий — no-op без новой версии.
    """
    code = await provider_code(conn, provider_id)
    if code is None:
        raise ProblemError("not_found")
    row = (
        await conn.execute(
            text(
                "SELECT id, version, definition FROM policies WHERE policy_type = :t AND name = :n AND active"
                " AND project_id IS NULL AND workspace_id = :ws FOR UPDATE"
            ),
            {"t": PROVIDER_SETTINGS_POLICY, "n": code, "ws": workspace_id},
        )
    ).first()
    version = int(row[1]) if row else 0
    if version != expected_version:
        raise ProblemError("precondition_failed", headers={"ETag": f'"v{version}"'})
    current = _settings_view(_as_dict(row[2]) if row else None, version)
    # Список, а не словарь по имени поля: имена вроде `max_parallel_sessions` редактор секретов считает ключами
    # сессии и скрыл бы значения (redact_value по имени ключа).
    diff = [{"field": k, "from": current[k], "to": v} for k, v in changes.items() if current[k] != v]
    if not diff:
        return current
    definition = {k: current[k] for k in SETTINGS_FIELDS} | dict(changes)
    try:
        async with conn.begin_nested():
            if row is not None:
                await conn.execute(text("UPDATE policies SET active = false WHERE id = :id"), {"id": row[0]})
            await conn.execute(
                text(
                    "INSERT INTO policies (id, workspace_id, project_id, policy_type, name, version, definition,"
                    " active, created_by) VALUES (:id, :ws, NULL, :t, :n, :v, CAST(:d AS jsonb), true, :by)"
                ),
                {
                    "id": uuid7(),
                    "ws": workspace_id,
                    "t": PROVIDER_SETTINGS_POLICY,
                    "n": code,
                    "v": version + 1,
                    "d": json.dumps(definition),
                    "by": actor_id,
                },
            )
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != "23505":
            raise
        raise ProblemError("precondition_failed") from exc
    await audit.record(
        conn,
        action="provider.settings.update",
        resource_type="cli_provider",
        resource_id=provider_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"provider": code, "settings_version": version + 1, "changes": diff},
    )
    return _settings_view(definition, version + 1)
