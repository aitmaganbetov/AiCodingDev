"""SC-OBS-02 / P8-06: метрика отклонённых подписей вебхука GitHub и вид потока в gauge SSE-соединений."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi import FastAPI

from aicd_api.routers.github import _signature_failure
from aicd_platform import metrics
from tests.integration.test_github_api import _post_webhook, _webhook_body, admin, env, gapp

__all__ = ["admin", "env", "gapp"]  # фикстуры pytest

pytestmark = pytest.mark.db


def _sample(name: str, labels: dict[str, str]) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def test_signature_failure_reason_is_low_cardinality() -> None:
    assert _signature_failure(None) == "missing"
    assert _signature_failure("") == "missing"
    assert _signature_failure("sha1=" + "0" * 40) == "malformed"
    assert _signature_failure("sha256=zz") == "malformed"
    assert _signature_failure("sha256=" + "a" * 64) == "mismatch"
    assert {_signature_failure(h) for h in (None, "x", "sha256=" + "0" * 64)} <= metrics.WEBHOOK_SIGNATURE_REASONS


async def test_rejected_webhook_signatures_are_counted(gapp: FastAPI, admin: psycopg.Connection) -> None:
    body = _webhook_body()
    before = {
        r: _sample("aicd_webhook_signature_invalid_total", {"reason": r}) for r in metrics.WEBHOOK_SIGNATURE_REASONS
    }
    cases = {"mismatch": "sha256=" + "0" * 64, "missing": None, "malformed": "sha1=" + "0" * 40}
    for signature in cases.values():
        r = await _post_webhook(gapp, body, guid=str(uuid.uuid4()), signature=signature)
        assert r.status_code == 401
    for reason in cases:
        assert _sample("aicd_webhook_signature_invalid_total", {"reason": reason}) == before[reason] + 1
    ok = await _post_webhook(gapp, body, guid=str(uuid.uuid4()))
    assert ok.status_code in (200, 202)
    for reason in cases:
        assert _sample("aicd_webhook_signature_invalid_total", {"reason": reason}) == before[reason] + 1


def test_sse_gauge_has_stream_kind_label() -> None:
    metrics.SSE_CONNECTIONS.labels("workspace").inc()
    metrics.SSE_CONNECTIONS.labels("workspace").dec()
    exposed = metrics.render()[0].decode()
    assert 'aicd_sse_connections{stream="workspace"}' in exposed
    assert {"run", "workspace"} == metrics.SSE_STREAMS
