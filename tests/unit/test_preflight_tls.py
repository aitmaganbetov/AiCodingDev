"""Предрелизная проверка TLS/HSTS (TH-46): локальный TLS-сервер с сертификатом от сгенерированного CA."""

from __future__ import annotations

import datetime as dt
import ipaddress
import ssl
import threading
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from scripts.release import preflight_tls as pf

HSTS_OK = "max-age=31536000; includeSubDomains"
NGINX_HEADERS = pf.expected_headers()


# --- Сертификаты ------------------------------------------------------------------------------------


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


@dataclass(frozen=True)
class Pki:
    ca_file: Path
    other_ca_file: Path
    tmp: Path
    ca_key: ec.EllipticCurvePrivateKey
    ca_cert: x509.Certificate

    def leaf(self, name: str, *, days: float = 90, sans: tuple[str, ...] = ("localhost",)) -> tuple[Path, Path]:
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.UTC)
        alt: list[x509.GeneralName] = []
        for san in sans:
            try:
                alt.append(x509.IPAddress(ipaddress.ip_address(san)))
            except ValueError:
                alt.append(x509.DNSName(san))
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(sans[0]))
            .issuer_name(self.ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()), critical=False)
            .sign(self.ca_key, hashes.SHA256())
        )
        cert_file, key_file = self.tmp / f"{name}.crt", self.tmp / f"{name}.key"
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        return cert_file, key_file


