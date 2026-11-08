"""Доставка, подтверждение и выполнение поручений (FR-CHAT-05, AC-06; EVENT_SCHEMAS §4, DATA_FLOW §3.4/§6.2).

DISPATCHED → ack DELIVERED на старте попытки задачи-получателя → ACKNOWLEDGED по принятому итогу агента
(`directive_acks`) → COMPLETED, когда задачи замены прошли проверки; ошибки — FAILED с причиной, отмена run —
ack EXPIRED; повторная обработка событий идемпотентна; исполнитель в другом процессе останавливает попытку
по флагу в БД (поручение / пауза run), без in-process хука оркестратора.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration import directives, runs
from aicd_orchestration.agent_io import AgentIoSummary
from aicd_orchestration.agents.result import DirectiveAck
from aicd_orchestration.approvals import Approvals
from aicd_orchestration.engine import Orchestrator, TaskExecution
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_platform.sse import OutboxRecord, envelope, project
from tests.support import db as testdb
from tests.support.contract import validate_response
from tests.support.events import validate_sse
from tests.support.orchestration import connect_admin
from tests.support.phase3 import Slice, create_run, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


# --- Обвязка -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Passed:
    passed: bool = True
    gate_execution_ids: tuple[uuid.UUID, ...] = ()
    summary: str = "ok"


class _Verifier:
    async def verify_task(self, **kwargs: Any) -> _Passed:
        del kwargs
        return _Passed()


@dataclass
class _Scripted:
    """Исполнитель-заглушка: итог агента по роли задачи (`results[role](task, run) -> TaskExecution`)."""

    results: dict[str, Callable[[dict[str, Any], dict[str, Any]], TaskExecution]]
    calls: list[str] = field(default_factory=list)

    async def execute(
        self, *, task: dict[str, Any], run: dict[str, Any], provider: str, **kwargs: Any
    ) -> TaskExecution:
        del kwargs
        self.calls.append(task["task_key"])
        return self.results[task["agent_role"]](task, {**run, "provider": provider})


def _io(*acks: DirectiveAck) -> AgentIoSummary:
    return AgentIoSummary(
        instruction_template_version="developer@1.0.0",
        prompt_sha256="0" * 64,
        prompt_artifact_id=None,
        result_status="parsed",
        directive_acks=acks,
    )


def _completed(*acks: DirectiveAck) -> Callable[[dict[str, Any], dict[str, Any]], TaskExecution]:
    def result(task: dict[str, Any], run: dict[str, Any]) -> TaskExecution:
        del task
        return TaskExecution(
            "completed", None, None, run["provider"], None, run["base_commit_sha"], uuid7(), agent_io=_io(*acks)
        )

    return result


def _failed(category: str) -> Callable[[dict[str, Any], dict[str, Any]], TaskExecution]:
    def result(task: dict[str, Any], run: dict[str, Any]) -> TaskExecution:
        del task
        return TaskExecution("failed", category, None, run["provider"], None, None, uuid7())

    return result


def _orchestrator(world: Slice, executor: Any, **extra: Any) -> Orchestrator:
    return Orchestrator(
        engine=world.worker,
        approvals=Approvals(),
        verifier=_Verifier(),
        executor=executor,
        artifacts=FileSystemArtifactStore(world.tmp / "artifacts"),
        **extra,
    )


@dataclass
class Scene:
    world: Slice
    database: testdb.TestDatabase
    run: uuid.UUID
    ws: uuid.UUID

    def admin(self) -> psycopg.Connection:
        return connect_admin(self.database)

    def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        with self.admin() as conn:
            return list(conn.execute(sql, params).fetchall())

    def tasks(self) -> dict[str, tuple[uuid.UUID, str, bool]]:
        """task_key → (id, status, superseded) живых (последних по plan_version) задач run."""
        out: dict[str, tuple[uuid.UUID, str, bool]] = {}
        for tid, key_, status, sup in self.rows(
            "SELECT id, task_key, status, superseded_at IS NOT NULL FROM tasks WHERE run_id = %s"
            " ORDER BY plan_version, created_at",
            self.run,
        ):
            out[key_] = (tid, status, sup)
        return out

    async def submit(self, dtype: str, target: dict[str, Any], message: str = "Добавьте экспорт в XLSX") -> str:
        [(version,)] = self.rows("SELECT version FROM runs WHERE id = %s", self.run)
        r = await self.world.user.client.post(
            f"/api/v1/runs/{self.run}/directives",
            json={
                "target": target,
                "directive_type": dtype,
                "message": message,
                "expected_run_version": version,
                "requires_impact_preview": False,
            },
            headers=self.world.user.headers(idem=True),
        )
        validate_response("createDirective", r)
        assert r.status_code == 202, r.text
        return str(r.json()["directive_id"])

    async def process(self, directive_id: str, orch: Orchestrator) -> dict[str, Any]:
        assert orch.artifacts is not None
        d = await directives.process(self.world.worker, self.ws, uuid.UUID(directive_id), artifacts=orch.artifacts)
        assert d is not None
        return d

    async def view(self, directive_id: str) -> dict[str, Any]:
        r = await self.world.user.client.get(f"/api/v1/runs/{self.run}/directives/{directive_id}")
        validate_response("getDirective", r)
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body

    def transitions(self, directive_id: str) -> list[str]:
        return [
            r[0]
            for r in self.rows(
                "SELECT payload->>'to' FROM outbox_events WHERE event_type = 'directive.state_changed'"
                " AND aggregate_id = %s ORDER BY created_at, id",
                uuid.UUID(directive_id),
            )
        ]

    def ack_events(self, directive_id: str) -> list[tuple[str, str]]:
        return [
            (r[0], r[1])
            for r in self.rows(
                "SELECT payload->>'recipient_role', payload->>'ack_status' FROM outbox_events"
                " WHERE event_type = 'directive.ack_changed' AND aggregate_id = %s ORDER BY created_at, id",
                uuid.UUID(directive_id),
            )
        ]

    def acks(self, directive_id: str) -> dict[str, tuple[str, bool, bool]]:
        """роль → (status, delivered_at задан, acknowledged_at задан)."""
        return {
            r[0]: (r[1], r[2], r[3])
            for r in self.rows(
                "SELECT recipient_role, status, delivered_at IS NOT NULL, acknowledged_at IS NOT NULL"
                " FROM directive_acknowledgements WHERE directive_id = %s",
                uuid.UUID(directive_id),
            )
        }

    def outbox_count(self, directive_id: str) -> int:
        [(n,)] = self.rows("SELECT count(*) FROM outbox_events WHERE aggregate_id = %s", uuid.UUID(directive_id))
        return int(n)


async def _scene(world: Slice, database: testdb.TestDatabase, orch: Orchestrator) -> Scene:
    """Run DEVELOP (developer `main` → reviewer `review`) в RUNNING; задачи ещё не исполнялись."""
    await install_both(world.worker)
    run, ws = uuid.UUID(await create_run(world)), uuid.UUID(world.ws)
    await orch.on_run_created(run, ws)
    scene = Scene(world, database, run, ws)
    statuses = {k: v[1] for k, v in scene.tasks().items()}
    assert statuses == {"main": "READY", "review": "PENDING"}, statuses
    return scene


async def _sse_payloads(engine: AsyncEngine, directive_id: str) -> list[dict[str, Any]]:
    """Проекция outbox-событий поручения в SSE (каждое — валидно по sse-event.schema.json)."""
    async with transaction(engine, DbContext(actor_type="system")) as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, created_at, workspace_id, run_id, aggregate_type, aggregate_id, event_type,"
                        " schema_version, payload, correlation_id, causation_id FROM outbox_events"
                        " WHERE aggregate_id = :a AND event_type IN ('directive.state_changed', 'directive.ack_changed')"
                        " ORDER BY created_at, id"
                    ),
                    {"a": uuid.UUID(directive_id)},
                )
            )
            .mappings()
            .all()
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        async with transaction(engine, DbContext(workspace_id=row["workspace_id"], actor_type="system")) as conn:
            projection = await project(conn, OutboxRecord(**dict(row)))
        assert projection is not None, row["event_type"]
        validate_sse(envelope({"event_id": uuid7(), "run_id": row["run_id"], "seq": len(out) + 1,
                               "event_type": projection.event_type, "occurred_at": row["created_at"],
                               "payload": projection.payload}))  # fmt: skip
        out.append(projection.payload)
    return out


# --- Сквозной сценарий на Fake CLI ---------------------------------------------------------------------


async def test_change_request_delivered_acknowledged_and_completed_on_fake_cli(
    world: Slice, database: testdb.TestDatabase
) -> None:
    """AC-06/FR-CHAT-05: CHANGE_REQUEST → DISPATCHED → DELIVERED на старте попыток → ACKNOWLEDGED (явные
    directive_acks агента) → COMPLETED после того, как задачи замены прошли обязательные gates."""
    orch = world.orchestrator()
    scene = await _scene(world, database, orch)
    did = await scene.submit("CHANGE_REQUEST", {"type": "run"})
    assert (await scene.process(did, orch))["status"] == "DISPATCHED"
    view = await scene.view(did)
    assert view["delivery"]["state"] == "DISPATCHED" and view["completion"]["state"] == "IN_PROGRESS"
    assert {(a["recipient_role"], a["status"], a["delivered_at"]) for a in view["delivery"]["acknowledgements"]} == {
        ("developer", "PENDING", None), ("reviewer", "PENDING", None)}  # fmt: skip
    resulting = {uuid.UUID(t) for t in view["completion"]["resulting_task_ids"]}
    assert {uuid.UUID(a["task_id"]) for a in view["delivery"]["acknowledgements"]} == resulting

    assert await orch.drive(scene.run, scene.ws, fast_forward_backoff=True) == "COMPLETED"
    view = await scene.view(did)
    assert view["status"] == "COMPLETED", view
    assert view["delivery"]["state"] == "ACKNOWLEDGED" and view["completion"]["state"] == "COMPLETED"
    assert view["completion"]["completed_at"]
    for ack in view["delivery"]["acknowledgements"]:
        assert ack["status"] == "ACKNOWLEDGED" and ack["delivered_at"] and ack["acknowledged_at"], ack
    assert scene.transitions(did)[-3:] == ["DISPATCHED", "ACKNOWLEDGED", "COMPLETED"]
    events = scene.ack_events(did)
    assert sorted(events) == sorted([("developer", "DELIVERED"), ("developer", "ACKNOWLEDGED"),
                                     ("reviewer", "DELIVERED"), ("reviewer", "ACKNOWLEDGED")])  # fmt: skip
    assert events.index(("developer", "ACKNOWLEDGED")) < events.index(("reviewer", "DELIVERED")), "по DAG"

    # Поручение ушло агенту разделом directives Context Pack, агент подтвердил его явно (а не неявно).
    prompts = scene.rows(
        "SELECT payload FROM agent_activity_events WHERE run_id = %s AND event_type = 'agent.prompt_assembled'"
        " AND task_id = ANY(%s)", scene.run, list(resulting),
    )  # fmt: skip
    assert len(prompts) == 2
    for (payload,) in prompts:
        refs = [f["ref"] for f in payload["fragments"] if f["kind"] == "directives"]
        assert refs == [f"directive:{did}"], payload["fragments"]
    results = scene.rows(
        "SELECT payload->'directive_acks' FROM agent_activity_events WHERE run_id = %s"
        " AND event_type = 'agent.result_processed' AND task_id = ANY(%s)", scene.run, list(resulting),
    )  # fmt: skip
    assert [r[0] for r in results] == [[{"directive_id": did, "status": "accepted", "note": None}]] * 2

    # SSE: переходы и изменения ack — валидные `directive.state_changed` (ack — с recipients).
    sse = await _sse_payloads(world.worker, did)
    recipients = [p["recipients"][0] for p in sse if "recipients" in p]
    assert {(r["role"], r["ack_status"]) for r in recipients} == set(events)
    assert all(p["directive_type"] == "CHANGE_REQUEST" for p in sse)
    assert [p["to_status"] for p in sse if "recipients" not in p][-2:] == ["ACKNOWLEDGED", "COMPLETED"]

    # Идемпотентность (at-least-once): повторные tick/доставка/подтверждение/отмена — без новых событий.
    before = scene.outbox_count(did)
    for _ in range(2):
        await orch.tick(scene.run, scene.ws)
        await orch.on_run_paused_or_cancelled(scene.run, scene.ws, "CANCELLED")
        async with transaction(world.worker, DbContext(workspace_id=scene.ws, actor_type="system")) as conn:
            for task_id in resulting:
                assert await directives.mark_delivered(conn, task_id) == 0
                assert await directives.acknowledge(conn, task_id, []) == 0
            assert await directives.reconcile_run(conn, scene.run) == []
    assert scene.outbox_count(did) == before and (await scene.view(did))["status"] == "COMPLETED"


# --- Пути ошибок (исполнитель-заглушка) ----------------------------------------------------------------


async def test_implicit_ack_and_completion_with_scripted_agent(world: Slice, database: testdb.TestDatabase) -> None:
    """Итог агента без записи о поручении — неявное accepted; COMPLETED, когда задачи замены SUCCEEDED."""
    executor = _Scripted({"developer": _completed(), "reviewer": _completed()})
    orch = _orchestrator(world, executor)
    scene = await _scene(world, database, orch)
    did = await scene.submit("CHANGE_REQUEST", {"type": "role", "role": "developer"})
    await scene.process(did, orch)
    assert set(scene.acks(did)) == {"developer"}
    main_v2, review_v2 = scene.tasks()["main"][0], scene.tasks()["review"][0]
    # Рёбра утверждённого плана неизменяемы: не начатый зависимый review пересоздан в новой plan_version.
    assert scene.rows("SELECT depends_on_task_id FROM task_dependencies WHERE task_id = %s", review_v2) == [(main_v2,)]
    assert scene.rows(
        "SELECT count(*) FROM tasks WHERE run_id = %s AND superseded_at IS NOT NULL AND status = 'CANCELLED'", scene.run
    ) == [(2,)]
    assert [i["task_id"] for i in (await scene.view(did))["impacts"] if i["impact_type"] == "TASK_ADD"] == [
        str(main_v2), str(review_v2)]  # fmt: skip

    # Попытка стартует: DELIVERED в транзакции LEASED → RUNNING (до исполнения CLI).
    async with transaction(world.worker, DbContext(workspace_id=scene.ws, actor_type="system")) as conn:
        assert await directives.prompt_fragments(conn, main_v2) != []
    await orch.drive(scene.run, scene.ws, fast_forward_backoff=True)
    assert executor.calls[0] == "main"
    assert scene.acks(did) == {"developer": ("ACKNOWLEDGED", True, True)}
    view = await scene.view(did)
    assert view["status"] == "COMPLETED" and view["completion"]["state"] == "COMPLETED", view
    assert scene.transitions(did)[-3:] == ["DISPATCHED", "ACKNOWLEDGED", "COMPLETED"]


async def test_agent_cannot_comply_rejects_and_fails_directive(world: Slice, database: testdb.TestDatabase) -> None:
    holder: dict[str, str] = {}

    def refuse(task: dict[str, Any], run: dict[str, Any]) -> TaskExecution:
        return _completed(DirectiveAck(holder["did"], "cannot_comply", "противоречит ТЗ"))(task, run)

    orch = _orchestrator(world, _Scripted({"developer": refuse, "reviewer": _completed()}))
    scene = await _scene(world, database, orch)
    holder["did"] = did = await scene.submit("CHANGE_REQUEST", {"type": "run"})
    await scene.process(did, orch)
    await orch.drive(scene.run, scene.ws, fast_forward_backoff=True)
    assert scene.acks(did)["developer"] == ("REJECTED", True, False)
    view = await scene.view(did)
    assert view["status"] == "FAILED" and view["completion"]["state"] == "FAILED"
    [(message,)] = scene.rows("SELECT failure_message FROM user_directives WHERE id = %s", uuid.UUID(did))
    assert "developer" in message and "cannot_comply" in message
    assert scene.transitions(did)[-2:] == ["DISPATCHED", "FAILED"]
    # reviewer ещё не начинал — его ack истекает вместе с поручением.
    assert scene.acks(did)["reviewer"] == ("EXPIRED", False, False)


async def test_replacement_task_failure_fails_directive(world: Slice, database: testdb.TestDatabase) -> None:
    executor = _Scripted({"developer": _failed("SANDBOX_DENIED"), "reviewer": _completed()})
    orch = _orchestrator(world, executor)
    scene = await _scene(world, database, orch)
    did = await scene.submit("CHANGE_REQUEST", {"type": "run"})
    await scene.process(did, orch)
    assert await orch.drive(scene.run, scene.ws, fast_forward_backoff=True) == "FAILED"
    view = await scene.view(did)
    assert view["status"] == "FAILED" and view["completion"]["state"] == "FAILED"
    [(message,)] = scene.rows("SELECT failure_message FROM user_directives WHERE id = %s", uuid.UUID(did))
    assert message == "задача main завершилась со статусом FAILED"
    # Доставлено developer (попытка была), но не подтверждено; reviewer не получил — оба EXPIRED.
    assert scene.acks(did) == {"developer": ("EXPIRED", True, False), "reviewer": ("EXPIRED", False, False)}


async def test_run_cancel_expires_acks_and_fails_directive(world: Slice, database: testdb.TestDatabase) -> None:
    orch = _orchestrator(world, _Scripted({}))
    scene = await _scene(world, database, orch)
    did = await scene.submit("CHANGE_REQUEST", {"type": "run"})
    await scene.process(did, orch)
    async with transaction(world.worker, DbContext(workspace_id=scene.ws, actor_type="system")) as conn:
        await runs.command(conn, run_id=scene.run, workspace_id=scene.ws, actor_id=uuid.UUID(world.user.user_id),
                           command="cancel", expected_version=None, reason="test")  # fmt: skip
    # Обработчик run.state_changed(CANCELLED) — дважды (повторная доставка события): эффект один.
    await orch.on_run_paused_or_cancelled(scene.run, scene.ws, "CANCELLED")
    count = scene.outbox_count(did)
    await orch.on_run_paused_or_cancelled(scene.run, scene.ws, "CANCELLED")
    assert scene.outbox_count(did) == count
    assert scene.acks(did) == {"developer": ("EXPIRED", False, False), "reviewer": ("EXPIRED", False, False)}
    view = await scene.view(did)
    assert view["status"] == "FAILED" and view["completion"]["state"] == "FAILED"
    assert {a["status"] for a in view["delivery"]["acknowledgements"]} == {"EXPIRED"}
    [(message,)] = scene.rows("SELECT failure_message FROM user_directives WHERE id = %s", uuid.UUID(did))
    assert message == "run отменён до выполнения поручения"
    assert scene.ack_events(did)[-2:] == [("developer", "EXPIRED"), ("reviewer", "EXPIRED")]


async def test_stop_run_directive_completes_when_run_cancelled(world: Slice, database: testdb.TestDatabase) -> None:
    orch = _orchestrator(world, _Scripted({}))
    scene = await _scene(world, database, orch)
    did = await scene.submit("STOP", {"type": "run"}, "Остановить")
    assert (await scene.process(did, orch))["status"] == "COMPLETED"
    assert scene.acks(did) == {}
    view = await scene.view(did)
    assert view["delivery"]["state"] == "ACKNOWLEDGED" and view["completion"]["state"] == "COMPLETED"
    assert scene.rows("SELECT status FROM runs WHERE id = %s", scene.run) == [("CANCELLED",)]


# --- Межпроцессный safe point: исполнитель видит флаг в БД ---------------------------------------------


@dataclass
class _Blocking:
    """Исполнение, которое длится, пока его не остановят через зарегистрированный хук отмены."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    reasons: list[str] = field(default_factory=list)

    async def execute(self, *, provider: str, register_cancel: Any, **kwargs: Any) -> TaskExecution:
        del kwargs
        stop = asyncio.Event()

        async def hook(reason: str) -> None:
            self.reasons.append(reason)
            stop.set()

        register_cancel(hook)
        self.started.set()
        await asyncio.wait_for(stop.wait(), timeout=30)
        return TaskExecution("cancelled", "CANCELLED", None, provider, None, None, uuid7())


