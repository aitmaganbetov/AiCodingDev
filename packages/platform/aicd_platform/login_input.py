"""Код авторизации из UI в процесс входа CLI на worker-exec (P2-01, cli-auth.md §2a).

Часть официальных CLI завершает вход вводом кода, который провайдер показывает оператору в браузере
(Claude Code 2.1.319: `Paste code here if prompted >`). С хоста исполнения браузера нет, поэтому код
вводит оператор в UI, а платформа передаёт его процессу CLI — и **нигде не сохраняет**: ни в PG, ни в
outbox, ни в audit, ни в логах, ни в SSE (SECURITY_ARCHITECTURE §6.2, строка «учётные данные подписки»).
Канал — pub/sub Redis: сообщение живёт в памяти между запросом оператора и записью в stdin CLI, подписчика
нет — сообщение просто теряется (SC-DATA-09: Redis — эфемерные подсказки, не источник истины).

Почему это не противоречит «платформа не получает учётных данных» (ТЗ §1, ADR-0003): это одноразовый
authorization code потока OAuth с PKCE. Обменять его на токен может только сам CLI — `code_verifier`
не покидает CLI и платформе неизвестен. Платформа здесь — провод между браузером оператора и stdin CLI,
а не хранилище. Токен по-прежнему пишет сам CLI в свой credential store.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final

#: Длиннее кода авторизации не бывает ни у одного официального CLI; ограничение и для защиты от записи
#: в stdin CLI произвольного объёма данных.
MAX_CODE_LENGTH: Final = 1024
#: Управляющие символы (включая перевод строки) в коде недопустимы: в stdin CLI уходит ровно одна строка.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def channel(session_id: uuid.UUID | str) -> str:
    """Канал pub/sub сессии входа. Слушает его только воркер, который ведёт эту сессию."""
    return f"aicd:login:{session_id}"


def normalize(code: str) -> str:
    """Проверить код оператора. Содержимое не интерпретируется — проверяются только длина и символы."""
    value = code.strip()
    if not value:
        raise ValueError("код не может быть пустым")
    if len(value) > MAX_CODE_LENGTH:
        raise ValueError(f"код длиннее {MAX_CODE_LENGTH} символов")
    if _CONTROL.search(value):
        raise ValueError("в коде недопустимы управляющие символы")
    return value


async def publish(redis: Any, *, session_id: uuid.UUID | str, code: str) -> int:
    """Отдать код процессу входа. Возвращает число получателей: 0 — сессию никто не слушает."""
    return int(await redis.publish(channel(session_id), code))


@asynccontextmanager
async def inbox(redis: Any, *, session_id: uuid.UUID | str) -> AsyncIterator[Callable[[], Awaitable[str | None]]]:
    """Подписка воркера на коды этой сессии: вернуть неблокирующую функцию чтения очередного кода.

    Отписка на выходе обязательна: канал привязан к сессии, и код не должен достаться следующей.
    """
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(channel(session_id))

    async def receive() -> str | None:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.0)
        if message is None or message.get("type") != "message":
            return None
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        try:
            return normalize(str(data))
        except ValueError:
            return None  # мусор в канале молча игнорируется: в stdin CLI он не попадёт

    try:
        yield receive
    finally:
        await pubsub.unsubscribe(channel(session_id))
        await pubsub.aclose()
