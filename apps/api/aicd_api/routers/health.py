"""Проверки живости/готовности (без аутентификации, без деталей ошибок)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from sqlalchemy import text
from starlette.responses import Response

from aicd_api import EXPECTED_SCHEMA_REVISION
from aicd_api.deps import app_state
from aicd_api.responses import json_response
from aicd_api.routers import op

router = APIRouter(prefix="/health", tags=["health"])
log = logging.getLogger("aicd.health")


@router.get("/live", **op("healthLive", "public"))
async def health_live() -> Response:
    return json_response({"status": "ok"})


@router.get("/ready", **op("healthReady", "public"))
async def health_ready(request: Request) -> Response:
    state = app_state(request)
    checks: dict[str, str] = {}
    try:
        async with state.engine.connect() as conn:
            revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
        checks["postgres"] = "ok"
        checks["migrations"] = "ok" if revision == EXPECTED_SCHEMA_REVISION else "fail"
    except Exception:
        log.warning("postgres not ready", exc_info=True)
        checks["postgres"] = checks["migrations"] = "fail"
    try:
        checks["redis"] = "ok" if await state.redis.ping() else "fail"
    except Exception:  # noqa: BLE001
        checks["redis"] = "fail"
    # Redis — транспорт/кэш: его отказ — degraded, а не fail (у rate limit есть fallback).
    try:
        checks["object_storage"] = "ok" if state.artifacts.healthy() else "fail"
    except Exception:  # noqa: BLE001
        checks["object_storage"] = "fail"
    critical = ("postgres", "migrations", "object_storage")
    if any(checks[c] != "ok" for c in critical):
        return json_response({"status": "fail", "checks": checks}, 503)
    return json_response({"status": "ok" if checks["redis"] == "ok" else "degraded", "checks": checks})
