"""Analytics (ТЗ §11, §16; FR-OBS-05, FR-CI-08, P8-07): сводка метрик workspace за окно времени.

Tenant — заголовок `X-Workspace-Id` (API_DESIGN §4); право `run:read` (агрегаты по run, доступным любой роли
workspace). Фильтр `project_id` проверяется на принадлежность тому же workspace (чужой ⇒ 404, AC-12).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import Response

from aicd_analytics import summary
from aicd_api.deps import (
    app_state,
    authorize,
    current_principal,
    parse_workspace_header,
    project_access,
    workspace_access,
)
from aicd_api.responses import json_response
from aicd_api.routers import op
from aicd_identity import api as identity
from aicd_platform.clock import utcnow
from aicd_platform.db import transaction
from aicd_platform.errors import FieldError, ProblemError

router = APIRouter(tags=["analytics"])

DEFAULT_WINDOW = timedelta(days=30)
MAX_WINDOW = timedelta(days=366)


def _window(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime]:
    end = to or utcnow()
    start = from_ or end - DEFAULT_WINDOW
    if start.tzinfo is None or end.tzinfo is None:
        raise ProblemError(
            "validation_error", errors=[FieldError("/query/from", "format", "время с часовым поясом (ISO 8601, Z)")]
        )
    if start >= end:
        raise ProblemError(
            "validation_error", errors=[FieldError("/query/from", "range", "from должен быть раньше to")]
        )
    if end - start > MAX_WINDOW:
        raise ProblemError("validation_error", errors=[FieldError("/query/from", "range", "окно не больше 366 дней")])
    return start, end


@router.get("/analytics/summary", **op("getAnalyticsSummary", "run:read"))
async def get_analytics_summary(
    request: Request,
    principal: identity.Principal = Depends(current_principal),
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    project_id: uuid.UUID | None = None,
) -> Response:
    access = await workspace_access(request, principal, parse_workspace_header(request))
    await authorize(request, access, "run:read")
    if project_id is not None:
        found = await project_access(request, principal, project_id)
        if found.workspace_id != access.workspace_id:  # pragma: no cover — project_access сверяет заголовок
            raise ProblemError("not_found")
    start, end = _window(from_, to)
    async with transaction(app_state(request).engine, access.db()) as conn:
        body = await summary.summary(conn, summary.Window(start, end, project_id))
    return json_response(body)
