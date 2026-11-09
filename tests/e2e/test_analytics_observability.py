"""P8-06 / P8-07 на сквозном срезе (Fake CLI, настоящие gateway/worktree/verification):

* одна трасса OpenTelemetry: `POST /runs` (API) → outbox (`traceparent`) → публикация диспетчером → обработчик
  оркестратора → сообщение exec → исполнение CLI (`cli.execution`);
* метрики Prometheus исполнения CLI и доменные счётчики по опубликованным outbox-событиям;
* Analytics API (FR-OBS-05, FR-CI-08): значения по реальным run, контракт, RBAC/tenancy (AC-12), валидация окна,
  property-based (Schemathesis) — без 5xx и с ответами по схеме.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import hypothesis
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.approvals import Approvals
from aicd_platform import metrics
from aicd_platform.db import DbContext, transaction
from aicd_platform.outbox_dispatcher import dispatch_batch
from aicd_worker import handlers, runtime
from tests.conftest import login
from tests.support.contract import OPENAPI_PATH, validate_response
from tests.support.orchestration import connect_admin
from tests.support.phase3 import Slice, create_run, install_both, install_cli, world
from tests.support.tracing import by_name, exporter, spans, trace_id

__all__ = ["spans", "world"]  # фикстуры pytest

pytestmark = pytest.mark.db


def _sample(name: str, labels: dict[str, str]) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


async def _pump(s: Slice, run_id: uuid.UUID, *, until: str, rounds: int = 40) -> None:
    """Очередь сообщений без Redis: диспетчер outbox → обработчик оркестратора → обработчик exec (как акторы)."""
    exec_queue: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    async def notify(_: uuid.UUID) -> None:
        return None

    async def dispatch(message: dict[str, Any]) -> None:
        exec_queue.append(message)

    rt = runtime.Runtime(
        engine=s.worker,
        orchestrator=s.orchestrator(),
        loop=asyncio.get_running_loop(),
        dispatch_execution=dispatch,
        stale_ready_seconds=1,
    )
    for _ in range(rounds):
        await dispatch_batch(s.worker, publish, notify, 500)
        own = [e for e in published if e.get("run_id") == str(run_id)]
        published.clear()
        for event in own:
            if event["event_type"] in handlers.ORCHESTRATOR_EVENT_TYPES:
                await runtime.traced(handlers.ORCHESTRATOR_CONSUMER, event, handlers.handle_event(rt, event))
        while exec_queue:
            message = exec_queue.pop(0)
            await runtime.traced(handlers.EXEC_CONSUMER, message, handlers.execute_task(rt, message))
        if by_name(exporter(), until):
            return
    raise AssertionError(f"спан {until} не появился")


async def test_one_trace_from_api_request_to_cli_execution(world: Slice, spans: InMemorySpanExporter) -> None:
    await install_both(world.worker)
    before = _sample(
        "aicd_cli_executions_total", {"provider": "codex", "status": "COLLECTED", "failure_category": "none"}
    )
    before += _sample(
        "aicd_cli_executions_total", {"provider": "claude", "status": "COLLECTED", "failure_category": "none"}
    )
    run_id = uuid.UUID(await create_run(world))
    request_span = next(s for s in spans.get_finished_spans() if s.name == "POST /api/v1/runs")
    async with transaction(world.worker, DbContext(actor_type="system")) as conn:
        stored = (
            await conn.execute(
                text("SELECT traceparent FROM outbox_events WHERE run_id = :r AND event_type = 'run.created'"),
                {"r": run_id},
            )
        ).scalar_one()
    assert stored is not None and stored.split("-")[1] == trace_id(request_span), "traceparent API → outbox"
    await _pump(world, run_id, until="cli.execution")
    root = trace_id(request_span)
    publish = [s for s in by_name(spans, "outbox.publish") if trace_id(s) == root]
    handled = [s for s in by_name(spans, "worker orchestrator.handle_event") if trace_id(s) == root]
    executed = [s for s in by_name(spans, "worker exec.execute_task") if trace_id(s) == root]
    cli = [s for s in by_name(spans, "cli.execution") if trace_id(s) == root]
    assert publish and handled and executed and cli, "одна трасса API → outbox → worker → exec → CLI"
    # Иерархия: exec-обработчик — потомок обработчика события; исполнение CLI — внутри exec-обработчика.
    ids = {s.context.span_id: s for s in spans.get_finished_spans() if s.context is not None}

    def ancestors(span: Any) -> set[str]:
        names: set[str] = set()
        while span.parent is not None and span.parent.span_id in ids:
            span = ids[span.parent.span_id]
            names.add(span.name)
        return names

    assert {"worker exec.execute_task", "worker orchestrator.handle_event", "outbox.publish", "POST /api/v1/runs"} <= (
        ancestors(cli[0])
    )
    for span in cli:
        assert set(span.attributes or {}) <= {
            "aicd.workspace_id", "aicd.execution_id", "aicd.provider", "aicd.status", "aicd.failure_category",
            "aicd.outcome",
        }  # fmt: skip
    after = _sample(
        "aicd_cli_executions_total", {"provider": "codex", "status": "COLLECTED", "failure_category": "none"}
    )
    after += _sample(
        "aicd_cli_executions_total", {"provider": "claude", "status": "COLLECTED", "failure_category": "none"}
    )
    assert after >= before + 1
    assert _sample("aicd_outbox_events_published_total", {"event_type": "run.created"}) >= 1


# --- Analytics -----------------------------------------------------------------------------------------------


async def _decide_plan(app_engine: AsyncEngine, s: Slice, run_id: str) -> None:
    ws, rid = uuid.UUID(s.ws), uuid.UUID(run_id)
    async with transaction(s.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        approval_id, version = (
            await conn.execute(text("SELECT id, version FROM approvals WHERE run_id = :r"), {"r": rid})
        ).one()
    user = uuid.UUID(s.user.user_id)
    async with transaction(app_engine, DbContext(user_id=user, workspace_id=ws)) as conn:
        await Approvals().decide(
            conn, approval_id, actor_id=user, actor_role="owner", decision="APPROVED", reason=None,
            expected_version=version,
        )  # fmt: skip


@pytest.fixture
async def analytics_world(app_engine: AsyncEngine, world: Slice) -> Slice:
    """Два run: DEVELOP с утверждением плана → COMPLETED; DEVELOP с падающим CLI → FAILED (3 попытки)."""
    await install_both(world.worker)
    ok = await create_run(world, approval_policy="always")
    orch = world.orchestrator()
    ws = uuid.UUID(world.ws)
    await orch.on_run_created(uuid.UUID(ok), ws)
    await _decide_plan(app_engine, world, ok)
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        pv = (
            await conn.execute(text("SELECT plan_version FROM runs WHERE id = :r"), {"r": uuid.UUID(ok)})
        ).scalar_one()
    await orch.on_plan_approval_decided(uuid.UUID(ok), ws, approved=True, plan_version=pv)
    assert await orch.drive(uuid.UUID(ok), ws) == "COMPLETED"
    await install_cli(world.worker)
    world.scenario["name"] = "nonzero_exit"
    failed = await create_run(world)
    orch = world.orchestrator()
    await orch.on_run_created(uuid.UUID(failed), ws)
    assert await orch.drive(uuid.UUID(failed), ws, fast_forward_backoff=True) == "FAILED"
    return world


async def test_analytics_summary_reflects_runs(analytics_world: Slice) -> None:
    s = analytics_world
    r = await s.user.client.get("/api/v1/analytics/summary", headers={"X-Workspace-Id": s.ws})
    validate_response("getAnalyticsSummary", r)
    assert r.status_code == 200, r.text
    body = r.json()
    runs = body["runs"]
    assert runs["total"] == 2 and runs["finished"] == 2 and runs["completed"] == 1
    assert runs["success_rate"] == 0.5
    assert runs["by_status"] == {"COMPLETED": 1, "FAILED": 1}
    assert [m["mode"] for m in runs["by_mode"]] == ["DEVELOP"] and runs["by_mode"][0]["total"] == 2
    assert runs["duration_seconds"]["p50"] is not None
    assert sum(d["runs_created"] for d in body["daily"]) == 2
    tasks = body["tasks"]
    assert tasks["retries"] >= 2, "две повторные попытки падающей задачи (retry count)"
    assert tasks["succeeded"] >= 1 and tasks["failed"] >= 3
    # PROCESS_FAILED характеризует агента (ROUTING_POLICY §7) ⇒ 0 < VCR < 1.
    assert tasks["verified_completion_rate"] is not None and 0 < tasks["verified_completion_rate"] < 1
    assert tasks["latency_seconds"]["p90"] is not None
    assert {"developer"} <= {row["role"] for row in tasks["by_role"]}
    assert any(g["passed"] >= 1 and g["pass_rate"] is not None for g in body["gates"])
    # D-8: настоящие gates пишут PASS только с VERIFIED evidence (триггер evidence-first) ⇒ всё — «Пройдено».
    assert sum(g["passed_verified"] for g in body["gates"]) >= 1
    for g in body["gates"]:
        assert g["passed_verified"] == g["passed"] and g["passed_unverified"] == 0, g
    providers = {p["provider"]: p for p in body["providers"]}
    assert providers and sum(p["executions"] for p in providers.values()) >= 4
    for p in providers.values():
        # Fake CLI не сообщает токены ⇒ unknown, а не 0 (ТЗ §9, §16).
        assert p["tokens"]["total"] is None and p["tokens"]["source"] == "not_exposed_by_cli"
    approvals = body["approvals"]
    assert approvals["by_status"].get("APPROVED", 0) >= 1 and approvals["wait_seconds"]["p50"] is not None
    assert body["agent_messages"]["total"] == sum(body["agent_messages"]["by_type"].values())
    usefulness = body["context"]["usefulness"]
    assert usefulness["fragments_referenced"] <= usefulness["fragments_total"]


async def test_analytics_project_filter_and_window(analytics_world: Slice) -> None:
    s = analytics_world
    h = {"X-Workspace-Id": s.ws}
    same = await s.user.client.get("/api/v1/analytics/summary", params={"project_id": s.project}, headers=h)
    assert same.status_code == 200 and same.json()["runs"]["total"] == 2
    other = await s.user.create_project(s.ws)
    empty = await s.user.client.get("/api/v1/analytics/summary", params={"project_id": other["id"]}, headers=h)
    validate_response("getAnalyticsSummary", empty)
    body = empty.json()
    assert body["runs"]["total"] == 0 and body["runs"]["success_rate"] is None and body["providers"] == []
    past = datetime.now(UTC) - timedelta(days=200)
    old = await s.user.client.get(
        "/api/v1/analytics/summary",
        params={"from": past.isoformat(), "to": (past + timedelta(days=1)).isoformat()},
        headers=h,
    )
    assert old.status_code == 200 and old.json()["runs"]["total"] == 0


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-02-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        {"from": "2024-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        {"from": "2026-01-01T00:00:00", "to": "2026-01-02T00:00:00"},
        {"from": "not-a-date"},
        {"project_id": "nope"},
    ],
)
async def test_analytics_validation(world: Slice, params: dict[str, str]) -> None:
    r = await world.user.client.get("/api/v1/analytics/summary", params=params, headers={"X-Workspace-Id": world.ws})
    assert r.status_code == 422, r.text
    validate_response("getAnalyticsSummary", r)


async def test_analytics_rbac_and_tenancy(app: FastAPI, app_engine: AsyncEngine, world: Slice, database: Any) -> None:
    viewer = await login(app, app_engine)
    outsider = await login(app, app_engine)
    try:
        with connect_admin(database) as conn:
            conn.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, 'viewer')",
                (uuid.UUID(world.ws), uuid.UUID(viewer.user_id)),
            )
        h = {"X-Workspace-Id": world.ws}
        ok = await viewer.client.get("/api/v1/analytics/summary", headers=h)
        assert ok.status_code == 200
        denied = await outsider.client.get("/api/v1/analytics/summary", headers=h)
        missing = await outsider.client.get("/api/v1/analytics/summary", headers={"X-Workspace-Id": str(uuid.uuid4())})
        assert denied.status_code == missing.status_code == 404, "не член ⇒ 404, как несуществующий (AC-12)"
        assert denied.json()["code"] == missing.json()["code"]
        # Проект чужого workspace в фильтре ⇒ 404.
        own_ws = await outsider.create_workspace()
        foreign = await outsider.create_project(own_ws["id"])
        cross = await world.user.client.get(
            "/api/v1/analytics/summary", params={"project_id": foreign["id"]}, headers=h
        )
        assert cross.status_code == 404
        no_header = await world.user.client.get("/api/v1/analytics/summary")
        assert no_header.status_code == 422
    finally:
        await viewer.client.aclose()
        await outsider.client.aclose()


def _cases(count: int) -> list[Any]:
    schema = schemathesis.openapi.from_path(OPENAPI_PATH)
    op = schema.find_operation_by_id("getAnalyticsSummary")
    drawn: list[Any] = []

    @hypothesis.settings(
        max_examples=count, deadline=None, database=None, suppress_health_check=list(HealthCheck), derandomize=True
    )
    @given(case=op.as_strategy())
    def collect(case: Any) -> None:
        drawn.append(case)

    collect()
    return drawn


async def test_analytics_contract_fuzz_never_5xx(world: Slice) -> None:
    for n, case in enumerate(_cases(40)):
        headers = {k: v for k, v in (case.headers or {}).items() if k.lower() not in ("cookie", "x-csrf-token")}
        if n % 2 == 0:  # половина случаев — в своём workspace, чтобы fuzz доходил до запросов к БД
            headers["X-Workspace-Id"] = world.ws
        r = await world.user.client.get("/api/v1/analytics/summary", headers=headers, params=case.query or None)
        assert r.status_code < 500, f"{case.query!r} {headers}: {r.status_code} {r.text}"
        validate_response("getAnalyticsSummary", r)
