"""Реестр хостов и установок CLI: запись результатов probe/check_auth и health (ТЗ §9, P2-12).

Пишет воркер (`aicd_worker`); читает API (`GET /providers`). В БД — только статусы и capabilities,
никаких учётных данных, путей к credential store или токенов (ТЗ §7.1).
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_cli_adapters.models import ProbeResult
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


async def list_providers(conn: AsyncConnection) -> list[dict[str, Any]]:
    providers = (
        (await conn.execute(text("SELECT id, code, display_name, enabled FROM cli_providers ORDER BY code")))
        .mappings()
        .all()
    )
    installs = (
        (
            await conn.execute(
                text(
                    "SELECT i.id, i.provider_id, h.hostname, i.detected_version, i.compatibility, i.auth_status,"
                    " i.capabilities, i.checked_at, ph.availability, ph.error_category"
                    " FROM cli_installations i JOIN worker_hosts h ON h.id = i.host_id"
                    " LEFT JOIN LATERAL (SELECT availability, error_category FROM provider_health"
                    "   WHERE installation_id = i.id ORDER BY observed_at DESC LIMIT 1) ph ON true"
                    " WHERE i.enabled ORDER BY h.hostname, i.id"
                )
            )
        )
        .mappings()
        .all()
    )
    result = []
    for p in providers:
        result.append(
            {
                "provider_id": p["id"],
                "code": p["code"],
                "display_name": p["display_name"],
                "enabled_globally": p["enabled"],
                "installations": [
                    {
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
                    for i in installs
                    if i["provider_id"] == p["id"]
                ],
                "workspace_settings": dict(DEFAULT_WORKSPACE_SETTINGS),
            }
        )
    return result


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    loaded: dict[str, Any] = json.loads(value)
    return loaded
