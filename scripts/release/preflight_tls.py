"""Предрелизная проверка TLS/HSTS развёртывания (TH-46, OQ-48 (b), SC-NET-03).

    uv run python -m scripts.release.preflight_tls --url https://aicd.example.internal [--ca-file corp-ca.pem]

Проверяет внешний вход (TLS-терминатор перед сервисом `web`) так, как его видит браузер:

* `tls-versions` — TLS 1.0 и 1.1 отклоняются, TLS 1.2+ принимается (согласованная версия — в выводе);
* `certificate` — цепочка доверена (системные CA или `--ca-file`), имя хоста совпадает, до истечения ≥ `--min-days`
  (по умолчанию 14);
* `hsts` — `Strict-Transport-Security` с `max-age` ≥ 31536000 и `includeSubDomains` (SECURITY_ARCHITECTURE §14);
* `http-redirect` — `http://` отвечает перенаправлением (301/302/307/308) на `https://` того же хоста;
* `security-headers` — заголовки безопасности из `infrastructure/docker/web/nginx.conf` (`add_header … always`)
  присутствуют с теми же значениями: CSP (с `frame-ancestors`), X-Content-Type-Options, X-Frame-Options, Referrer-Policy;
* `metrics-not-public` — `/metrics` и `/api/metrics` снаружи не отдают метрики Prometheus (bearer-токен и сеть
  backend — SC-OBS-03; fallback SPA `index.html` с кодом 200 допустим).

Сетевые запросы — напрямую (переменные `*_PROXY` не используются). Код выхода: 0 — все проверки PASS, 1 — есть FAIL,
2 — ошибка аргументов. `--json` — машиночитаемый отчёт (для журнала релиза).
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import time
import warnings
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = ROOT / "infrastructure" / "docker" / "web" / "nginx.conf"
HSTS_MIN_MAX_AGE = 31_536_000  # 1 год (SECURITY_ARCHITECTURE §14)
DEFAULT_MIN_DAYS = 14
TIMEOUT_SECONDS = 10.0
METRICS_PATHS = ("/metrics", "/api/metrics")
REDIRECT_CODES = frozenset({301, 302, 307, 308})
LEGACY_VERSIONS = (("TLSv1.0", ssl.TLSVersion.TLSv1), ("TLSv1.1", ssl.TLSVersion.TLSv1_1))
# Клиент не смог предложить версию (OpenSSL собран/настроен без неё): проверка не выполнена, а не «отклонено сервером».
_CLIENT_SIDE_REASONS = frozenset({"NO_PROTOCOLS_AVAILABLE", "NO_CIPHERS_AVAILABLE"})
_ADD_HEADER = re.compile(r"^\s*add_header\s+(\S+)\s+(\"[^\"]*\"|\S+)(?:\s+always)?\s*;", re.M)
_PROMETHEUS_LINE = re.compile(r"^# (?:HELP|TYPE) [a-zA-Z_:][a-zA-Z0-9_:]* ", re.M)


@dataclass(frozen=True)
class Result:
    check: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Target:
    url: str  # https://host[:port] (без пути)
    host: str
    port: int
    http_url: str  # адрес проверки перенаправления


class PreflightError(ValueError):
    pass


def parse_target(url: str, http_url: str | None = None) -> Target:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise PreflightError(f"--url должен быть https://<хост>[:порт], получено {url!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise PreflightError("--url — только схема, хост и порт (без пути, запроса и учётных данных)")
    host = parts.hostname
    port = parts.port or 443
    netloc = f"[{host}]" if ":" in host else host
    base = urlunsplit(("https", f"{netloc}:{port}" if parts.port else netloc, "", "", ""))
    if http_url is None:
        http_url = urlunsplit(("http", netloc, "/", "", ""))
    elif urlsplit(http_url).scheme != "http":
        raise PreflightError("--http-url должен начинаться с http://")
    return Target(url=base, host=host, port=port, http_url=http_url)


def expected_headers(nginx_conf: Path = NGINX_CONF) -> dict[str, str]:
    """Заголовки безопасности сервера `web` (`add_header Имя значение always;`) → {имя в нижнем регистре: значение}."""
    found: dict[str, str] = {}
    for name, value in _ADD_HEADER.findall(nginx_conf.read_text(encoding="utf-8")):
        found[name.lower()] = value.strip('"')
    return found


def client_context(ca_file: Path | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _handshake(target: Target, ctx: ssl.SSLContext) -> tuple[str | None, dict[str, Any]]:
    with (
        socket.create_connection((target.host, target.port), timeout=TIMEOUT_SECONDS) as raw,
        ctx.wrap_socket(raw, server_hostname=target.host) as tls,
    ):
        return tls.version(), tls.getpeercert() or {}


def _legacy_context(version: ssl.TLSVersion) -> ssl.SSLContext:
    """Клиент, предлагающий только устаревшую версию; сертификат не проверяется — проверяется лишь согласие сервера."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")  # иначе OpenSSL 3 сам не предложит TLS 1.0/1.1
    with warnings.catch_warnings():  # устаревшие версии используются намеренно — проверяется их отказ
        warnings.simplefilter("ignore", DeprecationWarning)
        ctx.minimum_version = version
        ctx.maximum_version = version
    return ctx


