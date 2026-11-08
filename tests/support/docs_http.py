"""Локальный «сайт документации» для тестов загрузки через `security.ssrf` (P5-10, AC-10).

Имя (например, `docs.example.com`) резолвится тестовым резолвером в 127.0.0.1 — адрес разрешён только явной
тестовой политикой (`extra_allowed_networks`); транспорт подменяет проверенную https-цель (pinned IP, порт 443)
локальным http-сервером. В production 127.0.0.1 недоступен по построению. Сервер считает запросы: так тесты
проверяют, что до APPROVED загрузка не начиналась.
"""

from __future__ import annotations

import ipaddress
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from aicd_security.ssrf import PinnedTransport, SsrfFetcher, SsrfPolicy, Target

DOCS_HOST = "docs.example.com"


class DocsHandler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, tuple[str, bytes]]] = {}
    requests: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.requests.append(self.path)
        ctype, body = self.routes.get(self.path, ("text/plain", b""))
        self.send_response(200 if self.path in self.routes else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — сигнатура базового класса
        return


@pytest.fixture(scope="module")
def docs_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DocsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


class StaticResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        if host not in self.mapping:
            raise OSError("NXDOMAIN")
        return [self.mapping[host]]


class LocalHttpTransport:
    """Тестовый транспорт: проверенная https-цель (pinned IP) обслуживается локальным http-сервером."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.inner = PinnedTransport()

    def stream(self, target: Target, headers: Mapping[str, str], policy: SsrfPolicy) -> Any:
        return self.inner.stream(replace(target, scheme="http", port=self.port), headers, policy)


def local_fetcher(port: int, ip: str = "127.0.0.1", host: str = DOCS_HOST) -> SsrfFetcher:
    policy = SsrfPolicy(extra_allowed_networks=(ipaddress.ip_network("127.0.0.1/32"),))
    return SsrfFetcher(policy, resolver=StaticResolver({host: ip}), transport=LocalHttpTransport(port))
