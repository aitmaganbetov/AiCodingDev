"""Фабрика FastAPI-приложения: `uvicorn aicd_api.main:create_app --factory`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from redis.asyncio import Redis

from aicd_api import errors
from aicd_api.middleware import BodyLimitMiddleware, request_context_middleware
from aicd_api.ratelimit import RateLimiter
from aicd_api.routers import audit, auth, health, projects, providers, workspaces
from aicd_api.state import AppState
from aicd_platform.artifacts import ArtifactStore, FileSystemArtifactStore, S3ArtifactStore
from aicd_platform.config import Settings, get_settings
from aicd_platform.db import create_engine
from aicd_platform.logging import configure_logging
from aicd_platform.telemetry import configure_tracing

API_PREFIX = "/api/v1"


def build_artifact_store(settings: Settings) -> ArtifactStore:
    if settings.artifact_backend == "s3":
        if not (
            settings.artifact_s3_endpoint
            and settings.artifact_s3_access_key_file
            and settings.artifact_s3_secret_key_file
        ):
            raise RuntimeError("для artifact_backend=s3 нужны endpoint и файлы ключей доступа")
        return S3ArtifactStore(
            endpoint_url=settings.artifact_s3_endpoint,
            bucket=settings.artifact_s3_bucket,
            access_key=Path(settings.artifact_s3_access_key_file).read_text().strip(),
            secret_key=Path(settings.artifact_s3_secret_key_file).read_text().strip(),
            region=settings.artifact_s3_region,
        )
    return FileSystemArtifactStore(settings.artifact_fs_root)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    configure_tracing("aicd-api", settings.otel_exporter)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.require_database_url(), pool_size=settings.db_pool_size)
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
    app.add_middleware(BodyLimitMiddleware)
    errors.install(app)
    for router in (health.router, auth.router, workspaces.router, projects.router, providers.router, audit.router):
        app.include_router(router, prefix=API_PREFIX)
    return app
