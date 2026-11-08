"""Ресурсы приложения (создаются в lifespan)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.ratelimit import RateLimiter
from aicd_identity.api import SessionPolicy
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.config import Settings


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    redis: Redis
    limiter: RateLimiter
    artifacts: ArtifactStore
    secret_key: bytes

    @property
    def session_policy(self) -> SessionPolicy:
        s = self.settings
        return SessionPolicy(
            absolute_ttl=timedelta(seconds=s.session_absolute_ttl_seconds),
            idle_ttl=timedelta(seconds=s.session_idle_ttl_seconds),
            max_failures=s.login_max_failures,
            lockout_seconds=s.login_lockout_seconds,
        )
