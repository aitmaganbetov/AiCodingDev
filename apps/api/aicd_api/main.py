"""Фабрика FastAPI-приложения: `uvicorn aicd_api.main:create_app --factory`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from aicd_api import errors
from aicd_api.middleware import BodyLimitMiddleware, request_context_middleware
from aicd_api.ratelimit import RateLimiter
from aicd_api.routers import (
    agent_profiles,
    aggregates,
    analytics,
    approvals,
    artifacts,
    audit,
    auth,
    chat,
    code_intelligence,
    directives,
    documentation,
    events,
    findings,
    github,
    health,
    metrics,
    projects,
    providers,
    quality,
    runs,
    search,
    standards,
    system,
    technologies,
    workspaces,
    worktrees,
)
from aicd_api.state import AppState
from aicd_platform.artifacts import build_artifact_store
from aicd_platform.config import Settings, get_settings
from aicd_platform.db import TcpKeepalive, create_engine
from aicd_platform.logging import configure_logging
from aicd_platform.telemetry import configure_tracing

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    configure_tracing("aicd-api", settings.otel_exporter)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(
            settings.require_database_url(),
            pool_size=settings.db_pool_size,
            keepalive=TcpKeepalive.from_settings(settings),
        )
        redis = Redis.from_url(settings.redis_url, socket_timeout=1.0, socket_connect_timeout=1.0)
        app.state.aicd = AppState(
            settings=settings,
            engine=engine,
            redis=redis,
            limiter=RateLimiter(redis),
            artifacts=build_artifact_store(settings),
            secret_key=settings.require_secret_key(),
        )
        try:
            yield
        finally:
            hub = getattr(app.state, "aicd_sse_hub", None)  # общий pub/sub SSE-потоков (aicd_api.sse_hub)
            if hub is not None:
                await hub.close()
            client = getattr(app.state, "aicd_github", None)
            if client is not None:
                await client.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="AiCodingDev Internal API",
        version="1.0.0",
        openapi_url=f"{API_PREFIX}/openapi.json" if settings.environment != "production" else None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.middleware("http")(request_context_middleware)
    # Вебхуки GitHub — до 25 MiB (SC-GH-01); остальные тела — 256 KiB (API_DESIGN §6).
    app.add_middleware(
        BodyLimitMiddleware, overrides={API_PREFIX + github.WEBHOOK_PATH: settings.github_webhook_max_bytes}
    )
    errors.install(app)
    for router in (
        health.router,
        auth.router,
        workspaces.router,
        projects.router,
        technologies.router,
        technologies.workspace_router,
        documentation.router,
        providers.router,
        runs.router,
        quality.router,
        findings.router,
        standards.router,
        approvals.router,
        directives.router,
        chat.router,
        events.router,
        audit.router,
        artifacts.router,
        github.router,
        analytics.router,
        code_intelligence.router,
        worktrees.router,
        search.router,
        aggregates.router,
        system.router,
        agent_profiles.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    app.include_router(metrics.router)  # /metrics вне /api/v1: только внутренняя сеть (P8-06)
    return app
