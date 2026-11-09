"""Профили 12 ролей агентов (UI redesign B7; UI_REDESIGN_PLAN §3.4, G-13) — только чтение.

`GET /agent-profiles` (tenant — `X-Workspace-Id`, право `run:read`): данные платформы, по которым orchestration
исполняет роли (`aicd_orchestration.agents.profiles`): права исполнения (`write_scope`, `network`, allowlist путей),
классы инструментов, допустимые для отправки типы ACP, уровни сложности по умолчанию, требование независимой проверки,
версия и sha256 шаблона инструкций. Рядом — строка справочника `agent_profiles` (миграция 0006; `null`, если
справочник ещё не синхронизирован `sync_profiles`) и провайдеры, которым роль разрешена настройками workspace
(`PATCH /providers/{id}/settings` → `allowed_roles`). Фиксированного «провайдера по умолчанию» у роли нет: выбор
адаптивный (ROUTING_POLICY §4), поэтому отдаётся множество допустимых провайдеров, а не выдуманное предпочтение.
Текст шаблона инструкций не отдаётся.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from starlette.responses import Response

from aicd_api.deps import app_state, authorize, current_principal, parse_workspace_header, workspace_access
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_cli_adapters import registry
from aicd_identity import api as identity
from aicd_orchestration.agents.profiles import ACP_TYPES, PROFILES, ROLES, Permissions, RoleProfile, load_template
from aicd_platform.db import transaction

router = APIRouter(tags=["agents"])


def _level(value: int | None) -> str | None:
    return f"L{value}" if value is not None else None


def _permissions(p: Permissions) -> dict[str, Any]:
    return p.as_json()


def profile_view(p: RoleProfile, catalog: dict[str, Any] | None, eligible: list[str]) -> dict[str, Any]:
    template = load_template(p.role, p.template_version)
    return {
        "role": p.role,
        "template_version": p.template_version,
        "template_ref": p.template_ref,
        "template_sha256": template.sha256,
        "permissions": _permissions(p.permissions),
        "prototype_permissions": _permissions(p.prototype_permissions) if p.prototype_permissions else None,
        "allowed_tools": list(p.allowed_tools),
        "required_capabilities": list(p.required_capabilities),
        "acp_send": [t for t in ACP_TYPES if t in p.acp_send],
        "complexity_band": {"min": _level(p.complexity_band[0]), "max": _level(p.complexity_band[1])},
        "independent_check_from": _level(p.independent_check_from),
        "must_differ_from_author": p.must_differ_from_author,
        "can_send_fix_request": p.can_send_fix_request,
        "eligible_providers": eligible,
        "catalog": catalog,
    }


@router.get("/agent-profiles", **op("listAgentProfiles", "run:read"))
async def list_agent_profiles(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "run:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT role, instruction_template_version, enabled, updated_at FROM agent_profiles")
                )
            )
            .mappings()
            .all()
        )
        providers = (await conn.execute(text("SELECT code, enabled FROM cli_providers ORDER BY code"))).all()
        settings = await registry.workspace_settings_by_code(conn)
    catalog = {
        str(r["role"]): {
            "template_version": r["instruction_template_version"],
            "enabled": r["enabled"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    }
    items = []
    for role in ROLES:
        eligible = []
        for code, globally_enabled in providers:
            ws = settings.get(str(code), registry.DEFAULT_WORKSPACE_SETTINGS)
            if globally_enabled and ws.get("enabled", True) and role in ws.get("allowed_roles", ()):
                eligible.append(str(code))
        items.append(profile_view(PROFILES[role], catalog.get(role), eligible))
    return json_response({"items": items})
