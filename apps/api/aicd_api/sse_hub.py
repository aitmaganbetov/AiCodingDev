"""Общий на процесс API приёмник сигналов Redis pub/sub для SSE-потоков (API_DESIGN §13, P9-04).

Раньше каждый SSE-поток держал собственное pub/sub-соединение из общего пула `redis.asyncio`: на ~100
одновременных потоках пул исчерпывался (`MaxConnectionsError`), и *любая* операция Redis процесса — в том
числе учёт слотов потоков и rate limiter — завершалась HTTP 500. Лимит `max_streams_per_process` (по умолчанию 250, SLO P9-04)
был недостижим.

Теперь процесс держит **одно** pub/sub-соединение: потоки регистрируют интерес к каналам (счётчик ссылок),
единственная задача-читатель раздаёт сигналы в очереди потоков. Сигнал — только «пробуждение» (данные всегда
читаются из `run_event_log`), поэтому при переполнении очереди медленного потока лишние сигналы
отбрасываются, а при недоступности Redis потоки деградируют до опроса (`poll_seconds`); читатель
переподключается и заново подписывается на все активные каналы.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Any, Final

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

log = logging.getLogger("aicd.sse.hub")

# Служебный канал: pub/sub-соединение подписано всегда (get_message без подписок не работает).
HUB_CHANNEL: Final = "aicd:sse:hub"
QUEUE_SIZE: Final = 32


class Subscription:
    """Интерес одного потока к набору каналов; `wait` — следующий сигнал или None по таймауту."""

    def __init__(self, hub: SignalHub, channels: tuple[str, ...]) -> None:
        self._hub = hub
        self.channels = channels
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.closed = False

    def deliver(self, channel: str) -> None:
        if self.queue.full():  # медленный поток: старый сигнал не нужен, важен факт пробуждения
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait(channel)

    async def wait(self, seconds: float) -> str | None:
        if seconds <= 0:
            return None
        try:
            return await asyncio.wait_for(self.queue.get(), seconds)
        except TimeoutError:
            return None

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self._hub.release(self)


class SignalHub:
    def __init__(self, redis: Redis, *, reconnect_seconds: float = 1.0, read_timeout: float = 1.0) -> None:
        self._redis = redis
        self._reconnect = reconnect_seconds
        self._read_timeout = read_timeout
        self._pubsub: PubSub | None = None
        self._subscribers: dict[str, set[Subscription]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self._pubsub is not None

    @property
    def subscriptions(self) -> int:
        return len({s for subs in self._subscribers.values() for s in subs})

    async def subscribe(self, channels: Iterable[str]) -> Subscription:
        sub = Subscription(self, tuple(dict.fromkeys(channels)))
        new = [c for c in sub.channels if not self._subscribers.get(c)]
        for channel in sub.channels:
            self._subscribers.setdefault(channel, set()).add(sub)
        self._ensure_reader()
        if new and self._pubsub is not None:
            try:
                await self._pubsub.subscribe(*new)
            except Exception:  # noqa: BLE001 — читатель переподключится и подпишется заново
                await self._drop("subscribe failed")
        return sub

    async def release(self, sub: Subscription) -> None:
        gone = []
        for channel in sub.channels:
            subs = self._subscribers.get(channel)
            if subs is None:
                continue
            subs.discard(sub)
            if not subs:
                del self._subscribers[channel]
                gone.append(channel)
        if gone and self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.unsubscribe(*gone)

    def _ensure_reader(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="aicd-sse-hub")

    async def _connect(self) -> None:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(HUB_CHANNEL, *self._subscribers)
        except BaseException:
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]
            raise
        self._pubsub = pubsub

    async def _drop(self, reason: str) -> None:
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is not None:
            log.warning("sse hub pubsub dropped", extra={"reason": reason})
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]

    def _dispatch(self, message: dict[str, Any]) -> None:
        raw = message.get("channel")
        channel = raw.decode() if isinstance(raw, bytes) else str(raw)
        for sub in tuple(self._subscribers.get(channel, ())):
            sub.deliver(channel)

    async def _run(self) -> None:
        while not self._stopping:
            if self._pubsub is None:
                try:
                    await self._connect()
                except Exception:  # noqa: BLE001 — Redis недоступен: потоки работают опросом
                    await asyncio.sleep(self._reconnect)
                    continue
            pubsub = self._pubsub
            if pubsub is None:  # pragma: no cover — _connect выставляет _pubsub
                continue
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=self._read_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await self._drop("read failed")
                await asyncio.sleep(self._reconnect)
                continue
            if message is not None and message.get("type") == "message":
                self._dispatch(message)

    async def close(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self._drop("shutdown")
        self._subscribers.clear()
