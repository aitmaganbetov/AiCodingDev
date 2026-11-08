"""SSRF-safe загрузка внешних ресурсов — единая точка `security.ssrf.fetch()` (SECURITY_ARCHITECTURE §9,
SC-DOC-01…05, TH-25).

Конвейер для каждого перехода (исходный URL и каждый редирект):

1. Разбор URL: только разрешённые схемы (по умолчанию `https`), порт из allowlist (по умолчанию 443),
   userinfo запрещён, имена без точки / `*.local` / `*.internal` / `localhost` / имена сервисов compose
   запрещены, неоднозначные числовые формы IPv4 (`2130706433`, `0177.0.0.1`, `0x7f.1`, `127.1`) запрещены
   всегда, IP-литералы — по умолчанию (egress-proxy их тоже запрещает).
2. DNS: разрешаются **все** A/AAAA; если хотя бы один адрес в denylist — отказ (SC-DOC-02/03).
3. Соединение строго с проверенным адресом (pinned IP; Host/SNI — исходное имя, сертификат проверяется
   по исходному имени) — защита от DNS rebinding между проверкой и соединением. Каждый редирект
   разрешается заново и проходит весь конвейер; https→http запрещён; не более `max_redirects`.
4. Ответ читается потоково с жёстким лимитом (Content-Length не доверяется); тайм-ауты connect/read/total;
   MIME — allowlist + сверка magic bytes.

Транспорт внедряемый (`FetchTransport`): `PinnedTransport` — прямое соединение с pinned IP;
`ProxyTransport` — через egress-proxy (production: `infrastructure/docker/egress-proxy`, второй слой
защиты — private-диапазоны запрещены на стороне proxy). Переменные окружения `HTTP(S)_PROXY`
не используются (`trust_env=False`): прокси задаётся только явно.

127.0.0.1 и прочие запрещённые адреса недоступны по умолчанию. Тесты с локальным HTTP-сервером
передают явную тестовую политику (`extra_allowed_networks`, схема `http`, порт сервера) — в production
это поле пусто. Адреса metadata (`169.254.169.254`, `fd00:ec2::254`) запрещены даже при явном allowlist.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import ssl
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Final, Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

MIB: Final = 1024 * 1024

# SECURITY_ARCHITECTURE §9 «Denylist IPv4/IPv6».
DENY_NETWORKS: Final[tuple[IPNetwork, ...]] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/32",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
# Адреса metadata облаков: не разрешаются никаким allowlist.
HARD_DENY: Final[frozenset[IPAddress]] = frozenset(
    {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
)
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))

INTERNAL_SUFFIXES: Final = (".local", ".internal", ".localhost", ".localdomain", ".lan", ".home.arpa", ".intranet")
# Имена сервисов compose (infrastructure/docker) — запрещены и с суффиксом домена по умолчанию.
COMPOSE_SERVICE_NAMES: Final = frozenset(
    {"localhost", "postgres", "redis", "minio", "api", "web", "worker", "egress-proxy", "proxy", "metadata"}
)

DEFAULT_MIME_ALLOWLIST: Final = frozenset(
    {
        "text/html",
        "text/markdown",
        "text/plain",
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-tar",
    }
)
_MIME_ALIASES: Final = {
    "application/x-gzip": "application/gzip",
    "text/x-markdown": "text/markdown",
    "application/x-zip-compressed": "application/zip",
    "application/xhtml+xml": "text/html",
}
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
_HOST_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")


class FetchError(Exception):
    """Отказ загрузки. `code` безопасен для API/журналов; адреса — в `detail` (только журналы сервера)."""

    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail


class SsrfBlockedError(FetchError):
    """Цель запрещена политикой SSRF (схема, порт, имя, адрес, редирект)."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__("SSRF_BLOCKED", message, detail=detail)


@dataclass(frozen=True)
class SsrfPolicy:
    allowed_schemes: frozenset[str] = frozenset({"https"})
    allowed_ports: frozenset[int] = frozenset({443})
    allow_ip_literals: bool = False
    max_redirects: int = 3
    max_bytes: int = 50 * MIB
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    total_timeout: float = 120.0
    allowed_mime: frozenset[str] = DEFAULT_MIME_ALLOWLIST
    blocked_hostnames: frozenset[str] = COMPOSE_SERVICE_NAMES
    # Только для тестов с локальным сервером; в production — пусто (127.0.0.1 запрещён по построению).
    extra_allowed_networks: tuple[IPNetwork, ...] = ()
    user_agent: str = "AiCodingDev-docs-fetcher/1"

    def __post_init__(self) -> None:
        if not self.allowed_schemes <= {"http", "https"}:
            raise ValueError("допустимы только схемы http/https")
        if self.max_bytes > 200 * MIB:
            raise ValueError("лимит ответа не выше 200 MiB (SC-DOC-05)")
        if not 0 <= self.max_redirects <= 3:
            raise ValueError("редиректов не больше 3 (SC-DOC-04)")


