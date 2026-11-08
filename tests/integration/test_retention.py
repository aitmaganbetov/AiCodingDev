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


def _seed_run_chat(admin: psycopg.Connection, ws: uuid.UUID, owner: uuid.UUID, run: uuid.UUID) -> dict[str, uuid.UUID]:
    """Пакет run для архива: m1 ← m2 (reply_to, на m2 ссылается agent_decisions), m3 (+доставка, +чат),
    обычное сообщение чата и сообщение поручения (user_directives)."""
    ids = {k: uuid7() for k in ("m1", "m2", "m3", "thread", "plain_chat", "acp_chat", "directive_chat")}
    for key, reply in (("m1", None), ("m2", ids["m1"]), ("m3", None)):
        admin.execute(
            "INSERT INTO agent_messages (id, workspace_id, run_id, sender_role, sender_provider, recipient_role,"
            " message_type, correlation_id, reply_to_id, payload)"
            " VALUES (%s, %s, %s, 'developer', 'codex', 'orchestrator', 'HANDOFF', %s, %s, '{\"k\": 1}')",
            (ids[key], ws, run, ids["m1"], reply),
        )
    admin.execute(
        "INSERT INTO message_deliveries (workspace_id, message_id, consumer_id) VALUES (%s, %s, 'chat.projector')",
        (ws, ids["m3"]),
    )
    admin.execute(
        "INSERT INTO agent_decisions (id, workspace_id, run_id, decision_type, rationale, acp_message_id,"
        " decided_by_type) VALUES (%s, %s, %s, 'plan', 'r', %s, 'system')",
        (uuid7(), ws, run, ids["m2"]),
    )
    admin.execute(
        "INSERT INTO chat_threads (id, workspace_id, run_id, scope) VALUES (%s, %s, %s, 'all_agents')",
        (ids["thread"], ws, run),
    )
    for key, kind, sender, acp in (
        ("plain_chat", "text", "system", None),
        ("acp_chat", "acp_message", "system", ids["m3"]),
        ("directive_chat", "directive", "user", None),
    ):
        admin.execute(
            "INSERT INTO chat_messages (id, workspace_id, run_id, thread_id, sender_type, sender_id, body,"
            " message_kind, acp_message_id) VALUES (%s, %s, %s, %s, %s, %s, 'секретный план', %s, %s)",
            (ids[key], ws, run, ids["thread"], sender, owner if sender == "user" else None, kind, acp),
        )
    admin.execute(
        "INSERT INTO user_directives (id, workspace_id, run_id, chat_message_id, target_type, directive_type,"
        " submitted_by) VALUES (%s, %s, %s, %s, 'run', 'ASK', %s)",
        (uuid7(), ws, run, ids["directive_chat"], owner),
    )
    return ids


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
        # Старая партиция ленты активности (архив → drop) и пакет ACP/чата терминального run (архив run).
        admin.execute("SELECT maint_ensure_monthly_partitions('agent_activity_events', %s, 0)", (AUDIT_MONTH,))
        for i in range(2):
            admin.execute(
                "INSERT INTO agent_activity_events (id, workspace_id, run_id, event_type, occurred_at)"
                " VALUES (%s, %s, %s, 'task.state_changed', %s)",
                (uuid7(), tenant.workspace_id, old_run, _ts(AUDIT_MONTH, 10 + i)),
            )
        acp = _seed_run_chat(admin, tenant.workspace_id, tenant.owner_id, old_run)
        active_acp = _seed_run_chat(admin, tenant.workspace_id, tenant.owner_id, active_run)
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
            (policy_id, json.dumps({"run_event_log_days": 45, "agent_messages_days": 45, "bogus_key": 1})),
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
    assert not {"agent_activity_events", "agent_messages", "chat_messages"} & set(report.skipped_tables)
    assert report.partitions_ahead["agent_activity_events"] >= 2
    assert report.rows_deleted["run_event_log"] >= 3
    assert report.rows_deleted["processed_events"] >= 1

    # Архив audit-партиции: NDJSON.gz с тремя событиями, sha256 совпадает с записанным в аудит.
    (archived,) = [a for a in report.archives if a.partition == audit_part]
    assert archived.storage_ref == "file://archive/global/audit_events/2002-03.ndjson.gz" and archived.rows == 3
    path = tmp_path / "archive" / "global" / "audit_events" / "2002-03.ndjson.gz"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == archived.sha256
    docs = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    assert [d["action"] for d in docs] == ["test.old_event"] * 3

    # Партиция активности архивирована (2 события) и удалена.
    activity_part = f"agent_activity_events_y{AUDIT_MONTH:%Y}m{AUDIT_MONTH:%m}"
    (activity_archive,) = [a for a in report.archives if a.partition == activity_part]
    assert activity_archive.rows == 2 and activity_part in report.partitions_dropped

    # Архив run: один NDJSON.gz пакета ACP/чата/поручений, строка artifacts(kind='archive').
    (run_archive,) = [r for r in report.runs_archived if r.run_id == old_run]
    assert active_run not in {r.run_id for r in report.runs_archived}
    assert run_archive.storage_ref == f"file://archive/{tenant.workspace_id}/runs/{old_run}.ndjson.gz"
    run_path = tmp_path / "archive" / str(tenant.workspace_id) / "runs" / f"{old_run}.ndjson.gz"
    assert hashlib.sha256(run_path.read_bytes()).hexdigest() == run_archive.sha256
    lines = [json.loads(line) for line in gzip.decompress(run_path.read_bytes()).splitlines()]
    archived_tables = [d["table"] for d in lines]
    assert archived_tables.count("agent_messages") == 3 and archived_tables.count("chat_messages") == 3
    assert archived_tables.count("message_deliveries") == 1 and archived_tables.count("user_directives") == 1
    assert all("search_vector" not in d["row"] for d in lines if d["table"] == "chat_messages")
    assert report.rows_deleted["agent_messages"] >= 1 and report.rows_deleted["chat_messages"] >= 2

    with connect_admin(database) as admin:
        assert not _exists(admin, activity_part)
        left_messages = {
            r[0] for r in admin.execute("SELECT id FROM agent_messages WHERE run_id = %s", (old_run,)).fetchall()
        }
        # m2 нужен agent_decisions, m1 — его цепочке reply_to; m3 удалён вместе с доставкой.
        assert left_messages == {acp["m1"], acp["m2"]}
        assert admin.execute(
            "SELECT count(*) FROM message_deliveries WHERE message_id = %s", (acp["m3"],)
        ).fetchone() == (0,)
        chat_left = admin.execute("SELECT id, body FROM chat_messages WHERE run_id = %s", (old_run,)).fetchall()
        assert chat_left == [(acp["directive_chat"], f"[архив run: {run_archive.storage_ref}]")]
        assert admin.execute("SELECT count(*) FROM chat_threads WHERE run_id = %s", (old_run,)).fetchone() == (1,)
        assert admin.execute(
            "SELECT sha256, retention_class FROM artifacts WHERE run_id = %s AND kind = 'archive'", (old_run,)
        ).fetchone() == (run_archive.sha256, "archive")
        # Активный run не тронут.
        assert admin.execute("SELECT count(*) FROM agent_messages WHERE run_id = %s", (active_run,)).fetchone() == (3,)
        assert active_acp["m3"] in {
            r[0] for r in admin.execute("SELECT id FROM agent_messages WHERE run_id = %s", (active_run,))
        }
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
    archived_runs = {m["run_id"]: m for a, m in rows if a == "maintenance.run_archived"}
    assert archived_runs[str(old_run)]["sha256"] == run_archive.sha256
    assert archived_runs[str(old_run)]["chat_bodies_replaced"] == 1
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
    assert second.partitions_dropped == [] and second.runs_archived == [] and first.session_id != second.session_id
    assert isinstance(second.session_id, uuid.UUID)
