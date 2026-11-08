"""ST-SEC-03 (ТЗ §20 «secret leakage», SC-SEC-03, SC-API-04, SC-OBS-01/03, TH-14/TH-15).

Канареечные секреты проходят через все выходы платформы и не должны появиться ни в одном из них:

* вывод CLI → CLI Gateway → **БД** (все таблицы схемы `aicd`, включая outbox/run_event_log/аудит) и
  **хранилище артефактов** (raw log, output) — секрет в stdout/stderr Fake CLI (`secret_leak`);
* **SSE** (`GET /runs/{id}/events`) и `GET /runs/{id}/activity` — событие `cli.output` с секретом в тексте;
* **логи** (JSON-формат платформы) — неверный пароль при входе/step-up, `Authorization`/`Cookie` с токенами,
  необработанное исключение с секретом в сообщении;
* **ответы об ошибках** (problem+json 401/403/422/500) — значения ввода не возвращаются, трассировок нет;
* **метки метрик** `/metrics` — нет секретов, e-mail, токенов и неограниченных идентификаторов.
"""

from __future__ import annotations

import io
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.main import create_app
from aicd_platform.config import Settings
from aicd_platform.logging import JsonFormatter
from tests.conftest import PASSWORD, UserSession, login, make_client
from tests.integration.test_gateway import Env, _adapter, _worktree, env  # noqa: F401 — фикстура gateway
from tests.integration.test_sse import Env as SseEnv
from tests.integration.test_sse import Reader, _client, _events, fast_stream, redis, server  # noqa: F401
from tests.support import db as testdb
from tests.support.cli import make_request

pytestmark = [pytest.mark.db, pytest.mark.security]

