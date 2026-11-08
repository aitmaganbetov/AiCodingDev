"""SSRF guard `security.ssrf` (SC-DOC-01…05, TH-25, ST-DOC-01…05; ТЗ §20).

Локальный HTTP-сервер на 127.0.0.1 недоступен политикой по умолчанию; тесты, которым нужен реальный
ответ, передают явную тестовую политику (`extra_allowed_networks=127.0.0.1/32`, схема http, порт сервера)
и тестовый резолвер имени `docs.example.com` → 127.0.0.1 — соединение идёт на pinned IP с Host исходного имени.
"""

from __future__ import annotations

import gzip
import ipaddress
import threading
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import httpx
import pytest

from aicd_security.ssrf import (
    FetchError,
    PinnedTransport,
    SsrfBlockedError,
    SsrfFetcher,
    SsrfPolicy,
    Target,
    address_block_reason,
    parse_url,
)

# --- Таблица адресов (ST-DOC-03) ----------------------------------------------------------------

BLOCKED_IPS = [
    "127.0.0.1",
    "127.255.255.254",
    "0.0.0.0",  # noqa: S104 — адрес из таблицы denylist, не bind
    "10.1.2.3",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "100.64.0.1",
    "169.254.169.254",
    "169.254.0.1",
    "192.0.0.8",
    "198.18.0.1",
    "224.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:10.0.0.1",
    "64:ff9b::7f00:1",  # NAT64 → 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 → 169.254.169.254
    "2002:7f00:1::1",  # 6to4 → 127.0.0.1
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo (клиент 127.0.0.1)
    "fc00::1",
    "fd00:ec2::254",  # metadata AWS IPv6
    "fe80::1",
    "fec0::1",
    "ff02::1",
    "2001:db8::1",
]
PUBLIC_IPS = ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111", "2a00:1450:4001:82a::200e"]


@pytest.mark.parametrize("ip", BLOCKED_IPS)
def test_denylist_blocks_private_and_special_addresses(ip: str) -> None:
    assert address_block_reason(ipaddress.ip_address(ip)) is not None


@pytest.mark.parametrize("ip", PUBLIC_IPS)
def test_public_addresses_allowed(ip: str) -> None:
    assert address_block_reason(ipaddress.ip_address(ip)) is None


