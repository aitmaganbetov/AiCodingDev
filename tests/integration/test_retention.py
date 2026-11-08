"""P3-18: retention/архивирование на засеянной БД под ролью aicd_maintenance (DATABASE_DESIGN §19.6, §20)."""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.config import Settings
from aicd_platform.db import create_engine
from aicd_platform.ids import uuid7
from aicd_platform.retention import (
    FileSystemArchiveSink,
    RetentionPeriods,
    partition_cutoff,
    partition_month,
    run_retention,
)
from tests.support import db as testdb
from tests.support.orchestration import connect_admin, new_run, seed_tenant, update

# Месяцы заведомо старше любых сроков хранения и не пересекаются с другими тестами сессии.
AUDIT_MONTH = date(2002, 3, 1)
OUTBOX_PUBLISHED_MONTH = date(2002, 3, 1)
OUTBOX_PENDING_MONTH = date(2002, 4, 1)


def _periods(**overrides: int) -> RetentionPeriods:
    base = RetentionPeriods(30, 90, 365, 400, 2, 30, 30, 30)
    return RetentionPeriods(**{**base.__dict__, **overrides})


def test_policy_overrides_are_validated() -> None:
    merged, warnings = _periods().merged(
        {"schema_version": "1.0", "run_event_log_days": 7, "audit_days": 5, "unknown": 1, "processed_events_days": True}
    )
    assert merged.run_event_log_days == 7
    assert merged.audit_days == 400  # < 30 — отклонено
    assert merged.processed_events_days == 30  # bool — не число дней
    assert len(warnings) == 3


def test_partition_cutoffs() -> None:
    now = datetime(2027, 5, 20, 12, tzinfo=UTC)
    # outbox: 2 полных месяца после месяца партиции → граница — начало марта.
    assert partition_cutoff("outbox_events", _periods(), now) == date(2027, 3, 1)
    assert partition_cutoff("audit_events", _periods(), now) == date(2026, 4, 15)
    # Срок короче месяца не позволяет тронуть текущую партицию.
    assert partition_cutoff("agent_activity_events", _periods(agent_activity_days=1), now) == date(2027, 5, 1)
    assert partition_month("audit_events_y2027m01", "audit_events") == date(2027, 1, 1)
    assert partition_month("audit_events_y2027m13", "audit_events") is None
    assert partition_month("outbox_events_y2027m01", "audit_events") is None


def test_archive_sink_rejects_traversal(tmp_path: Path) -> None:
    sink = FileSystemArchiveSink(tmp_path)
    src = tmp_path / "x"
    src.write_bytes(b"x")
    with pytest.raises(ValueError, match="ключ"):
        sink.put_file("../escape", src, hashlib.sha256(b"x").hexdigest())
    with pytest.raises(OSError, match="sha256"):
        sink.put_file("global/t/2000-01.ndjson.gz", src, "0" * 64)


@pytest.fixture
async def maintenance_engine(database: testdb.TestDatabase) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database.url_for("aicd_maintenance"), pool_size=1)
    yield engine
    await engine.dispose()


def _ts(month: date, day: int = 10) -> datetime:
    return datetime(month.year, month.month, day, tzinfo=UTC)


def _exists(admin: psycopg.Connection, relname: str) -> bool:
    return admin.execute("SELECT to_regclass(%s) IS NOT NULL", (f"aicd.{relname}",)).fetchone() == (True,)


