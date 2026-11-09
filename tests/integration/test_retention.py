"""P3-18: retention/архивирование на засеянной БД под ролью aicd_maintenance (DATABASE_DESIGN §19.6, §20)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.config import Settings
from aicd_platform.db import create_engine
from aicd_platform.ids import uuid7
from aicd_platform.retention import (
    ArchiveIntegrityError,
    FileSystemArchiveSink,
    RetentionPeriods,
    S3ArchiveSink,
    build_archive_sink,
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
S3_AUDIT_MONTH = date(2002, 5, 1)


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


def _artifact_row(
    admin: psycopg.Connection, store: FileSystemArtifactStore, ws: uuid.UUID, *, kind: str = "report",
    retention_class: str = "standard", age_days: int, run: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:  # fmt: skip
    art = uuid7()
    stored = store.put(ws, art, io.BytesIO(f"{kind}-{art}".encode()))
    admin.execute(
        "INSERT INTO artifacts (id, workspace_id, run_id, kind, storage_ref, sha256, size_bytes, mime_type,"
        " is_sensitive, retention_class, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'text/plain', %s, %s,"
        " now() - make_interval(days => %s))",
        (art, ws, run, kind, stored.storage_ref, stored.sha256, stored.size_bytes, kind == "cli_raw_log",
         retention_class, age_days),
    )  # fmt: skip
    return art, stored.storage_ref


def _snapshot(admin: psycopg.Connection, ws: uuid.UUID, repo: uuid.UUID, status: str, age_days: int) -> uuid.UUID:
    sid = uuid7()
    admin.execute(
        "INSERT INTO code_index_snapshots (id, workspace_id, repository_id, commit_sha, index_version, status,"
        " file_count, symbol_count, created_at, completed_at) VALUES (%s, %s, %s, %s, 1, %s, 1, 0,"
        " now() - make_interval(days => %s), now() - make_interval(days => %s))",
        (sid, ws, repo, secrets.token_hex(20), status, age_days, age_days),
    )
    return sid


@pytest.mark.db
async def test_retention_webhooks_snapshots_and_artifacts(
    database: testdb.TestDatabase, settings: Settings, maintenance_engine: AsyncEngine, tmp_path: Path
) -> None:
    """P3-18 (Phase 9), OQ-41 (a), OQ-47 (7): доставки webhook 30 дней, снимки индекса (5 последних READY,
    FAILED 7 дней, используемые сохраняются), SUPERSEDED-снимки документации 90 дней, артефакты по классу."""
    store = FileSystemArtifactStore(tmp_path / "artifacts")
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        ws, repo = tenant.workspace_id, tenant.repository_id
        # --- webhook-доставки
        deliveries = {}
        for key, status, age in (("old_processed", "PROCESSED", 40), ("old_received", "RECEIVED", 40),
                                 ("fresh", "PROCESSED", 1)):  # fmt: skip
            deliveries[key] = uuid7()
            admin.execute(
                "INSERT INTO github_webhook_deliveries (id, delivery_guid, event, signature_valid, payload_sha256,"
                " status, received_at, processed_at) VALUES (%s, %s, 'ping', true, %s, %s,"
                " now() - make_interval(days => %s), CASE WHEN %s = 'RECEIVED' THEN NULL ELSE now() END)",
                (deliveries[key], uuid.uuid4(), "0" * 64, status, age, status),
            )
        # --- снимки индекса: 7 READY (старые → новые), FAILED старый/свежий
        ready = [_snapshot(admin, ws, repo, "READY", 70 - i) for i in range(7)]
        failed_old = _snapshot(admin, ws, repo, "FAILED", 10)
        failed_fresh = _snapshot(admin, ws, repo, "FAILED", 1)
        admin.execute(
            "INSERT INTO code_files (id, workspace_id, snapshot_id, path, blob_sha, content_hash, size_bytes)"
            " VALUES (%s, %s, %s, 'a.py', %s, %s, 1)",
            (uuid7(), ws, ready[0], "a" * 40, "b" * 64),
        )
        # Завершённое инкрементальное задание на базе ready[0] удаляется вместе со снимком; ready[1] — база
        # ожидающего задания, сохраняется.
        done_job = uuid7()
        admin.execute(
            "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha, kind, base_snapshot_id)"
            " VALUES (%s, %s, %s, %s, 'incremental', %s)",
            (done_job, ws, repo, "c" * 40, ready[0]),
        )
        admin.execute("UPDATE indexing_jobs SET status = 'RUNNING', started_at = now() WHERE id = %s", (done_job,))
        admin.execute(
            "UPDATE indexing_jobs SET status = 'SUCCEEDED', finished_at = now(), snapshot_id = %s WHERE id = %s",
            (ready[2], done_job),
        )
        admin.execute(
            "INSERT INTO indexing_jobs (id, workspace_id, repository_id, commit_sha, kind, status, base_snapshot_id)"
            " VALUES (%s, %s, %s, %s, 'incremental', 'QUEUED', %s)",
            (uuid7(), ws, repo, "d" * 40, ready[1]),
        )
        # --- снимки документации: SUPERSEDED старый (свободный и со ссылкой из артефакта-хранилища)
        source = uuid7()
        admin.execute(
            "INSERT INTO documentation_sources (id, workspace_id, source_type, uri_or_file_ref, created_by)"
            " VALUES (%s, %s, 'url', 'https://docs.example.test/x', %s)",
            (source, ws, tenant.owner_id),
        )
        doc_art, doc_ref = _artifact_row(admin, store, ws, kind="documentation", age_days=400)
        ready_art, _ = _artifact_row(admin, store, ws, kind="documentation", age_days=400)
        docs = {}
        for key, status, ref in (("old_free", "SUPERSEDED", None), ("old_with_body", "SUPERSEDED", doc_art),
                                 ("ready", "READY", ready_art)):  # fmt: skip
            docs[key] = uuid7()
            admin.execute(
                "INSERT INTO documentation_snapshots (id, workspace_id, source_id, content_hash, status, storage_ref,"
                " created_at, fetched_at) VALUES (%s, %s, %s, %s, %s, %s, now() - interval '120 days',"
                " now() - interval '120 days')",
                (docs[key], ws, source, secrets.token_hex(32), status, ref),
            )
        # --- артефакты
        arts = {
            "ephemeral_old": _artifact_row(admin, store, ws, retention_class="ephemeral", age_days=8),
            "ephemeral_fresh": _artifact_row(admin, store, ws, retention_class="ephemeral", age_days=2),
            "raw_log_old": _artifact_row(admin, store, ws, kind="cli_raw_log", age_days=31),
            "standard_old": _artifact_row(admin, store, ws, age_days=181),
            "standard_fresh": _artifact_row(admin, store, ws, age_days=100),
            "evidence_old": _artifact_row(admin, store, ws, retention_class="evidence", age_days=900),
        }
        active_run = new_run(admin, tenant)
        arts["active_run_old"] = _artifact_row(admin, store, ws, age_days=400, run=active_run)
        referenced, referenced_ref = _artifact_row(admin, store, ws, kind="checkpoint_state", age_days=400)
        admin.execute(
            "INSERT INTO checkpoints (id, workspace_id, run_id, reason, state_version, plan_version, state_ref)"
            " VALUES (%s, %s, %s, 'periodic', 1, 1, %s)",
            (uuid7(), ws, active_run, referenced),
        )

    job_settings = settings.model_copy(update={"retention_archive_root": tmp_path / "archive"})
    report = await run_retention(maintenance_engine, job_settings, artifacts=store, reason="phase9 retention")

    with connect_admin(database) as admin:
        left = {r[0] for r in admin.execute("SELECT id FROM github_webhook_deliveries WHERE id = ANY(%s)",
                                            (list(deliveries.values()),)).fetchall()}  # fmt: skip
        assert left == {deliveries["old_received"], deliveries["fresh"]}, "старые обработанные доставки удалены"
        assert any("необработанных доставок" in w for w in report.warnings)

        snaps = {r[0] for r in admin.execute("SELECT id FROM code_index_snapshots WHERE repository_id = %s",
                                             (repo,)).fetchall()}  # fmt: skip
        assert snaps == {*ready[1:], failed_fresh}, "5 последних READY + база ожидающего задания + свежий FAILED"
        assert failed_old not in snaps and ready[0] not in snaps
        assert admin.execute("SELECT count(*) FROM code_files WHERE snapshot_id = %s", (ready[0],)).fetchone() == (0,)
        assert admin.execute("SELECT count(*) FROM indexing_jobs WHERE id = %s", (done_job,)).fetchone() == (0,)

        doc_left = {r[0] for r in admin.execute("SELECT id FROM documentation_snapshots WHERE source_id = %s",
                                                (source,)).fetchall()}  # fmt: skip
        assert doc_left == {docs["old_with_body"], docs["ready"]} or doc_left == {docs["ready"]}
        assert docs["old_free"] not in doc_left

        art_left = {r[0] for r in admin.execute("SELECT id FROM artifacts WHERE workspace_id = %s", (ws,)).fetchall()}
        gone = {"ephemeral_old", "raw_log_old", "standard_old"}
        for key, (art_id, art_ref) in arts.items():
            assert (art_id in art_left) is (key not in gone), key
            assert store.exists(art_ref) is (key not in gone), f"объект {key}"
        assert referenced in art_left and store.exists(referenced_ref), "артефакт со ссылкой (FK RESTRICT) сохранён"
        if docs["old_with_body"] not in doc_left:  # снимок удалён ⇒ его артефакт (400 дней, standard) тоже
            assert doc_art not in art_left and not store.exists(doc_ref)
        audited = {
            r[0]
            for r in admin.execute(
                "SELECT metadata->>'table' FROM audit_events WHERE action = 'maintenance.rows_deleted'"
                " AND resource_id = %s",
                (report.session_id,),
            ).fetchall()
        }
    assert {"github_webhook_deliveries", "code_index_snapshots", "documentation_snapshots", "artifacts"} <= audited
    assert report.artifact_objects_deleted >= len(gone)
    assert "usage_metrics" not in report.skipped_tables and "code_index_snapshots" not in report.skipped_tables


def test_archive_backend_follows_artifact_backend(tmp_path: Path) -> None:
    fs = Settings(environment="test", artifact_fs_root=tmp_path)
    assert fs.archive_backend() == "fs"
    assert isinstance(build_archive_sink(fs), FileSystemArchiveSink)
    s3_without_keys = Settings(environment="test", artifact_fs_root=tmp_path, retention_archive_backend="s3")
    with pytest.raises(RuntimeError, match="S3"):
        build_archive_sink(s3_without_keys)
    assert Settings(environment="test", artifact_backend="s3", retention_archive_backend="fs").archive_backend() == "fs"


@pytest.mark.db
@pytest.mark.s3
async def test_retention_s3_archive_and_integrity_mismatch(
    database: testdb.TestDatabase, settings: Settings, maintenance_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Архив партиции в S3 (§20.2): сверка sha256 до DETACH+DROP; несовпадение ⇒ партиция сохраняется."""
    endpoint = os.environ.get("AICD_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("AICD_TEST_S3_ENDPOINT не задан")
    (tmp_path / "s3-access").write_text(os.environ["AICD_TEST_S3_ACCESS_KEY"])
    (tmp_path / "s3-secret").write_text(os.environ["AICD_TEST_S3_SECRET_KEY"])
    bucket = f"aicd-archive-{secrets.token_hex(3)}"
    job_settings = settings.model_copy(
        update={
            "retention_archive_backend": "s3",
            "artifact_s3_endpoint": endpoint,
            "artifact_s3_access_key_file": str(tmp_path / "s3-access"),
            "artifact_s3_secret_key_file": str(tmp_path / "s3-secret"),
            "retention_archive_s3_bucket": bucket,
            "retention_batch_pause_seconds": 0,
        }
    )
    sink = build_archive_sink(job_settings)
    assert isinstance(sink, S3ArchiveSink)
    sink.ensure_bucket()
    client = sink.client
    partition = f"audit_events_y{S3_AUDIT_MONTH:%Y}m{S3_AUDIT_MONTH:%m}"
    object_key = f"archive/global/audit_events/{S3_AUDIT_MONTH:%Y-%m}.ndjson.gz"
    with connect_admin(database) as admin:
        admin.execute("SELECT maint_ensure_monthly_partitions('audit_events', %s, 0)", (S3_AUDIT_MONTH,))
        for i in range(2):
            admin.execute(
                "INSERT INTO audit_events (id, actor_type, action, resource_type, created_at)"
                " VALUES (%s, 'system', 'test.s3_archived_event', 'x', %s)",
                (uuid7(), _ts(S3_AUDIT_MONTH, 10 + i)),
            )

    real_put = client.put_object

    def tampered_body(**kwargs: object) -> object:
        # Тело искажено в пути, ChecksumSHA256 — от экспорта: хранилище обязано отвергнуть загрузку.
        if kwargs["Key"] == object_key:
            kwargs = {**kwargs, "Body": b"corrupted archive", "ContentLength": len(b"corrupted archive")}
        return real_put(**kwargs)

    def overwritten_after_upload(**kwargs: object) -> object:
        # Загрузка прошла, но объект подменён (сбой носителя/конкурентная запись): ловит только чтение обратно.
        result = real_put(**kwargs)
        if kwargs["Key"] == object_key:
            real_put(Bucket=bucket, Key=object_key, Body=b"overwritten")
        return result

    try:
        for corrupt in (tampered_body, overwritten_after_upload):
            client.put_object = corrupt
            failed = await run_retention(maintenance_engine, job_settings, sink=sink, reason=f"s3 {corrupt.__name__}")
            assert failed.outcome == "success", "сбой архива одной партиции не роняет проход"
            assert failed.partitions_skipped.get(partition) == "archive_integrity_failed", corrupt.__name__
            assert partition not in failed.partitions_dropped
            assert any(partition in w and "целостности" in w for w in failed.warnings)
            with connect_admin(database) as admin:
                assert _exists(admin, partition), "партиция с неподтверждённым архивом сохранена"
                assert admin.execute(
                    "SELECT count(*) FROM audit_events WHERE action = 'test.s3_archived_event'"
                ).fetchone() == (2,)
                (meta,) = admin.execute(
                    "SELECT metadata FROM audit_events WHERE action = 'maintenance.archive_failed'"
                    " AND resource_id = %s",
                    (failed.session_id,),
                ).fetchone() or (None,)
            assert meta is not None and meta["partition"] == partition and object_key in meta["error"]
        with pytest.raises(ArchiveIntegrityError):  # файл экспорта не совпадает с sha256 — до загрузки
            src = tmp_path / "x.ndjson.gz"
            src.write_bytes(b"x")
            sink.put_file("global/audit_events/1999-01.ndjson.gz", src, hashlib.sha256(b"y").hexdigest())
    finally:
        client.put_object = real_put

    report = await run_retention(maintenance_engine, job_settings, sink=sink, reason="s3 archive")
    assert report.outcome == "success"
    assert partition in report.partitions_dropped
    (archived,) = [a for a in report.archives if a.partition == partition]
    assert archived.storage_ref == f"s3://{bucket}/{object_key}" and archived.rows == 2
    obj = client.get_object(Bucket=bucket, Key=object_key)
    body = obj["Body"].read()
    assert hashlib.sha256(body).hexdigest() == archived.sha256 == obj["Metadata"]["sha256"]
    docs = [json.loads(line) for line in gzip.decompress(body).splitlines()]
    assert [d["action"] for d in docs] == ["test.s3_archived_event"] * 2
    with connect_admin(database) as admin:
        assert not _exists(admin, partition)
        (meta,) = admin.execute(
            "SELECT metadata FROM audit_events WHERE action = 'maintenance.partition_dropped' AND resource_id = %s"
            " AND metadata->>'partition' = %s",
            (report.session_id, partition),
        ).fetchone() or (None,)
    assert meta is not None and meta["archive_ref"] == archived.storage_ref and meta["sha256"] == archived.sha256


@pytest.mark.db
async def test_retention_deletes_notifications_older_than_90_days(
    database: testdb.TestDatabase, settings: Settings, maintenance_engine: AsyncEngine, tmp_path: Path
) -> None:
    """UI redesign B8 (миграция 0020): уведомления хранятся 90 дней от создания — прочитанные и нет; удаление
    пакетами под aicd_maintenance с аудитом `maintenance.rows_deleted`."""
    assert RetentionPeriods.from_settings(settings).notifications_days == 90
    with connect_admin(database) as admin:
        tenant = seed_tenant(admin)
        rows = {}
        for key, age, read in (("old_unread", 91, False), ("old_read", 120, True), ("fresh", 89, False)):
            rows[key] = uuid7()
            admin.execute(
                "INSERT INTO notifications (id, workspace_id, user_id, kind, severity, title, resource_type,"
                " resource_id, source_event_id, created_at, read_at) VALUES (%s, %s, %s, 'run.completed', 'success',"
                " 't', 'run', %s, %s, now() - make_interval(days => %s),"
                " CASE WHEN %s THEN now() - make_interval(days => %s) + interval '1 hour' END)",
                (rows[key], tenant.workspace_id, tenant.owner_id, uuid7(), uuid7(), age, read, age),
            )
    job_settings = settings.model_copy(update={"retention_archive_root": tmp_path / "archive"})
    report = await run_retention(maintenance_engine, job_settings, reason="notifications retention")
    assert "notifications" not in report.skipped_tables and report.rows_deleted["notifications"] >= 2
    with connect_admin(database) as admin:
        left = {
            r[0]
            for r in admin.execute(
                "SELECT id FROM notifications WHERE workspace_id = %s", (tenant.workspace_id,)
            ).fetchall()
        }
        assert left == {rows["fresh"]}
        audited = admin.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'maintenance.rows_deleted' AND resource_id = %s"
            " AND metadata->>'table' = 'notifications'",
            (report.session_id,),
        ).fetchone()
        assert audited is not None and audited[0] >= 1
