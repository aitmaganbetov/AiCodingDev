"""CLI-провайдеры (ТЗ §8, API_DESIGN §15): health и capabilities без учётных данных, перепроверка установки.

* `GET /providers` — установки и health (без токенов, путей, env);
* `POST /providers/{id}/probe` — асинхронная перепроверка на worker-exec (202 `ProbeAccepted`); API не выполняет
  login и не принимает credentials; лимит 6/час на workspace+провайдера (API_DESIGN §10);
* `GET /providers/{id}/probes/{probe_request_id}` — статус запроса (`ProbeAccepted.status_url`, OQ-45 (c)).

* `PATCH /providers/{id}/settings` — безопасные настройки workspace (`policies`, policy_type=provider_settings),
  If-Match = `settings_version`, аудит `provider.settings.update`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response

from aicd_api.deps import (
    _remember_rate,
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    workspace_access,
)
from aicd_api.idempotency import CommandResult, run_idempotent
from aicd_api.responses import etag, json_response, require_if_match
from aicd_api.routers import op
from aicd_api.schemas import ProviderSettingsUpdateRequest
from aicd_cli_adapters import registry
from aicd_identity import api as identity
from aicd_platform.clock import utcnow
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError

router = APIRouter(tags=["providers"])

PROBES_PER_HOUR = 6


def status_url(provider_id: uuid.UUID, probe_request_id: uuid.UUID) -> str:
    return f"/api/v1/providers/{provider_id}/probes/{probe_request_id}"


@router.get("/providers", **op("listProviders", "provider:read"))
async def list_providers(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        items = await registry.list_providers(conn)
    return json_response({"items": items})


@router.post("/providers/{provider_id}/probe", status_code=202, **op("probeProvider", "provider:probe"))
async def probe_provider(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:probe")
    state = app_state(request)
    async with transaction(state.engine, access.db()) as conn:
        code = await registry.provider_code(conn, provider_id)
    if code is None:
        raise ProblemError("not_found")
    _remember_rate(
        request,
        await state.limiter.hit(
            f"probe:ws:{access.workspace_id}:provider:{provider_id}", PROBES_PER_HOUR, window_seconds=3600
        ),
    )

    async def command(conn: AsyncConnection) -> CommandResult:
        probe_request_id, accepted_at = await registry.request_probe(
            conn, workspace_id=access.workspace_id, provider_id=provider_id, provider=code, actor_id=principal.user_id
        )
        url = status_url(provider_id, probe_request_id)
        body = {
            "provider_id": provider_id,
            "probe_request_id": probe_request_id,
            "accepted_at": accepted_at,
            "status_url": url,
        }
        return CommandResult(202, body, {})

    return await run_idempotent(request, access.db(), {"provider_id": str(provider_id)}, command)


@router.get("/providers/{provider_id}/probes/{probe_request_id}", **op("getProviderProbe", "provider:read"))
async def get_provider_probe(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    probe_request_id: Annotated[uuid.UUID, Path()],
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        body = await registry.probe_status(
            conn, provider_id=provider_id, probe_request_id=probe_request_id, now=utcnow()
        )
    if body is None:
        raise ProblemError("not_found")
    return json_response(body)


@router.patch("/providers/{provider_id}/settings", **op("updateProviderSettings", "provider:settings"))
async def update_provider_settings(
    request: Request,
    provider_id: Annotated[uuid.UUID, Path()],
    body: ProviderSettingsUpdateRequest,
    principal: identity.Principal = Depends(current_principal),
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:settings")
    expected = require_if_match(request.headers.get("if-match"))
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise ProblemError("validation_error", errors=[FieldError("/", "min_properties", "нет изменяемых полей")])
    for key, value in changes.items():
        if value is None:
            raise ProblemError("validation_error", errors=[FieldError(f"/{key}", "null", "значение обязательно")])
    roles = changes.get("allowed_roles")
    if roles is not None and len(set(roles)) != len(roles):
        raise ProblemError(
            "validation_error", errors=[FieldError("/allowed_roles", "unique_items", "роли не должны повторяться")]
        )
    async with transaction(app_state(request).engine, access.db()) as conn:
        view = await registry.update_workspace_settings(
            conn,
            workspace_id=access.workspace_id,
            provider_id=provider_id,
            actor_id=principal.user_id,
            expected_version=expected,
            changes=changes,
        )
    return json_response(view, headers={"ETag": etag(view["settings_version"])})
