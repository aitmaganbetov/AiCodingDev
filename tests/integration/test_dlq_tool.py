"""P9-06: инструмент DLQ (`python -m aicd_worker.dlq`) на настоящем Redis — просмотр, повтор, удаление."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from dramatiq import Message
from redis import Redis

from aicd_worker import dlq
from aicd_worker.broker import build_broker

REDIS_URL = os.environ.get("AICD_TEST_DLQ_REDIS_URL", "redis://127.0.0.1:6379/13")


@pytest.fixture
def redis() -> Redis:
    client = Redis.from_url(REDIS_URL, socket_timeout=2)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis недоступен: {exc}")
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


def _dead_letter(redis: Redis, queue: str) -> str:
    """Как `nack` dispatch.lua Dramatiq: сообщение в zset `.XQ` и тело в `.XQ.msgs`."""
    message = Message(
        queue_name=queue,
        actor_name="execute_task",
        args=({"run_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()), "prompt": "секрет не печатается"},),
        kwargs={},
        options={"retries": 5, "traceback": "Traceback…\nRuntimeError: boom"},
    )
    redis.zadd(f"aicd-dramatiq:{queue}.XQ", {message.message_id: 1_700_000_000_000})
    redis.hset(f"aicd-dramatiq:{queue}.XQ.msgs", message.message_id, message.encode())
    return message.message_id


def test_list_replay_and_drop(redis: Redis, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    first = _dead_letter(redis, "exec")
    second = _dead_letter(redis, "exec")
    listed = dlq.list_dead(redis, ["exec", "orchestrator"])
    assert {m["message_id"] for m in listed} == {first, second}
    entry = next(m for m in listed if m["message_id"] == first)
    assert entry["actor"] == "execute_task" and entry["retries"] == 5 and entry["last_error"] == "RuntimeError: boom"
    assert "prompt" not in entry["refs"] and "run_id" in entry["refs"]

    monkeypatch.setenv("AICD_REDIS_URL", REDIS_URL)
    assert dlq.main(["replay", "--queue", "exec", "--message-id", first]) == 0  # без --yes — только план
    assert "dry_run" in capsys.readouterr().out
    assert len(dlq.list_dead(redis, ["exec"])) == 2

    assert dlq.replay(redis, build_broker(REDIS_URL), "exec", [first]) == [first]
    queued = redis.lrange("aicd-dramatiq:exec", 0, -1)
    assert len(queued) == 1
    body = redis.hget("aicd-dramatiq:exec.msgs", queued[0])
    assert isinstance(body, bytes)
    replayed = Message.decode(body)
    assert replayed.actor_name == "execute_task" and "retries" not in replayed.options
    assert [m["message_id"] for m in dlq.list_dead(redis, ["exec"])] == [second]

    assert dlq.main(["drop", "--queue", "exec", "--message-id", second, "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["done"] == [second]
    assert dlq.list_dead(redis, ["exec"]) == []
    with pytest.raises(SystemExit):
        dlq.list_dead(redis, ["unknown"])
