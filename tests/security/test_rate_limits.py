"""ST-API-02 / ST-IAM-06 (SC-API-02, SC-IAM-06; TH-23, TH-35): rate limiting входа и команд на отдельном экземпляре API
с низкими лимитами (в общем тестовом приложении лимиты подняты, чтобы не мешать другим тестам)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.main import create_app
from aicd_platform.config import Settings
from tests.conftest import PASSWORD, make_client, register_user
from tests.support.contract import validate_response

pytestmark = [pytest.mark.db, pytest.mark.security]


@pytest.fixture(scope="module")
async def limited_app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(
        settings.model_copy(update={"rate_limit_login_per_ip_per_minute": 3, "rate_limit_mutation_per_minute": 4})
    )
    async with application.router.lifespan_context(application):
        yield application


async def test_login_is_rate_limited_per_ip(limited_app: FastAPI, app_engine: AsyncEngine) -> None:
    ip = f"198.18.{secrets.randbelow(250)}.{secrets.randbelow(250) + 1}"
    email = f"rl-{secrets.token_hex(4)}@example.test"
    await register_user(app_engine, email)
    async with make_client(limited_app, ip=ip) as client:
        codes = [
            (await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password-0"})).status_code
            for _ in range(3)
        ]
        blocked = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert codes == [401, 401, 401]
    validate_response("login", blocked)
    assert blocked.status_code == 429 and blocked.json()["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1 and blocked.json()["retry_after_seconds"] >= 1
    # Другой IP не затронут (лимит на IP, не глобальный).
    async with make_client(limited_app, ip="198.19.0.1") as other:
        assert (await other.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})).status_code == 200


async def test_mutations_are_rate_limited_per_user(limited_app: FastAPI, app_engine: AsyncEngine) -> None:
    email = f"rl-{secrets.token_hex(4)}@example.test"
    await register_user(app_engine, email)
    async with make_client(limited_app, ip="198.19.0.2") as client:
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        csrf = r.json()["csrf_token"]
        codes = []
        for _ in range(5):
            slug = "rl-" + secrets.token_hex(4)
            resp = await client.post(
                "/api/v1/workspaces",
                json={"name": slug, "slug": slug},
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "k-" + secrets.token_hex(8)},
            )
            codes.append(resp.status_code)
        # Чтение не расходует бюджет команд.
        assert (await client.get("/api/v1/workspaces")).status_code == 200
    assert codes[:4] == [201] * 4 and codes[4] == 429, codes
