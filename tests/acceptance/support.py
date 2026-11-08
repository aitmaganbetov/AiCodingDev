"""Обвязка приёмочных тестов P9-01: журнал исходящих соединений процесса платформы, SQL-хелперы среза.

`record_egress` — «egress-лог» процесса платформы (API, оркестратор, gateway, verification в одном процессе
теста): перехватывает разрешение имён (`socket.getaddrinfo`) и установку соединений (`socket.socket.connect`,
`connect_ex`) — через них проходят asyncio/httpx/redis-py. Соединения libpq (psycopg) идут из C-кода и в журнал
не попадают; это локальная БД теста. Самопроверка журнала — `EgressLog.selfcheck` (соединение с локальным
сокетом обязано попасть в журнал, иначе отсутствие записей ничего не доказывает).
"""

from __future__ import annotations

import ipaddress
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import text

from aicd_platform.db import DbContext, transaction
from tests.support.phase3 import Slice

# Эндпоинты AI API провайдеров (ТЗ §1: платформа не обращается к AI API — только официальные CLI).
AI_API_HOSTS = frozenset(
    {
        "api.openai.com",
        "api.anthropic.com",
        "chatgpt.com",
        "claude.ai",
        "generativelanguage.googleapis.com",
        "api.mistral.ai",
        "api.cohere.ai",
    }
)
LOCAL_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "ip6-localhost"})


@dataclass
class EgressLog:
    lookups: list[str] = field(default_factory=list)
    connects: list[Any] = field(default_factory=list)

    def remote(self) -> list[str]:
        """Не локальные назначения: имена вне loopback и IP-адреса не loopback (unix-сокеты — локальные)."""
        found: list[str] = []
        for host in self.lookups:
            if host not in LOCAL_NAMES and not _is_loopback(host):
                found.append(f"lookup:{host}")
        for address in self.connects:
            if isinstance(address, tuple) and address and not _is_loopback(str(address[0])):
                found.append(f"connect:{address[0]}:{address[1] if len(address) > 1 else ''}")
        return found

    def ai_api(self) -> list[str]:
        return [h for h in self.lookups if h in AI_API_HOSTS or any(h.endswith("." + a) for a in AI_API_HOSTS)]

    def selfcheck(self) -> None:
        """Перехват работает: соединение с локальным слушающим сокетом записано в журнал."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        before = len(self.connects)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("127.0.0.1", port))
        finally:
            client.close()
            server.close()
        assert ("127.0.0.1", port) in self.connects[before:], "журнал egress не перехватывает connect()"
        del self.connects[before:]


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return host in LOCAL_NAMES


@contextmanager
def record_egress(monkeypatch: pytest.MonkeyPatch) -> Iterator[EgressLog]:
    log = EgressLog()
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        log.lookups.append(host.decode() if isinstance(host, bytes) else str(host))
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self: socket.socket, address: Any) -> Any:
        log.connects.append(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        log.connects.append(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    log.selfcheck()
    yield log


async def rows(s: Slice, sql: str, **params: Any) -> list[dict[str, Any]]:
    async with transaction(s.worker, DbContext(workspace_id=uuid.UUID(s.ws), actor_type="system")) as conn:
        return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
