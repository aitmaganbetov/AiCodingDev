"""P8-06: распространение W3C traceparent и гигиена атрибутов спанов (SC-OBS-03)."""

from __future__ import annotations

import uuid

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from aicd_platform import metrics
from aicd_platform.telemetry import TRACEPARENT_RE, current_traceparent, safe_attributes, span, tracer
from aicd_worker import handlers, runtime
from tests.support.tracing import by_name, spans, trace_id

__all__ = ["spans"]


def test_traceparent_only_inside_span(spans: InMemorySpanExporter) -> None:
    assert current_traceparent() is None
    with tracer().start_as_current_span("root") as root:
        tp = current_traceparent()
        assert tp is not None and TRACEPARENT_RE.fullmatch(tp)
        assert tp.split("-")[1] == format(root.get_span_context().trace_id, "032x")


def test_child_span_continues_remote_trace(spans: InMemorySpanExporter) -> None:
    with tracer().start_as_current_span("producer"):
        carried = current_traceparent()
    assert carried is not None
    with span("consumer", traceparent=carried, attributes={"aicd.event_type": "run.created"}):
        pass
    producer, consumer = by_name(spans, "producer")[0], by_name(spans, "consumer")[0]
    assert trace_id(producer) == trace_id(consumer)
    assert consumer.parent is not None and producer.context is not None
    assert consumer.parent.span_id == producer.context.span_id


def test_invalid_traceparent_starts_new_trace(spans: InMemorySpanExporter) -> None:
    with span("orphan", traceparent="00-not-a-trace"):
        pass
    assert by_name(spans, "orphan")[0].parent is None


def test_span_attributes_are_allowlisted(spans: InMemorySpanExporter) -> None:
    attrs = safe_attributes(
        {"aicd.run_id": uuid.UUID(int=1), "prompt": "SECRET PROMPT", "user.email": "a@b.c", "aicd.status": "x" * 500}
    )
    assert set(attrs) == {"aicd.run_id", "aicd.status"}
    assert len(str(attrs["aicd.status"])) == 128
    with span("s", attributes={"prompt": "SECRET", "aicd.provider": "codex"}):
        pass
    exported = by_name(spans, "s")[0]
    assert dict(exported.attributes or {}) == {"aicd.provider": "codex"}


async def test_worker_handler_continues_trace_and_propagates_to_exec_message(spans: InMemorySpanExporter) -> None:
    """Обработчик Dramatiq-сообщения — дочерний спан публикации; сообщение exec несёт контекст обработчика."""
    with tracer().start_as_current_span("outbox.publish"):
        carried = current_traceparent()
    produced: dict[str, object] = {}

    async def work() -> str:
        produced.update(handlers.execution_message(uuid.uuid4(), uuid.uuid4()))
        return "execute_task"

    before = metrics.REGISTRY.get_sample_value(
        "aicd_worker_messages_total", {"actor": "exec.execute_task", "outcome": "execute_task"}
    )
    result = await runtime.traced("exec.execute_task", {"traceparent": carried, "event_id": "e1"}, work())
    assert result == "execute_task"
    worker = by_name(spans, "worker exec.execute_task")[0]
    publish = by_name(spans, "outbox.publish")[0]
    assert worker.kind == SpanKind.CONSUMER
    assert trace_id(worker) == trace_id(publish)
    tp = produced["traceparent"]
    assert isinstance(tp, str) and tp.split("-")[1] == trace_id(worker)
    assert worker.context is not None and tp.split("-")[2] == format(worker.context.span_id, "016x")
    after = metrics.REGISTRY.get_sample_value(
        "aicd_worker_messages_total", {"actor": "exec.execute_task", "outcome": "execute_task"}
    )
    assert (after or 0) == (before or 0) + 1


async def test_worker_handler_error_is_counted(spans: InMemorySpanExporter) -> None:
    async def boom() -> None:
        raise RuntimeError("fail")

    before = metrics.REGISTRY.get_sample_value(
        "aicd_worker_messages_total", {"actor": "orchestrator.handle_event", "outcome": "error"}
    )
    with pytest.raises(RuntimeError):
        await runtime.traced("orchestrator.handle_event", {}, boom())
    after = metrics.REGISTRY.get_sample_value(
        "aicd_worker_messages_total", {"actor": "orchestrator.handle_event", "outcome": "error"}
    )
    assert (after or 0) == (before or 0) + 1
    assert by_name(spans, "worker orchestrator.handle_event")[0].status.status_code.name == "ERROR"
