"""UI redesign B8 (G-15): персональные уведомления — генерация воркером из outbox, API, поток workspace.

* генерация `aicd_orchestration.notifications.project` из настоящих outbox-событий (approval через доменный сервис) и
  событий run/директив/PR/гейтов: получатели (кто может решить approval — four-eyes и роль; инициатор решения; автор
  run и поручения), повторная доставка (`duplicate`) и повтор после потери отметки `processed_events` (уникальный
  ключ — без дублей), выжимки редактированы;
* API `listNotifications` / `getNotificationUnreadCount` / `markNotificationsRead`: validate_response, фильтры,
  keyset-пагинация, семантика отметки (id — только свои, `up_to` включительно, `all`), идемпотентность, RBAC (все
  роли), изоляция пользователей одного workspace и чужого workspace (404), fuzz по контракту (никогда 5xx);
* поток `GET /workspaces/{id}/events`: кадр `notification.created` (схема notification-event.schema.json) получателю
  после сигнала worker-outbox, не другим участникам; повтор по `Last-Event-ID` (дедупликация по event_id).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import hypothesis
import psycopg
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration import notifications as gen
from aicd_orchestration.approvals import Approvals
from aicd_platform import sse
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_platform.outbox_dispatcher import dispatch_batch
from tests.conftest import UserSession, login
from tests.integration.test_sse import Env, Reader, _client, env, fast_stream, redis, server
from tests.support import db as testdb
from tests.support.contract import OPENAPI_PATH, validate_response
from tests.support.events import validate_notification_event
from tests.support.orch_api import Team, key, make_team, running_run
from tests.support.orchestration import Tenant, add_user, connect_admin, update

__all__ = ["env", "fast_stream", "redis", "server"]  # фикстуры pytest (поток workspace)

pytestmark = pytest.mark.db

ROLES = ("owner", "admin", "developer", "reviewer", "viewer")
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@dataclass
class World:
    team: Team
    run: uuid.UUID
    approvals: dict[str, uuid.UUID]
    outcomes: dict[str, str]

    @property
    def ws(self) -> str:
        return self.team.ws

    @property
    def tenant(self) -> Tenant:
        return self.team.tenant

    def uid(self, role: str) -> uuid.UUID:
        return uuid.UUID(self.team.users[role].user_id)


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


def _outbox_events(admin: psycopg.Connection, aggregate_id: uuid.UUID, event_type: str) -> list[dict[str, Any]]:
    rows = admin.execute(
        "SELECT id, created_at, workspace_id, run_id, aggregate_type, aggregate_id, event_type, schema_version,"
        " payload, correlation_id, causation_id FROM outbox_events WHERE aggregate_id = %s AND event_type = %s"
        " ORDER BY id",
        (aggregate_id, event_type),
    ).fetchall()
    return [
        sse.OutboxRecord(
            id=r[0], created_at=r[1], workspace_id=r[2], run_id=r[3], aggregate_type=r[4], aggregate_id=r[5],
            event_type=r[6], schema_version=r[7], payload=dict(r[8]), correlation_id=r[9], causation_id=r[10],
        ).message()
        for r in rows
    ]  # fmt: skip


def _event(world: World, event_type: str, payload: dict[str, Any], aggregate: uuid.UUID | None = None) -> dict[str, Any]:  # fmt: skip
    """Синтетическое сообщение диспетчера: потребитель всё равно перечитывает состояние из БД."""
    return {
        "event_id": str(uuid7()), "event_type": event_type, "schema_version": "1.0", "aggregate_type": "run",
        "aggregate_id": str(aggregate or world.run), "workspace_id": world.ws, "run_id": str(world.run),
        "occurred_at": "2026-11-09T00:00:00Z", "correlation_id": None, "causation_id": None, "payload": payload,
        "traceparent": None,
    }  # fmt: skip


def _rows(admin: psycopg.Connection, source: str) -> list[tuple[Any, ...]]:
    return admin.execute(
        "SELECT user_id, kind, severity, title, body, resource_type, resource_id, data FROM notifications"
        " WHERE source_event_id = %s ORDER BY user_id",
        (source,),
    ).fetchall()


@pytest.fixture(scope="module")
async def world(
    app: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, admin: psycopg.Connection
) -> AsyncIterator[World]:
    team = await make_team(app, app_engine, admin, ("admin", "developer", "reviewer", "viewer"))
    run = running_run(admin, team.tenant)
    update(admin, "runs", run, objective=f"Fix login, token {SECRET}")
    w = World(team, run, {}, {})
    ws = team.tenant.workspace_id
    async with transaction(app_engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        # four-eyes, admin+: решает только owner (инициатор admin исключён, остальные ниже по роли).
        w.approvals["migration"] = await Approvals().request(
            conn, workspace_id=ws, project_id=team.tenant.project_id, run_id=run, task_id=None,
            action_type="DESTRUCTIVE_MIGRATION", risk_level="high", subject={"summary": f"drop table, key {SECRET}"},
            requested_by_type="user", requested_by=w.uid("admin"),
        )  # fmt: skip
        # reviewer+, без four-eyes, запрошен системой: owner, admin, developer, reviewer.
        w.approvals["review"] = await Approvals().request(
            conn, workspace_id=ws, project_id=team.tenant.project_id, run_id=run, task_id=None,
            action_type="MANUAL_REVIEW", risk_level="medium", subject={"summary": "review"},
            requested_by_type="system", requested_by=None,
        )  # fmt: skip
    for name, approval in w.approvals.items():
        (event,) = _outbox_events(admin, approval, "approval.requested")
        w.outcomes[f"{name}.requested"] = await gen.project(worker_engine, event)
    async with transaction(app_engine, DbContext(user_id=w.uid("owner"), workspace_id=ws)) as conn:
        await Approvals().decide(
            conn, w.approvals["migration"], actor_id=w.uid("owner"), actor_role="owner", decision="APPROVED",
            reason="ok", expected_version=None,
        )  # fmt: skip
    (decided,) = _outbox_events(admin, w.approvals["migration"], "approval.decided")
    w.outcomes["migration.decided"] = await gen.project(worker_engine, decided)
    for name, event_type, payload in (
        ("run.failed", "run.completed", {"final_status": "FAILED"}),
        ("run.blocked", "run.state_changed", {"from": "RUNNING", "to": "BLOCKED", "blocked_reason": "MANUAL_REVIEW"}),
        ("run.reblocked", "run.state_changed", {"from": "BLOCKED", "to": "BLOCKED", "blocked_reason": "AUTH_REQUIRED"}),
        ("run.cancelled", "run.completed", {"final_status": "CANCELLED"}),
        ("gate.critical", "gate.completed", {"gate_execution_id": str(uuid7()), "gate_type": "sast", "status": "FAIL",
                                             "findings": {"critical": 2, "high": 1}}),
        ("gate.clean", "gate.completed", {"gate_execution_id": str(uuid7()), "findings": {"critical": 0}}),
        ("pr.merged", "external_action.state_changed", {"action_id": str(uuid7()), "action_type": "pr_merge",
                                                        "from": "EXECUTING", "to": "CONFIRMED", "external_ref": "#12"}),
        ("pr.failed", "external_action.state_changed", {"action_id": str(uuid7()), "action_type": "pr_create",
                                                        "from": "EXECUTING", "to": "FAILED"}),
        ("pr.push", "external_action.state_changed", {"action_id": str(uuid7()), "action_type": "git_push",
                                                      "from": "EXECUTING", "to": "CONFIRMED"}),
    ):  # fmt: skip
        event = _event(w, event_type, payload)
        w.outcomes[name] = await gen.project(worker_engine, event)
        w.outcomes[name + ".event"] = event["event_id"]
    yield w
    for session in team.users.values():
        await session.client.aclose()


@pytest.fixture(scope="module")
async def mallory(app: FastAPI, app_engine: AsyncEngine) -> AsyncIterator[tuple[UserSession, str]]:
    session = await login(app, app_engine, ip="203.0.113.88")
    ws = await session.create_workspace()
    yield session, ws["id"]
    await session.client.aclose()


# --- Генерация ------------------------------------------------------------------------------------------------


async def test_approval_recipients_follow_decider_rules(world: World, admin: psycopg.Connection) -> None:
    assert world.outcomes["migration.requested"] == "notified"
    (event,) = _outbox_events(admin, world.approvals["migration"], "approval.requested")
    rows = _rows(admin, event["event_id"])
    assert [r[0] for r in rows] == [world.uid("owner")], "four-eyes: инициатор admin не получает, роли ниже — тоже"
    _uid, kind, severity, title, body, rtype, rid, data = rows[0]
    assert (kind, severity, rtype, rid) == ("approval.requested", "warning", "approval", world.approvals["migration"])
    assert data == {"action_type": "DESTRUCTIVE_MIGRATION", "risk_level": "high"}
    assert SECRET not in body and SECRET not in title and "drop table" in body, "выжимка редактирована"
    (review,) = _outbox_events(admin, world.approvals["review"], "approval.requested")
    got = {r[0] for r in _rows(admin, review["event_id"])}
    assert got == {world.uid(r) for r in ("owner", "admin", "developer", "reviewer")}, "viewer не решает approvals"


async def test_decision_notifies_requester_only(world: World, admin: psycopg.Connection) -> None:
    (event,) = _outbox_events(admin, world.approvals["migration"], "approval.decided")
    rows = _rows(admin, event["event_id"])
    assert [(r[0], r[1], r[2], r[7]["status"]) for r in rows] == [
        (world.uid("admin"), "approval.decided", "success", "APPROVED")
    ]


async def test_run_gate_and_pr_events(world: World, admin: psycopg.Connection) -> None:
    owner = world.uid("owner")
    expected = {
        "run.failed": ("run.failed", "error", "run"),
        "run.blocked": ("run.blocked", "warning", "run"),
        "gate.critical": ("finding.critical", "error", "gate_execution"),
        "pr.merged": ("pull_request.merged", "success", "external_action"),
        "pr.failed": ("pull_request.failed", "error", "external_action"),
    }
    for name, (kind, severity, rtype) in expected.items():
        assert world.outcomes[name] == "notified", name
        rows = _rows(admin, world.outcomes[name + ".event"])
        assert [(r[0], r[1], r[2], r[5]) for r in rows] == [(owner, kind, severity, rtype)], name
        assert SECRET not in rows[0][4] and "Fix login" in rows[0][4]
    assert _rows(admin, world.outcomes["gate.critical.event"])[0][7]["critical"] == 2
    assert _rows(admin, world.outcomes["pr.merged.event"])[0][7]["external_ref"] == "#12"
    for name in ("run.reblocked", "run.cancelled", "gate.clean", "pr.push"):
        assert world.outcomes[name] == "no_recipients", name
        assert _rows(admin, world.outcomes[name + ".event"]) == []


async def test_directive_status_notifies_author(world: World, admin: psycopg.Connection, worker_engine: AsyncEngine) -> None:  # fmt: skip
    ws, run, author = world.tenant.workspace_id, world.run, world.uid("reviewer")
    thread, message, directive = uuid7(), uuid7(), uuid7()
    admin.execute(
        "INSERT INTO chat_threads (id, workspace_id, run_id, scope) VALUES (%s, %s, %s, 'all_agents')",
        (thread, ws, run),
    )
    admin.execute(
        "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, sender_id, body, message_kind)"
        " VALUES (%s, %s, %s, %s, 'user', %s, 'ask', 'directive')",
        (message, ws, run, thread, author),
    )
    admin.execute(
        "INSERT INTO user_directives (id, workspace_id, run_id, chat_message_id, target_type, directive_type,"
        " submitted_by) VALUES (%s, %s, %s, %s, 'run', 'ASK', %s)",
        (directive, ws, run, message, author),
    )
    for to, kind in (("ACKNOWLEDGED", "directive.acknowledged"), ("COMPLETED", "directive.completed"),
                     ("REJECTED", "directive.failed"), ("DISPATCHED", None)):  # fmt: skip
        payload = {"directive_id": str(directive), "directive_type": "ASK", "from": "X", "to": to}
        event = _event(world, "directive.state_changed", payload, directive)
        outcome = await gen.project(worker_engine, event)
        rows = _rows(admin, event["event_id"])
        if kind is None:
            assert outcome == "no_recipients" and rows == []
        else:
            assert [(r[0], r[1], r[5], r[6]) for r in rows] == [(author, kind, "directive", directive)], to


async def test_redelivery_is_idempotent(world: World, admin: psycopg.Connection, worker_engine: AsyncEngine) -> None:
    (event,) = _outbox_events(admin, world.approvals["review"], "approval.requested")
    before = _rows(admin, event["event_id"])
    outbox_before = admin.execute(
        "SELECT count(*) FROM outbox_events WHERE event_type = 'notification.created' AND causation_id = %s",
        (event["event_id"],),
    ).fetchone()
    assert outbox_before == (len(before),), "по одному notification.created на созданную строку"
    assert await gen.project(worker_engine, event) == "duplicate"
    # Потерянная отметка processed_events (например, retention) — уникальный ключ всё равно не даёт дублей.
    admin.execute(
        "DELETE FROM processed_events WHERE consumer = %s AND event_id = %s", (gen.CONSUMER, event["event_id"])
    )
    assert await gen.project(worker_engine, event) == "no_recipients"
    assert _rows(admin, event["event_id"]) == before
    after = admin.execute(
        "SELECT count(*) FROM outbox_events WHERE event_type = 'notification.created' AND causation_id = %s",
        (event["event_id"],),
    ).fetchone()
    assert after == outbox_before
    # Конкурентные дубли (at-least-once): ровно одна обработка.
    fresh = _event(world, "run.completed", {"final_status": "COMPLETED"})
    outcomes = await asyncio.gather(*(gen.project(worker_engine, dict(fresh)) for _ in range(4)))
    assert sorted(outcomes) == ["duplicate"] * 3 + ["notified"]
    assert len(_rows(admin, fresh["event_id"])) == 1
    assert await gen.project(worker_engine, {**fresh, "event_type": "chat.message"}) == "ignored"


async def test_removed_member_is_skipped(
    world: World, admin: psycopg.Connection, app_engine: AsyncEngine, worker_engine: AsyncEngine
) -> None:
    """Получатель, переставший быть участником к моменту обработки, пропускается (без ошибки и повтора)."""
    extra = add_user(admin, world.tenant, "owner")
    async with transaction(app_engine, DbContext(workspace_id=world.tenant.workspace_id, actor_type="system")) as conn:  # fmt: skip
        approval = await Approvals().request(
            conn, workspace_id=world.tenant.workspace_id, project_id=world.tenant.project_id, run_id=world.run,
            task_id=None, action_type="PRODUCTION_DEPLOY", risk_level="critical", subject={"summary": "deploy"},
            requested_by_type="system", requested_by=None,
        )  # fmt: skip
    (event,) = _outbox_events(admin, approval, "approval.requested")
    admin.execute(
        "DELETE FROM workspace_members WHERE workspace_id = %s AND user_id = %s", (world.tenant.workspace_id, extra)
    )
    assert await gen.project(worker_engine, event) == "notified"
    rows = _rows(admin, event["event_id"])
    assert [(r[0], r[2]) for r in rows] == [(world.uid("owner"), "error")], "critical ⇒ error; удалённый пропущен"


# --- API --------------------------------------------------------------------------------------------------------


async def _list(user: UserSession, ws: str, **params: Any) -> Any:
    r = await user.client.get("/api/v1/notifications", params=params or None, headers={"X-Workspace-Id": ws})
    validate_response("listNotifications", r)
    return r


async def _count(user: UserSession, ws: str) -> Any:
    r = await user.client.get("/api/v1/notifications/unread-count", headers={"X-Workspace-Id": ws})
    validate_response("getNotificationUnreadCount", r)
    return r


async def _mark(user: UserSession, ws: str, body: Any, idem: str | None = None) -> Any:
    r = await user.client.post(
        "/api/v1/notifications/mark-read", json=body, headers=user.headers(ws, idem=True, idem_key=idem)
    )
    validate_response("markNotificationsRead", r)
    return r


async def test_list_filters_pagination_and_isolation(world: World) -> None:
    owner = world.team.users["owner"]
    r = await _list(owner, world.ws)
    assert r.status_code == 200
    body = r.json()
    items = body["items"]
    kinds = {i["kind"] for i in items}
    assert {"approval.requested", "run.failed", "run.blocked", "finding.critical", "pull_request.merged"} <= kinds
    assert "approval.decided" not in kinds, "решение получил инициатор (admin), не владелец"
    assert body["unread_count"] == len(items) and all(not i["read"] and i["read_at"] is None for i in items)
    stamps = [(i["created_at"], i["id"]) for i in items]
    assert stamps == sorted(stamps, reverse=True), "новые сверху"
    assert all(SECRET not in json.dumps(i) for i in items)
    # keyset-пагинация
    seen: list[str] = []
    cursor = None
    while True:
        page = (await _list(owner, world.ws, limit=2, **({"cursor": cursor} if cursor else {}))).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [i["id"] for i in items]
    r = await _list(owner, world.ws, cursor=cursor or "bogus.cursor")
    assert r.status_code == 422 and r.json()["code"] == "invalid_cursor"
    filtered = (await _list(owner, world.ws, kind=["run.failed", "run.blocked"])).json()["items"]
    assert {i["kind"] for i in filtered} == {"run.failed", "run.blocked"}
    # Другой участник того же workspace не видит уведомления владельца.
    admin_items = (await _list(world.team.users["admin"], world.ws)).json()["items"]
    assert not {i["id"] for i in admin_items} & {i["id"] for i in items}
    assert {i["kind"] for i in admin_items} == {"approval.requested", "approval.decided"}
    viewer = (await _list(world.team.users["viewer"], world.ws)).json()
    assert viewer == {"items": [], "next_cursor": None, "unread_count": 0}


async def test_mark_read_semantics_and_idempotency(world: World) -> None:
    dev = world.team.users["developer"]
    items = (await _list(dev, world.ws)).json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "approval.requested"
    owner = world.team.users["owner"]
    owner_items = (await _list(owner, world.ws)).json()["items"]
    assert len(owner_items) >= 5
    unread = (await _count(owner, world.ws)).json()
    assert unread["unread_count"] == len(owner_items) and unread["latest_unread_at"] == owner_items[0]["created_at"]
    # Чужой id (developer) в списке игнорируется; свой — отмечается.
    target = owner_items[-1]["id"]
    r = await _mark(owner, world.ws, {"notification_ids": [target, items[0]["id"], str(uuid7())]})
    assert r.status_code == 200 and r.json() == {"marked": 1, "unread_count": len(owner_items) - 1}
    assert (await _list(dev, world.ws, unread_only="true")).json()["items"] == items, "чужое не тронуто"
    # Повтор с тем же Idempotency-Key — тот же ответ; новый ключ — 0 отмеченных (read_at не меняется).
    idem = key()
    first = await _mark(owner, world.ws, {"notification_ids": [target]}, idem)
    replay = await _mark(owner, world.ws, {"notification_ids": [target]}, idem)
    assert first.json() == replay.json() == {"marked": 0, "unread_count": len(owner_items) - 1}
    assert replay.headers.get("Idempotency-Replayed") == "true"
    read_at = next(i for i in (await _list(owner, world.ws)).json()["items"] if i["id"] == target)["read_at"]
    assert read_at is not None
    # up_to: всё до указанного включительно (по created_at, id).
    anchor = owner_items[-3]
    r = await _mark(owner, world.ws, {"up_to": anchor["id"]})
    assert r.status_code == 200 and r.json()["marked"] == 2
    unread_items = (await _list(owner, world.ws, unread_only="true")).json()["items"]
    assert [i["id"] for i in unread_items] == [i["id"] for i in owner_items[:-3]]
    # up_to чужого или несуществующего уведомления — 404, неотличимо.
    r1 = await _mark(owner, world.ws, {"up_to": items[0]["id"]})
    r2 = await _mark(owner, world.ws, {"up_to": str(uuid7())})
    assert r1.status_code == r2.status_code == 404
    assert {k: v for k, v in r1.json().items() if k not in ("instance", "request_id")} == {
        k: v for k, v in r2.json().items() if k not in ("instance", "request_id")
    }
    # all
    r = await _mark(owner, world.ws, {"all": True})
    assert r.json() == {"marked": len(owner_items) - 3, "unread_count": 0}
    assert (await _count(owner, world.ws)).json() == {"unread_count": 0, "latest_unread_at": None}
    assert (await _list(dev, world.ws)).json()["unread_count"] == 1
    # Валидация: ровно одно поле.
    invalid: list[dict[str, Any]] = [
        {}, {"all": True, "up_to": target}, {"all": False}, {"notification_ids": []}, {"x": 1},
        {"notification_ids": [str(uuid7()) for _ in range(101)]},
    ]  # fmt: skip
    for bad in invalid:
        r = await _mark(owner, world.ws, bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    r = await owner.client.post("/api/v1/notifications/mark-read", json={"all": True}, headers=owner.headers(world.ws))
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_missing"
    validate_response("markNotificationsRead", r)


async def test_mark_read_is_audited(world: World, admin: psycopg.Connection) -> None:
    rows = admin.execute(
        "SELECT actor_id, metadata FROM audit_events WHERE action = 'notification.marked_read' AND workspace_id = %s",
        (world.tenant.workspace_id,),
    ).fetchall()
    assert rows and all(r[0] == world.uid("owner") for r in rows)
    assert {r[1]["mode"] for r in rows} >= {"ids", "up_to", "all"}
    assert all("notification_ids" not in r[1] for r in rows)


@pytest.mark.parametrize("role", ROLES)
async def test_rbac_every_role_reads_and_marks_own(world: World, role: str) -> None:
    user = world.team.users[role]
    assert (await _list(user, world.ws)).status_code == 200
    assert (await _count(user, world.ws)).status_code == 200
    assert (await _mark(user, world.ws, {"all": True})).status_code == 200


async def test_foreign_workspace_and_anonymous(world: World, mallory: tuple[UserSession, str], app: FastAPI) -> None:
    user, own_ws = mallory
    for r in (
        await _list(user, world.ws),
        await _count(user, world.ws),
        await _mark(user, world.ws, {"all": True}),
        await _list(user, str(uuid7())),
    ):
        assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert (await _list(user, own_ws)).json()["items"] == []
    victim_item = (await _list(world.team.users["developer"], world.ws)).json()["items"][0]["id"]
    r = await _mark(user, own_ws, {"up_to": victim_item})
    assert r.status_code == 404
    r = await _mark(user, own_ws, {"notification_ids": [victim_item]})
    assert r.json()["marked"] == 0
    from tests.conftest import make_client

    async with make_client(app) as anon:
        r = await anon.get("/api/v1/notifications", headers={"X-Workspace-Id": world.ws})
        assert r.status_code == 401


def _cases(operation_id: str, count: int) -> list[Any]:
    schema = schemathesis.openapi.from_path(OPENAPI_PATH)
    op = schema.find_operation_by_id(operation_id)
    drawn: list[Any] = []

    @hypothesis.settings(
        max_examples=count, deadline=None, database=None, suppress_health_check=list(HealthCheck), derandomize=True
    )
    @given(case=op.as_strategy())
    def collect(case: Any) -> None:
        drawn.append(case)

    collect()
    return drawn


@pytest.mark.parametrize("op_id", ["listNotifications", "getNotificationUnreadCount", "markNotificationsRead"])
async def test_contract_fuzz_never_5xx_and_responses_conform(world: World, op_id: str) -> None:
    user = world.team.users["reviewer"]
    for case in _cases(op_id, 25):
        headers = {k: v for k, v in (case.headers or {}).items() if k.lower() not in ("cookie", "x-csrf-token")}
        headers["X-Workspace-Id"] = world.ws
        headers["X-CSRF-Token"] = user.csrf
        kwargs: dict[str, Any] = {"headers": headers, "params": case.query or None}
        if case.method.upper() == "POST":
            headers["Idempotency-Key"] = key()
            kwargs["json"] = case.body if isinstance(case.body, dict | list | str | int | float | bool) else None
        r = await user.client.request(case.method.upper(), f"/api/v1{case.formatted_path}", **kwargs)
        assert r.status_code < 500, f"{op_id} {case.query!r} {case.body!r}: {r.status_code} {r.text}"
        validate_response(op_id, r)


# --- Поток workspace ------------------------------------------------------------------------------------------


@pytest.mark.redis
async def test_notification_frames_on_workspace_stream(
    server: str, env: Env, user: UserSession, app: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine
) -> None:
    ws = env.tenant.workspace_id
    member = await login(app, app_engine)
    try:
        r = await user.client.post(
            f"/api/v1/workspaces/{ws}/members", json={"email": member.email, "role": "viewer"},
            headers=user.headers(idem=True),
        )  # fmt: skip
        assert r.status_code == 201, r.text

        async def dispatch() -> None:
            async def publish(event: dict[str, Any]) -> None:
                return None

            async def notify(run_id: uuid.UUID) -> None:
                await env.redis.publish(sse.run_channel(run_id), "1")

            async def notify_workspace(workspace_id: uuid.UUID) -> None:
                await env.redis.publish(sse.workspace_channel(workspace_id), "1")

            while await dispatch_batch(env.worker_engine, publish, notify, notify_workspace=notify_workspace):
                pass

        url = f"/workspaces/{ws}/events"
        params = {"types": "notification.created"}
        async with _client(server, user) as client, _client(server, member) as other:
            async with client.stream("GET", url, params=params) as mine, other.stream("GET", url, params=params) as theirs:  # fmt: skip
                assert mine.status_code == theirs.status_code == 200
                reader, other_reader = Reader(mine), Reader(theirs)
                await reader.heartbeat()
                await other_reader.heartbeat()
                event = {
                    "event_id": str(uuid7()), "event_type": "run.completed", "workspace_id": str(ws),
                    "run_id": str(env.run), "aggregate_id": str(env.run), "payload": {"final_status": "COMPLETED"},
                }  # fmt: skip
                assert await gen.project(worker_engine, event) == "notified"
                await dispatch()  # outbox notification.created ⇒ сигнал aicd:ws:{id}
                frames, closed = await reader.frames(1, wait=5.0)
                assert not closed
                data = [f for f in frames if f.get("event") == "notification.created"]
                assert len(data) == 1 and data[0]["id"].isdigit()
                body = json.loads(data[0]["data"])
                validate_notification_event(body)
                assert body["payload"]["kind"] == "run.completed" and body["run_id"] == str(env.run)
                assert body["event_id"] == body["payload"]["id"] and body["seq"] is None
                cursor = data[0]["id"]
                # Другой участник того же workspace уведомление владельца не получает (кадров с id нет).
                with pytest.raises(TimeoutError):
                    await other_reader.frames(1, wait=1.5)
            # Повтор по Last-Event-ID (перекрытие 60 с): тот же event_id — клиент дедуплицирует.
            async with client.stream("GET", url, headers={"Last-Event-ID": cursor}, params=params) as again:
                frames, _ = await Reader(again).frames(1, wait=5.0)
                replayed = [json.loads(f["data"]) for f in frames if f.get("event") == "notification.created"]
                assert [b["event_id"] for b in replayed] == [body["event_id"]]
            # Поток без фильтра типов тоже несёт уведомление.
            async with client.stream("GET", url, headers={"Last-Event-ID": cursor}) as full:
                frames, _ = await Reader(full).frames(1, wait=5.0)
                assert [json.loads(f["data"])["event_id"] for f in frames if "id" in f] == [body["event_id"]]
    finally:
        await member.client.aclose()
