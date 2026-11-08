"""P8-06: реестр метрик Prometheus — метки, доменные счётчики по outbox, защита `/metrics` (API и воркеры)."""

from __future__ import annotations

import types
import urllib.error
import urllib.request

import httpx
import pytest
from pydantic import SecretStr

from aicd_api.main import create_app
from aicd_platform import metrics
from aicd_platform.config import Settings
from aicd_platform.ids import uuid7


def sample(name: str, labels: dict[str, str] | None = None) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_label_values_are_bounded() -> None:
    assert metrics.label("COMPLETED", metrics.RUN_STATUSES) == "COMPLETED"
    assert metrics.label("DROP TABLE", metrics.RUN_STATUSES) == "other"
    assert metrics.label(None) == "none"
    assert metrics.label("run.state_changed") == "run.state_changed"
    # Идентификаторы-подобные и длинные значения/свободный текст в метки не попадают.
    assert metrics.label("user@example.com") == "other"
    assert metrics.label("x" * 65) == "other"
    assert metrics.label("ошибка: нет доступа") == "other"


def test_outbox_events_update_domain_counters() -> None:
    before = {
        "run": sample("aicd_runs_finished_total", {"final_status": "COMPLETED"}),
        "trans": sample("aicd_run_transitions_total", {"to_status": "RUNNING"}),
        "retry": sample("aicd_task_transitions_total", {"to_status": "RETRY_WAIT"}),
        "blocked": sample("aicd_task_transitions_total", {"to_status": "BLOCKED"}),
        "gate": sample("aicd_gate_executions_total", {"gate_type": "UNIT", "status": "FAIL", "required": "true"}),
        "acp": sample("aicd_agent_messages_total", {"message_type": "FIX_REQUEST"}),
        "decided": sample("aicd_approvals_decided_total", {"action_type": "MERGE", "status": "APPROVED"}),
        "wait": sample("aicd_approval_wait_seconds_count", {"action_type": "MERGE"}),
        "wait_sum": sample("aicd_approval_wait_seconds_sum", {"action_type": "MERGE"}),
        "events": sample("aicd_outbox_events_published_total", {"event_type": "gate.completed"}),
    }
    metrics.observe_outbox_event("run.completed", {"final_status": "COMPLETED"})
    metrics.observe_outbox_event("run.state_changed", {"from": "PLANNED", "to": "RUNNING"})
    metrics.observe_outbox_event("task.state_changed", {"from": "RUNNING", "to": "RETRY_WAIT"})
    # BLOCKED → BLOCKED (смена причины блокировки) — не переход FSM, не считается.
    metrics.observe_outbox_event("task.state_changed", {"from": "BLOCKED", "to": "BLOCKED"})
    metrics.observe_outbox_event(
        "gate.completed", {"gate_type": "UNIT", "status": "FAIL", "required": True, "duration_ms": 1500}
    )
    metrics.observe_outbox_event("agent.message", {"message_type": "FIX_REQUEST", "summary": "secret text"})
    metrics.observe_outbox_event("approval.decided", {"action_type": "MERGE", "status": "APPROVED", "wait_seconds": 42})
    assert sample("aicd_runs_finished_total", {"final_status": "COMPLETED"}) == before["run"] + 1
    assert sample("aicd_run_transitions_total", {"to_status": "RUNNING"}) == before["trans"] + 1
    assert sample("aicd_task_transitions_total", {"to_status": "RETRY_WAIT"}) == before["retry"] + 1
    assert sample("aicd_task_transitions_total", {"to_status": "BLOCKED"}) == before["blocked"]
    assert (
        sample("aicd_gate_executions_total", {"gate_type": "UNIT", "status": "FAIL", "required": "true"})
        == before["gate"] + 1
    )
    assert sample("aicd_agent_messages_total", {"message_type": "FIX_REQUEST"}) == before["acp"] + 1
    assert (
        sample("aicd_approvals_decided_total", {"action_type": "MERGE", "status": "APPROVED"}) == before["decided"] + 1
    )
    assert sample("aicd_approval_wait_seconds_count", {"action_type": "MERGE"}) == before["wait"] + 1
    assert sample("aicd_approval_wait_seconds_sum", {"action_type": "MERGE"}) == before["wait_sum"] + 42
    assert sample("aicd_outbox_events_published_total", {"event_type": "gate.completed"}) == before["events"] + 1