# Секреты из сценария Fake CLI `secret_leak` (stdout и stderr).
CLI_SECRETS = ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "hunter2hunter2", "AKIAABCDEFGHIJKLMNOP")
CANARY_TOKEN = "ghp_CANARYcanaryCANARYcanaryCANARY0123"  # формат GitHub PAT (детектор redaction)
CANARY_PASSWORD = "Canary-Pa55word-Never-Logged-7"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
async def sse_env(
    database: testdb.TestDatabase,
    app_engine: AsyncEngine,
    worker_engine: AsyncEngine,
    redis: Any,  # noqa: F811
    user: UserSession,
) -> SseEnv:
    return SseEnv(database, app_engine, worker_engine, redis, user)


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    """Все записи логгеров (root) в JSON-формате платформы — как их увидит сборщик логов."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(previous)


def _scan_files(root: Path, needles: tuple[str, ...]) -> tuple[list[str], int]:
    files = [p for p in root.rglob("*") if p.is_file()]
    leaked = [p.name for p in files for n in needles if n.encode() in p.read_bytes()]
    return leaked, len(files)


def _scan_database(database: testdb.TestDatabase, needles: tuple[str, ...]) -> dict[str, list[str]]:
    """Таблицы схемы `aicd`, в текстовом представлении строк которых встречается секрет."""
    hits: dict[str, list[str]] = {}
    with psycopg.connect(database.superuser_url, autocommit=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'aicd' AND c.relkind IN ('r','p') AND NOT c.relispartition"
            ).fetchall()
        ]
        for table in tables:
            for needle in needles:
                row = conn.execute(
                    f"SELECT count(*) FROM aicd.{table} t WHERE t::text LIKE %s",
                    (f"%{needle}%",),
                ).fetchone()
                if row and row[0]:
                    hits.setdefault(table, []).append(needle)
    return hits


async def test_cli_output_secrets_absent_from_db_and_artifacts(env: Env, database: testdb.TestDatabase) -> None:  # noqa: F811
    request = make_request(worktree_id=_worktree(env), workspace_id=env.workspace_id, task_attempt_id=env.attempt_id)
    seen: list[str] = []

    async def on_event(e: Any) -> None:
        seen.append(json.dumps(e.payload, ensure_ascii=False, default=str))

    outcome = await env.gateway.run(
        request, _adapter(env, "secret_leak"), installation_id=env.installation_id, on_event=on_event
    )
    assert outcome.result.status == "completed"
    assert seen and not any(s in text for s in CLI_SECRETS for text in seen), "события Gateway (→ outbox/SSE)"
    assert _scan_database(database, CLI_SECRETS) == {}, "секрет сохранён в БД"
    leaked, stored = _scan_files(Path(env.store._root), CLI_SECRETS)
    assert stored, "raw log и output сохранены"
    assert leaked == [], f"секрет в артефактах: {leaked}"


async def test_sse_and_activity_never_carry_secrets(
    server: str,  # noqa: F811
    sse_env: SseEnv,
    user: UserSession,
    database: testdb.TestDatabase,
) -> None:
    text = f"export GITHUB_TOKEN={CANARY_TOKEN} AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP"
    await sse_env.emit(
        1,
        "cli.output",
        {"execution_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()), "provider": "codex",
         "agent_role": "developer", "cli_seq": 1, "cli_event_type": "output", "stream": "stdout", "text": text},
    )  # fmt: skip
    await sse_env.dispatch()
    async with _client(server, user) as client:
        activity = await client.get(f"/runs/{sse_env.run}/activity")
        assert activity.status_code == 200
        assert CANARY_TOKEN not in activity.text and "AKIAABCDEFGHIJKLMNOP" not in activity.text
        async with client.stream("GET", f"/runs/{sse_env.run}/events") as response:
            assert response.status_code == 200
            frames, _ = await Reader(response).frames(1)
    events = _events(frames)
    assert events and events[0]["type"] == "cli.output"
    body = json.dumps(events)
    assert CANARY_TOKEN not in body and "AKIAABCDEFGHIJKLMNOP" not in body
    assert "[REDACTED]" in body, body[:500]
    assert _scan_database(database, (CANARY_TOKEN,)) == {}


@pytest.fixture
async def boom_app(settings: Settings) -> AsyncIterator[FastAPI]:
    """Отдельный экземпляр API с тестовым маршрутом, падающим с секретом в сообщении исключения."""
    application = create_app(settings)

    async def boom() -> None:
        raise RuntimeError(f"connect failed: postgresql://aicd:{CANARY_PASSWORD}@db/aicd token={CANARY_TOKEN}")

    application.add_api_route("/api/v1/__security_boom", boom, methods=["GET"])
    async with application.router.lifespan_context(application):
        yield application


async def test_error_responses_and_logs_never_echo_secrets(
    app: FastAPI, app_engine: AsyncEngine, boom_app: FastAPI, captured_logs: io.StringIO
) -> None:
    user = await login(app, app_engine, ip="203.0.113.91")
    responses: list[httpx.Response] = []
    try:
        async with make_client(app, ip="203.0.113.91") as anon:
            # 401: неверный пароль; 422: пароль валиден по схеме, e-mail нет — значение ввода не возвращается.
            responses.append(
                await anon.post("/api/v1/auth/login", json={"email": user.email, "password": CANARY_PASSWORD})
            )
            responses.append(
                await anon.post("/api/v1/auth/login", json={"email": "not-an-email", "password": CANARY_PASSWORD})
            )
            responses.append(
                await anon.post(
                    "/api/v1/auth/login", json={"email": user.email, "password": CANARY_PASSWORD, "extra": CANARY_TOKEN}
                )
            )
            # Чужие/поддельные учётные данные в заголовках: 401 без отражения значения.
            responses.append(
                await anon.get(
                    "/api/v1/auth/session",
                    headers={"Authorization": f"Bearer {CANARY_TOKEN}", "Cookie": f"aicd_session={CANARY_TOKEN}"},
                )
            )
        # Step-up с неверным паролем: 403 без пароля.
        responses.append(
            await user.client.post(
                "/api/v1/auth/reauthenticate", json={"password": CANARY_PASSWORD}, headers=user.headers()
            )
        )
        responses.append(
            await user.client.post(
                "/api/v1/auth/reauthenticate", json={"password": CANARY_PASSWORD * 60}, headers=user.headers()
            )
        )
        # 500: сообщение исключения (с DSN и токеном) не попадает в ответ; в логе — только после redaction.
        transport = httpx.ASGITransport(app=boom_app, client=("203.0.113.92", 51000), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            responses.append(await client.get("/api/v1/__security_boom"))
    finally:
        await user.client.aclose()
    assert [r.status_code for r in responses] == [401, 422, 422, 401, 403, 422, 500]
    for r in responses:
        assert r.headers["content-type"].startswith("application/problem+json")
        for needle in (CANARY_PASSWORD, CANARY_TOKEN, "Traceback", "postgresql://", "RuntimeError"):
            assert needle not in r.text, f"{r.status_code}: {needle} в ответе"
    logs = captured_logs.getvalue()
    assert "unhandled error" in logs, "необработанная ошибка логируется (с redaction)"
    for needle in (CANARY_PASSWORD, CANARY_TOKEN, PASSWORD):
        assert needle not in logs, f"{needle[:6]}… в логах"


async def test_metrics_labels_carry_no_secrets_or_unbounded_ids(app: FastAPI, user: UserSession) -> None:
    await user.client.post("/api/v1/auth/reauthenticate", json={"password": CANARY_PASSWORD}, headers=user.headers())
    async with make_client(app) as client:
        r = await client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    for needle in (CANARY_PASSWORD, CANARY_TOKEN, user.email, user.csrf, PASSWORD):
        assert needle not in body
    labels = re.findall(r"\{([^}]*)\}", body)
    assert labels, "экспозиция содержит метрики с метками"
    for label in labels:
        assert not UUID_RE.search(label), f"неограниченный идентификатор в метке: {label}"
        assert "@" not in label, f"e-mail в метке: {label}"
    assert 'reason="reauth_failed"' in body, "метка — код причины, не значение"
