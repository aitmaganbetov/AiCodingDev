"""DR drill AiCodingDev (P9-05, NFR-REL-03, SC-DATA-03, RG-13): бэкап → авария → восстановление → проверка.

    export AICD_DR_KEEP=0
    uv run python -m scripts.dr.drill [--report reports/dr/DR_DRILL.md] [--keep]

Всё выполняется на **одноразовых** контейнерах дрилла (имена `aicd-dr-*`, свои порты 55532/56479/59100/4290,
своя сеть `aicd-dr-net`); образы закреплены по digest (postgres и redis — те же, что в compose.yaml).
Общие контейнеры разработки (aicd-pg-dev, aicd-s3-dev, Redis) и демон Docker не затрагиваются.

1. **Стенд**: PostgreSQL с архивированием WAL (как в compose.yaml), Redis, S3 (RustFS); bootstrap ролей
   штатным `postgres-init`, миграции Alembic, процессы `uvicorn aicd_api` и `aicd-worker outbox|orchestrator|exec`
   (Fake CLI, gates — LocalProcessRunner), владельцы — `aicd admin create-owner`, установки CLI — через
   `POST /providers/{id}/probe`, run №1 — до COMPLETED через API.
2. **Бэкап** (`python -m scripts.backup`): pg-base, pg-dump (aicd_maintenance), artifacts (S3 → репозиторий,
   сверка с artifacts.sha256), secrets; затем run №2 и №3 (исполнитель остановлен — run «в полёте»),
   непрерывный `pg-wal-sync` каждые `--sync-interval` с, heartbeat-запись для измерения RPO.
3. **Авария**: SIGKILL процессов, `docker kill` PostgreSQL, удаление контейнеров и томов PostgreSQL/архива
   WAL/S3/Redis, каталога секретов и рабочих каталогов хоста. Уцелел только репозиторий бэкапов и ключ.
4. **Восстановление** по runbook (docs/runbooks/backups.md): секреты → PostgreSQL (base + WAL, PITR до конца
   архива) → post-restore → пустой Redis → S3 + артефакты → API и воркеры.
5. **Проверки**: RPO (потерянные heartbeat), счётчики строк всех таблиц, RLS/FORCE и роли, sha256 артефактов
   (хранилище и скачивание через API), вход пользователя, run №1 COMPLETED, run №2/№3 продолжаются до
   COMPLETED, сдвиг fencing token, логический дамп восстанавливается.

Отчёт — Markdown (`--report`) и JSON рядом. Код возврата 0 — все проверки PASS.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import yaml

from aicd_platform.ids import uuid7
from aicd_verification.registry import GATE_SPECS
from scripts.backup.docker import POSTGRES_IMAGE
from scripts.backup.pg import WAL_ARCHIVE_MOUNT, WAL_RESTORE_MOUNT, WalArchiveConfig

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infrastructure" / "docker" / "compose.yaml"
NET = "aicd-dr-net"
PG, REDIS, S3 = "aicd-dr-pg", "aicd-dr-redis", "aicd-dr-s3"
VOLUMES = ("aicd-dr-pgdata", "aicd-dr-pgwal", "aicd-dr-s3data", "aicd-dr-pgdata-restored", "aicd-dr-walrestore",
           "aicd-dr-pgwal-restored", "aicd-dr-s3data-restored")  # fmt: skip
# S3-совместимое хранилище дрилла — тот же образ, что у контейнера разработки aicd-s3-dev, по digest.
S3_IMAGE = "rustfs/rustfs@sha256:ed9210d6411d77ffceb70d94b964a3cb7d225983368bc25bebe01e995293dcc2"
BUCKET = "aicd-artifacts"
PASSWORD = "Dr-Drill-Correct-Horse-9"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
SECRET_NAMES = ("pg_admin_password", "pg_migrator_password", "pg_app_password", "pg_worker_password",
                "pg_maintenance_password", "app_secret_key", "redis_password", "s3_access_key", "s3_secret_key")  # fmt: skip


def compose_image(service: str) -> str:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return str(data["services"][service]["image"])


def now() -> float:
    return time.time()


def ts(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sh(*cmd: str, check: bool = True, timeout: float = 600, env: dict[str, str] | None = None,
       input_text: str | None = None) -> subprocess.CompletedProcess[str]:  # fmt: skip
    proc = subprocess.run(  # noqa: S603 — аргументы списком, бинарь фиксирован вызывающим
        list(cmd), capture_output=True, text=True, timeout=timeout, check=False, env=env, input=input_text
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}… → {proc.returncode}: {proc.stderr[-2000:]}{proc.stdout[-500:]}")
    return proc


def docker(*args: str, check: bool = True, timeout: float = 600) -> str:
    return sh("docker", *args, check=check, timeout=timeout).stdout.strip()


@dataclass
class Step:
    name: str
    started: float
    finished: float = 0.0
    ok: bool = True
    detail: str = ""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Drill:
    args: argparse.Namespace
    tmp: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="aicd-dr-")))
    steps: list[Step] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    procs: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)

    # --- пути и окружение -------------------------------------------------------------------------------

    def __post_init__(self) -> None:
        self.secrets_dir = self.tmp / "secrets"
        self.repos = self.tmp / "repos"
        self.logs = self.tmp / "logs"
        self.key_dir = self.tmp / "offline-key"  # «офлайн-хранилище» ключа — отдельно от репозитория бэкапов
        self.backup_repo = self.tmp / "backup-repo"
        for path in (self.repos, self.logs, self.key_dir):
            path.mkdir(parents=True)
        self.key_dir.chmod(0o700)
        self.status_file = self.tmp / "backup-status.json"
        self.origin = f"http://127.0.0.1:{self.args.api_port}"
        self.pg_image = compose_image("postgres")
        self.redis_image = compose_image("redis")
        if self.pg_image != POSTGRES_IMAGE:
            raise RuntimeError(f"образ postgres compose.yaml ({self.pg_image}) ≠ образ инструментов ({POSTGRES_IMAGE})")

    def secret(self, name: str) -> str:
        return (self.secrets_dir / name).read_text().strip()

    def url(self, role: str, db: str = "aicd") -> str:
        password = {
            "aicd_admin": self.secret("pg_admin_password"),
            "aicd_migrator": self.secret("pg_migrator_password"),
            "aicd_app": self.secret("pg_app_password"),
            "aicd_worker": self.secret("pg_worker_password"),
            "aicd_maintenance": self.secret("pg_maintenance_password"),
        }[role]
        return f"postgresql://{role}:{password}@127.0.0.1:{self.args.pg_port}/{db}"

    def app_env(self) -> dict[str, str]:
        files = self.tmp / "runtime-secrets"
        files.mkdir(exist_ok=True, mode=0o700)
        for name in ("app_secret_key", "s3_access_key", "s3_secret_key"):
            (files / name).write_text(self.secret(name))
        return {
            "AICD_ENVIRONMENT": "development",
            "AICD_DATABASE_URL": self.url("aicd_app"),
            "AICD_REDIS_URL": f"redis://:{self.secret('redis_password')}@127.0.0.1:{self.args.redis_port}/0",
            "AICD_SECRET_KEY_FILE": str(files / "app_secret_key"),
            "AICD_COOKIE_SECURE": "false",
            "AICD_ALLOWED_ORIGINS": self.origin,
            "AICD_REPOSITORY_ROOTS": f"main={self.repos}",
            "AICD_RATE_LIMIT_LOGIN_PER_IP_PER_MINUTE": "1000",
            "AICD_RATE_LIMIT_MUTATION_PER_MINUTE": "10000",
            "AICD_RATE_LIMIT_READ_PER_MINUTE": "100000",
            "AICD_LOG_LEVEL": "WARNING",
            "AICD_ARTIFACT_BACKEND": "s3",
            "AICD_ARTIFACT_S3_ENDPOINT": f"http://127.0.0.1:{self.args.s3_port}",
            "AICD_ARTIFACT_S3_BUCKET": BUCKET,
            "AICD_ARTIFACT_S3_ACCESS_KEY_FILE": str(files / "s3_access_key"),
            "AICD_ARTIFACT_S3_SECRET_KEY_FILE": str(files / "s3_secret_key"),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def worker_env(self) -> dict[str, str]:
        return self.app_env() | {
            "AICD_DATABASE_URL": self.url("aicd_worker"),
            "AICD_EXEC_ADAPTER": "fake",
            "AICD_FAKE_SCENARIO": "agent",
            "AICD_VERIFICATION_RUNNER": "local",
            "AICD_WORKTREE_ROOT": str(self.tmp / "worktrees"),
            "AICD_VERIFICATION_STAGING_ROOT": str(self.tmp / "staging"),
            "AICD_INDEX_EXPORT_CACHE_ROOT": str(self.tmp / "index-export"),
            "AICD_TICK_INTERVAL_SECONDS": "1",
            "AICD_OUTBOX_POLL_SECONDS": "0.2",
            "AICD_STALE_READY_SECONDS": "5",
        }

    # --- шаги -------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def step(self, name: str) -> Iterator[Step]:
        record = Step(name, now())
        self.steps.append(record)
        print(f"[dr] {name} …", flush=True)
        try:
            yield record
        except BaseException as exc:
            record.ok = False
            record.detail = f"{type(exc).__name__}: {exc}"[:2000]
            raise
        finally:
            record.finished = now()
            print(f"[dr]   {'ok' if record.ok else 'FAILED'} {record.finished - record.started:.1f}s {record.detail[:300]}",
                  flush=True)  # fmt: skip

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))
        print(f"[dr] CHECK {'PASS' if ok else 'FAIL'} {name}: {detail[:300]}", flush=True)

    # --- контейнеры -------------------------------------------------------------------------------------

    def cleanup_docker(self) -> None:
        for name in (PG, REDIS, S3):
            docker("rm", "-f", name, check=False)
        for volume in VOLUMES:
            docker("volume", "rm", "-f", volume, check=False)
        docker("network", "rm", NET, check=False)

    def make_secrets(self, target: Path) -> None:
        target.mkdir(mode=0o755)
        for name in SECRET_NAMES:
            value = secrets.token_urlsafe(32) if name != "app_secret_key" else secrets.token_urlsafe(48)
            path = target / name
            path.write_text(value)
            path.chmod(0o644)  # одноразовый стенд: postgres-init в контейнере читает секреты от uid 70

    def start_postgres(self, data_volume: str, wal_volume: str, restore_volume: str | None = None) -> None:
        args = ["run", "-d", "--name", PG, "--network", NET, "-p", f"127.0.0.1:{self.args.pg_port}:5432"]
        args += ["-e", "POSTGRES_USER=aicd_admin", "-e", "POSTGRES_PASSWORD_FILE=/run/secrets/pg_admin_password"]
        args += ["-e", "POSTGRES_DB=aicd", "-v", f"{self.secrets_dir}:/run/secrets:ro"]
        args += ["-v", f"{ROOT}/infrastructure/docker/postgres-init:/docker-entrypoint-initdb.d:ro"]
        args += ["-v", f"{ROOT}/infrastructure/postgres:/aicd-bootstrap:ro"]
        args += ["-v", f"{data_volume}:/var/lib/postgresql/data", "-v", f"{wal_volume}:{WAL_ARCHIVE_MOUNT}"]
        if restore_volume:
            args += ["-v", f"{restore_volume}:{WAL_RESTORE_MOUNT}:ro"]
        entry = f'install -d -o postgres -g postgres -m 0700 {WAL_ARCHIVE_MOUNT} && exec docker-entrypoint.sh "$@"'
        args += ["--entrypoint", "/bin/sh", self.pg_image, "-c", entry, "aicd-postgres", "postgres"]
        args += WalArchiveConfig(archive_timeout_seconds=self.args.archive_timeout).server_args()
        docker(*args)
        deadline = now() + 180
        last = ""
        while now() < deadline:
            try:
                with psycopg.connect(self.url("aicd_admin", "postgres"), connect_timeout=2, autocommit=True) as conn:
                    row = conn.execute(
                        "SELECT pg_is_in_recovery(), EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aicd_migrator')"
                    ).fetchone()
                    if row is not None and row[0] is False and row[1] is True:
                        return
                    last = f"in_recovery={row[0] if row else '?'}"
            except psycopg.Error as exc:
                last = str(exc)[:200]
            time.sleep(0.5)
        raise RuntimeError(
            f"PostgreSQL не готов за 180 с: {last}; логи: {docker('logs', '--tail', '40', PG, check=False)}"
        )

    def start_redis(self) -> None:
        docker("run", "-d", "--name", REDIS, "--network", NET, "-p", f"127.0.0.1:{self.args.redis_port}:6379",
               self.redis_image, "redis-server", "--appendonly", "no", "--requirepass", self.secret("redis_password"))  # fmt: skip

    def start_s3(self, volume: str) -> None:
        docker("run", "-d", "--name", S3, "--network", NET, "-p", f"127.0.0.1:{self.args.s3_port}:9000",
               "-e", f"RUSTFS_ACCESS_KEY={self.secret('s3_access_key')}",
               "-e", f"RUSTFS_SECRET_KEY={self.secret('s3_secret_key')}", "-e", "RUSTFS_VOLUMES=/data",
               "-v", f"{volume}:/data", S3_IMAGE)  # fmt: skip
        from scripts.backup.artifacts import S3Source

        source = S3Source(f"http://127.0.0.1:{self.args.s3_port}", BUCKET, self.secret("s3_access_key"),
                          self.secret("s3_secret_key"))  # fmt: skip
        deadline = now() + 90
        while True:
            try:
                source.ensure_bucket()
                return
            except Exception as exc:
                if now() > deadline:
                    raise RuntimeError(f"S3 не готов: {exc}") from exc
                time.sleep(0.5)

    # --- процессы приложения ----------------------------------------------------------------------------

    def spawn(self, name: str, cmd: list[str], env: dict[str, str]) -> None:
        log = (self.logs / f"{name}.log").open("ab")
        self.procs[name] = subprocess.Popen(  # noqa: S603
            cmd, cwd=ROOT, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT
        )

    def start_api(self) -> None:
        self.spawn("api", [sys.executable, "-m", "uvicorn", "aicd_api.main:create_app", "--factory", "--host",
                           "127.0.0.1", "--port", str(self.args.api_port), "--no-access-log"], self.app_env())  # fmt: skip
        deadline = now() + 90
        last = ""
        while now() < deadline:
            if self.procs["api"].poll() is not None:
                raise RuntimeError(f"API завершился: {self.tail('api')}")
            try:
                r = httpx.get(f"{self.origin}/api/v1/health/ready", timeout=2, trust_env=False)
                if r.status_code == 200:
                    return
                last = f"{r.status_code} {r.text[:300]}"
            except httpx.HTTPError as exc:
                last = str(exc)
            time.sleep(0.3)
        raise RuntimeError(f"API не готов: {last}; {self.tail('api')}")

    def start_worker(self, kind: str) -> None:
        self.spawn(kind, [sys.executable, "-m", "aicd_worker.main", kind], self.worker_env())

    def stop(self, name: str, *, hard: bool = False) -> None:
        proc = self.procs.pop(name, None)
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGKILL if hard else signal.SIGTERM)
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(10)

    def tail(self, name: str, lines: int = 30) -> str:
        path = self.logs / f"{name}.log"
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:]) if path.exists() else ""

    # --- API ---------------------------------------------------------------------------------------------

    def client(self, email: str) -> httpx.Client:
        client = httpx.Client(base_url=self.origin, timeout=30, trust_env=False)
        r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        r.raise_for_status()
        client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        client.headers["Origin"] = self.origin
        return client

    @staticmethod
    def idem() -> dict[str, str]:
        return {"Idempotency-Key": "dr-" + secrets.token_hex(8)}

    def create_owner(self, email: str, slug: str) -> str:
        out = sh(sys.executable, "-m", "aicd_cli.main", "admin", "create-owner", "--email", email, "--name",
                 f"DR {slug}", "--workspace-slug", slug, "--workspace-name", f"DR {slug}", "--password-stdin",
                 env={**os.environ, **self.app_env()}, input_text=PASSWORD + "\n").stdout  # fmt: skip
        return out.rsplit("(", 1)[1].split(")", 1)[0]

    def probe_providers(self, client: httpx.Client, ws: str) -> list[str]:
        providers = client.get("/api/v1/providers", headers={"X-Workspace-Id": ws})
        providers.raise_for_status()
        done = []
        for item in providers.json()["items"]:
            r = client.post(f"/api/v1/providers/{item['provider_id']}/probe",
                            headers={"X-Workspace-Id": ws, **self.idem()})  # fmt: skip
            r.raise_for_status()
            url = r.json()["status_url"]
            deadline = now() + 120
            while True:
                status = client.get(url, headers={"X-Workspace-Id": ws}).json()
                if status["status"] == "COMPLETED":
                    done.append(f"{item['code']}:{status['installation']['auth_status']}")
                    break
                if status["status"] in ("FAILED", "EXPIRED") or now() > deadline:
                    raise RuntimeError(f"probe {item['code']}: {status} {self.tail('exec')}")
                time.sleep(0.5)
        return done

    def local_profiles(self, ws: str) -> None:
        """Профили workspace без gates «только в контейнере» (gates исполняет LocalProcessRunner стенда)."""
        container_only = sorted(name for name, spec in GATE_SPECS.items() if spec.image_key is not None)
        with psycopg.connect(self.url("aicd_admin"), autocommit=True, options="-c search_path=aicd") as conn:
            for code in ("basic", "strict", "critical"):
                profile = uuid7()
                conn.execute(
                    "INSERT INTO aicd.quality_profiles (id, workspace_id, code, version, definition)"
                    " SELECT %s, %s, code, 1, definition FROM aicd.quality_profiles"
                    " WHERE workspace_id IS NULL AND code = %s AND version = 1",
                    (profile, ws, code),
                )
                conn.execute(
                    "INSERT INTO aicd.quality_gates (id, workspace_id, profile_id, name, gate_type, required, config,"
                    " timeout_seconds) SELECT overlay(overlay(md5(random()::text || g.id::text) PLACING '7' FROM 13)"
                    " PLACING '8' FROM 17)::uuid, %s, %s, g.name, g.gate_type, g.required, g.config, g.timeout_seconds"
                    " FROM aicd.quality_gates g JOIN aicd.quality_profiles p ON p.id = g.profile_id"
                    " WHERE p.workspace_id IS NULL AND p.code = %s AND p.version = 1 AND g.name <> ALL(%s)",
                    (ws, profile, code, container_only),
                )

    def new_project(self, client: httpx.Client, ws: str, name: str, *, with_repo: bool) -> str:
        r = client.post("/api/v1/projects", json={"name": name}, headers={"X-Workspace-Id": ws, **self.idem()})
        r.raise_for_status()
        project = str(r.json()["id"])
        if with_repo:
            repo = self.repos / f"repo-{secrets.token_hex(3)}"
            git_init(repo)
            r = client.post(f"/api/v1/projects/{project}/repositories",
                            json={"provider": "local", "local_path": str(repo)}, headers=self.idem())  # fmt: skip
            r.raise_for_status()
        return project

    def create_run(self, client: httpx.Client, project: str, objective: str) -> str:
        r = client.post("/api/v1/runs", json={"project_id": project, "mode": "DEVELOP", "objective": objective},
                        headers=self.idem())  # fmt: skip
        r.raise_for_status()
        return str(r.json()["run_id"])

    def run_status(self, client: httpx.Client, run_id: str) -> str:
        r = client.get(f"/api/v1/runs/{run_id}")
        r.raise_for_status()
        return str(r.json()["status"])

    def wait_run(self, client: httpx.Client, run_id: str, timeout: float = 300) -> str:
        deadline = now() + timeout
        status = ""
        while now() < deadline:
            status = self.run_status(client, run_id)
            if status in TERMINAL:
                return status
            time.sleep(1)
        return status

    # --- база -------------------------------------------------------------------------------------------

    def table_counts(self) -> dict[str, int]:
        with psycopg.connect(self.url("aicd_admin"), autocommit=True) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT relname FROM pg_class WHERE relnamespace = 'aicd'::regnamespace AND relkind IN ('r','p')"
                " AND NOT relispartition ORDER BY relname").fetchall()]  # fmt: skip
            return {t: int(conn.execute(f'SELECT count(*) FROM aicd."{t}"').fetchone()[0]) for t in tables}  # type: ignore[index]

    def rls_facts(self) -> dict[str, Any]:
        with psycopg.connect(self.url("aicd_admin"), autocommit=True) as conn:
            forced = conn.execute(
                "SELECT count(*) FILTER (WHERE relrowsecurity AND relforcerowsecurity), count(*) FROM pg_class"
                " WHERE relnamespace = 'aicd'::regnamespace AND relkind IN ('r','p') AND NOT relispartition"
            ).fetchone()
            bypass = conn.execute(
                "SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname LIKE 'aicd_%' ORDER BY rolname"
            ).fetchall()
        return {"rls_forced_tables": forced[0] if forced else None, "tables": forced[1] if forced else None,
                "bypassrls": {r[0]: r[1] for r in bypass}}  # fmt: skip

    def rls_probe(self, ws_a: str, ws_b: str, project_b: str) -> dict[str, int]:
        with psycopg.connect(self.url("aicd_app")) as conn:
            no_ctx = conn.execute("SELECT count(*) FROM projects").fetchone()
            conn.execute("SELECT set_config('app.workspace_id', %s, true), set_config('app.actor_type', 'user', true)",
                         (ws_a,))  # fmt: skip
            in_a = conn.execute("SELECT count(*) FROM projects").fetchone()
            b_from_a = conn.execute("SELECT count(*) FROM projects WHERE id = %s", (project_b,)).fetchone()
            conn.rollback()
            conn.execute("SELECT set_config('app.workspace_id', %s, true), set_config('app.actor_type', 'user', true)",
                         (ws_b,))  # fmt: skip
            in_b = conn.execute("SELECT count(*) FROM projects WHERE id = %s", (project_b,)).fetchone()
            conn.rollback()
        return {"no_context": no_ctx[0] if no_ctx else -1, "ws_a_projects": in_a[0] if in_a else -1,
                "ws_b_project_seen_from_a": b_from_a[0] if b_from_a else -1,
                "ws_b_project_seen_from_b": in_b[0] if in_b else -1}  # fmt: skip

    # --- бэкап-утилита ----------------------------------------------------------------------------------

    def backup(self, *args: str, check: bool = True) -> dict[str, Any]:
        env = {**os.environ, "AICD_BACKUP_REPO": str(self.backup_repo),
               "AICD_BACKUP_KEY_FILE": str(self.key_dir / "backup.key"),
               "AICD_BACKUP_STATUS_FILE": str(self.status_file)}  # fmt: skip
        proc = sh(sys.executable, "-m", "scripts.backup", *args, env=env, check=False, timeout=1800)
        try:
            result: dict[str, Any] = json.loads(proc.stdout)
        except ValueError:
            result = {"ok": False, "error": proc.stdout[-1000:] + proc.stderr[-1000:]}
        if check and (proc.returncode != 0 or not result.get("ok")):
            raise RuntimeError(f"backup {args[0]}: {json.dumps(result, ensure_ascii=False)[:1500]}")
        return result

    def pg_args(self, user: str, password: str | None = None) -> list[str]:
        """password=None — режим `docker exec` в контейнер сервера (суперпользователь по unix-сокету)."""
        if password is None:
            return ["--pg-container", PG, "--pg-user", user]
        return ["--pg-host", PG, "--pg-port", "5432", "--pg-user", user, "--pg-password-file",
                str(self.secrets_dir / password), "--pg-network", NET]  # fmt: skip

    def s3_args(self) -> list[str]:
        files = self.tmp / "runtime-secrets"
        return ["--s3-endpoint", f"http://127.0.0.1:{self.args.s3_port}", "--s3-bucket", BUCKET,
                "--s3-access-key-file", str(files / "s3_access_key"), "--s3-secret-key-file",
                str(files / "s3_secret_key")]  # fmt: skip

    def maintenance_dsn_file(self) -> Path:
        path = self.tmp / "runtime-secrets" / "maintenance_dsn"
        path.write_text(self.url("aicd_maintenance"))
        path.chmod(0o600)
        return path


def git_init(path: Path) -> None:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(path), "PATH": os.environ["PATH"]}
    path.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", str(path), env=env)
    (path / "README.md").write_text("DR drill repository\n")
    sh("git", "-C", str(path), "add", ".", env=env)
    sh("git", "-C", str(path), "-c", "user.name=dr", "-c", "user.email=dr@example.test", "commit", "-qm", "init",
       env=env)  # fmt: skip


class Heartbeat:
    """Непрерывная запись строк (dr_drill.heartbeat) до аварии: RPO = потерянное окно подтверждённых коммитов."""

    def __init__(self, url: str, interval: float) -> None:
        self.url = url
        self.interval = interval
        self.stop = threading.Event()
        self.last_acked: tuple[int, float] | None = None
        self.acked = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def setup(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS dr_drill")
            conn.execute("CREATE TABLE dr_drill.heartbeat (id bigserial PRIMARY KEY,"
                         " committed_at timestamptz NOT NULL DEFAULT clock_timestamp())")  # fmt: skip

    def _run(self) -> None:
        try:
            with psycopg.connect(self.url, autocommit=True, connect_timeout=2) as conn:
                while not self.stop.is_set():
                    row = conn.execute("INSERT INTO dr_drill.heartbeat DEFAULT VALUES RETURNING id").fetchone()
                    if row is not None:
                        self.last_acked = (int(row[0]), now())  # подтверждён клиенту (autocommit)
                        self.acked += 1
                    time.sleep(self.interval)
        except psycopg.Error:
            return  # сервер убит — ожидаемо


class WalSyncLoop:
    """Периодические `pg-wal-sync` и инкрементальный `artifacts` (в эксплуатации — systemd timer, см. runbook)."""

    def __init__(self, drill: Drill, volume: str, interval: float) -> None:
        self.drill = drill
        self.volume = volume
        self.interval = interval
        self.stop = threading.Event()
        self.runs: list[tuple[float, bool, int]] = []
        self.artifact_runs: list[tuple[float, bool, int]] = []
        self.artifacts_args: list[str] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(self.interval):
            result = self.drill.backup("pg-wal-sync", "--wal-volume", self.volume, check=False)
            self.runs.append((now(), bool(result.get("ok")), len(result.get("synced", []))))
            if self.artifacts_args:  # инкрементальный снимок артефактов тем же таймером
                result = self.drill.backup("artifacts", *self.artifacts_args, check=False)
                self.artifact_runs.append((now(), bool(result.get("ok")), int(result.get("new_objects", 0))))


def run_drill(drill: Drill) -> None:
    a = drill.args
    facts = drill.facts
    facts["images"] = {"postgres": drill.pg_image, "redis": drill.redis_image, "s3": S3_IMAGE}
    facts["archive_timeout_s"] = a.archive_timeout
    facts["wal_sync_interval_s"] = a.sync_interval

    # 1. Стенд ------------------------------------------------------------------------------------------
    with drill.step("стенд: сеть, секреты, PostgreSQL (архив WAL), Redis, S3"):
        drill.cleanup_docker()
        docker("network", "create", NET)
        drill.make_secrets(drill.secrets_dir)
        drill.start_postgres("aicd-dr-pgdata", "aicd-dr-pgwal")
        drill.start_redis()
        drill.start_s3("aicd-dr-s3data")
        facts["server_version"] = docker("exec", PG, "postgres", "--version")
    with drill.step("миграции Alembic (aicd_migrator)"):
        env = {**os.environ, "AICD_MIGRATOR_DATABASE_URL": drill.url("aicd_migrator")}
        sh(sys.executable, "-m", "alembic", "-c", str(ROOT / "infrastructure/migrations/alembic.ini"), "upgrade",
           "head", env=env)  # fmt: skip
        with psycopg.connect(drill.url("aicd_admin"), autocommit=True) as conn:
            row = conn.execute("SELECT version_num FROM aicd.alembic_version").fetchone()
        facts["alembic_head"] = row[0] if row else None
    with drill.step("приложение: API + воркеры outbox/orchestrator/exec"):
        drill.start_api()
        for kind in ("outbox", "orchestrator", "exec"):
            drill.start_worker(kind)
    with drill.step("данные через приложение: владельцы (aicd CLI), probe CLI, проекты, run №1"):
        ws_a = drill.create_owner("dr-owner-a@example.test", "dr-a")
        ws_b = drill.create_owner("dr-owner-b@example.test", "dr-b")
        client_a = drill.client("dr-owner-a@example.test")
        client_b = drill.client("dr-owner-b@example.test")
        facts["probes"] = drill.probe_providers(client_a, ws_a)
        # Версия Fake CLI не входит в матрицу совместимости (probe → `untested`, Router её исключает — верно для
        # production); стенд помечает установки `supported`, как live-стенд E2E (tests/support/phase3.install_cli).
        with psycopg.connect(drill.url("aicd_admin"), autocommit=True) as conn:
            conn.execute("UPDATE aicd.cli_installations SET compatibility = 'supported'")
        drill.local_profiles(ws_a)
        project_a = drill.new_project(client_a, ws_a, "DR проект A", with_repo=True)
        project_b = drill.new_project(client_b, ws_b, "DR проект B", with_repo=False)
        run1 = drill.create_run(client_a, project_a, "DR: run №1 до бэкапа")
        status1 = drill.wait_run(client_a, run1)
        facts |= {"ws_a": ws_a, "ws_b": ws_b, "project_b": project_b, "run1": run1, "run1_status": status1}
        if status1 != "COMPLETED":
            raise RuntimeError(f"run №1: {status1}; exec: {drill.tail('exec')}")

    # 2. Бэкап ------------------------------------------------------------------------------------------
    with drill.step("run №2 «в полёте»: исполнитель остановлен"):
        drill.stop("exec")
        run2 = drill.create_run(client_a, project_a, "DR: run №2 в полёте во время аварии")
        time.sleep(4)
        facts["run2"] = run2
        facts["run2_status_at_backup"] = drill.run_status(client_a, run2)
    with drill.step("бэкап: ключ (офлайн), репозиторий, pg-base, pg-dump, artifacts, secrets") as st:
        facts["keygen"] = drill.backup("keygen", "--out", str(drill.key_dir / "backup.key"))["key_id"]
        drill.backup("init")
        base = drill.backup("pg-base", *drill.pg_args("aicd_admin"))
        facts["base_backup"] = {k: base[k] for k in ("backup_id", "start_lsn", "end_lsn", "start_wal_segment")}
        facts["base_backup"]["size_bytes"] = base["files"][0]["ciphertext_bytes"]
        dump = drill.backup("pg-dump", *drill.pg_args("aicd_maintenance", "pg_maintenance_password"), check=False)
        facts["dump_role"] = "aicd_maintenance"
        if not dump.get("ok"):
            facts["dump_maintenance_error"] = str(dump.get("error"))[:600]
            dump = drill.backup("pg-dump", *drill.pg_args("aicd_admin"))
            facts["dump_role"] = "aicd_admin (fallback)"
        facts["dump_backup_id"] = dump["backup_id"]
        facts["counts_at_dump"] = drill.table_counts()
        arts = drill.backup("artifacts", *drill.s3_args(), "--maintenance-dsn-file", str(drill.maintenance_dsn_file()))
        facts["artifacts_backup"] = {k: arts[k] for k in ("backup_id", "objects", "new_objects", "rows_checked", "ok")}
        sec = drill.backup("secrets", "--dir", str(drill.secrets_dir))
        facts["secrets_backup"] = sec["files"]
        drill.backup("verify")
        st.detail = f"base {base['backup_id']}, artifacts ok={arts['ok']}/{arts['rows_checked']}"
    wal_loop = WalSyncLoop(drill, "aicd-dr-pgwal", a.sync_interval)
    wal_loop.artifacts_args = [*drill.s3_args(), "--maintenance-dsn-file", str(drill.maintenance_dsn_file())]
    wal_loop.thread.start()
    heartbeat = Heartbeat(drill.url("aicd_admin"), a.heartbeat_interval)
    with drill.step("после бэкапа: run №3 (есть только в WAL), heartbeat, непрерывный pg-wal-sync"):
        heartbeat.setup()
        heartbeat.thread.start()
        run3 = drill.create_run(client_a, project_a, "DR: run №3 создан после базового бэкапа")
        facts["run3"] = run3
        time.sleep(a.archive_timeout + a.sync_interval + 3)  # run №3 успевает попасть в архив и в репозиторий
        facts["counts_before_crash"] = drill.table_counts()
        facts["rls_before"] = drill.rls_facts()
        time.sleep(a.post_backup_seconds)

    # 3. Авария -----------------------------------------------------------------------------------------
    with drill.step("АВАРИЯ: kill процессов и PostgreSQL, удаление томов/контейнеров/секретов/рабочих каталогов"):
        wal_loop.stop.set()
        wal_loop.thread.join(timeout=120)
        for name in list(drill.procs):
            drill.stop(name, hard=True)
        client_a.close()
        client_b.close()
        docker("kill", "-s", "KILL", PG)
        crash = now()
        heartbeat.stop.set()
        heartbeat.thread.join(timeout=10)
        for name in (PG, REDIS, S3):
            docker("rm", "-f", name)
        for volume in ("aicd-dr-pgdata", "aicd-dr-pgwal", "aicd-dr-s3data"):
            docker("volume", "rm", volume)
        shutil.rmtree(drill.secrets_dir)
        shutil.rmtree(drill.tmp / "runtime-secrets")
        for sub in ("worktrees", "staging", "index-export"):
            shutil.rmtree(drill.tmp / sub, ignore_errors=True)
        facts["crash_at"] = ts(crash)
        facts["heartbeat_last_acked"] = (
            {"id": heartbeat.last_acked[0], "at": ts(heartbeat.last_acked[1])} if heartbeat.last_acked else None
        )
        facts["wal_sync_runs"] = {"total": len(wal_loop.runs), "ok": sum(1 for r in wal_loop.runs if r[1]),
                                  "last_ok_at": ts(max((r[0] for r in wal_loop.runs if r[1]), default=0))}  # fmt: skip
        facts["artifact_sync_runs"] = {"total": len(wal_loop.artifact_runs),
                                       "ok": sum(1 for r in wal_loop.artifact_runs if r[1]),
                                       "new_objects": sum(r[2] for r in wal_loop.artifact_runs),
                                       "last_ok_at": ts(max((r[0] for r in wal_loop.artifact_runs if r[1]), default=0))}  # fmt: skip

    # 4. Восстановление ---------------------------------------------------------------------------------
    with drill.step("восстановление: секреты из бэкапа"):
        restored = drill.backup("restore-secrets", "--to", str(drill.secrets_dir))
        drill.secrets_dir.chmod(0o755)
        for name in restored["restored"]:
            (drill.secrets_dir / name).chmod(0o644)  # см. make_secrets: только для одноразового стенда
    with drill.step("восстановление: PostgreSQL (base + WAL → promote), post-restore") as st:
        docker("network", "inspect", NET)
        rp = drill.backup(
            "restore-pg", "--data-volume", "aicd-dr-pgdata-restored", "--wal-volume", "aicd-dr-walrestore"
        )
        drill.start_postgres("aicd-dr-pgdata-restored", "aicd-dr-pgwal-restored", restore_volume="aicd-dr-walrestore")
        post = drill.backup("post-restore", *drill.pg_args("aicd_admin"))
        facts["restore_pg"] = {"backup_id": rp["backup_id"], "wal_staged": rp["wal_staged"],
                               "pg_verifybackup": rp["pg_verifybackup"], "post_restore": {
                                   k: post[k] for k in ("fencing_seq", "timeline", "alembic_revision")}}  # fmt: skip
        st.detail = f"timeline {post['timeline']}, WAL {rp['wal_staged']['segments']} сегм."
    pg_ready = now()
    with drill.step("восстановление: Redis (пустой), S3 + артефакты, проверка sha256"):
        drill.start_redis()
        drill.start_s3("aicd-dr-s3data-restored")
        drill.app_env()  # пересоздаёт runtime-secrets из восстановленных секретов
        ra = drill.backup("restore-artifacts", *drill.s3_args())
        va = drill.backup("verify-artifacts", *drill.s3_args(), "--maintenance-dsn-file",
                          str(drill.maintenance_dsn_file()), "--snapshot", "latest", check=False)  # fmt: skip
        report = va.get("detail") or va
        facts["restore_artifacts"] = {"restored": ra["restored"], "snapshot": ra["backup_id"], "verify": {
            k: report.get(k) for k in ("ok", "rows_checked", "missing", "mismatched", "missing_after_snapshot",
                                       "orphans")}}  # fmt: skip
    with drill.step("восстановление: API и воркеры"):
        drill.start_api()
        for kind in ("outbox", "orchestrator", "exec"):
            drill.start_worker(kind)
    service_ready = now()

    # 5. Проверки ---------------------------------------------------------------------------------------
    with drill.step("проверки: RPO, строки, RLS, артефакты, вход, run продолжаются"):
        with psycopg.connect(drill.url("aicd_admin"), autocommit=True) as conn:
            hb = conn.execute("SELECT max(id), max(committed_at), count(*) FROM dr_drill.heartbeat").fetchone()
        assert hb is not None and heartbeat.last_acked is not None
        lost = heartbeat.last_acked[0] - int(hb[0] or 0)
        restored_last = hb[1].timestamp() if hb[1] else 0.0
        rpo = max(heartbeat.last_acked[1] - restored_last, 0.0)
        facts["rpo"] = {"last_acked_id": heartbeat.last_acked[0], "restored_max_id": hb[0], "lost_commits": lost,
                        "restored_last_commit_at": ts(restored_last), "rpo_seconds": round(rpo, 2),
                        "rpo_bound_seconds": a.archive_timeout + a.sync_interval}  # fmt: skip
        drill.check("RPO в пределах archive_timeout + интервал wal-sync",
                    rpo <= a.archive_timeout + a.sync_interval + 2,
                    f"RPO {rpo:.1f} с, потеряно {lost} из {heartbeat.acked} подтверждённых heartbeat-коммитов")  # fmt: skip

        after = drill.table_counts()
        before = facts["counts_before_crash"]
        diffs = {
            t: (before.get(t), after.get(t)) for t in sorted(set(before) | set(after)) if before.get(t) != after.get(t)
        }
        facts["counts_after_restore"] = after
        drill.check("счётчики строк всех таблиц aicd = снимок перед аварией", not diffs,
                    f"{len(after)} таблиц, {sum(after.values())} строк; расхождения: {diffs or 'нет'}")  # fmt: skip

        rls_after = drill.rls_facts()
        probe = drill.rls_probe(facts["ws_a"], facts["ws_b"], facts["project_b"])
        facts["rls_after"] = rls_after | {"probe": probe}
        rls_ok = (rls_after["rls_forced_tables"] == facts["rls_before"]["rls_forced_tables"]
                  and not rls_after["bypassrls"].get("aicd_app") and not rls_after["bypassrls"].get("aicd_worker")
                  and probe["no_context"] == 0 and probe["ws_b_project_seen_from_a"] == 0
                  and probe["ws_b_project_seen_from_b"] == 1 and probe["ws_a_projects"] >= 1)  # fmt: skip
        drill.check("RLS: ENABLE+FORCE и роли сохранены, чужой workspace не виден, без контекста — 0 строк", rls_ok,
                    json.dumps(probe | {"forced": rls_after["rls_forced_tables"]}, ensure_ascii=False))  # fmt: skip

        verify = facts["restore_artifacts"]["verify"]
        after_snapshot = verify.get("missing_after_snapshot") or []
        drill.check("артефакты: объекты снимка восстановлены, sha256 = artifacts.sha256, повреждений нет",
                    verify.get("missing") == [] and verify.get("mismatched") == []
                    and verify.get("ok") == (verify.get("rows_checked") or 0) - len(after_snapshot),
                    json.dumps(verify, ensure_ascii=False))  # fmt: skip
        facts["artifacts_rpo_loss"] = len(after_snapshot)

        client = drill.client("dr-owner-a@example.test")
        drill.check(
            "вход пользователя после восстановления (хэш пароля и сессии из БД)", True, "POST /auth/login → 200"
        )
        ready = httpx.get(f"{drill.origin}/api/v1/health/ready", timeout=5, trust_env=False)
        drill.check("health/ready приложения", ready.status_code == 200, ready.text[:200])
        status1 = drill.run_status(client, facts["run1"])
        with psycopg.connect(drill.url("aicd_admin"), autocommit=True) as conn:
            art = conn.execute("SELECT id, sha256 FROM aicd.artifacts WHERE run_id = %s ORDER BY created_at LIMIT 1",
                               (facts["run1"],)).fetchone()  # fmt: skip
        download_ok = False
        if art is not None:
            r = client.get(f"/api/v1/artifacts/{art[0]}/download", headers={"X-Workspace-Id": facts["ws_a"]})
            download_ok = r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == art[1]
        drill.check("run №1 COMPLETED и его артефакт скачивается через API с тем же sha256",
                    status1 == "COMPLETED" and download_ok, f"status={status1}, download_sha_ok={download_ok}")  # fmt: skip

        for run_key in ("run2", "run3"):
            status = drill.wait_run(client, facts[run_key], timeout=a.run_timeout)
            facts[f"{run_key}_final"] = status
            facts[f"{run_key}_completed_at"] = ts(now())
            drill.check(f"{'run №2 (в полёте при бэкапе)' if run_key == 'run2' else 'run №3 (только в WAL)'}"
                        " продолжается после восстановления до COMPLETED", status == "COMPLETED",
                        f"status={status}")  # fmt: skip
        runs_done = now()
        client.close()

        with psycopg.connect(drill.url("aicd_admin"), autocommit=True) as conn:
            tokens = conn.execute(
                "SELECT GREATEST((SELECT coalesce(max(fencing_token),0) FROM aicd.worker_leases),"
                " (SELECT coalesce(max(fencing_token),0) FROM aicd.task_attempts))").fetchone()  # fmt: skip
        fencing = facts["restore_pg"]["post_restore"]["fencing_seq"]
        drill.check("fencing token сдвинут после PITR (новые аренды > любых прежних + 1e6)",
                    tokens is not None and int(tokens[0]) > fencing - 1, f"seq={fencing}, max token сейчас={tokens}")  # fmt: skip

        dump_check = sh(
            "docker", "exec", PG, "sh", "-c", "createdb -U aicd_admin aicd_dumpcheck && echo ok", check=False
        )
        restore_log = ""
        if dump_check.returncode == 0:
            env = {
                **os.environ,
                "AICD_BACKUP_REPO": str(drill.backup_repo),
                "AICD_BACKUP_KEY_FILE": str(drill.key_dir / "backup.key"),
            }
            restore_log = dump_restore(drill, facts["dump_backup_id"], env)
        counts_dump = facts["counts_at_dump"]
        with psycopg.connect(drill.url("aicd_admin", "aicd_dumpcheck"), autocommit=True) as conn:
            mismatched = []
            for table, count in counts_dump.items():
                got = conn.execute(f'SELECT count(*) FROM aicd."{table}"').fetchone()
                if got is None or int(got[0]) != count:
                    mismatched.append((table, count, got[0] if got else None))
        drill.check("логический дамп восстанавливается (pg_restore) со строками на момент дампа", not mismatched,
                    f"роль дампа: {facts['dump_role']}; расхождения: {mismatched or 'нет'}; {restore_log[-200:]}")  # fmt: skip

    facts["rto"] = {
        "crash_at": facts["crash_at"],
        "postgres_ready_seconds": round(pg_ready - crash, 1),
        "service_ready_seconds": round(service_ready - crash, 1),
        "runs_continued_seconds": round(runs_done - crash, 1),
    }


def dump_restore(drill: Drill, backup_id: str, env: dict[str, str]) -> str:
    """Расшифровка дампа → `pg_restore` в отдельную БД восстановленного сервера (через stdin контейнера)."""
    from scripts.backup.crypto import BackupKey, decrypt_file
    from scripts.backup.repo import Repository

    repo = Repository.open(Path(env["AICD_BACKUP_REPO"]), BackupKey.load(Path(env["AICD_BACKUP_KEY_FILE"])))
    manifest = repo.read_manifest("dump", backup_id)
    entry = manifest["files"][0]
    path = repo.set_dir("dump", backup_id) / entry["path"]
    proc = subprocess.Popen(  # noqa: S603
        ["docker", "exec", "-i", PG, "pg_restore", "-U", "aicd_admin", "-d", "aicd_dumpcheck", "--exit-on-error"],  # noqa: S607
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None
    for chunk in decrypt_file(repo.key, f"dump/{backup_id}/{entry['name']}", path):
        proc.stdin.write(chunk)
    proc.stdin.close()
    out = proc.stdout.read().decode(errors="replace")
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"pg_restore → {code}: {out[-1500:]}")
    return f"pg_restore exit 0 {out[-200:]}"


def render_report(drill: Drill, ok: bool, error: str | None) -> str:
    f = drill.facts
    lines = [
        "# DR drill — AiCodingDev (P9-05, NFR-REL-03)",
        "",
        f"**Итог: {'PASS' if ok else 'FAIL'}** · {ts(drill.steps[0].started) if drill.steps else ''} · "
        f"команда: `uv run python -m scripts.dr.drill` · сгенерировано скриптом (`scripts/dr/drill.py`), числа — измерены в этом прогоне.",
        "",
    ]
    if error:
        lines += ["## Ошибка", "", "```", error[-3000:], "```", ""]
    rpo, rto = f.get("rpo", {}), f.get("rto", {})
    lines += [
        "## RPO / RTO (измерено)",
        "",
        "| Показатель | Значение | Как измерено |",
        "|---|---|---|",
        f"| RPO | **{rpo.get('rpo_seconds', '—')} с** (потеряно {rpo.get('lost_commits', '—')} подтверждённых коммитов) "
        f"| heartbeat-коммиты каждые {drill.args.heartbeat_interval} с до `docker kill`; последний подтверждённый "
        f"{(f.get('heartbeat_last_acked') or {}).get('at', '—')}, последний восстановленный {rpo.get('restored_last_commit_at', '—')} |",
        f"| Граница RPO конфигурации | {rpo.get('rpo_bound_seconds', '—')} с | `archive_timeout`={f.get('archive_timeout_s')} с + интервал `pg-wal-sync`={f.get('wal_sync_interval_s')} с (production по умолчанию: 60 с + 60 с) |",
        f"| RTO: PostgreSQL готов | {rto.get('postgres_ready_seconds', '—')} с | от аварии до promote + post-restore |",
        f"| **RTO: сервис готов** | **{rto.get('service_ready_seconds', '—')} с** | от аварии до `/health/ready` = 200 (секреты, PG, Redis, S3+артефакты, API, воркеры) |",
        f"| RTO: run «в полёте» завершены | {rto.get('runs_continued_seconds', '—')} с | от аварии до COMPLETED run №2 и №3 |",
        "",
        "Время обнаружения аварии и решения оператора в RTO не входит (дрилл начинает восстановление сразу); "
        "шаги выполнялись скриптом без ручных пауз — у оператора по runbook RTO больше.",
        "",
        "## Проверки",
        "",
        "| Проверка | Итог | Детали |",
        "|---|---|---|",
    ]
    for c in drill.checks:
        lines.append(f"| {c.name} | {'PASS' if c.ok else '**FAIL**'} | {c.detail.replace('|', '/')[:400]} |")
    lines += ["", "## Шаги", "", "| Шаг | Начало (UTC) | Длительность, с | Итог |", "|---|---|---|---|"]
    for s in drill.steps:
        lines.append(
            f"| {s.name} | {ts(s.started)[11:23]} | {s.finished - s.started:.1f} | {'ok' if s.ok else 'FAILED: ' + s.detail[:200]} |"
        )
    lines += [
        "",
        "## Окружение и факты",
        "",
        f"* Образы (по digest): postgres `{f.get('images', {}).get('postgres')}`, redis `{f.get('images', {}).get('redis')}`, "
        f"S3 `{f.get('images', {}).get('s3')}`; `{f.get('server_version', '')}`; миграции до `{f.get('alembic_head')}`.",
        f"* Базовый бэкап: `{f.get('base_backup')}`.",
        f"* Логический дамп: роль `{f.get('dump_role')}`"
        + (
            f"; ошибка под aicd_maintenance: `{f.get('dump_maintenance_error')}`"
            if f.get("dump_maintenance_error")
            else ""
        )
        + ".",
        f"* Артефакты при бэкапе: `{f.get('artifacts_backup')}`; при восстановлении: `{f.get('restore_artifacts')}`.",
        f"* Секретов в бэкапе: {f.get('secrets_backup')}; учётные данные CLI не копируются (их нет в каталоге секретов, проверка имён — `scripts/backup/secrets_bundle.py`).",
        f"* Восстановление PG: `{f.get('restore_pg')}`.",
        f"* pg-wal-sync во время окна: `{f.get('wal_sync_runs')}`.",
        f"* Probe CLI (Fake CLI, development): {f.get('probes')}; run №1 `{f.get('run1_status')}`, run №2 при бэкапе `{f.get('run2_status_at_backup')}` → `{f.get('run2_final')}`, run №3 → `{f.get('run3_final')}`.",
        f"* RLS до: `{f.get('rls_before')}`; после: `{f.get('rls_after')}`.",
        "",
        "## Ограничения дрилла",
        "",
        "* Одна машина: «авария» удаляет контейнеры, тома PostgreSQL/архива WAL/S3, секреты и рабочие каталоги, но "
        "репозиторий бэкапов и ключ — на том же хосте (в эксплуатации — внешнее хранилище и офлайн-хранилище ключа).",
        "* CLI — Fake CLI (реальные Codex/Claude CLI в окружении не авторизованы); после настоящего восстановления "
        "оператор заново входит в CLI (тома учётных данных не бэкапятся, `docs/runbooks/cli-auth.md`).",
        "* Gates исполняются LocalProcessRunner без gates «только в контейнере» (как в live-стенде E2E).",
        "* Локальные Git-репозитории проектов — данные оператора вне бэкапа платформы (каталог `AICD_REPOS_DIR`).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "dr" / "DR_DRILL.md")
    parser.add_argument("--pg-port", type=int, default=55532)
    parser.add_argument("--redis-port", type=int, default=56479)
    parser.add_argument("--s3-port", type=int, default=59100)
    parser.add_argument("--api-port", type=int, default=4290)
    parser.add_argument("--archive-timeout", type=int, default=10, help="archive_timeout дрилла, с (production: 60)")
    parser.add_argument("--sync-interval", type=float, default=5.0, help="интервал pg-wal-sync, с (production: 60)")
    parser.add_argument("--heartbeat-interval", type=float, default=0.2)
    parser.add_argument("--post-backup-seconds", type=float, default=7.0, help="работа после run №3 до аварии")
    parser.add_argument("--run-timeout", type=float, default=300.0)
    parser.add_argument("--keep", action="store_true", help="не удалять контейнеры/тома/временный каталог")
    args = parser.parse_args(argv)
    drill = Drill(args)
    error: str | None = None
    try:
        run_drill(drill)
    except Exception as exc:  # noqa: BLE001 — отчёт об ошибке дрилла
        import traceback

        error = traceback.format_exc()
        print(f"[dr] ERROR {exc}", file=sys.stderr)
        for name in ("api", "outbox", "orchestrator", "exec"):
            tail = drill.tail(name, 15)
            if tail:
                error += f"\n--- {name}.log ---\n{tail}"
    finally:
        for name in list(drill.procs):
            drill.stop(name, hard=True)
        if not args.keep:
            drill.cleanup_docker()
    ok = error is None and bool(drill.checks) and all(c.ok for c in drill.checks)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(drill, ok, error), encoding="utf-8")
    payload = {"ok": ok, "facts": drill.facts, "checks": [c.__dict__ for c in drill.checks],
               "steps": [s.__dict__ for s in drill.steps], "error": error}  # fmt: skip
    args.report.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if not args.keep:
        shutil.rmtree(drill.tmp, ignore_errors=True)
    else:
        print(f"[dr] kept {drill.tmp}")
    print(f"[dr] {'PASS' if ok else 'FAIL'} → {args.report}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
