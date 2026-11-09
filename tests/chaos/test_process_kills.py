"""P9-03 (NFR-REL-01/02): SIGKILL настоящих процессов API и outbox-воркера под нагрузкой.

* **API** (uvicorn, `aicd_api.main:create_app`) убивается посреди пачки `POST /runs` с Idempotency-Key.
  После перезапуска клиент повторяет каждый запрос тем же ключом: завершённые — повтор сохранённого ответа,
  прерванные — 409 до истечения lease ключа (60 с), затем исполняются. Итог: на каждый ключ ровно один run,
  у каждого run ровно одно событие `run.created` (команда и outbox — одна транзакция, ADR-0002).
* **outbox-воркер** (`aicd_worker.main outbox`) убивается посреди публикации пачки событий и перезапускается:
  все события опубликованы, проекция SSE (`run_event_log`) — ровно одна на событие, `seq` по run без
  пропусков и повторов; в Dramatiq возможны повторы (at-least-once) — их гасит `processed_events`.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from redis import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform import outbox
from aicd_platform.db import DbContext, transaction
from aicd_worker.broker import ORCHESTRATOR_QUEUE
from tests.conftest import PASSWORD, init_git_repo, register_user
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.processes import AppProcess, RedisProcess, api_process, wait_ready, worker_process

pytestmark = pytest.mark.db


@pytest.fixture
def redis_proc() -> Iterator[RedisProcess]:
    proc = RedisProcess()
    proc.start()
    yield proc
    proc.kill()


def _api_env(database: testdb.TestDatabase, redis: RedisProcess, repo_root: Path, tmp: Path) -> dict[str, str]:
    return {
        "AICD_ENVIRONMENT": "test",
        "AICD_DATABASE_URL": database.url_for("aicd_app"),
        "AICD_REDIS_URL": redis.url,
        "AICD_SECRET_KEY": secrets.token_urlsafe(48),
        "AICD_COOKIE_SECURE": "false",
        "AICD_REPOSITORY_ROOTS": f"main={repo_root}",
        "AICD_ARTIFACT_FS_ROOT": str(tmp / "artifacts"),
        "AICD_RATE_LIMIT_READ_PER_MINUTE": "1000000",
        "AICD_RATE_LIMIT_MUTATION_PER_MINUTE": "1000000",
        "AICD_RATE_LIMIT_LOGIN_PER_IP_PER_MINUTE": "1000000",
        "AICD_LOG_LEVEL": "WARNING",
    }


async def test_api_killed_mid_burst_retries_give_exactly_one_run_per_key(
    database: testdb.TestDatabase, app_engine: AsyncEngine, redis_proc: RedisProcess, repo_root: Path, tmp_path: Path
) -> None:
    api = api_process(_api_env(database, redis_proc, repo_root, tmp_path))
    base = f"http://127.0.0.1:{api.port}"
    api.start()
    try:
        wait_ready(base, proc=api)
        email = f"chaos-{secrets.token_hex(5)}@example.test"
        await register_user(app_engine, email)
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            csrf = {"X-CSRF-Token": r.json()["csrf_token"]}
            slug = "ws-" + secrets.token_hex(4)
            r = await client.post("/api/v1/workspaces", json={"name": slug, "slug": slug},
                                  headers={**csrf, "Idempotency-Key": "k-" + secrets.token_hex(8)})  # fmt: skip
            ws = r.json()["id"]
            r = await client.post("/api/v1/projects", json={"name": "chaos"},
                                  headers={**csrf, "X-Workspace-Id": ws, "Idempotency-Key": "k-" + secrets.token_hex(8)})  # fmt: skip
            project = r.json()["id"]
            local = repo_root / f"api-chaos-{secrets.token_hex(4)}"
            init_git_repo(local)
            r = await client.post(f"/api/v1/projects/{project}/repositories",
                                  json={"provider": "local", "local_path": str(local)},
                                  headers={**csrf, "Idempotency-Key": "k-" + secrets.token_hex(8)})  # fmt: skip
            assert r.status_code == 201, r.text
            keys = [f"chaos-run-{i:03d}-{secrets.token_hex(4)}" for i in range(60)]

            async def create(key: str) -> tuple[str, int | None, str | None]:
                body = {"project_id": project, "mode": "DEVELOP", "objective": f"chaos {key}"}
                try:
                    resp = await client.post("/api/v1/runs", json=body, headers={**csrf, "Idempotency-Key": key})
                except httpx.HTTPError:
                    return key, None, None
                return key, resp.status_code, resp.json().get("run_id") if resp.status_code == 201 else None

            tasks = [asyncio.create_task(create(k)) for k in keys]
            # SIGKILL после первых ответов — остальные запросы в полёте (часть уже в транзакции БД).
            deadline = time.monotonic() + 30
            while sum(t.done() for t in tasks) < 8 and time.monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.005)
            api.kill()
            first = {k: (code, run) for k, code, run in await asyncio.gather(*tasks)}
            completed_before = {k: run for k, (code, run) in first.items() if code == 201}
            interrupted = [k for k, (code, _) in first.items() if code is None]
            assert completed_before and interrupted, f"гибель API пришлась на середину пачки: {first}"

            api.start()
            wait_ready(base, proc=api)
            final: dict[str, str] = {}
            conflicts: list[str] = []
            for _, (key, code, run) in zip(keys, [await create(k) for k in keys], strict=True):
                if code == 201 and run:
                    final[key] = run
                else:
                    assert code == 409, f"{key}: {code} — прерванный ключ отвечает 409 до истечения lease"
                    conflicts.append(key)
            for key, run in completed_before.items():
                assert final[key] == run, "повтор завершённого ключа возвращает тот же run"
            if conflicts:  # истечение lease ключа (60 с) — вместо ожидания сдвигаем created_at
                with orch.connect_admin(database) as admin:
                    admin.execute(
                        "UPDATE idempotency_keys SET created_at = now() - interval '2 minutes'"
                        " WHERE key = ANY(%s) AND status = 'IN_PROGRESS'",
                        (conflicts,),
                    )
                for key, code, run in [await create(k) for k in conflicts]:
                    assert code == 201 and run, f"{key}: после истечения lease команда выполняется"
                    final[key] = run
    finally:
        api.kill()

    assert len(final) == len(keys) and len(set(final.values())) == len(keys)
    with orch.connect_admin(database) as admin:
        rows = admin.execute(
            "SELECT r.id, (SELECT count(*) FROM outbox_events o WHERE o.aggregate_id = r.id"
            " AND o.event_type = 'run.created') FROM runs r WHERE r.project_id = %s",
            (uuid.UUID(project),),
        ).fetchall()
    assert {str(r[0]) for r in rows} == set(final.values()), "ровно один run на ключ, без «осиротевших» run"
    assert all(r[1] == 1 for r in rows), "ровно одно событие run.created на run"


def _worker_env(database: testdb.TestDatabase, redis: RedisProcess) -> dict[str, str]:
    return {
        "AICD_ENVIRONMENT": "test",
        "AICD_DATABASE_URL": database.url_for("aicd_worker"),
        "AICD_REDIS_URL": redis.url,
        "AICD_OUTBOX_POLL_SECONDS": "0.05",
        "AICD_OUTBOX_BATCH_SIZE": "10",
        "AICD_LOG_LEVEL": "WARNING",
    }


async def test_outbox_worker_killed_mid_dispatch_projects_each_event_once(
    database: testdb.TestDatabase, worker_engine: AsyncEngine, redis_proc: RedisProcess
) -> None:
    with orch.connect_admin(database) as admin:
        tenant = orch.seed_tenant(admin)
        runs = [orch.new_run(admin, tenant) for _ in range(5)]
    ids: list[uuid.UUID] = []
    for i in range(400):
        async with transaction(worker_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
            run = runs[i % len(runs)]
            ids.append(await outbox.emit(conn, event_type="run.state_changed", aggregate_type="run",
                                         aggregate_id=run, workspace_id=tenant.workspace_id, run_id=run,
                                         payload={"from": "CREATED", "to": "DISCOVERY", "n": i}))  # fmt: skip

    def published(admin: psycopg.Connection) -> int:
        row = admin.execute("SELECT count(*) FROM outbox_events WHERE id = ANY(%s) AND published_at IS NOT NULL",
                            (ids,)).fetchone()  # fmt: skip
        return int(row[0]) if row else 0

    worker: AppProcess = worker_process("outbox", _worker_env(database, redis_proc))
    kills = 0
    with orch.connect_admin(database) as admin:
        try:
            for threshold in (60, 200):  # два SIGKILL посреди публикации
                worker.start()
                deadline = time.monotonic() + 60
                while published(admin) < threshold:
                    assert worker.alive(), worker.stderr_tail()
                    assert time.monotonic() < deadline, "outbox-воркер не публикует"
                    await asyncio.sleep(0.01)
                worker.kill()
                kills += 1
            assert published(admin) < len(ids), "гибель пришлась на середину пачки"
            worker.start()
            deadline = time.monotonic() + 90
            while published(admin) < len(ids):
                assert worker.alive(), worker.stderr_tail()
                assert time.monotonic() < deadline, f"опубликовано {published(admin)} из {len(ids)}"
                await asyncio.sleep(0.05)
        finally:
            worker.stop()
        projected = admin.execute(
            "SELECT event_id, run_id, seq FROM run_event_log WHERE event_id = ANY(%s)", (ids,)
        ).fetchall()
    assert kills == 2
    assert len(projected) == len(ids) and {r[0] for r in projected} == set(ids), "проекция — ровно одна на событие"
    by_run: dict[uuid.UUID, list[int]] = {}
    for _, run, seq in projected:
        by_run.setdefault(run, []).append(int(seq))
    for seqs in by_run.values():
        seqs.sort()
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "seq без пропусков и повторов"
    client = Redis.from_url(redis_proc.url)
    with contextlib.closing(client):
        messages = [
            m.decode() if isinstance(m, bytes) else str(m)
            for m in client.hvals(f"aicd-dramatiq:{ORCHESTRATOR_QUEUE}.msgs")
        ]
    delivered = {i for i in ids if any(str(i) in m for m in messages)}
    assert delivered == set(ids), "каждое событие доставлено в очередь (at-least-once)"