@dataclass(frozen=True)
class Target:
    """Проверенная цель одного перехода: исходное имя + проверенный (pinned) адрес."""

    url: str
    scheme: str
    host: str
    port: int
    raw_path: str
    pinned_ip: str

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    mime_type: str
    content: bytes
    sha256: str
    size_bytes: int
    pinned_ip: str
    redirects: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)


# --- Проверки адресов и имён --------------------------------------------------------------------


def embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv4, встроенный в IPv4-mapped / NAT64 / 6to4 / Teredo (SC-DOC-03)."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if any(ip in net for net in _NAT64):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def address_block_reason(ip: IPAddress, policy: SsrfPolicy | None = None) -> str | None:
    """None — адрес допустим; иначе причина отказа."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id:
        return "IPv6 с zone id"
    candidates: list[IPAddress] = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and (inner := embedded_ipv4(ip)) is not None:
        candidates.append(inner)
    if any(c in HARD_DENY for c in candidates):
        return "адрес metadata облака"
    if policy is not None and any(ip in net for net in policy.extra_allowed_networks if net.version == ip.version):
        return None
    for c in candidates:
        if any(c in net for net in DENY_NETWORKS if net.version == c.version):
            return "адрес в denylist"
        if not c.is_global:
            return "адрес не глобальный"
    return None


def _parse_numeric_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Распознаёт «легаси» формы IPv4 (десятичная, восьмеричная, шестнадцатеричная, сокращённая)."""
    labels = host.split(".")
    if not labels or not all(_NUMERIC_LABEL.fullmatch(lbl) for lbl in labels):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return ipaddress.IPv4Address("0.0.0.0")  # числовая, но некорректная форма — всё равно отказ


def validate_hostname(host: str, policy: SsrfPolicy) -> str | IPAddress:
    """Возвращает нормализованное имя (ASCII) или IP-литерал; иначе SsrfBlockedError."""
    host = host.strip().lower().rstrip(".")
    if not host or len(host) > 253:
        raise SsrfBlockedError("некорректное имя хоста")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not policy.allow_ip_literals:
            raise SsrfBlockedError("IP-литералы запрещены", detail=host)
        reason = address_block_reason(literal, policy)
        if reason:
            raise SsrfBlockedError(reason, detail=host)
        return literal
    if _parse_numeric_ipv4(host) is not None:
        raise SsrfBlockedError("неоднозначная числовая форма IPv4", detail=host)
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SsrfBlockedError("некорректное IDN-имя") from exc
    labels = ascii_host.split(".")
    if not all(_HOST_LABEL.fullmatch(lbl) for lbl in labels):
        raise SsrfBlockedError("некорректное имя хоста", detail=ascii_host)
    if len(labels) < 2:
        raise SsrfBlockedError("имя без домена (внутреннее)", detail=ascii_host)
    if ascii_host in policy.blocked_hostnames:
        raise SsrfBlockedError("внутреннее имя сервиса", detail=ascii_host)
    if ascii_host == "localhost" or ascii_host.endswith(INTERNAL_SUFFIXES):
        raise SsrfBlockedError("внутреннее доменное имя", detail=ascii_host)
    return ascii_host


@dataclass(frozen=True)
class ParsedUrl:
    url: str
    scheme: str
    host: str | IPAddress
    port: int
    raw_path: str


