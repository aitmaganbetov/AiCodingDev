"""Трассировка OpenTelemetry (FR-OBS-03, P1-06, P8-06).

Экспорт выключен по умолчанию (`AICD_OTEL_EXPORTER=none`); `console` — для отладки. Отправка в коллектор
(OTLP) требует отдельной зависимости-экспортёра и не входит в P8-06 (OPEN_QUESTIONS OQ-46).

Сквозная трасса (P8-06): API-запрос → outbox-событие (`outbox_events.traceparent`, W3C) → публикация
диспетчером (поле `traceparent` Dramatiq-сообщения) → обработчик воркера → сообщение очереди exec →
исполнение CLI. Контекст передаётся только в виде W3C `traceparent`; tracestate/baggage не переносятся.

Атрибуты спанов — только из allowlist (SC-OBS-03): идентификаторы корреляции и низкокардинальные
перечисления; промпты, код, вывод CLI, e-mail и тексты ошибок в спаны не пишутся.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")

# Разрешённые атрибуты спанов (SC-OBS-03): корреляция и перечисления, без содержимого.
ALLOWED_ATTRIBUTES = frozenset(
    {
        "http.method",
        "http.route",
        "http.status_code",
        "aicd.workspace_id",
        "aicd.run_id",
        "aicd.task_id",
        "aicd.execution_id",
        "aicd.event_id",
        "aicd.event_type",
        "aicd.actor",
        "aicd.outcome",
        "aicd.provider",
        "aicd.status",
        "aicd.failure_category",
        "messaging.system",
        "messaging.destination.name",
    }
)

_configured = False
_propagator = TraceContextTextMapPropagator()


def configure_tracing(service_name: str, exporter: str) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("aicd")


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")


def current_traceparent() -> str | None:
    """W3C `traceparent` текущего спана (None — вне трассы)."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    value = carrier.get("traceparent")
    return value if value and TRACEPARENT_RE.fullmatch(value) else None


def safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str | int | float | bool]:
    """Атрибуты из allowlist; значения — примитивы, строки ≤ 128 символов."""
    out: dict[str, str | int | float | bool] = {}
    for key, value in (attributes or {}).items():
        if key not in ALLOWED_ATTRIBUTES or value is None:
            continue
        if isinstance(value, bool | int | float):
            out[key] = value
        else:
            out[key] = str(value)[:128]
    return out


@contextmanager
def span(
    name: str,
    *,
    traceparent: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
) -> Iterator[trace.Span]:
    """Спан как дочерний к `traceparent` (если передан и корректен), иначе — к текущему контексту."""
    parent = None
    if traceparent and TRACEPARENT_RE.fullmatch(traceparent):
        parent = _propagator.extract({"traceparent": traceparent})
    token = otel_context.attach(parent) if parent is not None else None
    try:
        with tracer().start_as_current_span(name, kind=kind, attributes=safe_attributes(attributes)) as current:
            yield current
    finally:
        if token is not None:
            otel_context.detach(token)
