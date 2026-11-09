"""AC-05 (P9-01): все межагентные сообщения run видны в Live Agent Chat, без скрытых рассуждений.

Run с handoff на Fake CLI (developer → REVIEW_REQUEST → reviewer → FIX_REQUEST → fix → повторное ревью), затем
события outbox `agent.message` проходят тем же обработчиком воркера (`aicd_worker.handlers._route`), что и в
проде, — проекция в чат. Проверяется:
* каждое сообщение `agent_messages` run ровно один раз присутствует в ленте `All Agents` (`GET /runs/{id}/chat`)
  с типом ACP, отправителем (роль + провайдер), получателем, временем и задачей;
* потоки `Role`/`Task`/провайдер содержат ровно сообщения своего фильтра;
* «статус» сообщения: в `agent_messages` хранятся только принятые брокером (ACCEPTED) сообщения, отклонённые —
  в ленте активности (`acp.rejected`) и не попадают в чат; каждое сообщение имеет запись доставки;
* в ответах чата и `GET /tasks/{id}/messages` нет полей скрытых рассуждений (thinking/reasoning/CoT) — только
  ACP payload и нормализованные данные.
UI-часть (отображение в браузере в реальном времени) — apps/web/e2e/live/mission-control.spec.ts (AC-05).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aicd_platform.outbox_dispatcher import dispatch_batch
from aicd_worker import handlers
from aicd_worker.runtime import Runtime
from tests.acceptance.support import rows
from tests.e2e.test_agent_handoff import scenario
from tests.support.contract import validate_response
from tests.support.phase3 import Slice, create_run, drive, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

HIDDEN_REASONING_KEYS = ("thinking", "reasoning", "chain_of_thought", "thought", "scratchpad", "cot", "inner_monologue")


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(k).lower() for k in value} | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def _hidden(value: Any) -> set[str]:
    return {k for k in _keys(value) if any(h == k or k.startswith(h + "_") for h in HIDDEN_REASONING_KEYS)}


async def _project_agent_messages(s: Slice, run_id: str) -> int:
    events: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        events.append(event)

    async def notify(_: uuid.UUID) -> None:
        return None

    for _ in range(100):
        if await dispatch_batch(s.worker, publish, notify) == 0:
            break
    rt = Runtime(engine=s.worker, orchestrator=None, loop=None, dispatch_execution=None)  # type: ignore[arg-type]
    mine = [e for e in events if e.get("event_type") == "agent.message" and e.get("run_id") == run_id]
    for event in mine:
        await handlers._route(rt, event, uuid.uuid4(), uuid.UUID(run_id), uuid.UUID(s.ws))
    return len(mine)


async def _feed(s: Slice, run_id: str, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    query: dict[str, Any] = {"order": "asc", "limit": 100, **params}
    for _ in range(50):
        r = await s.user.client.get(f"/api/v1/runs/{run_id}/chat", params=query)
        validate_response("getRunChat", r)
        assert r.status_code == 200, r.text
        assert not _hidden(r.json()), f"скрытые рассуждения в ответе чата: {_hidden(r.json())}"
        items += r.json()["items"]
        if r.json()["next_cursor"] is None:
            return items
        query["cursor"] = r.json()["next_cursor"]
    raise AssertionError("лента не закончилась")


async def test_ac05_every_agent_message_is_visible_in_live_chat_without_hidden_reasoning(world: Slice) -> None:
    await install_both(world.worker)
    world.scenario["name"] = scenario
    run_id = await create_run(world)
    assert await drive(world, run_id) == "COMPLETED"
    assert await _project_agent_messages(world, run_id) > 0

    messages = await rows(
        world,
        "SELECT m.id, m.message_type::text AS type, m.sender_role, m.sender_provider::text AS provider,"
        " m.recipient_role, m.task_id, m.created_at, m.payload,"
        " (SELECT count(*) FROM message_deliveries d WHERE d.message_id = m.id) AS deliveries"
        " FROM agent_messages m WHERE m.run_id = :r ORDER BY m.created_at",
        r=uuid.UUID(run_id),
    )
    assert len(messages) >= 4, "handoff: REVIEW_REQUEST, FIX_REQUEST, повторный REVIEW_REQUEST, итог ревью"
    assert {"REVIEW_REQUEST", "FIX_REQUEST", "TASK_COMPLETED"} <= {m["type"] for m in messages}
    assert all(m["deliveries"] >= 1 for m in messages), "каждое принятое сообщение имеет запись доставки"
    for m in messages:
        assert not _hidden(m["payload"]), f"ACP payload содержит скрытые рассуждения: {m['type']}"

    projected = {
        r["acp_message_id"]: r["id"]
        for r in await rows(
            world,
            "SELECT id, acp_message_id FROM chat_messages WHERE run_id = :r AND acp_message_id IS NOT NULL",
            r=uuid.UUID(run_id),
        )
    }
    feed = await _feed(world, run_id)
    by_id = {i["id"]: i for i in feed}
    agent_items = [i for i in feed if i["item_type"] == "agent_message"]
    assert len(agent_items) == len(messages) == len(projected), "каждое сообщение в ленте ровно один раз"
    for m in messages:
        item = by_id[str(projected[m["id"]])]
        assert item["acp_type"] == m["type"]
        assert item["sender"]["role"] == m["sender_role"] and item["sender"]["provider"] == m["provider"]
        assert item["sender_type"] == "agent" and item["created_at"]
        if m["recipient_role"] not in ("orchestrator", "user"):
            assert item["recipient_role"] == m["recipient_role"]
        if m["task_id"] is not None:
            assert item["task_id"] == str(m["task_id"])

    # Потоки Role / Task / провайдер — фильтры той же ленты.
    reviewer = {i["id"] for i in await _feed(world, run_id, stream="role", role="reviewer", item_type="agent_message")}
    expected = {str(projected[m["id"]]) for m in messages if "reviewer" in (m["sender_role"], m["recipient_role"])}
    assert reviewer == expected
    main_task = next(m["task_id"] for m in messages if m["type"] == "REVIEW_REQUEST")
    task_items = await _feed(world, run_id, stream="task", task_id=str(main_task), item_type="agent_message")
    assert task_items and all(i["task_id"] == str(main_task) for i in task_items)
    for provider in ("codex", "claude"):
        got = {i["id"] for i in await _feed(world, run_id, stream=provider, item_type="agent_message")}
        assert got == {str(projected[m["id"]]) for m in messages if m["provider"] == provider}

    # Входящие сообщения задачи (ACP-конверты) — тоже без скрытых рассуждений.
    inbox = await world.user.client.get(f"/api/v1/tasks/{main_task}/messages")
    validate_response("listTaskMessages", inbox)
    assert inbox.status_code == 200 and inbox.json()["items"]
    assert not _hidden(inbox.json())
