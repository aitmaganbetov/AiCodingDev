"""`GET /metrics` — экспозиция Prometheus процесса API (P8-06, SC-OBS-03).

Не часть REST API v1: путь вне `/api/v1`, в OpenAPI не публикуется, reverse-proxy (`infrastructure/docker/web`)
проксирует только `/api/`, поэтому endpoint доступен лишь из внутренней сети (Prometheus в сети backend).
Доступ — `Authorization: Bearer <AICD_METRICS_TOKEN_FILE>`; в production без настроенного токена — 404.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.responses import Response

from aicd_api.deps import app_state
from aicd_platform import metrics

router = APIRouter(include_in_schema=False)


def _sample_storage(root: Path) -> None:
    usage = shutil.disk_usage(root)
    metrics.ARTIFACT_STORAGE_FREE.set(usage.free)
    metrics.ARTIFACT_STORAGE_TOTAL.set(usage.total)


@router.get("/metrics")
async def prometheus_metrics(request: Request) -> Response:
    state = app_state(request)
    allowed = metrics.metrics_allowed(
        token=state.settings.metrics_token_value(),
        environment=state.settings.environment,
        authorization=request.headers.get("authorization"),
    )
    if allowed is None:
        return Response("not found\n", status_code=404, media_type="text/plain")
    if not allowed:
        return Response(
            "unauthorized\n", status_code=401, media_type="text/plain", headers={"WWW-Authenticate": "Bearer"}
        )
    if state.settings.artifact_backend == "fs":
        try:
            await asyncio.to_thread(_sample_storage, state.settings.artifact_fs_root)
        except OSError:
            metrics.SAMPLER_ERRORS.inc()
    body, content_type = metrics.render()
    return Response(body, media_type=content_type)
