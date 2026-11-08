"""SSE-поток уведомлений workspace `GET /workspaces/{workspace_id}/events` (OQ-45 (c), API_DESIGN §13.1).

Тот же реальный HTTP-сервер, что и для потока run (tests/integration/test_sse.py): события run_event_log всех run
workspace подмножества типов, курсор `id:` (микросекунды), повтор с перекрытием и дедупликацией по event_id,
410 вне окна повтора, heartbeat без `id:`, закрытие потока после удаления участника, изоляция workspace.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.routers import events
from aicd_platform import sse
from aicd_platform.clock import utcnow
from aicd_platform.outbox_dispatcher import dispatch_batch
from tests.conftest import UserSession, login
from tests.integration.test_sse import Env, Reader, _client, env, fast_stream, redis, server
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.contract import _registry, _rewrite_refs, openapi
from tests.support.events import validate_sse

__all__ = ["env", "fast_stream", "redis", "server"]  # фикстуры pytest

pytestmark = [pytest.mark.db, pytest.mark.redis]


def _validate_ws(body: dict[str, Any]) -> None:
    schema = _rewrite_refs(openapi()["components"]["schemas"]["WorkspaceSseEvent"])
    validator = Draft202012Validator(schema, registry=_registry(), format_checker=FormatChecker())
    errors = list(validator.iter_errors(body))
    assert not errors, errors[0].message


def _ws_events(frames: list[dict[str, str]]) -> list[dict[str, Any]]:
    result = []
    for frame in frames:
        if "data" not in frame:
            continue
        body = json.loads(frame["data"])
        _validate_ws(body)
        if frame.get("event") == "heartbeat":
            assert "id" not in frame
            continue
        validate_sse(body)
        assert frame["event"] == body["type"] and frame["id"].isdigit()
        body["_cursor"] = frame["id"]
        result.append(body)
    return result


async def _dispatch(env: Env) -> None:
    async def publish(event: dict[str, Any]) -> None:
        return None

    async def notify(run_id: uuid.UUID) -> None:
        await env.redis.publish(sse.run_channel(run_id), "1")

    async def notify_workspace(workspace_id: uuid.UUID) -> None:
        await env.redis.publish(sse.workspace_channel(workspace_id), "1")

    while await dispatch_batch(env.worker_engine, publish, notify, notify_workspace=notify_workspace):
        pass


async def test_workspace_stream_live_filter_and_resume(
    server: str, env: Env, user: UserSession, database: testdb.TestDatabase
) -> None:
    ws = env.tenant.workspace_id
    with orch.connect_admin(database) as conn:
        second_run = orch.new_run(conn, env.tenant)
    url = f"/workspaces/{ws}/events"
    async with _client(server, user) as client:
        async with client.stream("GET", url) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-store"
            reader = Reader(r)
            # chat.message не входит в поток workspace; run.state_changed двух run — входит.
            await env.emit(1, "chat.message", {"chat_message_id": str(uuid.uuid4()), "body": "hi"})
            await env.emit(2)
            env.run, first_run = second_run, env.run
            await env.emit(1)
            env.run = first_run
            await _dispatch(env)
            frames, closed = await reader.frames(3)
            assert not closed
            got = _ws_events(frames)
            assert [e["type"] for e in got] == ["run.state_changed"] * 3
            assert {e["run_id"] for e in got} == {str(first_run), str(second_run)}
            last_cursor = got[-1]["_cursor"]
            seen = {e["event_id"] for e in got}
            hb = await reader.heartbeat()
            body = json.loads(hb["data"])
            _validate_ws(body)
            assert body["workspace_id"] == str(ws)

        # Возобновление с Last-Event-ID: события после курсора доставляются, повтор перекрытия
        # дедуплицируется клиентом по event_id (контракт), новых дублей сервер в одном соединении не шлёт.
        await env.emit(1)
        await _dispatch(env)
        async with client.stream("GET", url, headers={"Last-Event-ID": last_cursor}) as r:
            assert r.status_code == 200
            frames, _ = await Reader(r).frames(4, wait=5.0)
            resumed = _ws_events(frames)
            ids = [e["event_id"] for e in resumed]
            assert len(ids) == len(set(ids))
            fresh = [i for i in ids if i not in seen]
            assert len(fresh) == 1

        # Фильтр types.
        async with client.stream("GET", url, params={"types": "run.completed"}) as r:
            await env.emit(1)
            await _dispatch(env)
            lines: list[str] = []
            async for line in r.aiter_lines():
                lines.append(line)
                if line.startswith("event: heartbeat"):
                    break
            assert not any(line.startswith("id:") for line in lines)


async def test_workspace_stream_errors_and_isolation(
    server: str, env: Env, app: FastAPI, app_engine: AsyncEngine, user: UserSession
) -> None:
    ws = env.tenant.workspace_id
    url = f"/workspaces/{ws}/events"
    async with _client(server, user) as client:
        old = sse.cursor_of(utcnow() - events.WORKSPACE_REPLAY_WINDOW - events.WORKSPACE_OVERLAP)
        r = await client.get(url, headers={"Last-Event-ID": str(old)})
        assert r.status_code == 410 and r.json()["code"] == "replay_window_exceeded"
        r = await client.get(url, headers={"Last-Event-ID": "abc"})
        assert r.status_code == 422
        r = await client.get(url, headers={"Last-Event-ID": "9" * 19})
        assert r.status_code in (200, 422)
        r = await client.get(url, params={"types": "cli.output"})
        assert r.status_code == 422
    stranger = await login(app, app_engine)
    try:
        await stranger.create_workspace()
        async with _client(server, stranger) as client:
            r = await client.get(url)
            assert r.status_code == 404 and r.json()["code"] == "not_found"
    finally:
        await stranger.client.aclose()


async def test_workspace_stream_closes_after_member_removed(
    server: str, env: Env, app: FastAPI, app_engine: AsyncEngine, user: UserSession
) -> None:
    ws = env.tenant.workspace_id
    member = await login(app, app_engine)
    try:
        r = await user.client.post(
            f"/api/v1/workspaces/{ws}/members", json={"email": member.email, "role": "viewer"},
            headers=user.headers(idem=True),
        )  # fmt: skip
        assert r.status_code == 201, r.text
        version = r.json()["version"]
        async with _client(server, member) as client:
            async with client.stream("GET", f"/workspaces/{ws}/events") as stream:
                assert stream.status_code == 200
                reader = Reader(stream)
                await reader.heartbeat()
                r = await user.client.delete(
                    f"/api/v1/workspaces/{ws}/members/{member.user_id}",
                    headers=user.headers(if_match=f'"v{version}"'),
                )
                assert r.status_code == 204
                await env.emit(1)
                await _dispatch(env)
                _frames, closed = await reader.frames(5, wait=5.0)
                assert closed
    finally:
        await member.client.aclose()


async def test_workspace_stream_respects_connection_limit(server: str, env: Env, user: UserSession) -> None:
    url = f"/workspaces/{env.tenant.workspace_id}/events"
    async with _client(server, user) as client:
        streams = []
        try:
            for _ in range(events.CONFIG.max_streams_per_user):
                cm = client.stream("GET", url)
                resp = await cm.__aenter__()
                assert resp.status_code == 200
                streams.append(cm)
            r = await client.get(url)
            assert r.status_code == 429
        finally:
            for cm in streams:
                await cm.__aexit__(None, None, None)
        await asyncio.sleep(0.3)
        async with client.stream("GET", url) as again:
            assert again.status_code == 200