async def _start_blocking(world: Slice, database: testdb.TestDatabase) -> tuple[Scene, _Blocking, asyncio.Task[None]]:
    blocking = _Blocking()
    # «Процесс exec»: свой экземпляр оркестратора; команды приходят из других экземпляров/процессов.
    exec_orch = _orchestrator(world, blocking, cancel_poll_seconds=0.05)
    scene = await _scene(world, database, exec_orch)
    main = scene.tasks()["main"][0]
    running = asyncio.create_task(exec_orch.execute_task(main, scene.ws))
    await asyncio.wait_for(blocking.started.wait(), timeout=30)
    assert scene.tasks()["main"][1] == "RUNNING"
    return scene, blocking, running


async def test_exec_stops_running_task_for_directive_applied_elsewhere(
    world: Slice, database: testdb.TestDatabase
) -> None:
    scene, blocking, running = await _start_blocking(world, database)
    main = scene.tasks()["main"][0]
    other = _orchestrator(world, _Scripted({}))  # «процесс orchestrator»: хуков отмены exec у него нет
    did = await scene.submit("CHANGE_REQUEST", {"type": "task", "task_id": str(main)})
    assert (await scene.process(did, other))["status"] == "DISPATCHED"
    await asyncio.wait_for(running, timeout=30)
    assert blocking.reasons == ["directive"], "safe point: исполнитель остановил CLI по флагу в БД"
    [(status, superseded)] = scene.rows("SELECT status, superseded_at IS NOT NULL FROM tasks WHERE id = %s", main)
    assert (status, superseded) == ("CANCELLED", True), "задача вытеснена заменой после остановки"
    # Попытка до dispatch поручение не получала: ack замены ждёт её собственного старта.
    assert scene.acks(did) == {"developer": ("PENDING", False, False)}


async def test_exec_stops_running_task_when_run_paused_elsewhere(world: Slice, database: testdb.TestDatabase) -> None:
    scene, blocking, running = await _start_blocking(world, database)
    async with transaction(world.worker, DbContext(workspace_id=scene.ws, actor_type="system")) as conn:
        await runs.command(conn, run_id=scene.run, workspace_id=scene.ws, actor_id=uuid.UUID(world.user.user_id),
                           command="pause", expected_version=None, reason="test")  # fmt: skip
    await asyncio.wait_for(running, timeout=30)
    assert blocking.reasons == ["pause"]
    [(status, reason, attempts)] = scene.rows(
        "SELECT status, blocked_reason, attempt_count FROM tasks WHERE id = %s", scene.tasks()["main"][0]
    )
    assert (status, reason, attempts) == ("BLOCKED", "RUN_PAUSED", 0), "пауза не расходует бюджет попыток"
