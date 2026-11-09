"""Dead-letter очереди Dramatiq: просмотр, повторная постановка, удаление (P9-06, docs/runbooks/recovery.md).

    python -m aicd_worker.dlq list   [--queue orchestrator|exec|docs|github]
    python -m aicd_worker.dlq replay --queue Q (--message-id ID … | --all) [--yes]
    python -m aicd_worker.dlq drop   --queue Q --message-id ID … --yes

Redis — только транспорт (ADR-0002): сообщение в DLQ — это исчерпавшая повторы доставка события/исполнения,
состояние в PostgreSQL не потеряно. Повтор безопасен, потому что потребители идемпотентны (processed_events,
аренды с fencing token, ledger внешних действий) — повторная доставка не выполняет опасное действие второй раз
(NFR-REL-02). Сначала устраните причину (`list` показывает актора, число повторов и тип последней ошибки).

Подключение — `AICD_REDIS_URL(_FILE)` (в compose: `docker compose run --rm --no-deps api python -m aicd_worker.dlq list`).
Вывод — JSON; аргументы сообщений не печатаются целиком (только ключи и идентификаторы run/task).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from dramatiq import Message
from redis import Redis

from aicd_platform.config import Settings
from aicd_worker.broker import (
    DOCS_QUEUE,
    EXEC_QUEUE,
    GITHUB_QUEUE,
    NOTIFICATIONS_QUEUE,
    ORCHESTRATOR_QUEUE,
    build_broker,
)

NAMESPACE = "aicd-dramatiq"
QUEUES = (ORCHESTRATOR_QUEUE, EXEC_QUEUE, DOCS_QUEUE, GITHUB_QUEUE, NOTIFICATIONS_QUEUE)
SAFE_ARG_KEYS = ("event_type", "run_id", "task_id", "workspace_id", "event_id", "execution_id")


def _keys(queue: str) -> tuple[str, str]:
    if queue not in QUEUES:
        raise SystemExit(f"неизвестная очередь {queue!r}; допустимо: {', '.join(QUEUES)}")
    base = f"{NAMESPACE}:{queue}.XQ"
    return base, f"{base}.msgs"


def _summary(queue: str, message_id: str, raw: bytes | None, failed_at: float) -> dict[str, Any]:
    entry: dict[str, Any] = {"queue": queue, "message_id": message_id, "dead_lettered_at_ms": int(failed_at)}
    if raw is None:
        return entry | {"error": "нет тела сообщения"}
    message = Message.decode(raw)
    args = message.args[0] if message.args and isinstance(message.args[0], dict) else {}
    traceback = str(message.options.get("traceback") or "")
    entry |= {
        "actor": message.actor_name,
        "retries": message.options.get("retries", 0),
        "last_error": traceback.strip().splitlines()[-1][:200] if traceback else None,
        "refs": {k: args[k] for k in SAFE_ARG_KEYS if k in args},
    }
    return entry


def list_dead(redis: Redis, queues: Sequence[str]) -> list[dict[str, Any]]:
    out = []
    for queue in queues:
        zkey, hkey = _keys(queue)
        members: list[tuple[bytes | str, float]] = redis.zrange(zkey, 0, -1, withscores=True)  # type: ignore[assignment]
        for member, score in members:
            message_id = member.decode() if isinstance(member, bytes) else str(member)
            raw = redis.hget(hkey, message_id)
            out.append(_summary(queue, message_id, raw if isinstance(raw, bytes) else None, float(score)))
    return out


def replay(redis: Redis, broker: Any, queue: str, message_ids: Sequence[str]) -> list[str]:
    """Новая доставка с обнулённым счётчиком повторов; запись DLQ удаляется только после успешной постановки."""
    zkey, hkey = _keys(queue)
    replayed = []
    for message_id in message_ids:
        raw = redis.hget(hkey, message_id)
        if not isinstance(raw, bytes):
            continue
        message = Message.decode(raw)
        options = {k: v for k, v in message.options.items() if k not in ("retries", "traceback", "redis_message_id")}
        fresh: Message[Any] = Message(
            queue_name=message.queue_name,
            actor_name=message.actor_name,
            args=message.args,
            kwargs=message.kwargs,
            options=options,  # copy() сливает options со старыми — счётчик повторов не сбросился бы
        )
        broker.enqueue(fresh)
        redis.zrem(zkey, message_id)
        redis.hdel(hkey, message_id)
        replayed.append(message_id)
    return replayed


def drop(redis: Redis, queue: str, message_ids: Sequence[str]) -> list[str]:
    zkey, hkey = _keys(queue)
    dropped = []
    for message_id in message_ids:
        if redis.zrem(zkey, message_id):
            redis.hdel(hkey, message_id)
            dropped.append(message_id)
    return dropped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aicd_worker.dlq", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--queue", choices=QUEUES)
    for name in ("replay", "drop"):
        p = sub.add_parser(name)
        p.add_argument("--queue", choices=QUEUES, required=True)
        p.add_argument("--message-id", action="append", default=[])
        if name == "replay":
            p.add_argument("--all", action="store_true")
        p.add_argument("--yes", action="store_true", help="подтверждение действия")
    args = parser.parse_args(argv)
    settings = Settings()
    redis = Redis.from_url(settings.redis_url, socket_timeout=5)
    if args.command == "list":
        print(json.dumps(list_dead(redis, [args.queue] if args.queue else QUEUES), ensure_ascii=False, indent=2))
        return 0
    ids = list(args.message_id)
    if args.command == "replay" and args.all:
        ids = [m["message_id"] for m in list_dead(redis, [args.queue])]
    if not ids:
        print("не указаны сообщения (--message-id или --all)", file=sys.stderr)
        return 2
    if not args.yes:
        print(json.dumps({"dry_run": True, "command": args.command, "queue": args.queue, "messages": ids}))
        return 0
    if args.command == "replay":
        done = replay(redis, build_broker(settings.redis_url), args.queue, ids)
    else:
        done = drop(redis, args.queue, ids)
    print(json.dumps({"command": args.command, "queue": args.queue, "done": done, "requested": len(ids)}))
    return 0 if len(done) == len(ids) else 1


if __name__ == "__main__":
    sys.exit(main())
