"""ASGI/HTTP middleware: корреляция, заголовки безопасности, лимит тела, access-лог."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aicd_api.responses import problem_response
from aicd_platform import context, metrics
from aicd_platform.errors import ProblemError
from aicd_platform.telemetry import current_trace_id, tracer

log = logging.getLogger("aicd.access")

MAX_BODY_BYTES = 256 * 1024
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class _BodyTooLargeError(Exception):
    pass


class BodyLimitMiddleware:
    """413 для тел > 256 KiB (API_DESIGN §6) — по Content-Length и по фактическому потоку."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES, overrides: dict[str, int] | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = dict(overrides or {})  # точный путь → лимит (вебхук GitHub, SC-GH-01)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        max_bytes = self.overrides.get(scope.get("path", ""), self.max_bytes)
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            await self._reject(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise _BodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLargeError:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = problem_response(ProblemError("payload_too_large"), instance=scope.get("path", ""), request_id=None)
        await response(scope, receive, send)


async def request_context_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    context.clear()
    started = time.perf_counter()
    with tracer().start_as_current_span(f"{request.method} {request.url.path}") as span:
        request_id = current_trace_id() or uuid.uuid4().hex
        context.bind(request_id=request_id, trace_id=request_id)
        request.state.request_id = request_id
        if request.method in ("POST", "PUT", "PATCH") and request.headers.get("content-length", "0") != "0":
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                response = problem_response(
                    ProblemError("unsupported_media_type"), instance=request.url.path, request_id=request_id
                )
                return _finish(request, response, started)
        response = await call_next(request)
        route = request.scope.get("route")
        span.set_attribute("http.route", getattr(route, "path", "unmatched"))
        span.set_attribute("http.status_code", response.status_code)
    return _finish(request, response, started)


def _finish(request: Request, response: Response, started: float) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers["X-Request-Id"] = request.state.request_id
    rate = getattr(request.state, "rate", None)
    if rate is not None:
        response.headers["RateLimit-Limit"] = str(rate.limit)
        response.headers["RateLimit-Remaining"] = str(rate.remaining)
        response.headers["RateLimit-Reset"] = str(rate.reset_seconds)
    route = request.scope.get("route")
    template = getattr(route, "path", "unmatched")  # шаблон пути, без query string и ID
    elapsed = time.perf_counter() - started
    method = metrics.label(request.method, HTTP_METHODS)
    metrics.HTTP_REQUESTS.labels(method, template, str(response.status_code)).inc()
    metrics.HTTP_DURATION.labels(method, template).observe(elapsed)
    log.info(
        "request",
        extra={
            "method": request.method,
            "route": template,
            "status": response.status_code,
            "duration_ms": round(elapsed * 1000, 1),
        },
    )
    return response
