"""Живой вывод CLI (`cli.output`, API_DESIGN §13–§14, EVENT_SCHEMAS §2.2/§4.3) на Fake CLI:
worker-exec (адаптер → `CliOutputPublisher`) → outbox → run_event_log → SSE.

* живые чанки приходят по SSE во время исполнения и по порядку (`cli_seq`, `seq`);
* лимит ≤ 20 событий/с на run: избыток — коалесцированные события с `dropped_chunks`, все нормализованные
  события учтены в `coalesced_count`;
* секрет из stdout/stderr (в т.ч. разрезанный между записями процесса) и скрытое рассуждение модели не попадают
  ни в БД (все таблицы workspace), ни в SSE, ни в artifacts; полный санитизированный вывод — artifact `cli_output`;
* выдержки CLI в ленте чата (`cli_excerpt` / SSE `chat.message` `CLI_HIGHLIGHT`);
* payload — по нормативной схеме SSE (`validate_sse` каждого кадра).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import ExecutionGateway, worktree_resolver
from aicd_cli_adapters.models import ExecutionRequest, NormalizedCliEvent
from aicd_orchestration.cli_output import (
    MAX_EVENTS_PER_SECOND,
    CliOutputContext,
    CliOutputPublisher,
    RunRateLimiter,
    coalesce,
    payload_for,
)
from aicd_platform import sse
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.ids import uuid7
from aicd_platform.outbox_dispatcher import dispatch_batch
from aicd_security.local_runner import LocalProcessRunner
from tests.conftest import UserSession
from tests.integration.test_sse import Reader, _client, _events, fast_stream, redis, server  # noqa: F401
from tests.security.test_secret_leakage import _scan_database, _scan_files
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.cli import make_request
from tests.support.events import validate_sse

SPLIT_SECRET = "ghp_SPLITsplitSPLITsplitSPLIT0123456789"  # сценарий live_output: секрет разрезан между записями
HIDDEN = "HIDDEN-REASONING-CANARY"
CLI_SECRETS = ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "hunter2hunter2", "AKIAABCDEFGHIJKLMNOP")


# ------------------------------------------------------------------------------------- без БД: правила


def _ctx() -> CliOutputContext:
    return CliOutputContext(uuid7(), uuid7(), uuid7(), uuid7(), "codex", "developer")


def _event(seq: int, type_: str, payload: dict[str, Any]) -> NormalizedCliEvent:
    return NormalizedCliEvent(
        contract_version="1.0",
        execution_id=uuid7(),
        seq=seq,
        type=type_,
        timestamp=datetime.now(UTC),
        payload=payload,
    )


def _envelope(payload: dict[str, Any], seq: int = 1) -> dict[str, Any]:
    return {
        "event_id": str(uuid7()),
        "run_id": str(uuid7()),
        "seq": seq,
        "type": "cli.output",
        "timestamp": "2026-11-08T10:00:00Z",
        "schema_version": "1.0",
        "payload": payload,
    }


def test_payloads_of_every_cli_event_type_match_sse_schema() -> None:
    ctx = _ctx()
    art = str(uuid7())
    events = [
        _event(1, "started", {"provider": "codex", "cli_version": "1.2.3", "command_profile": "p", "runner_ref": "r"}),
        _event(2, "output", {"stream": "stderr", "text": "x" * 5000, "redactions": 1, "truncated": False}),
        _event(3, "tool_event", {"tool_kind": "command", "phase": "completed", "target": "npm test"}),
        _event(4, "warning", {"code": "OUTPUT_TRUNCATED", "message": "token=abcdefghijkl"}),
        _event(
            5,
            "artifact",
            {"artifact_type": "output_excerpt", "ref": f"artifact:{art}", "sha256": "0" * 64, "size_bytes": 1},
        ),
        _event(6, "finished", {"execution_status": "EXITED", "exit_code": 0, "error_code": None}),
    ]
    payloads = [payload_for(ctx, e) for e in events]
    for i, p in enumerate(payloads, 1):
        validate_sse(_envelope(p, i))
    assert len(payloads[1]["text"]) == 4096, "текст ограничен схемой (4096 символов)"
    assert "abcdefghijkl" not in payloads[3]["text"], "текст повторно редактируется"
    assert payloads[4]["artifact_id"] == art
    merged = coalesce([*payloads[1:3], payloads[1]])
    validate_sse(_envelope(merged))
    assert merged["cli_event_type"] == "tool_event" and merged["coalesced_count"] == 3
    assert merged["dropped_chunks"] == 2 and merged["cli_seq"] == 2


def test_coalesce_merges_output_in_order_and_counts_dropped() -> None:
    ctx = _ctx()
    outs = [
        payload_for(
            ctx,
            _event(
                i, "output", {"stream": "agent_message", "text": f"line {i}", "redactions": i % 2, "truncated": False}
            ),
        )
        for i in range(1, 6)
    ]
    merged = coalesce(outs)
    assert merged["text"] == "\n".join(f"line {i}" for i in range(1, 6))
    assert (merged["coalesced_count"], merged["dropped_chunks"], merged["cli_seq"], merged["redactions"]) == (
        5,
        0,
        5,
        3,
    )
    big = [
        payload_for(
            ctx, _event(i, "output", {"stream": "stdout", "text": "y" * 3000, "redactions": 0, "truncated": False})
        )
        for i in range(1, 5)
    ]
    merged = coalesce(big)
    validate_sse(_envelope(merged))
    assert len(merged["text"]) == 4096 and merged["truncated"] is True
    assert merged["dropped_chunks"] == 2, "третий и четвёртый чанки не уместились"
    mixed = coalesce([outs[0], {**outs[1], "stream": "stderr"}])
    assert mixed["text"] == "[agent_message] line 1\n[stderr] line 2"


def test_rate_limiter_token_bucket() -> None:
    now = [0.0]
    limiter = RunRateLimiter(rate=20, clock=lambda: now[0])
    run = uuid7()
    assert sum(limiter.try_acquire(run) for _ in range(50)) == 20, "запас — 20 событий"
    assert limiter.wait_seconds(run) == pytest.approx(0.05)
    now[0] += 0.5
    assert sum(limiter.try_acquire(run) for _ in range(50)) == 10, "20 событий/с"
    assert limiter.try_acquire(uuid7()), "бюджет — на run"


# --------------------------------------------------------------------------------- с БД, Fake CLI, SSE


@dataclass
class World:
    database: testdb.TestDatabase
    worker: AsyncEngine
    redis: Any
    store: FileSystemArtifactStore
    gateway: ExecutionGateway
    root: Path
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    attempt_id: uuid.UUID
    installation_id: uuid.UUID

    def adapter(self, scenario: str) -> FakeCLIExecutor:
        return FakeCLIExecutor(
            scenario=scenario,
            runner=LocalProcessRunner(),
            artifact_sink=self.gateway.store_artifact,
            worktree_path=worktree_resolver(self.root),
            env_source={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"},
        )

    def request(self) -> ExecutionRequest:
        wid = uuid7()
        (self.root / str(wid)).mkdir()
        return make_request(worktree_id=wid, workspace_id=self.workspace_id, task_attempt_id=self.attempt_id)

    def publisher(self, request: ExecutionRequest, **kw: Any) -> CliOutputPublisher:
        ctx = CliOutputContext(self.workspace_id, self.run_id, self.task_id, request.execution_id, "codex", "developer")
        return CliOutputPublisher(self.worker, ctx, **kw)

    async def dispatch(self) -> None:
        async def publish(event: dict[str, Any]) -> None:
            return None

        async def notify(run_id: uuid.UUID) -> None:
            await self.redis.publish(sse.run_channel(run_id), "1")

        while await dispatch_batch(self.worker, publish, notify):
            pass

    def outbox_payloads(self, execution_id: uuid.UUID) -> list[dict[str, Any]]:
        with orch.connect_admin(self.database) as conn:
            rows = conn.execute(
                "SELECT payload FROM outbox_events WHERE event_type = 'cli.output' AND aggregate_id = %s"
                " ORDER BY created_at, id",
                (execution_id,),
            ).fetchall()
        return [r[0] for r in rows]


@pytest.fixture
async def world(
    database: testdb.TestDatabase,
    worker_engine: AsyncEngine,
    redis: Any,  # noqa: F811
    user: UserSession,
    tmp_path: Path,
) -> World:
    with orch.connect_admin(database) as conn:
        tenant = orch.seed_tenant(conn, owner_id=uuid.UUID(user.user_id))
        run = orch.new_run(conn, tenant)
        orch.drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
        task = orch.new_task(conn, tenant, run)
        orch.lease_task(conn, task)
        attempt = orch.new_attempt(conn, tenant, task)
    host, inst = uuid7(), uuid7()
    with psycopg.connect(database.url_for("aicd_worker")) as conn:
        conn.execute(
            "INSERT INTO worker_hosts (id, hostname, kind) VALUES (%s, %s, 'exec')", (host, f"h-{host.hex[-8:]}")
        )
        conn.execute(
            "INSERT INTO cli_installations (id, provider_id, host_id, binary_path, auth_status, max_concurrency)"
            " SELECT %s, id, %s, '/usr/bin/python3', 'AUTHENTICATED', 4 FROM cli_providers WHERE code = 'codex'",
            (inst, host),
        )
    store = FileSystemArtifactStore(tmp_path / "artifacts")
    root = tmp_path / "worktrees"
    root.mkdir()
    return World(
        database, worker_engine, redis, store, ExecutionGateway(engine=worker_engine, store=store), root,
        tenant.workspace_id, run, task, attempt, inst,
    )  # fmt: skip


async def _execute(world: World, scenario: str, publisher_kw: dict[str, Any] | None = None) -> tuple[
    ExecutionRequest, CliOutputPublisher, list[NormalizedCliEvent], Any
]:  # fmt: skip
    request = world.request()
    publisher = world.publisher(request, **(publisher_kw or {}))
    seen: list[NormalizedCliEvent] = []

    async def sink(event: NormalizedCliEvent) -> None:
        seen.append(event)
        await publisher(event)

    try:
        outcome = await world.gateway.run(
            request, world.adapter(scenario), installation_id=world.installation_id, on_event=sink
        )
    finally:
        await publisher.close()
    return request, publisher, seen, outcome


@pytest.mark.db
@pytest.mark.redis
async def test_live_cli_output_arrives_over_sse_in_order_sanitized(
    server: str,  # noqa: F811
    world: World,
    user: UserSession,
) -> None:
    received: list[tuple[float, dict[str, Any]]] = []
    stop = asyncio.Event()

    async def dispatcher() -> None:
        while not stop.is_set():
            await world.dispatch()
            await asyncio.sleep(0.05)

    async def reader() -> None:
        async with _client(server, user) as client:
            async with client.stream("GET", f"/runs/{world.run_id}/events", params={"after_seq": 0}) as response:
                assert response.status_code == 200
                frames = Reader(response)
                while True:
                    batch, closed = await frames.frames(1, wait=10.0)
                    for env in _events(batch):  # каждый кадр — по нормативной схеме SSE
                        received.append((time.monotonic(), env))
                    done = any(
                        e["type"] == "cli.output" and e["payload"]["cli_event_type"] == "finished" for _, e in received
                    )
                    if done or closed or not batch:
                        return

    loop_task = asyncio.create_task(dispatcher())
    read_task = asyncio.create_task(reader())
    await asyncio.sleep(0.3)  # подписка установлена до начала вывода
    request, publisher, seen, outcome = await _execute(world, "live_output")
    finished_at = time.monotonic()
    await asyncio.wait_for(read_task, 20)
    stop.set()
    await loop_task
    assert outcome.result.status == "completed"

    cli = [(t, e) for t, e in received if e["type"] == "cli.output"]
    payloads = [e["payload"] for _, e in cli]
    assert cli and cli[0][0] < finished_at, "первые чанки пришли по SSE до завершения процесса (живой вывод)"
    assert [e["seq"] for _, e in cli] == sorted(e["seq"] for _, e in cli)
    seqs = [p["cli_seq"] for p in payloads]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "порядок нормализованных событий сохранён"
    assert all(p["execution_id"] == str(request.execution_id) and p["task_id"] == str(world.task_id) for p in payloads)
    text = "\n".join(str(p.get("text", "")) for p in payloads)
    positions = [text.find(f"live line {i:02d}") for i in range(1, 31)]
    assert all(pos >= 0 for pos in positions) and positions == sorted(positions), "строки по порядку, без потерь"
    assert sum(p.get("coalesced_count", 1) for p in payloads) == len(seen), "каждое событие CLI учтено"

    # Санитизация: ANSI удалён, секрет, разрезанный между записями процесса, отредактирован; рассуждения скрыты.
    body = json.dumps([e for _, e in received], ensure_ascii=False)
    assert "\x1b" not in body and "\\u001b" not in body
    assert SPLIT_SECRET not in body and "ghp_SPLIT" not in body and HIDDEN not in body
    stderr = next(p for p in payloads if p.get("stream") == "stderr" or "[stderr]" in str(p.get("text")))
    assert "[REDACTED]" in stderr["text"] and "warn: token=" in stderr["text"] and stderr["redactions"] >= 1

    finished = payloads[-1]
    assert finished["cli_event_type"] == "finished" and finished["execution_status"] == "EXITED"
    artifact_id = uuid.UUID(finished["artifact_id"])
    with orch.connect_admin(world.database) as conn:
        ref, kind, sha = conn.execute(
            "SELECT storage_ref, kind, sha256 FROM artifacts WHERE id = %s", (artifact_id,)
        ).fetchone() or (None, None, None)
    assert kind == "cli_output"
    full = b"".join(world.store.open(ref, expected_sha256=sha)).decode()
    assert "[agent_message] live line 30" in full and "[stderr] warn: token=[REDACTED]" in full
    assert "[tool] command completed npm test" in full
    for needle in (SPLIT_SECRET, HIDDEN, "\x1b"):
        assert needle not in full

    # Выдержки CLI в ленте чата: инструмент `command` и завершение (CLI_HIGHLIGHT) — в треде задачи.
    chat = [e["payload"] for _, e in received if e["type"] == "chat.message"]
    highlights = [c for c in chat if c["message_kind"] == "CLI_HIGHLIGHT"]
    assert any("command npm test — completed" in c["body"] for c in highlights), chat
    assert any("CLI завершён — EXITED" in c["body"] for c in highlights)
    assert all(
        c["sender_role"] == "developer" and c["provider"] == "codex" and c["scope"] == "TASK" for c in highlights
    )
    assert publisher.highlights == len(highlights)
    async with _client(server, user) as client:
        feed = await client.get(f"/runs/{world.run_id}/chat", params={"stream": "codex"})
    assert feed.status_code == 200, feed.text
    assert {i["item_type"] for i in feed.json()["items"]} >= {"cli_excerpt"}

    assert _scan_database(world.database, (SPLIT_SECRET, HIDDEN), (world.workspace_id,)) == {}
    leaked, stored = _scan_files(world.root.parent / "artifacts", (SPLIT_SECRET,))
    assert stored >= 3 and leaked == [], "секрет, разрезанный между записями, не попал ни в один artifact"
    leaked, _ = _scan_files(world.root.parent / "artifacts", (HIDDEN,))
    assert leaked == [], "скрытое рассуждение модели не сохранено ни в одном artifact (включая raw log)"


@pytest.mark.db
@pytest.mark.redis
async def test_rate_cap_coalesces_with_dropped_chunks(world: World) -> None:
    started = time.monotonic()
    request, publisher, seen, outcome = await _execute(world, "big_output")
    elapsed = time.monotonic() - started
    assert outcome.result.status == "completed"
    payloads = world.outbox_payloads(request.execution_id)
    assert len(seen) > 5000 and len(payloads) == publisher.emitted
    # ≤ 20 событий/с на run: запас 20 + 20 × длительность (+1 на округление).
    assert len(payloads) <= MAX_EVENTS_PER_SECOND * (1 + elapsed) + 1, (len(payloads), elapsed)
    assert any(p.get("dropped_chunks", 0) > 0 for p in payloads), "избыток коалесцирован с dropped_chunks > 0"
    assert sum(p.get("coalesced_count", 1) for p in payloads) == len(seen), "все нормализованные события учтены"
    seqs = [p["cli_seq"] for p in payloads]
    assert seqs == sorted(seqs) and payloads[-1]["cli_event_type"] == "finished"
    for i, p in enumerate(payloads, 1):
        validate_sse(_envelope(p, i))
    await world.dispatch()
    with orch.connect_admin(world.database) as conn:
        projected = conn.execute(
            "SELECT count(*) FROM run_event_log WHERE run_id = %s AND event_type = 'cli.output'"
            " AND payload ->> 'execution_id' = %s",
            (world.run_id, str(request.execution_id)),
        ).fetchone()
    assert projected == (len(payloads),), "каждое событие outbox спроецировано в run_event_log"


@pytest.mark.db
@pytest.mark.redis
async def test_cli_secrets_never_reach_db_sse_or_artifacts(world: World) -> None:
    before = _scan_database(world.database, CLI_SECRETS, (world.workspace_id,))
    request, publisher, _seen, outcome = await _execute(world, "secret_leak")
    assert outcome.result.status == "completed" and publisher.emitted
    await world.dispatch()
    payloads = world.outbox_payloads(request.execution_id)
    body = json.dumps(payloads)
    assert "[REDACTED]" in body and not any(s in body for s in CLI_SECRETS)
    with orch.connect_admin(world.database) as conn:
        sse_rows = conn.execute(
            "SELECT payload::text FROM run_event_log WHERE run_id = %s AND event_type = 'cli.output'", (world.run_id,)
        ).fetchall()
    assert sse_rows and not any(s in r[0] for r in sse_rows for s in CLI_SECRETS)
    assert _scan_database(world.database, CLI_SECRETS, (world.workspace_id,)) == before
    leaked, stored = _scan_files(world.root.parent / "artifacts", CLI_SECRETS)
    assert stored >= 2 and leaked == [], "raw log, итог и полный санитизированный вывод — без секретов"
