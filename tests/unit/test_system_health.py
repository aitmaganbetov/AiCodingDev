"""`GET /system/health` (UI redesign B2): правила итогового статуса, разбор файла статуса бэкапа, имена очередей."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aicd_api.routers import system
from aicd_api.routers.system import Component, Status, backup_entries, overall
from aicd_worker import broker, dlq

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _components(**statuses: Status) -> list[Component]:
    return [Component(name, status, NOW) for name, status in statuses.items()]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({"api": "ok", "database": "ok", "redis": "ok"}, "ok"),
        ({"api": "ok", "database": "ok", "backup": "unknown", "workers": "unknown"}, "ok"),
        ({"api": "ok", "database": "ok", "redis": "down"}, "degraded"),
        ({"api": "ok", "database": "ok", "queues": "degraded"}, "degraded"),
        ({"api": "ok", "database": "down", "redis": "ok"}, "down"),
        ({"api": "ok", "object_storage": "down"}, "down"),
        ({"api": "ok", "database": "degraded", "outbox": "down"}, "degraded"),
    ],
)
def test_overall_status(statuses: dict[str, Status], expected: Status) -> None:
    assert overall(_components(**statuses)) == expected


def test_backup_entries_known_components_and_staleness() -> None:
    ts = NOW.timestamp()
    status = {
        "components": {
            "postgres_base": {"last_status": "success", "last_success_unixtime": ts - 27 * 3600},
            "postgres_wal": {"last_status": "success", "last_success_unixtime": ts - 60},
            "postgres_dump": {"last_status": "failure"},
            "secrets": {"last_status": "weird"},
            "unknown": {"last_status": "success"},
            "artifacts": "not-a-mapping",
        }
    }
    entries = {e["component"]: e for e in backup_entries(status, NOW)}
    assert list(entries) == ["postgres_base", "postgres_wal", "postgres_dump", "secrets"]
    assert entries["postgres_base"]["stale"] is True, "базовый бэкап старше 26 ч"
    assert entries["postgres_wal"]["stale"] is False
    assert entries["postgres_wal"]["last_success_at"] == NOW - timedelta(seconds=60)
    assert entries["postgres_dump"]["last_success_at"] is None and entries["postgres_dump"]["stale"] is False
    assert entries["secrets"]["last_status"] == "unknown", "неизвестное значение не выдаётся за успех"
    assert backup_entries(None, NOW) == [] and backup_entries({"components": []}, NOW) == []


def test_queue_names_match_worker_broker() -> None:
    """API читает те же ключи Redis, что и воркеры (`aicd_worker.dlq`, `aicd_worker.observability`)."""
    assert system.QUEUES == dlq.QUEUES
    assert system.DRAMATIQ_NAMESPACE == dlq.NAMESPACE
    assert {
        broker.ORCHESTRATOR_QUEUE, broker.EXEC_QUEUE, broker.DOCS_QUEUE, broker.GITHUB_QUEUE, broker.NOTIFICATIONS_QUEUE
    } == set(system.QUEUES)  # fmt: skip
