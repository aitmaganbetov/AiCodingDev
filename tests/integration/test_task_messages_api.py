"""P4-12: `GET /tasks/{id}`, `/tasks/{id}/attempts`, `/tasks/{id}/messages` — контракт, фильтры, пагинация, RBAC.

ACP-сообщения пишет только брокер (роль `aicd_worker`); здесь строки `agent_messages` сеются суперпользователем,
API читает их под `aicd_app` в RLS-контексте workspace. Fuzz (Schemathesis): нет 5xx, ответы по контракту.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.ids import uuid7
from tests.conftest import login
from tests.integration.test_runs_api import _cases, _send
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.acp import valid_payload
from tests.support.contract import validate_response
from tests.support.phase3 import Slice, create_run, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

CANARY = "ghp_" + "A" * 36  # формат GitHub-токена: должен редактироваться на чтении


@dataclass
class TaskScene:
    run: str
    task: uuid.UUID
    other_task: uuid.UUID
    attempt: uuid.UUID
    messages: dict[str, uuid.UUID]
    correlation: uuid.UUID


def _insert_message(conn: Any, ws: str, run: str, **cols: Any) -> uuid.UUID:
    mid = uuid7()
    row = {
        "id": mid,
        "workspace_id": ws,
        "run_id": run,
        "task_id": None,
        "task_attempt_id": None,
        "sender_role": "orchestrator",
        "sender_provider": None,
        "sender_user_id": None,
        "recipient_role": "developer",
        "recipient_task_id": None,
        "message_type": "TASK_ASSIGN",
        "correlation_id": mid,
        "reply_to_id": None,
        "payload": {},
        **cols,
    }
    row["payload"] = json.dumps(row["payload"])
    names = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    conn.execute(f"INSERT INTO agent_messages ({names}) VALUES ({marks})", list(row.values()))
    return mid


async def _scene(world: Slice, database: testdb.TestDatabase) -> TaskScene:
    run = await create_run(world)
    tenant = orch.Tenant(uuid.UUID(world.ws), uuid.UUID(world.user.user_id), uuid.UUID(world.project), uuid7())
    with orch.connect_admin(database) as conn:
        orch.drive_run(conn, uuid.UUID(run), "DISCOVERY", "PLANNED", "RUNNING")
        task = orch.new_task(conn, tenant, uuid.UUID(run), key="dev")
        other = orch.new_task(conn, tenant, uuid.UUID(run), key="rev")
        conn.execute("UPDATE tasks SET agent_role = 'reviewer', version = version + 1 WHERE id = %s", (other,))
        orch.lease_task(conn, task)
        decision = uuid7()
        conn.execute(
            "INSERT INTO routing_decisions (id, workspace_id, task_id, outcome, selected_provider, complexity_level,"
            " reason, policy_version) VALUES (%s, %s, %s, 'SELECTED', 'codex', 2, 'H5: codex — лучший скор', 1)",
            (decision, world.ws, task),
        )
        attempt = orch.new_attempt(conn, tenant, task)
        conn.execute("UPDATE task_attempts SET routing_decision_id = %s WHERE id = %s", (decision, attempt))
        assign = _insert_message(
            conn,
            world.ws,
            run,
            recipient_task_id=task,
            payload=valid_payload("TASK_ASSIGN", task_id=str(task)),
        )
        question = _insert_message(
            conn,
            world.ws,
            run,
            task_id=task,
            task_attempt_id=attempt,
            sender_role="developer",
            sender_provider="codex",
            recipient_role="user",
            message_type="QUESTION",
            payload=valid_payload("QUESTION", question=f"Можно ли использовать {CANARY}?"),
        )
        answer = _insert_message(
            conn,
            world.ws,
            run,
            sender_role="user",
            sender_user_id=world.user.user_id,
            recipient_role="developer",
            recipient_task_id=task,
            message_type="ANSWER",
            correlation_id=question,
            reply_to_id=question,
            payload=valid_payload("ANSWER"),
        )
        foreign = _insert_message(conn, world.ws, run, recipient_role="reviewer", recipient_task_id=other)
    return TaskScene(
        run,
        task,
        other,
        attempt,
        {"assign": assign, "question": question, "answer": answer, "foreign": foreign},
        question,
    )


async def test_task_messages_contract_filters_and_pagination(world: Slice, database: testdb.TestDatabase) -> None:
    s = await _scene(world, database)
    client = world.user.client
    r = await client.get(f"/api/v1/tasks/{s.task}/messages")
    validate_response("listTaskMessages", r)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    ids = [i["message_id"] for i in items]
    # Отправленные задачей (task_id) и адресованные ей (to.task_id); чужая задача не попадает.
    assert ids == [str(s.messages[k]) for k in ("assign", "question", "answer")]
    by_type = {i["type"]: i for i in items}
    assert by_type["TASK_ASSIGN"]["from"] == {"role": "orchestrator", "provider": None, "user_id": None}
    assert by_type["ANSWER"]["from"]["user_id"] == world.user.user_id
    assert by_type["ANSWER"]["reply_to_id"] == str(s.messages["question"])
    assert by_type["QUESTION"]["task_attempt_id"] == str(s.attempt)
    assert CANARY not in r.text, "секреты в payload редактируются на чтении"

    typed = await client.get(f"/api/v1/tasks/{s.task}/messages", params={"type": ["QUESTION", "ANSWER"]})
    validate_response("listTaskMessages", typed)
    assert {i["type"] for i in typed.json()["items"]} == {"QUESTION", "ANSWER"}
    corr = await client.get(f"/api/v1/tasks/{s.task}/messages", params={"correlation_id": str(s.correlation)})
    assert [i["type"] for i in corr.json()["items"]] == ["QUESTION", "ANSWER"]
    bad = await client.get(f"/api/v1/tasks/{s.task}/messages", params={"type": "CHAT"})
    validate_response("listTaskMessages", bad)
    assert bad.status_code == 422

    seen: list[str] = []
    cursors: list[str] = []
    params: dict[str, Any] = {"limit": 1}
    for _ in range(5):
        page = await client.get(f"/api/v1/tasks/{s.task}/messages", params=params)
        validate_response("listTaskMessages", page)
        body = page.json()
        seen += [i["message_id"] for i in body["items"]]
        if body["next_cursor"] is None:
            break
        cursors.append(body["next_cursor"])
        params = {"limit": 1, "cursor": body["next_cursor"]}
    assert seen == ids, "курсор проходит выборку без пропусков и повторов"
    mismatch = await client.get(
        f"/api/v1/tasks/{s.task}/messages", params={"limit": 1, "cursor": cursors[0], "type": "ANSWER"}
    )
    validate_response("listTaskMessages", mismatch)
    assert mismatch.status_code in (400, 422), "курсор привязан к фильтрам выборки"


async def test_get_task_and_attempts_expose_links_and_routing_reason(
    world: Slice, database: testdb.TestDatabase
) -> None:
    s = await _scene(world, database)
    task = await world.user.client.get(f"/api/v1/tasks/{s.task}")
    validate_response("getTask", task)
    assert task.status_code == 200
    assert task.json()["links"]["messages"] == f"/api/v1/tasks/{s.task}/messages"
    assert task.headers["ETag"].startswith('"v')
    attempts = await world.user.client.get(f"/api/v1/tasks/{s.task}/attempts")
    validate_response("listTaskAttempts", attempts)
    [attempt] = attempts.json()["items"]
    assert attempt["id"] == str(s.attempt) and attempt["routing_reason"] == "H5: codex — лучший скор"


async def test_task_messages_rbac_and_tenant_isolation(
    app: FastAPI, app_engine: AsyncEngine, world: Slice, database: testdb.TestDatabase
) -> None:
    s = await _scene(world, database)
    outsider = await login(app, app_engine)
    for path in (f"/api/v1/tasks/{s.task}", f"/api/v1/tasks/{s.task}/attempts", f"/api/v1/tasks/{s.task}/messages"):
        r = await outsider.client.get(path)
        assert r.status_code == 404, f"{path}: чужая задача неотличима от несуществующей"
    foreign_header = await world.user.client.get(
        f"/api/v1/tasks/{s.task}/messages", headers={"X-Workspace-Id": str(uuid7())}
    )
    assert foreign_header.status_code == 404
    missing = await world.user.client.get(f"/api/v1/tasks/{uuid7()}/messages")
    validate_response("listTaskMessages", missing)
    assert missing.status_code == 404
    viewer = await login(app, app_engine)
    added = await world.user.client.post(
        f"/api/v1/workspaces/{world.ws}/members",
        json={"email": viewer.email, "role": "viewer"},
        headers=world.user.headers(world.ws, idem=True),
    )
    assert added.status_code == 201, added.text
    seen = await viewer.client.get(f"/api/v1/tasks/{s.task}/messages")
    validate_response("listTaskMessages", seen)
    assert seen.status_code == 200 and len(seen.json()["items"]) == 3, "run:read есть у всех ролей, включая viewer"
    await outsider.client.aclose()
    await viewer.client.aclose()


async def test_task_messages_contract_fuzz_never_5xx(world: Slice, database: testdb.TestDatabase) -> None:
    s = await _scene(world, database)
    for i, case in enumerate(_cases("listTaskMessages", 40)):
        if i % 2 == 0:  # половина — существующая задача (фильтры и курсор по реальным строкам)
            case.path_parameters = {"task_id": str(s.task)}
        r = await _send(world.user, case, world.ws)
        assert r.status_code < 500, f"{case.formatted_path} {case.query!r}: {r.status_code} {r.text}"
        validate_response("listTaskMessages", r)