def test_metadata_addresses_blocked_even_when_allowlisted() -> None:
    policy = SsrfPolicy(
        extra_allowed_networks=(ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fd00::/8"))
    )
    for ip in ("169.254.169.254", "fd00:ec2::254", "::ffff:169.254.169.254"):
        assert address_block_reason(ipaddress.ip_address(ip), policy) is not None
    assert address_block_reason(ipaddress.ip_address("169.254.1.1"), policy) is None


# --- Разбор URL (ST-DOC-01, ST-DOC-03) ----------------------------------------------------------

BLOCKED_URLS = [
    "http://example.com/",  # только https по умолчанию
    "file:///etc/passwd",
    "gopher://example.com/",
    "ftp://example.com/",
    "data:text/html,hi",
    "jar:https://example.com!/",
    "https://user:pass@example.com/",
    "https://user@example.com/",
    "https://example.com:8443/",  # порт вне allowlist
    "https://example.com:22/",
    "https://localhost/",
    "https://postgres/",
    "https://minio:443/",
    "https://redis/",
    "https://intranet/",
    "https://db.internal/",
    "https://printer.local/",
    "https://app.localhost/",
    "https://127.0.0.1/",
    "https://[::1]/",
    "https://[::ffff:127.0.0.1]/",
    "https://[::ffff:7f00:1]/",
    "https://169.254.169.254/latest/meta-data/",
    "https://2130706433/",  # десятичная форма 127.0.0.1
    "https://0x7f000001/",  # шестнадцатеричная
    "https://0177.0.0.1/",  # восьмеричная
    "https://017700000001/",  # восьмеричная целым
    "https://127.1/",  # сокращённая
    "https://0x7f.0.0.1/",
    "https://2852039166/",  # десятичная форма 169.254.169.254
    "https://0251.0376.0251.0376/",  # восьмеричная 169.254.169.254
    "https://8.8.8.8/",  # IP-литералы запрещены по умолчанию (как и на egress-proxy)
    "https://exa mple.com/",
    "https://example.com\\@evil.test/",
    "",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_url_pipeline_blocks(url: str) -> None:
    with pytest.raises(SsrfBlockedError):
        parse_url(url, SsrfPolicy())


def test_ip_literals_checked_against_denylist_when_allowed() -> None:
    policy = SsrfPolicy(allow_ip_literals=True)
    assert parse_url("https://93.184.216.34/x", policy).host == ipaddress.ip_address("93.184.216.34")
    for url in ("https://10.0.0.1/", "https://[::ffff:169.254.169.254]/", "https://[fd00:ec2::254]/"):
        with pytest.raises(SsrfBlockedError):
            parse_url(url, policy)
    with pytest.raises(SsrfBlockedError):  # неоднозначные формы запрещены всегда
        parse_url("https://2130706433/", policy)


def test_valid_url_normalized() -> None:
    parsed = parse_url("https://Docs.Example.COM./guide/ä?x=1", SsrfPolicy())
    assert parsed.host == "docs.example.com" and parsed.port == 443 and parsed.raw_path == "/guide/%C3%A4?x=1"


def test_policy_cannot_be_widened_beyond_limits() -> None:
    with pytest.raises(ValueError, match="схемы"):
        SsrfPolicy(allowed_schemes=frozenset({"https", "file"}))
    with pytest.raises(ValueError, match="200 MiB"):
        SsrfPolicy(max_bytes=500 * 1024 * 1024)
    with pytest.raises(ValueError, match="3"):
        SsrfPolicy(max_redirects=10)


# --- DNS pinning / rebinding (ST-DOC-02) --------------------------------------------------------


@dataclass
class SequenceResolver:
    """Тестовый DNS: каждый вызов для имени возвращает следующий ответ (DNS rebinding)."""

    answers: dict[str, list[list[str]]]
    calls: list[str] = field(default_factory=list)

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        self.calls.append(host)
        seq = self.answers.get(host)
        if not seq:
            raise OSError("NXDOMAIN")
        return seq.pop(0) if len(seq) > 1 else seq[0]


@dataclass
class RecordingTransport:
    """Транспорт-заглушка: фиксирует, к какому адресу шло соединение, и отдаёт заданные ответы."""

    responses: list[httpx.Response]
    targets: list[Target] = field(default_factory=list)

    @asynccontextmanager
    async def stream(
        self, target: Target, headers: Mapping[str, str], policy: SsrfPolicy
    ) -> AsyncIterator[httpx.Response]:
        self.targets.append(target)
        yield self.responses.pop(0)


def _html(body: bytes = b"<html><body><h1>Doc</h1></body></html>") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=body)


async def test_connection_uses_pinned_ip_not_second_resolution() -> None:
    resolver = SequenceResolver({"docs.example.com": [["93.184.216.34"], ["127.0.0.1"]]})
    transport = RecordingTransport([_html()])
    result = await SsrfFetcher(resolver=resolver, transport=transport).fetch("https://docs.example.com/guide")
    assert result.pinned_ip == "93.184.216.34"
    assert [t.pinned_ip for t in transport.targets] == ["93.184.216.34"]
    assert transport.targets[0].host_header == "docs.example.com"
    assert resolver.calls == ["docs.example.com"], "имя разрешается ровно один раз на переход"


async def test_any_private_answer_blocks_whole_name() -> None:
    resolver = SequenceResolver({"docs.example.com": [["93.184.216.34", "10.0.0.5"]]})
    transport = RecordingTransport([_html()])
    with pytest.raises(SsrfBlockedError):
        await SsrfFetcher(resolver=resolver, transport=transport).fetch("https://docs.example.com/")
    assert transport.targets == [], "соединение не открывалось"


@pytest.mark.parametrize("answer", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "169.254.169.254", "fd00:ec2::254"])
async def test_name_resolving_to_blocked_address(answer: str) -> None:
    resolver = SequenceResolver({"evil.example.com": [[answer]]})
    transport = RecordingTransport([_html()])
    with pytest.raises(SsrfBlockedError):
        await SsrfFetcher(resolver=resolver, transport=transport).fetch("https://evil.example.com/")
    assert transport.targets == []


