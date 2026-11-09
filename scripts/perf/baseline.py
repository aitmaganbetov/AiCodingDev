"""Базовый замер производительности (P9-04, NFR-PERF-01): воспроизводимый прогон на одноразовом стенде.

Стенд поднимается и удаляется скриптом: отдельная БД (роли и миграции — как у тестов, `tests/support/db.py`)
на PostgreSQL из `AICD_PERF_DATABASE_ADMIN_URL` (или `AICD_TEST_DATABASE_ADMIN_URL`), частный `redis-server`,
API (uvicorn, 1 процесс) и воркеры `outbox`/`index` — настоящие процессы приложения. Сценарии:

1. `api_reads_c{N}` — чтение: список run проекта, run, задачи run, список проектов (закрытый цикл, N клиентов);
2. `run_create_c{N}` — `POST /runs` с Idempotency-Key (команда + outbox в одной транзакции);
3. `sse_fanout_{N}` — N пользователей подписаны на поток событий одного run, генератор пишет события в outbox;
   задержка = момент получения клиентом − `occurred_at` события (часы одного хоста), доля доставленных;
4. `outbox_throughput` — пачка событий в outbox одной транзакцией → время, за которое воркер опубликовал все;
5. `indexing_medium_repo` — индексация копии репозитория AiCodingDev (HEAD) воркером `index` после attach.

Запуск: `uv run python -m scripts.perf.baseline --out reports/perf/results.json` (см. reports/perf/PERF_BASELINE.md).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import psycopg
from scripts.perf.loadgen import Stats, closed_loop, percentile
from tests.support import db as testdb  # служебные функции стенда (роли/миграции), как в CI
from tests.support.processes import RedisProcess, api_process, free_port, wait_ready, worker_process

from aicd_platform.ids import uuid7

ROOT = Path(__file__).resolve().parents[2]

PASSWORD = "Perf-Horse-Battery-9-x"


# ------------------------------------------------------------------------------------------- стенд
class Stand:
    def __init__(self, admin_url: str, work: Path) -> None:
        self.work = work
        self.db = testdb.create_database(admin_url)
        testdb.alembic(self.db, "upgrade", "head")
        self.redis = RedisProcess()
        self.redis.start()
        self.repos = work / "repos"
        self.repos.mkdir(parents=True)
        common = {
            "AICD_ENVIRONMENT": "test",
            "AICD_REDIS_URL": self.redis.url,
            "AICD_REPOSITORY_ROOTS": f"main={self.repos}",
            "AICD_ARTIFACT_FS_ROOT": str(work / "artifacts"),
            "AICD_LOG_LEVEL": "WARNING",
        }
        self.api = api_process({
            **common,
            "AICD_DATABASE_URL": self.db.url_for("aicd_app"),
            "AICD_SECRET_KEY": secrets.token_urlsafe(48),
            "AICD_COOKIE_SECURE": "false",
            "AICD_RATE_LIMIT_READ_PER_MINUTE": "10000000",
            "AICD_RATE_LIMIT_MUTATION_PER_MINUTE": "10000000",
            "AICD_RATE_LIMIT_LOGIN_PER_IP_PER_MINUTE": "10000000",
        }, port=free_port())  # fmt: skip
        worker_env = {**common, "AICD_DATABASE_URL": self.db.url_for("aicd_worker")}
        self.outbox = worker_process("outbox", worker_env)
        self.index = worker_process("index", {
            **worker_env, "AICD_INDEX_POLL_SECONDS": "0.5",
            "AICD_INDEX_EXPORT_CACHE_ROOT": str(work / "index-export"),
            "AICD_WORKTREE_ROOT": str(work / "wt"),
        })  # fmt: skip
        self.base = f"http://127.0.0.1:{self.api.port}"
        self.api.start()
        wait_ready(self.base, proc=self.api)
        self.outbox.start()

    def admin(self) -> psycopg.Connection:
        return psycopg.connect(self.db.superuser_url, autocommit=True, options="-c search_path=aicd")

    def close(self) -> None:
        for proc in (self.index, self.outbox, self.api):
            proc.stop()
        self.redis.kill()
        testdb.drop_database(self.db)


def create_users(stand: Stand, emails: list[str]) -> None:
    code = (
        "import asyncio,sys\n"
        "from aicd_identity import api\n"
        "from aicd_platform.db import DbContext, create_engine, transaction\n"
        "async def main():\n"
        "    e = create_engine(sys.argv[1])\n"
        "    for email in sys.argv[3:]:\n"
        "        async with transaction(e, DbContext(actor_type='system')) as c:\n"
        "            await api.create_user(c, email=email, display_name='perf', password=sys.argv[2])\n"
        "    await e.dispose()\n"
        "asyncio.run(main())\n"
    )
    subprocess.run([sys.executable, "-c", code, stand.db.url_for("aicd_app"), PASSWORD, *emails], check=True, cwd=ROOT)


async def login(base: str, email: str) -> tuple[httpx.AsyncClient, dict[str, str]]:
    client = httpx.AsyncClient(base_url=base, timeout=60, limits=httpx.Limits(max_connections=200))
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return client, {"X-CSRF-Token": r.json()["csrf_token"]}


def idem() -> dict[str, str]:
    return {"Idempotency-Key": "perf-" + secrets.token_hex(10)}


def init_repo(path: Path) -> None:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(path), "PATH": os.environ["PATH"]}
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    (path / "README.md").write_text("perf\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=p", "-c", "user.email=p@e", "commit", "-qm", "init"],
                   check=True, env=env)  # fmt: skip


# --------------------------------------------------------------------------------------- сценарии
async def scenario_reads(base: str, client: httpx.AsyncClient, ws: str, project: str, runs: list[str],
                         concurrency: int, duration: float) -> Stats:  # fmt: skip
    hdr = {"X-Workspace-Id": ws}

    async def request(c: httpx.AsyncClient, i: int) -> httpx.Response:
        run = runs[i % len(runs)]
        match i % 4:
            case 0:
                return await c.get("/api/v1/runs", params={"project_id": project, "limit": 50}, headers=hdr)
            case 1:
                return await c.get(f"/api/v1/runs/{run}")
            case 2:
                return await c.get(f"/api/v1/runs/{run}/tasks")
            case _:
                return await c.get("/api/v1/projects", headers=hdr)

    return await closed_loop(f"api_reads_c{concurrency}", [client] * concurrency, request, duration=duration)


async def scenario_create(client: httpx.AsyncClient, csrf: dict[str, str], project: str, concurrency: int,
                          duration: float, created: list[str]) -> Stats:  # fmt: skip
    async def request(c: httpx.AsyncClient, i: int) -> httpx.Response:
        r = await c.post("/api/v1/runs", json={"project_id": project, "mode": "DEVELOP", "objective": f"perf {i}"},
                         headers={**csrf, **idem()})  # fmt: skip
        if r.status_code == 201:
            created.append(r.json()["run_id"])
        return r

    return await closed_loop(f"run_create_c{concurrency}", [client] * concurrency, request, duration=duration)


async def scenario_sse(stand: Stand, ws: str, run: str, clients_n: int, events_n: int,
                       rate: float) -> dict[str, Any]:  # fmt: skip
    emails = [f"sse-{i}-{secrets.token_hex(3)}@perf.test" for i in range(clients_n)]
    await asyncio.to_thread(create_users, stand, emails)
    with stand.admin() as admin:
        admin.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) SELECT %s, id, 'viewer' FROM users"
            " WHERE email = ANY(%s)",
            (uuid.UUID(ws), emails),
        )
    sessions = [await login(stand.base, e) for e in emails]
    with stand.admin() as admin:
        row = admin.execute("SELECT last_event_seq FROM runs WHERE id = %s", (uuid.UUID(run),)).fetchone()
    after_seq = int(row[0]) if row else 0
    received: list[list[tuple[str, float, str]]] = [[] for _ in sessions]
    connected = asyncio.Event()
    ready = 0

    failures: dict[str, int] = {}

    def settled() -> None:
        if ready + sum(failures.values()) == len(sessions):
            connected.set()

    async def listen(idx: int, client: httpx.AsyncClient) -> None:
        nonlocal ready
        try:
            async with client.stream("GET", f"/api/v1/runs/{run}/events", params={"after_seq": after_seq},
                                     timeout=httpx.Timeout(None, connect=60)) as resp:  # fmt: skip
                if resp.status_code != 200:
                    failures[str(resp.status_code)] = failures.get(str(resp.status_code), 0) + 1
                    settled()
                    return
                ready += 1
                settled()
                await consume(idx, resp)
        except httpx.HTTPError as exc:
            if idx not in connected_idx:
                failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
                settled()

    connected_idx: set[int] = set()

    async def consume(idx: int, resp: httpx.Response) -> None:
        connected_idx.add(idx)
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                env = json.loads(line[5:])
                if env.get("type") == "run.state_changed":
                    received[idx].append((env["event_id"], time.time(), env["timestamp"]))

    listeners = [asyncio.create_task(listen(i, s[0])) for i, s in enumerate(sessions)]
    try:
        await asyncio.wait_for(connected.wait(), 120)
    except TimeoutError:
        for t in listeners:
            t.cancel()
        raise RuntimeError(f"подключилось {ready} из {len(sessions)} SSE-клиентов, отказы {failures}") from None
    await asyncio.sleep(1.0)
    emitted = await asyncio.to_thread(_emit_events, stand, ws, run, events_n, rate)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and sum(len(r) for r in received) < events_n * clients_n:  # noqa: ASYNC110
        await asyncio.sleep(0.2)
    for t in listeners:
        t.cancel()
    await asyncio.gather(*listeners, return_exceptions=True)
    for client, _ in sessions:
        await client.aclose()
    latencies = [
        (got - dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) * 1000
        for per in received
        for _, got, ts in per
    ]
    delivered = sum(len({e for e, _, _ in per} & emitted) for per in received)
    return {
        "name": f"sse_fanout_{clients_n}",
        "clients": clients_n,
        "events": events_n,
        "emit_rate_per_s": rate,
        "expected_deliveries": clients_n * events_n,
        "delivered": delivered,
        "delivery_ratio": round(delivered / (clients_n * events_n), 4),
        "connected": ready,
        "connect_failures": failures,
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "p99_ms": round(percentile(latencies, 99), 1),
        "max_ms": round(max(latencies), 1) if latencies else None,
    }


def _emit_events(stand: Stand, ws: str, run: str, n: int, rate: float) -> set[str]:
    """События outbox по одному на транзакцию с заданной частотой (как их порождает оркестратор)."""
    ids: set[str] = set()
    with psycopg.connect(stand.db.url_for("aicd_worker"), autocommit=True) as conn:
        conn.execute("SET search_path = aicd, public")
        for i in range(n):
            with conn.transaction():
                conn.execute("SELECT set_config('app.workspace_id', %s, true), set_config('app.actor_type', 'system', true)",
                             (ws,))  # fmt: skip
                eid = uuid7()
                conn.execute(
                    "INSERT INTO outbox_events (id, workspace_id, run_id, aggregate_type, aggregate_id, event_type,"
                    " payload) VALUES (%s, %s, %s, 'run', %s, 'run.state_changed', %s)",
                    (eid, ws, run, run, json.dumps({"from": "CREATED", "to": "DISCOVERY", "n": i})),
                )
            ids.add(str(eid))
            time.sleep(1 / rate)
    return ids


def scenario_outbox(stand: Stand, ws: str, runs: list[str], n: int) -> dict[str, Any]:
    with psycopg.connect(stand.db.url_for("aicd_worker"), autocommit=True) as conn:
        conn.execute("SET search_path = aicd, public")
        with conn.transaction():
            conn.execute("SELECT set_config('app.workspace_id', %s, true), set_config('app.actor_type', 'system', true)",
                         (ws,))  # fmt: skip
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO outbox_events (id, workspace_id, run_id, aggregate_type, aggregate_id, event_type,"
                    " payload) VALUES (%s, %s, %s, 'run', %s, 'run.state_changed', %s)",
                    [(uuid7(), ws, runs[i % len(runs)], runs[i % len(runs)],
                      json.dumps({"from": "CREATED", "to": "DISCOVERY", "n": i})) for i in range(n)],
                )  # fmt: skip
        committed = time.monotonic()
    with stand.admin() as admin:
        while True:
            pending = admin.execute(
                "SELECT count(*) FROM outbox_events WHERE published_at IS NULL AND workspace_id = %s",
                (uuid.UUID(ws),),
            ).fetchone()
            if pending == (0,) or time.monotonic() - committed > 600:
                break
            time.sleep(0.05)
        drained = time.monotonic() - committed
        lags = [
            float(r[0]) * 1000
            for r in admin.execute(
                "SELECT EXTRACT(EPOCH FROM published_at - created_at) FROM outbox_events WHERE workspace_id = %s"
                " AND payload ? 'n' AND event_type = 'run.state_changed' AND published_at IS NOT NULL"
                " ORDER BY created_at DESC LIMIT %s",
                (uuid.UUID(ws), n),
            ).fetchall()
        ]
    return {
        "name": "outbox_throughput",
        "events": n,
        "runs": len(runs),
        "drain_seconds": round(drained, 2),
        "events_per_second": round(n / drained, 1),
        "publish_lag_p50_ms": round(percentile(lags, 50), 1),
        "publish_lag_p95_ms": round(percentile(lags, 95), 1),
        "publish_lag_p99_ms": round(percentile(lags, 99), 1),
    }


def _clone_medium_repo(stand: Stand) -> tuple[Path, str, int]:
    repo = stand.repos / "aicodingdev-medium"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(repo)], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    files = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True)
    return repo, head.stdout.strip(), len(files.stdout.splitlines())


def _wait_indexing(stand: Stand, repository_id: uuid.UUID) -> tuple[Any, ...]:
    with stand.admin() as admin:
        deadline = time.monotonic() + 1800
        while True:
            row = admin.execute(
                "SELECT j.status, EXTRACT(EPOCH FROM j.finished_at - j.started_at),"
                " EXTRACT(EPOCH FROM j.started_at - j.created_at), s.file_count, s.symbol_count, j.error"
                " FROM indexing_jobs j LEFT JOIN code_index_snapshots s ON s.id = j.snapshot_id"
                " WHERE j.repository_id = %s ORDER BY j.created_at DESC LIMIT 1",
                (repository_id,),
            ).fetchone()
            if row is not None and row[0] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                return tuple(row)
            if time.monotonic() > deadline:
                raise RuntimeError("индексация не завершилась за 30 мин")
            time.sleep(0.5)


async def scenario_indexing(
    stand: Stand, client: httpx.AsyncClient, csrf: dict[str, str], project: str
) -> dict[str, Any]:
    repo, head, tracked = await asyncio.to_thread(_clone_medium_repo, stand)
    stand.index.start()
    r = await client.post(f"/api/v1/projects/{project}/repositories",
                          json={"provider": "local", "local_path": str(repo)}, headers={**csrf, **idem()})  # fmt: skip
    r.raise_for_status()
    row = await asyncio.to_thread(_wait_indexing, stand, uuid.UUID(r.json()["id"]))
    return {
        "name": "indexing_medium_repo",
        "repository": f"AiCodingDev@{head[:12]}",
        "tracked_files": tracked,
        "status": row[0],
        "index_seconds": round(float(row[1] or 0), 2),
        "queue_wait_seconds": round(float(row[2] or 0), 2),
        "indexed_files": row[3],
        "symbols": row[4],
        "error": row[5],
    }


# --------------------------------------------------------------------------------------------- main
def hardware() -> dict[str, Any]:
    cpu = ""
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    mem = ""
    with contextlib.suppress(OSError):
        mem = Path("/proc/meminfo").read_text().splitlines()[0].split(":", 1)[1].strip()
    pg = ""
    with contextlib.suppress(Exception), psycopg.connect(os.environ.get("AICD_PERF_DATABASE_ADMIN_URL")
                                                         or os.environ["AICD_TEST_DATABASE_ADMIN_URL"]) as c:  # fmt: skip
        row = c.execute("SHOW server_version").fetchone()
        pg = str(row[0]) if row else ""
    return {"cpu": cpu, "cpus": os.cpu_count(), "memory": mem, "kernel": platform.release(),
            "python": platform.python_version(), "postgres": pg,
            "redis": subprocess.run(["redis-server", "--version"], capture_output=True, text=True).stdout.strip()}  # fmt: skip


async def run(args: argparse.Namespace) -> dict[str, Any]:
    admin_url = os.environ.get("AICD_PERF_DATABASE_ADMIN_URL") or os.environ.get("AICD_TEST_DATABASE_ADMIN_URL")
    if not admin_url:
        raise SystemExit("нужен AICD_PERF_DATABASE_ADMIN_URL (или AICD_TEST_DATABASE_ADMIN_URL)")
    work = Path(tempfile.mkdtemp(prefix="aicd-perf-"))
    stand = Stand(admin_url, work)
    results: dict[str, Any] = {
        "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "hardware": hardware(),
        "parameters": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "scenarios": [],
    }
    try:
        email = f"perf-owner-{secrets.token_hex(3)}@perf.test"
        await asyncio.to_thread(create_users, stand, [email])
        client, csrf = await login(stand.base, email)
        slug = "perf-" + secrets.token_hex(3)
        ws = (await client.post("/api/v1/workspaces", json={"name": slug, "slug": slug},
                                headers={**csrf, **idem()})).json()["id"]  # fmt: skip
        project = (await client.post("/api/v1/projects", json={"name": "perf"},
                                     headers={**csrf, **idem(), "X-Workspace-Id": ws})).json()["id"]  # fmt: skip
        small = stand.repos / "small"
        init_repo(small)
        (await client.post(f"/api/v1/projects/{project}/repositories", json={"provider": "local", "local_path": str(small)},
                           headers={**csrf, **idem()})).raise_for_status()  # fmt: skip

        created: list[str] = []
        for c in args.create_concurrency:
            stats = await scenario_create(client, csrf, project, c, args.duration, created)
            results["scenarios"].append(stats.summary())
            print(json.dumps(results["scenarios"][-1]), flush=True)
        for c in args.read_concurrency:
            stats = await scenario_reads(stand.base, client, ws, project, created[:200], c, args.duration)
            results["scenarios"].append(stats.summary())
            print(json.dumps(results["scenarios"][-1]), flush=True)
        if not created:  # сценарии SSE/outbox нуждаются хотя бы в одном run
            r = await client.post("/api/v1/runs", json={"project_id": project, "mode": "DEVELOP", "objective": "perf"},
                                  headers={**csrf, **idem()})  # fmt: skip
            created.append(r.json()["run_id"])
        if args.outbox_events > 0:
            results["scenarios"].append(
                await asyncio.to_thread(scenario_outbox, stand, ws, created[:20], args.outbox_events)
            )
            print(json.dumps(results["scenarios"][-1]), flush=True)
        for n in args.sse_clients:
            results["scenarios"].append(await scenario_sse(stand, ws, created[0], n, args.sse_events,
                                                           args.sse_rate))  # fmt: skip
            print(json.dumps(results["scenarios"][-1]), flush=True)
        if not args.skip_indexing:
            results["scenarios"].append(await scenario_indexing(stand, client, csrf, project))
            print(json.dumps(results["scenarios"][-1]), flush=True)
        await client.aclose()
    finally:
        stand.close()
        shutil.rmtree(work, ignore_errors=True)
    results["finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(prog="perf-baseline", description=__doc__.splitlines()[0])
    parser.add_argument("--duration", type=float, default=20.0, help="секунд измерения на сценарий закрытого цикла")
    parser.add_argument("--read-concurrency", type=int, nargs="*", default=[1, 16, 64])
    parser.add_argument("--create-concurrency", type=int, nargs="*", default=[1, 8])
    parser.add_argument("--sse-clients", type=int, nargs="*", default=[50, 200])
    parser.add_argument("--sse-events", type=int, default=50)
    parser.add_argument("--sse-rate", type=float, default=10.0, help="событий в секунду")
    parser.add_argument("--outbox-events", type=int, default=5000)
    parser.add_argument("--skip-indexing", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "perf" / "results.json")
    args = parser.parse_args()
    results = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str) + "\n")
    print(f"результаты: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
