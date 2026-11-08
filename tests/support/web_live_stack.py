"""Живой стенд для browser E2E Mission Control (P7-14): `apps/web/e2e/live` против настоящего backend.

Запуск (из корня репозитория; обычно его запускает `apps/web/playwright.live.config.ts` при AICD_E2E_LIVE_STACK=1):

    uv run python -m tests.support.web_live_stack --port 4190 --control-port 4191 --dist apps/web/dist-live

Поднимает на отдельной (создаваемой и удаляемой) базе PostgreSQL 17:

* API (`aicd_api.main.create_app`) и собранный SPA на **одном origin** (как nginx профиля local:
  `/api/` → API без буферизации SSE, остальное — статические файлы с fallback на index.html и те же
  заголовки безопасности, включая CSP `script-src 'self'`);
* воркеры `aicd-worker outbox | orchestrator | exec` (Dramatiq через Redis, отдельная БД Redis),
  исполнитель — Fake CLI (`AICD_FAKE_SCENARIO`, по умолчанию `agent`: developer → REVIEW_REQUEST →
  независимый reviewer), gates — `LocalProcessRunner` без gates «только в контейнере»;
* артефакты — S3 (MinIO, отдельный bucket) при заданном AICD_E2E_S3_ENDPOINT, иначе FS;
* данные: пользователь (email/пароль), workspace, проект с локальным git-репозиторием, установки CLI codex+claude
  (probe Fake CLI → `untested`, без отметки `supported` в БД).

Управляющий HTTP (только 127.0.0.1, для тестов): `GET /fixture`, `POST /project` (новый проект с репозиторием),
`POST /exec/stop|start` (остановить/запустить воркер исполнения — темп сценария),
`POST /api/kill|start` (жёстко убить/поднять процесс API — разрыв SSE на стороне сервера).
SIGINT/SIGTERM — остановка процессов и удаление базы.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import mimetypes
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL_DEFAULT = "postgresql://aicd_admin:devpass@127.0.0.1:55432/postgres"
PASSWORD_DEFAULT = "Correct-Horse-Battery-9"

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

# Те же заголовки, что у nginx профиля local (infrastructure/docker/web/nginx.conf).
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;"
        b" connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    ),
]


# --- Same-origin сервер: API + SPA ------------------------------------------------------------------


def create_server() -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    """ASGI-приложение для uvicorn (`--factory`): `/api/` → API, остальное — SPA (try_files $uri /index.html)."""
    from aicd_api.main import create_app

    api = create_app()
    dist = Path(os.environ["AICD_E2E_SPA_DIST"]).resolve()
    index = dist / "index.html"

    async def spa(scope: Scope, send: Send) -> None:
        rel = scope["path"].lstrip("/")
        candidate = (dist / rel).resolve() if rel else index
        if not candidate.is_file() or (dist not in candidate.parents and candidate != index):
            candidate = index
        body = candidate.read_bytes()
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        cache = b"no-cache" if candidate == index else b"public, max-age=31536000, immutable"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", ctype.encode()), (b"cache-control", cache), *SECURITY_HEADERS],
            }
        )
        await send({"type": "http.response.body", "body": body if scope["method"] != "HEAD" else b""})

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith("/api/"):
            await api(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 405, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await spa(scope, send)

    return app


# --- Стенд ------------------------------------------------------------------------------------------


class Stack:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.origin = f"http://127.0.0.1:{args.port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="aicd-web-live-"))
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.email = args.email
        self.password = args.password
        self.fixture: dict[str, Any] = {}
        self.lock = threading.Lock()
        self.database: Any = None
        self.env: dict[str, str] = {}
        self.worker_env: dict[str, str] = {}

    # Окружение ------------------------------------------------------------------------------------

    def prepare(self) -> None:
        from tests.support import db as testdb

        admin = os.environ.get("AICD_TEST_DATABASE_ADMIN_URL", ADMIN_URL_DEFAULT)
        self.database = testdb.create_database(admin, prefix="aicd_web_e2e")
        print(f"[live-stack] database {self.database.name}", flush=True)
        testdb.alembic(self.database, "upgrade", "head")

        secrets_dir = self.tmp / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "secret_key").write_text(secrets.token_urlsafe(48))
        repos = self.tmp / "repos"
        repos.mkdir()
        env = {
            "AICD_ENVIRONMENT": "test",
            "AICD_DATABASE_URL": self.database.url_for("aicd_app"),
            "AICD_REDIS_URL": self.args.redis_url,
            "AICD_SECRET_KEY_FILE": str(secrets_dir / "secret_key"),
            "AICD_COOKIE_SECURE": "false",  # http://127.0.0.1 (TLS терминирует прокси стенда в профиле server)
            "AICD_ALLOWED_ORIGINS": self.origin,
            "AICD_REPOSITORY_ROOTS": f"main={repos}",
            "AICD_RATE_LIMIT_LOGIN_PER_IP_PER_MINUTE": "1000",
            "AICD_RATE_LIMIT_MUTATION_PER_MINUTE": "10000",
            "AICD_RATE_LIMIT_READ_PER_MINUTE": "100000",
            "AICD_LOG_LEVEL": "WARNING",
            "AICD_E2E_SPA_DIST": str(Path(self.args.dist).resolve()),
        }
        s3 = os.environ.get("AICD_E2E_S3_ENDPOINT")
        if s3:
            from aicd_platform.artifacts import S3ArtifactStore

            access = os.environ.get("AICD_E2E_S3_ACCESS_KEY", "")
            secret = os.environ.get("AICD_E2E_S3_SECRET_KEY", "")
            (secrets_dir / "s3_access").write_text(access)
            (secrets_dir / "s3_secret").write_text(secret)
            bucket = f"aicd-web-e2e-{secrets.token_hex(4)}"
            S3ArtifactStore(endpoint_url=s3, bucket=bucket, access_key=access, secret_key=secret).ensure_bucket()
            env |= {
                "AICD_ARTIFACT_BACKEND": "s3",
                "AICD_ARTIFACT_S3_ENDPOINT": s3,
                "AICD_ARTIFACT_S3_BUCKET": bucket,
                "AICD_ARTIFACT_S3_ACCESS_KEY_FILE": str(secrets_dir / "s3_access"),
                "AICD_ARTIFACT_S3_SECRET_KEY_FILE": str(secrets_dir / "s3_secret"),
            }
            print(f"[live-stack] artifacts s3 {s3} bucket {bucket}", flush=True)
        else:
            env |= {"AICD_ARTIFACT_BACKEND": "fs", "AICD_ARTIFACT_FS_ROOT": str(self.tmp / "artifacts")}
        self.env = env
        self.worker_env = env | {
            "AICD_DATABASE_URL": self.database.url_for("aicd_worker"),
            "AICD_EXEC_ADAPTER": "fake",
            "AICD_FAKE_SCENARIO": self.args.scenario,
            "AICD_VERIFICATION_RUNNER": "local",
            "AICD_WORKTREE_ROOT": str(self.tmp / "worktrees"),
            "AICD_VERIFICATION_STAGING_ROOT": str(self.tmp / "staging"),
            "AICD_INDEX_EXPORT_CACHE_ROOT": str(self.tmp / "index-export"),
            "AICD_TICK_INTERVAL_SECONDS": "1",
            "AICD_OUTBOX_POLL_SECONDS": "0.2",
            "AICD_STALE_READY_SECONDS": "5",
        }

    # Процессы -------------------------------------------------------------------------------------

    def _spawn(self, name: str, cmd: list[str], env: dict[str, str]) -> None:
        log = (self.logs / f"{name}.log").open("ab")
        full_env = {**os.environ, **env}
        self.procs[name] = subprocess.Popen(cmd, cwd=ROOT, env=full_env, stdout=log, stderr=subprocess.STDOUT)

    def start_api(self) -> None:
        current = self.procs.get("api")
        if current is not None and current.poll() is None:
            return
        self._spawn(
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.support.web_live_stack:create_server",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.args.port),
                "--no-access-log",
                "--timeout-graceful-shutdown",
                "1",
            ],
            self.env,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.procs["api"].poll() is not None:
                raise RuntimeError(f"API завершился: см. {self.logs / 'api.log'}")
            try:
                if httpx.get(f"{self.origin}/api/v1/health/live", timeout=1).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("API не поднялся за 60 с")

    def kill_api(self) -> None:
        proc = self.procs.get("api")
        if proc and proc.poll() is None:
            proc.kill()  # жёсткий обрыв: SSE-соединения рвутся на стороне сервера
            proc.wait(10)

    def start_worker(self, kind: str) -> None:
        proc = self.procs.get(kind)
        if proc and proc.poll() is None:
            return
        self._spawn(kind, [sys.executable, "-m", "aicd_worker.main", kind], self.worker_env)

    def stop_worker(self, kind: str) -> None:
        proc = self.procs.get(kind)
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)  # штатно: текущие сообщения дорабатываются
            try:
                proc.wait(60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)

    def stop_all(self) -> None:
        for name in list(self.procs):
            if name == "api":
                self.kill_api()
            else:
                self.stop_worker(name)

    # Данные ---------------------------------------------------------------------------------------

    def seed(self) -> None:
        asyncio.run(self._seed_async())

    async def _seed_async(self) -> None:
        from aicd_identity import api as identity
        from aicd_platform.db import DbContext, create_engine, transaction
        from tests.support.phase3 import install_probed

        app_engine = create_engine(self.database.url_for("aicd_app"))
        worker = create_engine(self.database.url_for("aicd_worker"))
        try:
            async with transaction(app_engine, DbContext(actor_type="system")) as conn:
                await identity.create_user(conn, email=self.email, display_name="E2E Оператор", password=self.password)
            # Установки — штатным probe Fake CLI (`untested`, допускается политикой профиля), как у worker-exec.
            await install_probed(worker, provider="codex")
            await install_probed(worker, provider="claude", isolate=False)
        finally:
            await app_engine.dispose()
            await worker.dispose()
        with self._client() as client:
            r = client.post(
                "/api/v1/workspaces",
                json={"name": "E2E Mission Control", "slug": f"e2e-mc-{secrets.token_hex(3)}"},
                headers=self._headers(client, idem=True),
            )
            r.raise_for_status()
            ws = r.json()["id"]
        self._profiles(uuid.UUID(ws))
        self.fixture = {
            "email": self.email,
            "password": self.password,
            "workspace_id": ws,
            "base_url": self.origin,
            "scenario": self.args.scenario,
            "database": self.database.name,
        }
        self.fixture["project_id"] = self.new_project("Платёжный шлюз")["project_id"]

    def _profiles(self, ws: uuid.UUID) -> None:
        from tests.support.phase3 import local_profile

        for code in ("basic", "strict", "critical"):
            local_profile(self.database, ws, code)

    @contextlib.contextmanager
    def _client(self) -> Iterator[httpx.Client]:
        with httpx.Client(base_url=self.origin, timeout=30) as client:
            r = client.post("/api/v1/auth/login", json={"email": self.email, "password": self.password})
            r.raise_for_status()
            client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
            client.headers["Origin"] = self.origin
            yield client

    @staticmethod
    def _headers(client: httpx.Client, *, ws: str | None = None, idem: bool = False) -> dict[str, str]:
        del client
        h: dict[str, str] = {}
        if ws:
            h["X-Workspace-Id"] = ws
        if idem:
            h["Idempotency-Key"] = "k-" + secrets.token_hex(8)
        return h

    def new_project(self, name: str) -> dict[str, Any]:
        from tests.conftest import init_git_repo

        ws = self.fixture["workspace_id"]
        repo = self.tmp / "repos" / f"repo-{secrets.token_hex(4)}"
        init_git_repo(repo)
        with self._client() as client:
            r = client.post(
                "/api/v1/projects",
                json={"name": f"{name} {secrets.token_hex(2)}"},
                headers=self._headers(client, ws=ws, idem=True),
            )
            r.raise_for_status()
            project = r.json()
            r = client.post(
                f"/api/v1/projects/{project['id']}/repositories",
                json={"provider": "local", "local_path": str(repo)},
                headers=self._headers(client, idem=True),
            )
            r.raise_for_status()
        return {"project_id": project["id"], "project_name": project["name"], "workspace_id": ws}

    def cleanup(self) -> None:
        from tests.support import db as testdb

        self.stop_all()
        if self.database is not None and not self.args.keep:
            testdb.drop_database(self.database)
            print(f"[live-stack] database {self.database.name} dropped", flush=True)
        if not self.args.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)
        else:
            print(f"[live-stack] kept {self.tmp}", flush=True)


# --- Управляющий HTTP (тесты) -----------------------------------------------------------------------


def control_server(stack: Stack, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — сигнатура базового класса
            del format, args

        def _reply(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/fixture":
                self._reply(200, stack.fixture)
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            try:
                with stack.lock:
                    if self.path == "/project":
                        self._reply(201, stack.new_project(str(body.get("name") or "E2E")))
                    elif self.path == "/exec/stop":
                        stack.stop_worker("exec")
                        self._reply(200, {"exec": "stopped"})
                    elif self.path == "/exec/start":
                        stack.start_worker("exec")
                        self._reply(200, {"exec": "started"})
                    elif self.path == "/api/kill":
                        stack.kill_api()
                        self._reply(200, {"api": "killed"})
                    elif self.path == "/api/start":
                        stack.start_api()
                        self._reply(200, {"api": "started"})
                    else:
                        self._reply(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001 — отчёт тесту вместо обрыва соединения
                self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=4190)
    parser.add_argument("--control-port", type=int, default=4191)
    parser.add_argument("--dist", default=str(ROOT / "apps/web/dist-live"))
    parser.add_argument("--redis-url", default=os.environ.get("AICD_E2E_REDIS_URL", "redis://127.0.0.1:6379/12"))
    parser.add_argument("--scenario", default="agent")
    parser.add_argument("--email", default=os.environ.get("AICD_E2E_EMAIL", "e2e-operator@example.test"))
    parser.add_argument("--password", default=os.environ.get("AICD_E2E_PASSWORD", PASSWORD_DEFAULT))
    parser.add_argument("--keep", action="store_true", help="не удалять базу и временный каталог (отладка)")
    args = parser.parse_args(argv)
    if not (Path(args.dist) / "index.html").is_file():
        print(f"[live-stack] нет сборки SPA: {args.dist}/index.html", file=sys.stderr)
        return 2

    stack = Stack(args)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    server: ThreadingHTTPServer | None = None
    try:
        stack.prepare()
        stack.start_api()
        for kind in ("outbox", "orchestrator", "exec"):
            stack.start_worker(kind)
        stack.seed()
        server = control_server(stack, args.control_port)
        print(f"[live-stack] ready {stack.origin} control http://127.0.0.1:{args.control_port} logs {stack.logs}",
              flush=True)  # fmt: skip
        while not stop.wait(1):
            for name, proc in list(stack.procs.items()):
                if name != "exec" and name != "api" and proc.poll() is not None:
                    print(f"[live-stack] процесс {name} завершился ({proc.returncode}), см. {stack.logs}", flush=True)
                    stop.set()
    finally:
        if server is not None:
            server.shutdown()
        stack.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
