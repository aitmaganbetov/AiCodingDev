"""Общие фикстуры. Тесты с БД требуют AICD_TEST_DATABASE_ADMIN_URL (суперпользователь PostgreSQL 17)."""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.main import create_app
from aicd_identity import api as identity
from aicd_platform.config import Settings
from aicd_platform.db import DbContext, create_engine, transaction
from tests.support import db as testdb

REDIS_URL = os.environ.get("AICD_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
PASSWORD = "Correct-Horse-Battery-9"


def _mark_acceptance(items: list[pytest.Item]) -> None:
    """P9-01: тесты из матрицы приёмки (tests/acceptance/matrix.yaml) — маркер `acceptance` и свойства junit `ac`."""
    from tests.acceptance import matrix

    loaded = matrix.load()
    for item in items:
        acs = loaded.acs_for_nodeid(item.nodeid)
        if acs:
            item.add_marker(pytest.mark.acceptance)
            item.user_properties.append(("ac", ",".join(sorted(acs))))
            item.user_properties.append(("nodeid", item.nodeid))


def _mark_security(items: list[pytest.Item]) -> None:
    """P9-02: security suite — всё в tests/security/ и тесты из каталога tests/security/catalog.py получают маркер
    `security` (`pytest -m security`) и свойство junit `controls` (отчёт scripts/security_report.py)."""
    from tests.security import catalog

    for item in items:
        if catalog.is_security_test(item.nodeid):
            item.add_marker(pytest.mark.security)
            controls = catalog.controls_for_nodeid(item.nodeid)
            if controls:
                item.user_properties.append(("controls", ",".join(controls)))
            item.user_properties.append(("security_nodeid", item.nodeid))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    _mark_acceptance(items)
    _mark_security(items)
    if testdb.admin_url():
        return
    skip = pytest.mark.skip(reason="AICD_TEST_DATABASE_ADMIN_URL не задан")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def database() -> Iterator[testdb.TestDatabase]:
    url = testdb.admin_url()
    if not url:
        pytest.skip("AICD_TEST_DATABASE_ADMIN_URL не задан")
    db = testdb.create_database(url)
    try:
        testdb.alembic(db, "upgrade", "head")
        yield db
    finally:
        testdb.drop_database(db)


@pytest.fixture(scope="session")
def repo_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("repos")
    return root


@pytest.fixture(scope="session")
def settings(database: testdb.TestDatabase, repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        environment="test",
        database_url=database.url_for("aicd_app"),
        redis_url=REDIS_URL,
        secret_key=secrets.token_urlsafe(48),
        cookie_secure=True,
        repository_roots={"main": repo_root},
        artifact_fs_root=tmp_path_factory.mktemp("artifacts"),
        rate_limit_login_per_ip_per_minute=10_000,
        rate_limit_mutation_per_minute=10_000,
        rate_limit_read_per_minute=10_000,
        log_level="WARNING",
    )


@pytest.fixture(scope="session")
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    from redis.asyncio import Redis

    redis = Redis.from_url(REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture(scope="session")
async def app_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings.require_database_url())
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def worker_engine(database: testdb.TestDatabase) -> AsyncIterator[AsyncEngine]:
    """Движок под ролью БД воркеров (aicd_worker)."""
    engine = create_engine(database.url_for("aicd_worker"), pool_size=8)
    yield engine
    await engine.dispose()


def make_client(app: FastAPI, ip: str = "203.0.113.10") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 51000))
    return httpx.AsyncClient(transport=transport, base_url="https://testserver")


@dataclass
class UserSession:
    client: httpx.AsyncClient
    user_id: str
    email: str
    csrf: str

    def headers(
        self,
        workspace_id: str | None = None,
        idem: bool = False,
        *,
        if_match: str | None = None,
        idem_key: str | None = None,
    ) -> dict[str, str]:
        h = {"X-CSRF-Token": self.csrf}
        if workspace_id:
            h["X-Workspace-Id"] = workspace_id
        if idem or idem_key:
            h["Idempotency-Key"] = idem_key or "k-" + secrets.token_hex(8)
        if if_match:
            h["If-Match"] = if_match
        return h

    async def create_workspace(self, name: str | None = None) -> dict[str, Any]:
        slug = "ws-" + secrets.token_hex(4)
        r = await self.client.post(
            "/api/v1/workspaces", json={"name": name or slug, "slug": slug}, headers=self.headers(idem=True)
        )
        assert r.status_code == 201, r.text
        data: dict[str, Any] = r.json()
        return data

    async def create_project(self, workspace_id: str, name: str | None = None) -> dict[str, Any]:
        r = await self.client.post(
            "/api/v1/projects",
            json={"name": name or "p-" + secrets.token_hex(3)},
            headers=self.headers(workspace_id, idem=True),
        )
        assert r.status_code == 201, r.text
        data: dict[str, Any] = r.json()
        return data


@pytest.fixture
def unique_email() -> str:
    return f"user-{secrets.token_hex(5)}@example.test"


async def register_user(engine: AsyncEngine, email: str, password: str = PASSWORD, name: str = "Тест") -> str:
    async with transaction(engine, DbContext(actor_type="system")) as conn:
        uid = await identity.create_user(conn, email=email, display_name=name, password=password)
    return str(uid)


async def login(app: FastAPI, engine: AsyncEngine, email: str | None = None, ip: str = "203.0.113.10") -> UserSession:
    email = email or f"user-{secrets.token_hex(5)}@example.test"
    uid = await register_user(engine, email)
    client = make_client(app, ip)
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return UserSession(client=client, user_id=uid, email=email, csrf=r.json()["csrf_token"])


@pytest.fixture
async def user(app: FastAPI, app_engine: AsyncEngine) -> AsyncIterator[UserSession]:
    session = await login(app, app_engine)
    yield session
    await session.client.aclose()


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(path), "PATH": os.environ["PATH"]}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    (path / "README.md").write_text("test\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "init"],
        check=True,
        env=env,
    )
