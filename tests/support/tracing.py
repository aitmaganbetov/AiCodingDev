"""In-memory экспорт спанов OpenTelemetry для тестов сквозной трассировки (P8-06)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aicd_platform.telemetry import configure_tracing

_exporter: InMemorySpanExporter | None = None


def exporter() -> InMemorySpanExporter:
    """Экспортёр, подключённый к глобальному TracerProvider процесса (провайдер задаётся один раз)."""
    global _exporter
    if _exporter is None:
        configure_tracing("aicd-test", "none")
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):  # pragma: no cover — провайдер задан не платформой
            raise RuntimeError("глобальный TracerProvider не SDK — трассировка в тестах недоступна")
        _exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
    return _exporter


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exp = exporter()
    exp.clear()
    yield exp
    exp.clear()


def trace_id(span: ReadableSpan) -> str:
    assert span.context is not None
    return format(span.context.trace_id, "032x")


def by_name(exp: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in exp.get_finished_spans() if s.name == name]