def _ca(cn: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    tmp = tmp_path_factory.mktemp("pki")
    key, cert = _ca("AICD preflight test CA")
    _, other = _ca("Unrelated CA")
    ca_file, other_file = tmp / "ca.pem", tmp / "other-ca.pem"
    ca_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    other_file.write_bytes(other.public_bytes(serialization.Encoding.PEM))
    return Pki(ca_file=ca_file, other_ca_file=other_file, tmp=tmp, ca_key=key, ca_cert=cert)


# --- Локальные серверы --------------------------------------------------------------------------------


@dataclass
class Behaviour:
    headers: dict[str, str] = field(default_factory=lambda: {"Strict-Transport-Security": HSTS_OK, **NGINX_HEADERS})
    metrics_body: str | None = None  # None — 404 на /metrics
    spa_fallback: bool = False  # /metrics → 200 index.html (как try_files nginx)
    redirect_status: int = 301
    redirect_location: str | None = None  # None — https://localhost:<tls_port>/


def _handler(behaviour: Behaviour, *, tls: bool, tls_port: list[int]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not tls:
                location = behaviour.redirect_location or f"https://localhost:{tls_port[0]}{self.path}"
                if behaviour.redirect_status in pf.REDIRECT_CODES:
                    self._send(behaviour.redirect_status, b"", {"Location": location})
                else:
                    self._send(behaviour.redirect_status, b"<html></html>", {"Content-Type": "text/html"})
                return
            headers = dict(behaviour.headers)
            if self.path in pf.METRICS_PATHS:
                if behaviour.metrics_body is not None:
                    self._send(200, behaviour.metrics_body.encode(), {**headers, "Content-Type": "text/plain"})
                elif behaviour.spa_fallback:
                    self._send(
                        200, b"<!doctype html><title>AiCodingDev</title>", {**headers, "Content-Type": "text/html"}
                    )
                else:
                    self._send(404, b"not found", headers)
                return
            self._send(200, b"<!doctype html><title>AiCodingDev</title>", {**headers, "Content-Type": "text/html"})

    return Handler


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # сбои рукопожатия проверочных клиентов ожидаемы


@dataclass
class Deployment:
    behaviour: Behaviour
    tls_port: int
    http_port: int

    def target(self, host: str = "localhost") -> pf.Target:
        return pf.parse_target(f"https://{host}:{self.tls_port}", f"http://{host}:{self.http_port}/")


def _server_context(cert: Path, key: Path, *, legacy: bool) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    if legacy:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ssl.TLSVersion.TLSv1
    else:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


@pytest.fixture
def serve(pki: Pki) -> Iterator[Any]:
    servers: list[ThreadingHTTPServer] = []

    def start(
        behaviour: Behaviour | None = None,
        *,
        cert_days: float = 90,
        legacy: bool = False,
        sans: tuple[str, ...] = ("localhost",),
    ) -> Deployment:
        behaviour = behaviour or Behaviour()
        cert, key = pki.leaf(f"leaf-{len(servers)}", days=cert_days, sans=sans)
        tls_port: list[int] = []
        https = _QuietServer(("127.0.0.1", 0), _handler(behaviour, tls=True, tls_port=tls_port))
        https.socket = _server_context(cert, key, legacy=legacy).wrap_socket(https.socket, server_side=True)
        tls_port.append(https.server_address[1])
        http = _QuietServer(("127.0.0.1", 0), _handler(behaviour, tls=False, tls_port=tls_port))
        for srv in (https, http):
            servers.append(srv)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        return Deployment(behaviour, tls_port[0], http.server_address[1])

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _by_check(results: list[pf.Result]) -> dict[str, pf.Result]:
    return {r.check: r for r in results}


# --- Тесты ----------------------------------------------------------------------------------------------


def test_expected_headers_come_from_nginx_conf() -> None:
    assert set(NGINX_HEADERS) == {
        "x-content-type-options", "x-frame-options", "referrer-policy", "content-security-policy",
    }  # fmt: skip
    assert "frame-ancestors 'none'" in NGINX_HEADERS["content-security-policy"]
    assert NGINX_HEADERS["x-content-type-options"] == "nosniff"


def test_well_configured_deployment_passes(serve: Any, pki: Pki, capsys: pytest.CaptureFixture[str]) -> None:
    dep = serve()
    results = _by_check(pf.run_checks(dep.target(), ca_file=pki.ca_file))
    assert set(results) == {
        "tls-versions",
        "certificate",
        "hsts",
        "http-redirect",
        "security-headers",
        "metrics-not-public",
    }
    assert all(r.ok for r in results.values()), results
    assert "TLSv1.3" in results["tls-versions"].detail or "TLSv1.2" in results["tls-versions"].detail
    # CLI: код 0 и JSON-отчёт
    code = pf.main([
        "--url", f"https://localhost:{dep.tls_port}", "--http-url", f"http://localhost:{dep.http_port}/",
        "--ca-file", str(pki.ca_file), "--json",
    ])  # fmt: skip
    assert code == 0
    assert '"ok": true' in capsys.readouterr().out


def test_spa_fallback_on_metrics_path_is_not_exposure(serve: Any, pki: Pki) -> None:
    dep = serve(Behaviour(spa_fallback=True))
    assert _by_check(pf.run_checks(dep.target(), ca_file=pki.ca_file))["metrics-not-public"].ok


def test_public_metrics_fail(serve: Any, pki: Pki) -> None:
    body = "# HELP aicd_runs_finished_total Runs\n# TYPE aicd_runs_finished_total counter\naicd_runs_finished_total 1\n"
    dep = serve(Behaviour(metrics_body=body))
    result = _by_check(pf.run_checks(dep.target(), ca_file=pki.ca_file))["metrics-not-public"]
    assert not result.ok and "/metrics" in result.detail


def test_legacy_tls_versions_fail(serve: Any, pki: Pki) -> None:
    dep = serve(legacy=True)
    result = _by_check(pf.run_checks(dep.target(), ca_file=pki.ca_file))["tls-versions"]
    assert not result.ok
    assert "TLSv1.0 принят" in result.detail and "TLSv1.1 принят" in result.detail


def test_untrusted_wrong_host_and_expiring_certificates_fail(serve: Any, pki: Pki) -> None:
    dep = serve()
    untrusted = _by_check(pf.run_checks(dep.target(), ca_file=pki.other_ca_file))
    assert not untrusted["certificate"].ok and "не прошёл проверку" in untrusted["certificate"].detail
    # Без доверия к сертификату HTTP-проверки тоже не проходят (заголовки не читаются по недоверенному каналу).
    assert not untrusted["hsts"].ok
    assert untrusted["tls-versions"].ok  # политика версий проверяется отдельно от доверия к сертификату
    wrong_host = _by_check(pf.run_checks(dep.target("127.0.0.1"), ca_file=pki.ca_file))["certificate"]
    assert not wrong_host.ok and "127.0.0.1" in wrong_host.detail
    expiring = serve(cert_days=5)
    result = _by_check(pf.run_checks(expiring.target(), ca_file=pki.ca_file))["certificate"]
    assert not result.ok and "< 14" in result.detail
    assert _by_check(pf.run_checks(expiring.target(), ca_file=pki.ca_file, min_days=3))["certificate"].ok


@pytest.mark.parametrize(
    ("hsts", "fragment"),
    [
        (None, "нет заголовка"),
        ("max-age=86400; includeSubDomains", "max-age=86400"),
        ("max-age=31536000", "includeSubDomains"),
        ("max-age=abc; includeSubDomains", "max-age=None"),
    ],
)
def test_hsts_failures(serve: Any, pki: Pki, hsts: str | None, fragment: str) -> None:
    headers = dict(NGINX_HEADERS)
    if hsts is not None:
        headers["Strict-Transport-Security"] = hsts
    dep = serve(Behaviour(headers=headers))
    result = _by_check(pf.run_checks(dep.target(), ca_file=pki.ca_file))["hsts"]
    assert not result.ok and fragment in result.detail


def test_hsts_parsing() -> None:
    assert pf.parse_hsts("max-age=63072000; includeSubDomains; preload") == (63072000, True)
    assert pf.parse_hsts('max-age="31536000"') == (31536000, False)
    assert pf.parse_hsts("includesubdomains") == (None, True)


@pytest.mark.parametrize(
    ("status", "location", "fragment"),
    [
        (200, None, "без перенаправления"),
        (301, "http://localhost/", "не https://localhost"),
        (302, "https://evil.example/", "не https://localhost"),
    ],
)
def test_redirect_failures(serve: Any, pki: Pki, status: int, location: str | None, fragment: str) -> None:
    dep = serve(Behaviour(redirect_status=status, redirect_location=location))
    result = _by_check(pf.run_checks(dep.target(), ca_file=pki.ca_file))["http-redirect"]
    assert not result.ok and fragment in result.detail


def test_redirect_unreachable_fails(pki: Pki) -> None:
    target = pf.parse_target("https://localhost:1", "http://localhost:1/")
    assert not pf.check_redirect(target).ok


def test_security_header_missing_or_changed_fails(serve: Any, pki: Pki) -> None:
    headers = {"Strict-Transport-Security": HSTS_OK, **NGINX_HEADERS}
    del headers["referrer-policy"]
    headers["content-security-policy"] = "default-src *"
    dep = serve(Behaviour(headers=headers))
    result = _by_check(pf.run_checks(dep.target(), ca_file=pki.ca_file))["security-headers"]
    assert not result.ok
    assert "нет referrer-policy" in result.detail and "content-security-policy=" in result.detail


def test_framing_protection_required_even_without_nginx_expectations(serve: Any, pki: Pki) -> None:
    dep = serve(Behaviour(headers={"Strict-Transport-Security": HSTS_OK, "X-Content-Type-Options": "nosniff"}))
    results = pf.run_checks(dep.target(), ca_file=pki.ca_file, expected={"x-content-type-options": "nosniff"})
    assert "встраивания" in _by_check(results)["security-headers"].detail
    assert not pf.check_security_headers(httpx.Response(200), {}).ok


def test_failing_deployment_exit_code(serve: Any, pki: Pki, capsys: pytest.CaptureFixture[str]) -> None:
    dep = serve(Behaviour(headers=dict(NGINX_HEADERS)))  # без HSTS
    code = pf.main([
        "--url", f"https://localhost:{dep.tls_port}", "--http-url", f"http://localhost:{dep.http_port}/",
        "--ca-file", str(pki.ca_file),
    ])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 1 and "FAIL hsts" in out and out.rstrip().endswith("FAIL")


@pytest.mark.parametrize(
    "url", ["http://aicd.example", "https://aicd.example/app", "https://u:p@aicd.example", "https://"]
)
def test_parse_target_rejects_bad_urls(url: str) -> None:
    with pytest.raises(pf.PreflightError):
        pf.parse_target(url)


def test_parse_target_defaults() -> None:
    t = pf.parse_target("https://aicd.example.internal/")
    assert (t.url, t.host, t.port, t.http_url) == (
        "https://aicd.example.internal", "aicd.example.internal", 443, "http://aicd.example.internal/",
    )  # fmt: skip
    with pytest.raises(pf.PreflightError):
        pf.parse_target("https://aicd.example", "https://aicd.example")