def check_tls_versions(target: Target, ctx: ssl.SSLContext) -> Result:
    problems: list[str] = []
    for label, version in LEGACY_VERSIONS:
        try:
            negotiated, _ = _handshake(target, _legacy_context(version))
        except ssl.SSLError as exc:
            if getattr(exc, "reason", None) in _CLIENT_SIDE_REASONS:
                problems.append(f"{label}: не проверено — локальный OpenSSL не может предложить версию ({exc.reason})")
            continue  # alert protocol_version / unsupported protocol — сервер отклонил
        except (ConnectionError, TimeoutError, OSError):
            continue  # соединение сброшено/закрыто сервером на ClientHello — тоже отказ
        problems.append(f"{label} принят сервером (согласовано {negotiated})")
    try:
        modern, _ = _handshake(target, ctx)
    except (ssl.SSLError, OSError) as exc:
        problems.append(f"TLS 1.2+ не установлен: {exc}")
        modern = None
    if problems:
        return Result("tls-versions", False, "; ".join(problems))
    return Result("tls-versions", True, f"TLS 1.0/1.1 отклонены, согласовано {modern}")


def check_certificate(target: Target, ctx: ssl.SSLContext, *, min_days: int, now: float | None = None) -> Result:
    try:
        _, cert = _handshake(target, ctx)
    except ssl.SSLCertVerificationError as exc:
        return Result("certificate", False, f"сертификат не прошёл проверку: {exc.verify_message or exc}")
    except (ssl.SSLError, OSError) as exc:
        return Result("certificate", False, f"TLS-соединение не установлено: {exc}")
    not_after = cert.get("notAfter")
    if not isinstance(not_after, str):
        return Result("certificate", False, "в сертификате нет notAfter")
    days_left = (ssl.cert_time_to_seconds(not_after) - (time.time() if now is None else now)) / 86400
    if days_left < min_days:
        return Result("certificate", False, f"истекает через {days_left:.1f} дн. (< {min_days}): {not_after}")
    return Result("certificate", True, f"доверен, имя {target.host} совпадает, до истечения {days_left:.0f} дн.")


def parse_hsts(value: str) -> tuple[int | None, bool]:
    max_age: int | None = None
    include_subdomains = False
    for directive in (d.strip() for d in value.split(";")):
        name, _, arg = directive.partition("=")
        name = name.strip().lower()
        if name == "max-age":
            arg = arg.strip().strip('"')
            max_age = int(arg) if arg.isdigit() else None
        elif name == "includesubdomains":
            include_subdomains = True
    return max_age, include_subdomains


def check_hsts(response: httpx.Response) -> Result:
    value = response.headers.get("strict-transport-security")
    if value is None:
        return Result("hsts", False, "нет заголовка Strict-Transport-Security")
    max_age, include_subdomains = parse_hsts(value)
    problems = []
    if max_age is None or max_age < HSTS_MIN_MAX_AGE:
        problems.append(f"max-age={max_age} < {HSTS_MIN_MAX_AGE}")
    if not include_subdomains:
        problems.append("нет includeSubDomains")
    if problems:
        return Result("hsts", False, f"{value!r}: " + ", ".join(problems))
    return Result("hsts", True, value)


def check_redirect(target: Target) -> Result:
    try:
        with httpx.Client(trust_env=False, timeout=TIMEOUT_SECONDS) as client:
            response = client.get(target.http_url, follow_redirects=False)
    except httpx.HTTPError as exc:
        return Result("http-redirect", False, f"{target.http_url} недоступен: {exc}")
    location = response.headers.get("location", "")
    dest = urlsplit(location)
    if response.status_code not in REDIRECT_CODES:
        return Result("http-redirect", False, f"{target.http_url} → {response.status_code} без перенаправления")
    if dest.scheme != "https" or (dest.hostname or "").lower() != target.host.lower():
        return Result(
            "http-redirect",
            False,
            f"{target.http_url} → {response.status_code} {location!r} (не https://{target.host})",
        )
    return Result("http-redirect", True, f"{target.http_url} → {response.status_code} {location}")