def parse_url(raw: str, policy: SsrfPolicy) -> ParsedUrl:
    if not raw or len(raw) > 2048:
        raise SsrfBlockedError("пустой или слишком длинный URL")
    if any(ord(ch) <= 0x20 or ch == "\x7f" or ch == "\\" for ch in raw):
        raise SsrfBlockedError("недопустимые символы в URL")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise SsrfBlockedError("некорректный URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in policy.allowed_schemes:
        raise SsrfBlockedError(f"схема {scheme or '∅'} запрещена")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise SsrfBlockedError("userinfo в URL запрещён")
    if not parts.hostname:
        raise SsrfBlockedError("в URL нет хоста")
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if effective_port not in policy.allowed_ports:
        raise SsrfBlockedError(f"порт {effective_port} не в allowlist")
    host = validate_hostname(parts.hostname, policy)
    raw_path = parts.path or "/"
    if parts.query:
        raw_path += "?" + parts.query
    # Не-ASCII символы пути кодируются; существующие %-последовательности сохраняются.
    raw_path = quote(raw_path, safe="/?=&%:@!$'()*+,;~-._")
    return ParsedUrl(url=raw, scheme=scheme, host=host, port=effective_port, raw_path=raw_path)


# --- DNS ----------------------------------------------------------------------------------------


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> Sequence[str]: ...


class SystemResolver:
    """getaddrinfo: все A/AAAA записи имени."""

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        seen: dict[str, None] = {}
        for _family, _type, _proto, _canon, sockaddr in infos:
            seen.setdefault(str(sockaddr[0]), None)
        return list(seen)


async def resolve_and_pin(parsed: ParsedUrl, policy: SsrfPolicy, resolver: Resolver) -> Target:
    """Разрешает имя ОДИН раз, проверяет каждый адрес и закрепляет первый (SC-DOC-02)."""
    if not isinstance(parsed.host, str):
        ip = parsed.host
        return Target(parsed.url, parsed.scheme, str(ip), parsed.port, parsed.raw_path, str(ip))
    try:
        addresses = list(await resolver.resolve(parsed.host, parsed.port))
    except OSError as exc:
        raise FetchError("DNS_FAILED", "имя не разрешается", detail=parsed.host) from exc
    if not addresses:
        raise FetchError("DNS_FAILED", "имя не разрешается", detail=parsed.host)
    checked: list[IPAddress] = []
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SsrfBlockedError("резолвер вернул некорректный адрес", detail=raw) from exc
        reason = address_block_reason(ip, policy)
        if reason:
            raise SsrfBlockedError(f"{reason} (DNS)", detail=f"{parsed.host} -> {ip}")
        checked.append(ip)
    return Target(parsed.url, parsed.scheme, parsed.host, parsed.port, parsed.raw_path, str(checked[0]))


# --- Транспорт ----------------------------------------------------------------------------------


class FetchTransport(Protocol):
    def stream(
        self, target: Target, headers: Mapping[str, str], policy: SsrfPolicy
    ) -> AbstractAsyncContextManager[httpx.Response]: ...


def _timeout(policy: SsrfPolicy) -> httpx.Timeout:
    return httpx.Timeout(connect=policy.connect_timeout, read=policy.read_timeout, write=30.0, pool=10.0)


class PinnedTransport:
    """Соединение строго с `target.pinned_ip`; Host и SNI — исходное имя; сертификат проверяется по нему."""

    def __init__(self, verify: ssl.SSLContext | bool = True) -> None:
        self._verify = verify

    @asynccontextmanager
    async def stream(
        self, target: Target, headers: Mapping[str, str], policy: SsrfPolicy
    ) -> AsyncIterator[httpx.Response]:
        url = httpx.URL(scheme=target.scheme, host=target.pinned_ip, port=target.port).copy_with(
            raw_path=target.raw_path.encode("ascii", "strict")
        )
        extensions: dict[str, object] = {}
        if target.scheme == "https":
            extensions["sni_hostname"] = target.host
        async with httpx.AsyncClient(
            verify=self._verify, trust_env=False, follow_redirects=False, timeout=_timeout(policy)
        ) as client:
            request = client.build_request(
                "GET", url, headers={**headers, "Host": target.host_header}, extensions=extensions
            )
            response = await client.send(request, stream=True)
            try:
                yield response
            finally:
                await response.aclose()


class ProxyTransport:
    """Через egress-proxy (CONNECT :443): proxy сам резолвит имя и запрещает private-адреса (второй слой).

    Предварительная проверка DNS/denylist выполняется до обращения к proxy тем же конвейером.
    """

    def __init__(self, proxy_url: str, verify: ssl.SSLContext | bool = True) -> None:
        self._proxy = proxy_url
        self._verify = verify

    @asynccontextmanager
    async def stream(
        self, target: Target, headers: Mapping[str, str], policy: SsrfPolicy
    ) -> AsyncIterator[httpx.Response]:
        url = httpx.URL(scheme=target.scheme, host=target.host, port=target.port).copy_with(
            raw_path=target.raw_path.encode("ascii", "strict")
        )
        async with httpx.AsyncClient(
            proxy=self._proxy, verify=self._verify, trust_env=False, follow_redirects=False, timeout=_timeout(policy)
        ) as client:
            async with client.stream("GET", url, headers=dict(headers)) as response:
                yield response


# --- MIME ---------------------------------------------------------------------------------------


def normalize_mime(content_type: str | None) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(mime, mime)


def sniff_matches(mime: str, head: bytes) -> bool:
    """Сверка magic bytes с заявленным типом (SC-DOC-05)."""
    if mime == "application/pdf":
        return head.startswith(b"%PDF-")
    if mime == "application/zip":
        return head.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    if mime == "application/gzip":
        return head.startswith(b"\x1f\x8b")
    if mime == "application/x-tar":
        return len(head) >= 262 and head[257:262] == b"ustar"
    if mime.startswith("text/"):
        binary_magic = (b"%PDF-", b"PK\x03\x04", b"\x1f\x8b", b"\x7fELF", b"MZ", b"\x89PNG")
        return b"\x00" not in head[:8192] and not head.startswith(binary_magic)
    return False


# --- Загрузка -----------------------------------------------------------------------------------


class SsrfFetcher:
    def __init__(
        self,
        policy: SsrfPolicy | None = None,
        *,
        resolver: Resolver | None = None,
        transport: FetchTransport | None = None,
    ) -> None:
        self.policy = policy or SsrfPolicy()
        self.resolver = resolver or SystemResolver()
        self.transport = transport or PinnedTransport()

    async def fetch(self, url: str) -> FetchResult:
        try:
            async with asyncio.timeout(self.policy.total_timeout):
                return await self._fetch(url)
        except TimeoutError as exc:
            raise FetchError("TIMEOUT", "превышено общее время загрузки") from exc
        except httpx.TimeoutException as exc:
            raise FetchError("TIMEOUT", "тайм-аут соединения/чтения") from exc
        except httpx.HTTPError as exc:
            raise FetchError("NETWORK_ERROR", "ошибка соединения", detail=type(exc).__name__) from exc

    async def _fetch(self, url: str) -> FetchResult:
        policy = self.policy
        current = url
        redirects: list[str] = []
        headers = {"User-Agent": policy.user_agent, "Accept-Encoding": "identity", "Accept": "*/*"}
        previous_scheme: str | None = None
        while True:
            parsed = parse_url(current, policy)
            if previous_scheme == "https" and parsed.scheme != "https":
                raise SsrfBlockedError("понижение https→http при редиректе запрещено")
            target = await resolve_and_pin(parsed, policy, self.resolver)
            async with self.transport.stream(target, headers, policy) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("HTTP_ERROR", "редирект без Location")
                    if len(redirects) >= policy.max_redirects:
                        raise SsrfBlockedError("слишком много редиректов")
                    previous_scheme = parsed.scheme
                    current = urljoin(current, location)
                    redirects.append(current)
                    continue
                if not 200 <= response.status_code < 300:
                    raise FetchError("HTTP_ERROR", f"статус ответа {response.status_code}")
                mime = normalize_mime(response.headers.get("content-type"))
                if mime not in policy.allowed_mime:
                    raise FetchError("MIME_REJECTED", f"тип содержимого {mime or '∅'} не разрешён")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > policy.max_bytes:
                    raise FetchError("TOO_LARGE", "ответ превышает лимит размера")
                body = await _read_capped(response, policy.max_bytes)
                if not sniff_matches(mime, body[:8192]):
                    raise FetchError("MIME_MISMATCH", "magic bytes не соответствуют заявленному типу")
                return FetchResult(
                    url=url,
                    final_url=current,
                    status_code=response.status_code,
                    mime_type=mime,
                    content=body,
                    sha256=hashlib.sha256(body).hexdigest(),
                    size_bytes=len(body),
                    pinned_ip=target.pinned_ip,
                    redirects=tuple(redirects),
                    headers={k.lower(): v for k, v in response.headers.items() if k.lower() in _KEPT_HEADERS},
                )


_KEPT_HEADERS: Final = frozenset({"content-type", "etag", "last-modified", "link"})


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Потоковое чтение с обрывом при превышении лимита (Content-Length не доверяется)."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise FetchError("TOO_LARGE", "ответ превышает лимит размера")
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch(url: str, *, fetcher: SsrfFetcher | None = None) -> FetchResult:
    """Единая точка загрузки внешнего ресурса (SC-DOC-01)."""
    return await (fetcher or SsrfFetcher()).fetch(url)
