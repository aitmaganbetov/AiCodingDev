"""Живость воркеров по heartbeat: пороги, выбор самого свежего процесса вида, ожидаемые виды, настройки."""

from __future__ import annotations

import uuid

import pytest

from aicd_api.routers.system import classify_kinds
from aicd_platform.config import Settings
from aicd_platform.worker_hosts import HostState, _interval, freshest_by_kind, liveness


def _host(kind: str, age: float, *, interval: float | None = 15.0, status: str = "ONLINE") -> HostState:
    return HostState(uuid.uuid4(), f"h-{uuid.uuid4().hex[:6]}", kind, status, None, age, interval, None)


@pytest.mark.parametrize(
    ("age", "interval", "expected"),
    [
        (0, 15, "ok"),
        (45, 15, "ok"),
        (45.1, 15, "degraded"),
        (150, 15, "degraded"),
        (150.1, 15, "down"),
        (10, None, "unknown"),
        (None, 15, "unknown"),
        (10, 0, "unknown"),
    ],
)
def test_liveness_thresholds(age: float | None, interval: float | None, expected: str) -> None:
    assert liveness(age, interval) == expected


def test_offline_host_is_not_counted_as_failed() -> None:
    offline = _host("exec", 10_000, status="OFFLINE")
    assert offline.liveness == "unknown"
    fresh = _host("exec", 5)
    assert freshest_by_kind([offline, _host("exec", 400), fresh]) == {"exec": fresh}
    assert freshest_by_kind([offline]) == {}


def test_classify_kinds_by_freshest_process_and_expectations() -> None:
    hosts = [
        _host("exec", 400),  # упавший прежний процесс
        _host("exec", 3),  # работающий — вид жив
        _host("outbox", 100),
        _host("index", 900),
        _host("orchestrator", 1, status="OFFLINE"),
        _host("verify", 1, interval=None),  # без heartbeat (строка до появления heartbeat)
    ]
    kinds = {k["kind"]: k for k in classify_kinds(hosts, expected=["exec", "outbox", "orchestrator", "api"])}
    assert kinds["exec"]["status"] == "ok" and kinds["exec"]["active_hosts"] == 2
    assert kinds["exec"]["freshest_heartbeat_age_seconds"] == 3
    assert kinds["outbox"]["status"] == "degraded"
    assert kinds["index"]["status"] == "down" and kinds["index"]["expected"] is False
    assert kinds["orchestrator"]["status"] == "down", "ожидаемый вид без работающих процессов"
    assert kinds["api"] == {
        "kind": "api", "status": "down", "expected": True, "hosts": 0, "active_hosts": 0,
        "freshest_heartbeat_age_seconds": None, "heartbeat_interval_seconds": None,
    }  # fmt: skip
    assert kinds["verify"]["status"] == "unknown"
    unexpected = {k["kind"]: k for k in classify_kinds([_host("orchestrator", 1, status="OFFLINE")], expected=[])}
    assert unexpected["orchestrator"]["status"] == "unknown", "ожидания не заданы — отсутствие не отказ"


def test_interval_label_parsing() -> None:
    assert _interval({"heartbeat_interval_seconds": 15}) == 15.0
    assert _interval('{"heartbeat_interval_seconds": 2.5}') == 2.5
    for bad in (
        {},
        {"heartbeat_interval_seconds": True},
        {"heartbeat_interval_seconds": "15"},
        {"heartbeat_interval_seconds": -1},
        None,
    ):
        assert _interval(bad) is None


def test_settings_parse_expected_kinds_and_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICD_EXPECTED_WORKER_KINDS", "outbox, exec,index")
    monkeypatch.setenv("AICD_WORKER_HEARTBEAT_SECONDS", "5")
    s = Settings(environment="test")
    assert s.expected_worker_kinds == ["outbox", "exec", "index"] and s.worker_heartbeat_seconds == 5.0
    assert Settings(environment="test", expected_worker_kinds=[]).expected_worker_kinds == []
    monkeypatch.setenv("AICD_EXPECTED_WORKER_KINDS", "bogus")
    with pytest.raises(ValueError, match="expected_worker_kinds"):
        Settings(environment="test")