def _normalize(value: str) -> str:
    return " ".join(value.split())


def check_security_headers(response: httpx.Response, expected: dict[str, str]) -> Result:
    if not expected:
        return Result("security-headers", False, f"в {NGINX_CONF.name} не найдены заголовки add_header")
    problems = []
    for name, value in sorted(expected.items()):
        actual = response.headers.get(name)
        if actual is None:
            problems.append(f"нет {name}")
        elif _normalize(actual) != _normalize(value):
            problems.append(f"{name}={actual!r}, ожидается {value!r}")
    csp = response.headers.get("content-security-policy", "")
    xfo = response.headers.get("x-frame-options", "")
    if "frame-ancestors" not in csp and xfo.upper() not in ("DENY", "SAMEORIGIN"):
        problems.append("нет защиты от встраивания (CSP frame-ancestors / X-Frame-Options)")
    if problems:
        return Result("security-headers", False, "; ".join(problems))
    return Result("security-headers", True, ", ".join(sorted(expected)))


def _looks_like_metrics(response: httpx.Response) -> bool:
    ctype = response.headers.get("content-type", "")
    if "openmetrics" in ctype or "version=0.0.4" in ctype:
        return True
    return bool(_PROMETHEUS_LINE.search(response.text[:65536]))


def check_metrics_not_public(client: httpx.Client, target: Target) -> Result:
    seen = []
    for path in METRICS_PATHS:
        try:
            response = client.get(target.url + path, follow_redirects=False)
        except httpx.HTTPError as exc:
            seen.append(f"{path}: нет ответа ({type(exc).__name__})")
            continue
        if response.status_code == 200 and _looks_like_metrics(response):
            return Result("metrics-not-public", False, f"{path} отдаёт метрики Prometheus наружу")
        seen.append(f"{path}: {response.status_code}")
    return Result("metrics-not-public", True, ", ".join(seen))


def run_checks(
    target: Target,
    *,
    ca_file: Path | None = None,
    min_days: int = DEFAULT_MIN_DAYS,
    expected: dict[str, str] | None = None,
) -> list[Result]:
    ctx = client_context(ca_file)
    results = [check_tls_versions(target, ctx), check_certificate(target, ctx, min_days=min_days)]
    expected = expected_headers() if expected is None else expected
    try:
        with httpx.Client(verify=ctx, trust_env=False, timeout=TIMEOUT_SECONDS) as client:
            page = client.get(target.url + "/", follow_redirects=False)
            results += [
                check_hsts(page),
                check_security_headers(page, expected),
                check_metrics_not_public(client, target),
            ]
    except httpx.HTTPError as exc:
        detail = f"HTTPS-запрос {target.url}/ не выполнен: {exc}"
        results += [Result(name, False, detail) for name in ("hsts", "security-headers", "metrics-not-public")]
    results.append(check_redirect(target))
    return results


def render(results: Sequence[Result], out: Callable[[str], object] = print) -> None:
    for r in results:
        out(f"{'PASS' if r.ok else 'FAIL'} {r.check}: {r.detail}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preflight_tls", description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--url", required=True, help="внешний адрес развёртывания, https://<хост>[:порт]")
    parser.add_argument("--http-url", help="адрес проверки перенаправления (по умолчанию http://<хост>/)")
    parser.add_argument("--ca-file", type=Path, help="доверенный CA (корпоративный/локальный), иначе системные")
    parser.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS, help="минимальный срок до истечения, дней")
    parser.add_argument("--json", action="store_true", help="отчёт JSON в stdout")
    args = parser.parse_args(argv)
    try:
        target = parse_target(args.url, args.http_url)
    except PreflightError as exc:
        parser.error(str(exc))
    results = run_checks(target, ca_file=args.ca_file, min_days=args.min_days)
    ok = all(r.ok for r in results)
    if args.json:
        print(json.dumps({"url": target.url, "ok": ok, "results": [asdict(r) for r in results]}, ensure_ascii=False))
    else:
        render(results)
        print(f"preflight TLS {target.url}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