def test_unknown_values_do_not_create_high_cardinality_series() -> None:
    metrics.observe_outbox_event("run.completed", {"final_status": str(uuid7())})
    exposed = metrics.render()[0].decode()
    assert 'final_status="other"' in exposed
    for line in exposed.splitlines():
        assert "@" not in line or line.startswith("#")


def test_context_pack_metrics_use_snapshot_uuid7_age() -> None:
    count = sample("aicd_index_snapshot_age_seconds_count")
    stale = sample("aicd_context_pack_fragments_total", {"stale": "true"})
    fresh = sample("aicd_context_pack_fragments_total", {"stale": "false"})
    metrics.observe_context_pack(size_bytes=2048, fragments=5, stale=2, snapshot_id=uuid7())
    metrics.observe_context_pack(size_bytes=10, fragments=1, stale=0, snapshot_id=None)
    assert sample("aicd_index_snapshot_age_seconds_count") == count + 1
    assert sample("aicd_context_pack_fragments_total", {"stale": "true"}) == stale + 2
    assert sample("aicd_context_pack_fragments_total", {"stale": "false"}) == fresh + 4


@pytest.mark.parametrize(
    ("token", "environment", "header", "expected"),
    [
        ("t0ken", "production", "Bearer t0ken", True),
        ("t0ken", "production", "bearer t0ken", True),
        ("t0ken", "production", "Bearer wrong", False),
        ("t0ken", "production", None, False),
        ("t0ken", "production", "Basic dDBrZW4=", False),
        (None, "production", "Bearer anything", None),  # production без токена — endpoint выключен
        (None, "development", None, True),
    ],
)
def test_metrics_access_policy(token: str | None, environment: str, header: str | None, expected: bool | None) -> None:
    assert metrics.metrics_allowed(token=token, environment=environment, authorization=header) is expected


def _get(url: str, token: str | None = None) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})  # noqa: S310 — http://127.0.0.1
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_worker_metrics_server_requires_token() -> None:
    server = metrics.start_worker_server(host="127.0.0.1", port=0, token="w0rker", environment="production")
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert _get(f"{base}/metrics")[0] == 401
        assert _get(f"{base}/metrics", "nope")[0] == 401
        status, body = _get(f"{base}/metrics", "w0rker")
        assert status == 200 and "aicd_outbox_lag_seconds_bucket" in body
        assert _get(f"{base}/other", "w0rker")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


async def _api_get(settings: Settings, headers: dict[str, str] | None = None) -> httpx.Response:
    app = create_app(settings)
    app.state.aicd = types.SimpleNamespace(settings=settings)  # /metrics не использует БД/Redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        return await client.get("/metrics", headers=headers or {})


async def test_api_metrics_endpoint_is_protected_and_outside_contract(tmp_path: object) -> None:
    prod = Settings(environment="production", metrics_token=SecretStr("api-t0ken"), log_level="WARNING")
    assert (await _api_get(prod)).status_code == 401
    assert (await _api_get(prod, {"Authorization": "Bearer bad"})).status_code == 401
    ok = await _api_get(prod, {"Authorization": "Bearer api-t0ken"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")
    assert "aicd_http_request_duration_seconds" in ok.text
    no_token = Settings(environment="production", log_level="WARNING")
    assert (await _api_get(no_token, {"Authorization": "Bearer api-t0ken"})).status_code == 404
    spec = create_app(Settings(environment="test", log_level="WARNING")).openapi()
    assert not any("metrics" in path for path in spec["paths"])


async def test_http_metrics_use_route_template_not_raw_path() -> None:
    settings = Settings(environment="development", log_level="WARNING")
    app = create_app(settings)
    app.state.aicd = types.SimpleNamespace(settings=settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        await client.get("/metrics")
        body = (await client.get("/metrics")).text
    assert 'route="/metrics"' in body
    raw_id = str(uuid7())
    assert raw_id not in body
