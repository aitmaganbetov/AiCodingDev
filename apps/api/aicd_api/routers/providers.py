"""CLI-провайдеры (ТЗ §8): health и capabilities без учётных данных.

`POST /providers/{id}/probe` и `PATCH /providers/{id}/settings` подключаются в Phase 3:
им нужны очередь заданий воркерам (outbox/Dramatiq) и таблица policies.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from aicd_api.deps import app_state, authorize, current_principal, parse_workspace_header, workspace_access
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_cli_adapters import registry
from aicd_identity import api as identity
from aicd_platform.db import transaction

router = APIRouter(tags=["providers"])


@router.get("/providers", **op("listProviders", "provider:read"))
async def list_providers(request: Request, principal: identity.Principal = Depends(current_principal)) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "provider:read")
    async with transaction(app_state(request).engine, access.db()) as conn:
        items = await registry.list_providers(conn)
    return json_response({"items": items})
