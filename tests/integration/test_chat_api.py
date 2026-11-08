"""P4-12: Live Agent Chat API — `GET /runs/{id}/chat`, `POST /runs/{id}/chat/messages`.

Комментарий пользователя: chat_messages + outbox `chat.message` (форма SSE `chat_message`) + audit, без
`agent_messages` (агентам не доставляется — API_DESIGN §12.2). Лента: потоки-фильтры, поиск, item_type, курсор
в обе стороны, RBAC/tenant-изоляция, идемпотентность; fuzz по контракту (нет 5xx, ответы по контракту).
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_platform.sse import OutboxRecord, envelope, project
from tests.conftest import UserSession, login
from tests.integration.test_runs_api import _cases, _send
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.contract import validate_response
from tests.support.events import validate_sse
from tests.support.phase3 import Slice, create_run, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

CANARY = "ghp_" + "B" * 36


async def _post(user: UserSession, run_id: str, body: dict[str, Any], key: str | None = None) -> Any:
    return await user.client.post(
        f"/api/v1/runs/{run_id}/chat/messages", json=body, headers=user.headers(idem_key=key or secrets.token_hex(12))
    )


async def _outbox(world: Slice, aggregate_id: str) -> list[dict[str, Any]]:
    async with transaction(world.worker, DbContext(actor_type="system")) as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, created_at, workspace_id, run_id, aggregate_type, aggregate_id, event_type,"
                    " schema_version, payload, correlation_id, causation_id FROM outbox_events"
                    " WHERE aggregate_id = :a ORDER BY created_at"
                ),
                {"a": aggregate_id},
            )
        ).mappings()
        return [dict(r) for r in rows]


async def _assert_sse_shape(world: Slice, row: dict[str, Any]) -> dict[str, Any]:
    """Outbox `chat.message` проецируется в валидное SSE-событие `chat.message` (sse-event.schema.json)."""
    record = OutboxRecord(**row)
    async with transaction(world.worker, DbContext(workspace_id=row["workspace_id"], actor_type="system")) as conn:
        projection = await project(conn, record)
    assert projection is not None and projection.event_type == "chat.message"
    env = envelope(
        {
            "event_id": uuid7(),
            "run_id": row["run_id"],
            "seq": 1,
            "event_type": projection.event_type,
            "occurred_at": row["created_at"],
            "payload": projection.payload,
        }
    )
    validate_sse(env)
    return projection.payload


async def test_post_comment_is_idempotent_audited_and_emits_sse_shaped_event(
    world: Slice, database: testdb.TestDatabase
) -> None:
    run_id = await create_run(world)
    key = secrets.token_hex(12)
    body = {"body": f"Заметка для команды, токен {CANARY}", "mentions": ["developer", "qa"]}
    first = await _post(world.user, run_id, body, key)
    validate_response("postChatMessage", first)
    assert first.status_code == 201, first.text
    msg = first.json()
    assert msg["sender_type"] == "user" and msg["sender_id"] == world.user.user_id and msg["message_kind"] == "text"
    assert CANARY not in msg["body"], "тело редактируется до сохранения"
    assert first.headers["Location"] == f"/api/v1/runs/{run_id}/chat?thread_id={msg['thread_id']}"
    replay = await _post(world.user, run_id, body, key)
    assert replay.status_code == 201 and replay.json() == msg and replay.headers.get("Idempotency-Replayed") == "true"
    other = await _post(world.user, run_id, {**body, "body": "другое"}, key)
    validate_response("postChatMessage", other)
    assert other.status_code == 422
    missing = await world.user.client.post(
        f"/api/v1/runs/{run_id}/chat/messages", json=body, headers=world.user.headers()
    )
    validate_response("postChatMessage", missing)
    assert missing.status_code == 400

    events = await _outbox(world, msg["id"])
    assert [e["event_type"] for e in events] == ["chat.message"], "повтор не порождает второе событие"
    payload = await _assert_sse_shape(world, events[0])
    assert payload["scope"] == "RUN" and payload["message_kind"] == "USER_TEXT"
    assert payload["mentions"] == ["developer", "qa"] and payload["chat_message_id"] == msg["id"]
    with orch.connect_admin(database) as conn:
        audit = conn.execute(
            "SELECT action, metadata FROM audit_events WHERE resource_id = %s", (msg["id"],)
        ).fetchall()
        acp = conn.execute("SELECT count(*) FROM agent_messages WHERE run_id = %s", (run_id,)).fetchone()
    assert [a[0] for a in audit] == ["chat.message.create"]
    assert "body" not in audit[0][1], "тело сообщения не пишется в audit"
    assert acp is not None and acp[0] == 0, "комментарий не доставляется агентам (не создаёт ACP-сообщений)"


async def test_post_comment_validation(world: Slice) -> None:
    run_id = await create_run(world)
    for body in (
        {"body": ""},
        {"body": "x" * 8001},
        {"body": "ok", "mentions": ["developer", "developer"]},
        {"body": "ok", "mentions": ["orchestrator"]},
        {"body": "ok", "extra": 1},
        {"body": "nul\u0000"},
        {"body": "ok", "thread_id": str(uuid7())},
    ):
        r = await _post(world.user, run_id, body)
        validate_response("postChatMessage", r)
        assert r.status_code == 422, (body, r.text)


async def _seed_feed(world: Slice, database: testdb.TestDatabase, run_id: str) -> dict[str, Any]:
    """Ряды ленты от разных источников: ACP (codex), поручение, approval-уведомление, комментарии."""
    tenant = orch.Tenant(uuid.UUID(world.ws), uuid.UUID(world.user.user_id), uuid.UUID(world.project), uuid7())
    with orch.connect_admin(database) as conn:
        orch.drive_run(conn, uuid.UUID(run_id), "DISCOVERY", "PLANNED", "RUNNING")
        task = orch.new_task(conn, tenant, uuid.UUID(run_id), key="dev")
        acp_id = uuid7()
        evidence_ref, gate_ref = uuid7(), uuid7()
        acp_payload = {
            "evidence_refs": [f"evidence:{evidence_ref}", f"gate_execution:{gate_ref}", f"evidence:{evidence_ref}"],
            "context": f"message:{uuid7()}",  # вид ссылки без представления в чате — не попадает в refs
        }
        conn.execute(
            "INSERT INTO agent_messages (id, workspace_id, run_id, task_id, sender_role, sender_provider,"
            " recipient_role, message_type, correlation_id, payload) VALUES (%s, %s, %s, %s, 'developer', 'codex',"
            " 'reviewer', 'REVIEW_REQUEST', %s, %s::jsonb)",
            (acp_id, world.ws, run_id, task, acp_id, json.dumps(acp_payload)),
        )
        thread = uuid7()
        conn.execute(
            "INSERT INTO chat_threads (id, workspace_id, run_id, scope, target_task_id) VALUES (%s, %s, %s, 'task', %s)",
            (thread, world.ws, run_id, task),
        )
        agent_chat = uuid7()
        conn.execute(
            "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, sender_role,"
            " sender_provider, body, message_kind, acp_message_id, mentions) VALUES (%s, %s, %s, %s, 'agent',"
            " 'developer', 'codex', 'REVIEW_REQUEST developer → reviewer: готово к ревью', 'acp_message', %s,"
            " '{reviewer}')",
            (agent_chat, world.ws, run_id, thread, acp_id),
        )
        # Спроецированное событие chat.message (как outbox-диспетчер) — источник ChatItem.seq.
        conn.execute(
            "INSERT INTO run_event_log (workspace_id, run_id, seq, event_id, event_type, payload, occurred_at)"
            " VALUES (%s, %s, 7, %s, 'chat.message', %s::jsonb, now())",
            (world.ws, run_id, uuid7(), json.dumps({"chat_message_id": str(agent_chat)})),
        )
    run = await world.user.client.get(f"/api/v1/runs/{run_id}")
    directive = await world.user.client.post(
        f"/api/v1/runs/{run_id}/directives",
        json={
            "target": {"type": "role", "role": "qa"},
            "directive_type": "CHANGE_REQUEST",
            "message": "Добавьте e2e-тест",
            "expected_run_version": run.json()["version"],
        },
        headers=world.user.headers(idem=True),
    )
    assert directive.status_code == 202, directive.text
    comment = await _post(world.user, run_id, {"body": "Проверю миграцию позже", "mentions": ["database"]})
    assert comment.status_code == 201, comment.text
    task_comment = await _post(world.user, run_id, {"body": "Комментарий к задаче", "thread_id": str(thread)})
    assert task_comment.status_code == 201, task_comment.text
    return {
        "task": task,
        "thread": thread,
        "agent_chat": agent_chat,
        "refs": [{"type": "evidence", "id": str(evidence_ref)}, {"type": "gate_execution", "id": str(gate_ref)}],
        "directive": directive.json(),
        "comment": comment.json()["id"],
        "task_comment": task_comment.json()["id"],
    }


async def test_feed_streams_filters_search_and_pagination(world: Slice, database: testdb.TestDatabase) -> None:
    run_id = await create_run(world)
    seed = await _seed_feed(world, database, run_id)
    client = world.user.client
    url = f"/api/v1/runs/{run_id}/chat"

    async def ids(**params: Any) -> list[str]:
        r = await client.get(url, params=params)
        validate_response("getRunChat", r)
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    full = await client.get(url, params={"order": "asc"})
    validate_response("getRunChat", full)
    body = full.json()
    items = {i["id"]: i for i in body["items"]}
    order = [i["id"] for i in body["items"]]
    assert order == [
        str(seed["agent_chat"]),
        seed["directive"]["chat_message_id"],
        seed["comment"],
        seed["task_comment"],
    ]
    agent = items[str(seed["agent_chat"])]
    assert agent["item_type"] == "agent_message" and agent["acp_type"] == "REVIEW_REQUEST"
    assert agent["sender"]["provider"] == "codex" and agent["recipient_role"] == "reviewer"
    assert agent["task_id"] == str(seed["task"])
    assert agent["refs"] == seed["refs"] and agent["seq"] == 7, "refs/seq заполняются из сохранённых данных"
    directive = items[seed["directive"]["chat_message_id"]]
    assert directive["item_type"] == "directive" and directive["directive_id"] == seed["directive"]["directive_id"]
    assert directive["directive_status"] is not None and directive["recipient_role"] == "qa"
    comment = items[seed["comment"]]
    assert comment["refs"] == [] and comment["seq"] is None, "событие ещё не спроецировано в run_event_log"
    assert comment["item_type"] == "chat_message" and comment["sender"]["user"]["id"] == world.user.user_id
    scopes = {t["scope"] for t in body["threads"]}
    assert scopes == {"run", "task"}

    assert await ids(order="desc") == order[::-1]
    assert await ids(stream="codex") == [str(seed["agent_chat"])]
    assert await ids(stream="claude") == []
    assert set(await ids(stream="role", role="reviewer")) == {str(seed["agent_chat"])}
    assert set(await ids(stream="role", role="database")) == {seed["comment"]}
    assert set(await ids(stream="role", role="qa")) == {seed["directive"]["chat_message_id"]}
    assert set(await ids(stream="task", task_id=str(seed["task"]))) == {str(seed["agent_chat"]), seed["task_comment"]}
    assert await ids(thread_id=str(seed["thread"]), order="asc") == [str(seed["agent_chat"]), seed["task_comment"]]
    assert await ids(item_type=["directive", "agent_message"], order="asc") == order[:2]
    assert await ids(q="миграцию") == [seed["comment"]]
    assert await ids(stream="approvals") == []

    for invalid in ({"stream": "role"}, {"stream": "task"}, {"item_type": "nonsense"}, {"q": "a\u0000b"}):
        bad = await client.get(url, params=invalid)
        validate_response("getRunChat", bad)
        assert bad.status_code == 422, invalid

    for direction, expected in (("asc", order), ("desc", order[::-1])):
        seen: list[str] = []
        params: dict[str, Any] = {"limit": 1, "order": direction}
        first_cursor = None
        for _ in range(10):
            page = await client.get(url, params=params)
            validate_response("getRunChat", page)
            data = page.json()
            seen += [i["id"] for i in data["items"]]
            if data["next_cursor"] is None:
                break
            first_cursor = first_cursor or data["next_cursor"]
            params = {**params, "cursor": data["next_cursor"]}
        assert seen == expected, direction
        foreign = await client.get(url, params={"limit": 1, "order": direction, "cursor": first_cursor, "q": "x"})
        validate_response("getRunChat", foreign)
        assert foreign.status_code in (400, 422), "курсор привязан к фильтрам"


async def test_directive_chat_event_matches_sse_schema(world: Slice) -> None:
    run_id = await create_run(world)
    run = await world.user.client.get(f"/api/v1/runs/{run_id}")
    r = await world.user.client.post(
        f"/api/v1/runs/{run_id}/directives",
        json={
            "target": {"type": "role", "role": "developer"},
            "directive_type": "CHANGE_REQUEST",
            "message": "Используйте ISO 8601",
            "expected_run_version": run.json()["version"],
        },
        headers=world.user.headers(idem=True),
    )
    assert r.status_code == 202, r.text
    accepted = r.json()
    events = [e for e in await _outbox(world, accepted["chat_message_id"]) if e["event_type"] == "chat.message"]
    assert len(events) == 1
    payload = await _assert_sse_shape(world, events[0])
    assert payload["message_kind"] == "DIRECTIVE" and payload["directive_id"] == accepted["directive_id"]
    assert payload["target_role"] == "developer" and payload["scope"] == "RUN"


async def test_chat_rbac_and_tenant_isolation(app: FastAPI, app_engine: AsyncEngine, world: Slice) -> None:
    run_id = await create_run(world)
    outsider = await login(app, app_engine)
    r = await outsider.client.get(f"/api/v1/runs/{run_id}/chat")
    validate_response("getRunChat", r)
    assert r.status_code == 404, "чужой run неотличим от несуществующего"
    r = await _post(outsider, run_id, {"body": "чужой"})
    validate_response("postChatMessage", r)
    assert r.status_code == 404
    wrong_ws = await world.user.client.get(f"/api/v1/runs/{run_id}/chat", headers={"X-Workspace-Id": str(uuid7())})
    assert wrong_ws.status_code == 404
    roles: dict[str, UserSession] = {}
    for role in ("viewer", "reviewer", "developer"):
        member = await login(app, app_engine)
        added = await world.user.client.post(
            f"/api/v1/workspaces/{world.ws}/members",
            json={"email": member.email, "role": role},
            headers=world.user.headers(world.ws, idem=True),
        )
        assert added.status_code == 201, added.text
        roles[role] = member
    for role, member in roles.items():
        feed = await member.client.get(f"/api/v1/runs/{run_id}/chat")
        validate_response("getRunChat", feed)
        assert feed.status_code == 200, f"{role}: chat:read есть у всех ролей"
        posted = await _post(member, run_id, {"body": f"от {role}"})
        validate_response("postChatMessage", posted)
        assert posted.status_code == (403 if role == "viewer" else 201), f"{role}: chat:write — reviewer и выше"
    for session in (outsider, *roles.values()):
        await session.client.aclose()


@pytest.mark.parametrize("operation_id", ["getRunChat", "postChatMessage"])
async def test_chat_contract_fuzz_never_5xx(world: Slice, operation_id: str) -> None:
    run_id = await create_run(world)
    for i, case in enumerate(_cases(operation_id, 25)):
        if i % 2 == 0:  # половина — существующий run (реальная выборка и запись)
            case.path_parameters = {"run_id": run_id}
        r = await _send(world.user, case, world.ws)
        assert r.status_code < 500, f"{operation_id} {case.formatted_path} {case.body!r}: {r.status_code} {r.text}"
        validate_response(operation_id, r)