async def test_redirect_rebinding_is_rechecked() -> None:
    """Редирект на то же имя: повторное разрешение возвращает 127.0.0.1 — отказ (DNS rebinding)."""
    resolver = SequenceResolver({"docs.example.com": [["93.184.216.34"], ["127.0.0.1"]]})
    transport = RecordingTransport([httpx.Response(302, headers={"location": "/next"})])
    with pytest.raises(SsrfBlockedError):
        await SsrfFetcher(resolver=resolver, transport=transport).fetch("https://docs.example.com/start")
    assert len(transport.targets) == 1 and resolver.calls == ["docs.example.com", "docs.example.com"]


@pytest.mark.parametrize(
    "location",
    [
        "http://docs.example.com/x",  # понижение https→http
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost/admin",
        "https://[::1]/",
        "https://2130706433/",
        "file:///etc/passwd",
        "https://internal-host/",
    ],
)
async def test_redirect_to_blocked_target(location: str) -> None:
    resolver = SequenceResolver({"docs.example.com": [["93.184.216.34"]]})
    transport = RecordingTransport([httpx.Response(301, headers={"location": location})])
    with pytest.raises(SsrfBlockedError):
        await SsrfFetcher(resolver=resolver, transport=transport).fetch("https://docs.example.com/")
    assert len(transport.targets) == 1


async def test_too_many_redirects() -> None:
    resolver = SequenceResolver({"docs.example.com": [["93.184.216.34"]]})
    transport = RecordingTransport([httpx.Response(302, headers={"location": f"/r{i}"}) for i in range(5)])
    with pytest.raises(SsrfBlockedError, match="редирект"):
        await SsrfFetcher(resolver=resolver, transport=transport).fetch("https://docs.example.com/")
    assert len(transport.targets) == 4, "исходный запрос + 3 редиректа"


async def test_dns_failure() -> None:
    with pytest.raises(FetchError) as exc:
        await SsrfFetcher(resolver=SequenceResolver({}), transport=RecordingTransport([])).fetch(
            "https://nx.example.com/"
        )
    assert exc.value.code == "DNS_FAILED"


# --- Реальный HTTP через локальный сервер (тестовая политика) -------------------------------------