@pytest.mark.db
async def test_retention_job_on_seeded_db(
    database: testdb.TestDatabase, settings: Settings, maintenance_engine: AsyncEngine, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    policy_id = uuid7()
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        old_run, active_run = new_run(admin, tenant), new_run(admin, tenant)
        update(admin, "runs", old_run, status="CANCELLED", finished_at=now - timedelta(days=60))
        for run, count in ((old_run, 3), (active_run, 1)):
            for seq in range(1, count + 1):
                admin.execute(
                    "INSERT INTO run_event_log (workspace_id, run_id, seq, event_id, event_type, payload, occurred_at)"
                    " VALUES (%s, %s, %s, %s, 'run.state_changed', '{}', now())",
                    (tenant.workspace_id, run, seq, uuid7()),
                )
        # Старые партиции: audit (3 события), outbox — опубликованная и с неопубликованным событием.
        admin.execute("SELECT maint_ensure_monthly_partitions('audit_events', %s, 0)", (AUDIT_MONTH,))
        admin.execute("SELECT maint_ensure_monthly_partitions('outbox_events', %s, 1)", (OUTBOX_PUBLISHED_MONTH,))
        for i in range(3):
            admin.execute(
                "INSERT INTO audit_events (id, actor_type, action, resource_type, created_at)"
                " VALUES (%s, 'system', 'test.old_event', 'x', %s)",
                (uuid7(), _ts(AUDIT_MONTH, 10 + i)),
            )
        for month, published in ((OUTBOX_PUBLISHED_MONTH, True), (OUTBOX_PENDING_MONTH, False)):
            admin.execute(
                "INSERT INTO outbox_events (id, workspace_id, aggregate_type, aggregate_id, event_type, payload,"
                " created_at, published_at) VALUES (%s, %s, 'run', %s, 'run.retention_test', '{}', %s, %s)",
                (uuid7(), tenant.workspace_id, uuid7(), _ts(month), _ts(month) if published else None),
            )
        consumer = f"retention.test.{uuid7().hex[-8:]}"
        old_event, fresh_event = uuid7(), uuid7()
        admin.execute(
            "INSERT INTO processed_events (consumer, event_id, processed_at) VALUES (%s, %s, %s), (%s, %s, now())",
            (consumer, old_event, now - timedelta(days=45), consumer, fresh_event),
        )
        # Глобальная политика retention переопределяет срок журнала SSE; неизвестный ключ — предупреждение.
        admin.execute(
            "INSERT INTO policies (id, workspace_id, policy_type, name, version, definition)"
            " VALUES (%s, NULL, 'retention', 'default', 1, %s)",
            (policy_id, json.dumps({"run_event_log_days": 45, "bogus_key": 1})),
        )

    job_settings = settings.model_copy(
        update={
            "retention_archive_root": tmp_path / "archive",
            "retention_batch_size": 2,
            "retention_batch_pause_seconds": 0,
        }
    )
    try:
        report = await run_retention(maintenance_engine, job_settings, reason="test seeded db")
    finally:
        with connect_admin(database) as admin:
            admin.execute("UPDATE policies SET active = false WHERE id = %s", (policy_id,))
            pending = f"outbox_events_y{OUTBOX_PENDING_MONTH:%Y}m{OUTBOX_PENDING_MONTH:%m}"
            if _exists(admin, pending):  # неопубликованное событие 2002 года не должно попасть к dispatcher'у
                admin.execute(f"ALTER TABLE outbox_events DETACH PARTITION {pending}")
                admin.execute(f"DROP TABLE {pending}")

    assert report.outcome == "success"
    assert report.periods is not None and report.periods.run_event_log_days == 45
    assert any("bogus_key" in w for w in report.warnings)
    audit_part = f"audit_events_y{AUDIT_MONTH:%Y}m{AUDIT_MONTH:%m}"
    published_part = f"outbox_events_y{OUTBOX_PUBLISHED_MONTH:%Y}m{OUTBOX_PUBLISHED_MONTH:%m}"
    pending_part = f"outbox_events_y{OUTBOX_PENDING_MONTH:%Y}m{OUTBOX_PENDING_MONTH:%m}"
    assert {audit_part, published_part} <= set(report.partitions_dropped)
    assert report.partitions_skipped.get(pending_part) == "skipped_unpublished"
    assert report.partitions_ahead["audit_events"] >= 2 and report.partitions_ahead["outbox_events"] >= 2
    assert report.outbox_unpublished_stale >= 1
    assert "agent_activity_events" in report.skipped_tables and "agent_messages" in report.skipped_tables
    assert report.rows_deleted["run_event_log"] >= 3
    assert report.rows_deleted["processed_events"] >= 1

    # Архив audit-партиции: NDJSON.gz с тремя событиями, sha256 совпадает с записанным в аудит.
    (archived,) = [a for a in report.archives if a.partition == audit_part]
    assert archived.storage_ref == "file://archive/global/audit_events/2002-03.ndjson.gz" and archived.rows == 3
    path = tmp_path / "archive" / "global" / "audit_events" / "2002-03.ndjson.gz"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == archived.sha256
    docs = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    assert [d["action"] for d in docs] == ["test.old_event"] * 3

    with connect_admin(database) as admin:
        assert not _exists(admin, audit_part) and not _exists(admin, published_part)
        counts: dict[uuid.UUID, int] = dict(
            admin.execute(
                "SELECT run_id, count(*) FROM run_event_log WHERE run_id = ANY(%s) GROUP BY run_id",
                ([old_run, active_run],),
            ).fetchall()
        )
        assert counts == {active_run: 1}
        left = {r[0] for r in admin.execute("SELECT event_id FROM processed_events WHERE consumer = %s", (consumer,))}
        assert left == {fresh_event}
        # Каждое действие обслуживания аудируется (§19.6); архив audit — с sha256.
        rows = admin.execute(
            "SELECT action, metadata FROM audit_events WHERE resource_type = 'maintenance' AND resource_id = %s",
            (report.session_id,),
        ).fetchall()
    actions = [a for a, _ in rows]
    assert actions.count("maintenance.session_started") == 1 and actions.count("maintenance.session_finished") == 1
    dropped = {m["partition"]: m for a, m in rows if a == "maintenance.partition_dropped"}
    assert dropped[audit_part]["sha256"] == archived.sha256
    assert published_part in dropped
    deleted_tables = {m["table"] for a, m in rows if a == "maintenance.rows_deleted"}
    assert {"run_event_log", "processed_events"} <= deleted_tables
    finished = next(m for a, m in rows if a == "maintenance.session_finished")
    assert finished["outcome"] == "success"


@pytest.mark.db
async def test_retention_requires_maintenance_role(settings: Settings, app_engine: AsyncEngine) -> None:
    """Под aicd_app (NOBYPASSRLS) сессия обслуживания не открывается."""
    with pytest.raises(DBAPIError, match=r"permission denied|aicd_maintenance"):
        await run_retention(app_engine, settings, reason="must fail")


@pytest.mark.db
async def test_retention_is_idempotent(
    settings: Settings, maintenance_engine: AsyncEngine, tmp_path: Path, database: testdb.TestDatabase
) -> None:
    job_settings = settings.model_copy(update={"retention_archive_root": tmp_path / "archive"})
    first = await run_retention(maintenance_engine, job_settings, reason="first")
    second = await run_retention(maintenance_engine, job_settings, reason="second")
    assert second.partitions_created == dict.fromkeys(second.partitions_created, 0)
    assert second.partitions_dropped == [] and first.session_id != second.session_id
    assert isinstance(second.session_id, uuid.UUID)