class _Handler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, tuple[int, dict[str, str], bytes]]] = {}
    hosts: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.hosts.append(self.headers.get("Host", ""))
        status, headers, body = self.routes.get(self.path, (404, {"Content-Type": "text/plain"}, b"nf"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — сигнатура базового класса
        return


@pytest.fixture(scope="module")
def http_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def _test_fetcher(port: int, **overrides: object) -> SsrfFetcher:
    policy = SsrfPolicy(
        allowed_schemes=frozenset({"http", "https"}),
        allowed_ports=frozenset({port}),
        extra_allowed_networks=(ipaddress.ip_network("127.0.0.1/32"),),
        **overrides,  # type: ignore[arg-type]
    )
    resolver = SequenceResolver({"docs.example.com": [["127.0.0.1"]], "other.example.com": [["127.0.0.1"]]})
    return SsrfFetcher(policy, resolver=resolver, transport=PinnedTransport())


async def test_default_policy_blocks_local_server(http_server: int) -> None:
    _Handler.routes["/ok"] = (200, {"Content-Type": "text/plain"}, b"hello")
    resolver = SequenceResolver({"docs.example.com": [["127.0.0.1"]]})
    for policy in (
        SsrfPolicy(),
        SsrfPolicy(allowed_schemes=frozenset({"http"}), allowed_ports=frozenset({http_server})),
    ):
        with pytest.raises(SsrfBlockedError):
            await SsrfFetcher(policy, resolver=resolver).fetch(f"http://docs.example.com:{http_server}/ok")
    with pytest.raises(SsrfBlockedError):
        await SsrfFetcher(SsrfPolicy(allow_ip_literals=True)).fetch(f"https://127.0.0.1:{http_server}/ok")


async def test_fetch_through_pinned_transport(http_server: int) -> None:
    _Handler.routes["/doc.md"] = (200, {"Content-Type": "text/markdown"}, b"# Title\n\nbody\n")
    _Handler.hosts.clear()
    result = await _test_fetcher(http_server).fetch(f"http://docs.example.com:{http_server}/doc.md")
    assert result.content == b"# Title\n\nbody\n" and result.mime_type == "text/markdown"
    assert result.pinned_ip == "127.0.0.1"
    assert _Handler.hosts == [f"docs.example.com:{http_server}"], "Host — исходное имя, соединение — pinned IP"


async def test_env_proxy_is_ignored(http_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    _Handler.routes["/env"] = (200, {"Content-Type": "text/plain"}, b"direct")
    monkeypatch.setenv("HTTP_PROXY", "http://203.0.113.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://203.0.113.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    result = await _test_fetcher(http_server).fetch(f"http://docs.example.com:{http_server}/env")
    assert result.content == b"direct"


async def test_redirect_followed_through_full_pipeline(http_server: int) -> None:
    _Handler.routes["/r1"] = (302, {"Location": f"http://other.example.com:{http_server}/final"}, b"")
    _Handler.routes["/final"] = (200, {"Content-Type": "text/plain"}, b"final")
    result = await _test_fetcher(http_server).fetch(f"http://docs.example.com:{http_server}/r1")
    assert result.content == b"final" and result.redirects == (f"http://other.example.com:{http_server}/final",)


async def test_redirect_from_local_server_to_metadata_blocked(http_server: int) -> None:
    _Handler.routes["/meta"] = (302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b"")
    with pytest.raises(SsrfBlockedError):
        await _test_fetcher(http_server).fetch(f"http://docs.example.com:{http_server}/meta")


async def test_oversized_body_aborted_regardless_of_content_length(http_server: int) -> None:
    big = b"a" * (64 * 1024)
    _Handler.routes["/big"] = (200, {"Content-Type": "text/plain"}, big)
    # Content-Length занижен: лимит проверяется по фактически прочитанным байтам.
    _Handler.routes["/liar"] = (200, {"Content-Type": "text/plain", "Content-Length": str(len(big))}, big)
    fetcher = _test_fetcher(http_server, max_bytes=16 * 1024)
    for path in ("/big", "/liar"):
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch(f"http://docs.example.com:{http_server}{path}")
        assert exc.value.code == "TOO_LARGE"


async def test_gzip_content_encoding_bomb_capped(http_server: int) -> None:
    bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
    _Handler.routes["/gz"] = (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, bomb)
    with pytest.raises(FetchError) as exc:
        await _test_fetcher(http_server, max_bytes=1024 * 1024).fetch(f"http://docs.example.com:{http_server}/gz")
    assert exc.value.code == "TOO_LARGE"


@pytest.mark.parametrize(
    ("ctype", "body", "code"),
    [
        ("application/x-msdownload", b"MZ\x90\x00", "MIME_REJECTED"),
        ("image/svg+xml", b"<svg/>", "MIME_REJECTED"),
        ("", b"hello", "MIME_REJECTED"),
        ("application/pdf", b"<html>not a pdf</html>", "MIME_MISMATCH"),
        ("application/zip", b"%PDF-1.7", "MIME_MISMATCH"),
        ("text/html", b"\x7fELF\x02\x01\x01", "MIME_MISMATCH"),
        ("text/plain", b"PK\x03\x04binary", "MIME_MISMATCH"),
        ("text/markdown", b"abc\x00def", "MIME_MISMATCH"),
    ],
)
async def test_wrong_mime_rejected(http_server: int, ctype: str, body: bytes, code: str) -> None:
    path = f"/mime-{code}-{abs(hash((ctype, body)))}"
    headers = {"Content-Type": ctype} if ctype else {}
    _Handler.routes[path] = (200, headers, body)
    with pytest.raises(FetchError) as exc:
        await _test_fetcher(http_server).fetch(f"http://docs.example.com:{http_server}{path}")
    assert exc.value.code == code


async def test_http_error_status(http_server: int) -> None:
    with pytest.raises(FetchError) as exc:
        await _test_fetcher(http_server).fetch(f"http://docs.example.com:{http_server}/missing")
    assert exc.value.code == "HTTP_ERROR"
