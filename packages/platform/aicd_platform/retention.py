"""Retention и архивирование (P3-18, NFR-REL-05; DATABASE_DESIGN §19.6, §20).

Job выполняется **только** под ролью `aicd_maintenance` (BYPASSRLS) на отдельном соединении:
  1. `maint_begin` — запись `maintenance.session_started` в audit_events; без неё функции обслуживания и
     удаление из run_event_log отклоняются базой (§19.6);
  2. партиции `audit_events`, `outbox_events`, `agent_activity_events` создаются на N месяцев вперёд
     (`maint_ensure_monthly_partitions`), алерт — если впереди меньше 2;
  3. истёкшие партиции: `audit_events`/`agent_activity_events` — экспорт в NDJSON.gz (архив, sha256 в
     аудите) → `maint_drop_partition` (DETACH + DROP); `outbox_events` — drop, только если 0 неопубликованных;
  4. непартиционированные таблицы — пакетное удаление (`run_event_log` после терминального статуса run,
     `processed_events`, `idempotency_keys`, `user_sessions`, `provider_health`) с паузами;
  5. архив run (§20.2–20.3): терминальный run старше `agent_messages_days` → один NDJSON.gz
     (`chat_threads`, `chat_messages`, `agent_messages`, `message_deliveries`, `user_directives`, `directive_*`)
     → строка `artifacts(kind='archive')` → удаление детальных строк: `chat_messages` (кроме сообщений поручений —
     у них `body` заменяется ссылкой на архив), пустые `chat_threads`, `agent_messages` (+ `message_deliveries`
     каскадом), кроме сообщений, на которые ссылаются сохраняемые строки (`agent_decisions`,
     `directive_acknowledgements`, оставшиеся `chat_messages`, ответы из других run) и их цепочек `reply_to`;
  6. `maint_end` — `maintenance.session_finished` с итогами.
Каждое необратимое действие (drop партиции, удаление пакетов, архив run) аудируется в той же транзакции.

Сроки — из Settings (`retention_*`, значения по умолчанию §20.3); глобальная активная политика
`policies(policy_type='retention', workspace_id IS NULL)` переопределяет их ключами `RetentionPeriods`.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_platform import audit
from aicd_platform.clock import utcnow
from aicd_platform.config import Settings
from aicd_platform.ids import uuid7
from aicd_platform.redaction import redact_value

JOB_NAME = "retention"
TERMINAL_RUN_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")
# Помесячные партиции (§20.1): таблица → колонка ключа партиционирования (она же порядок экспорта).
PARTITIONED: dict[str, str] = {
    "audit_events": "created_at",
    "outbox_events": "created_at",
    "agent_activity_events": "occurred_at",
}
ARCHIVED_BEFORE_DROP = frozenset({"audit_events", "agent_activity_events"})
_PARTITION_RE = re.compile(r"^(?P<table>[a-z_]+)_y(?P<y>\d{4})m(?P<m>0[1-9]|1[0-2])$")
_EXPORT_CHUNK = 5000
OUTBOX_STALE_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class RetentionPeriods:
    """Сроки хранения (§20.3). Ключи совпадают с ключами `definition` политики retention."""

    run_event_log_days: int
    agent_activity_days: int
    agent_messages_days: int
    audit_days: int
    outbox_published_months: int
    processed_events_days: int
    user_sessions_days: int
    provider_health_days: int

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPeriods:
        return cls(
            run_event_log_days=settings.retention_run_event_log_days,
            agent_activity_days=settings.retention_agent_activity_days,
            agent_messages_days=settings.retention_agent_messages_days,
            audit_days=settings.retention_audit_days,
            outbox_published_months=settings.retention_outbox_published_months,
            processed_events_days=settings.retention_processed_events_days,
            user_sessions_days=settings.retention_user_sessions_days,
            provider_health_days=settings.retention_provider_health_days,
        )

    def merged(self, overrides: Mapping[str, Any]) -> tuple[RetentionPeriods, list[str]]:
        """Применяет переопределения политики; некорректные ключи/значения пропускаются с предупреждением."""
        known = {f.name for f in fields(self)}
        values = asdict(self)
        warnings: list[str] = []
        for key, value in overrides.items():
            if key == "schema_version":
                continue
            if key not in known:
                warnings.append(f"retention policy: неизвестный ключ {key!r} пропущен")
                continue
            minimum = 30 if key == "audit_days" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                warnings.append(f"retention policy: {key}={value!r} некорректно (целое ≥ {minimum}), пропущено")
                continue
            values[key] = value
        return RetentionPeriods(**values), warnings


@dataclass(frozen=True)
class ArchivedPartition:
    table: str
    partition: str
    storage_ref: str
    sha256: str
    size_bytes: int
    rows: int


@dataclass(frozen=True)
class ArchivedRun:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    storage_ref: str
    sha256: str
    size_bytes: int
    rows: dict[str, int]


@dataclass
class RetentionReport:
    session_id: uuid.UUID
    started_at: datetime
    periods: RetentionPeriods | None = None
    finished_at: datetime | None = None
    outcome: str = "success"
    partitions_created: dict[str, int] = field(default_factory=dict)
    partitions_ahead: dict[str, int] = field(default_factory=dict)
    partitions_dropped: list[str] = field(default_factory=list)
    partitions_skipped: dict[str, str] = field(default_factory=dict)
    archives: list[ArchivedPartition] = field(default_factory=list)
    runs_archived: list[ArchivedRun] = field(default_factory=list)
    rows_deleted: dict[str, int] = field(default_factory=dict)
    skipped_tables: dict[str, str] = field(default_factory=dict)
    outbox_unpublished_stale: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Итог для audit_events (ключи без «session»/«token» — их маскирует redaction)."""
        return {
            "outcome": self.outcome,
            "partitions_created": sum(self.partitions_created.values()),
            "partitions_dropped": list(self.partitions_dropped),
            "partitions_skipped": [{"partition": p, "reason": r} for p, r in self.partitions_skipped.items()],
            "rows_deleted": [{"table": t, "rows": n} for t, n in self.rows_deleted.items()],
            "runs_archived": len(self.runs_archived),
            "skipped_tables": sorted(self.skipped_tables),
            "outbox_unpublished_stale": self.outbox_unpublished_stale,
            "warnings": self.warnings[:20],
        }


class ArchiveSink(Protocol):
    """Хранилище архива (§20.2: `archive/<workspace_id|global>/<table>/<period>.ndjson.gz`)."""

    def put_file(self, key: str, path: Path, sha256: str) -> str:
        """Сохраняет файл под ключом (перезапись допустима: содержимое детерминировано); возвращает storage_ref."""
        ...


class FileSystemArchiveSink:
    """Архив на файловой системе: атомарная запись (tmp + fsync + rename) и сверка sha256."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put_file(self, key: str, path: Path, sha256: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", key) or ".." in key.split("/"):
            raise ValueError("некорректный ключ архива")
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".aicd-archive-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as out, path.open("rb") as src:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            if _sha256_file(Path(tmp)) != sha256:
                raise OSError("архив повреждён при записи (sha256 не совпадает)")
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return f"file://archive/{key}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_and_size(path: Path) -> tuple[str, int]:
    return _sha256_file(path), path.stat().st_size


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def partition_month(name: str, table: str) -> date | None:
    match = _PARTITION_RE.fullmatch(name)
    if match is None or match["table"] != table:
        return None
    return date(int(match["y"]), int(match["m"]), 1)


def partition_cutoff(table: str, periods: RetentionPeriods, now: datetime) -> date:
    """Партиция удаляется, если её верхняя граница ≤ cutoff; cutoff не позже начала текущего месяца."""
    today = now.astimezone(UTC).date()
    if table == "outbox_events":
        cutoff = _add_months(_month_start(today), -periods.outbox_published_months)
    elif table == "audit_events":
        cutoff = today - timedelta(days=periods.audit_days)
    else:
        cutoff = today - timedelta(days=periods.agent_activity_days)
    return min(cutoff, _month_start(today))


async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    return bool((await conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"aicd.{table}"})).scalar_one())


async def _partitions(conn: AsyncConnection, table: str) -> list[str]:
    rows = await conn.execute(
        text(
            "SELECT c.relname FROM pg_catalog.pg_inherits i"
            " JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid"
            " JOIN pg_catalog.pg_class p ON p.oid = i.inhparent"
            " JOIN pg_catalog.pg_namespace n ON n.oid = p.relnamespace"
            " WHERE n.nspname = 'aicd' AND p.relname = :t ORDER BY c.relname"
        ),
        {"t": table},
    )
    return [r[0] for r in rows]


async def _load_policy(conn: AsyncConnection) -> Mapping[str, Any] | None:
    row = (
        await conn.execute(
            text(
                "SELECT definition FROM policies WHERE policy_type = 'retention' AND workspace_id IS NULL"
                " AND project_id IS NULL AND active ORDER BY version DESC LIMIT 1"
            )
        )
    ).scalar_one_or_none()
    return row if isinstance(row, Mapping) else None


async def _export_partition(
    conn: AsyncConnection, table: str, partition: str, month: date, sink: ArchiveSink
) -> ArchivedPartition:
    """NDJSON.gz строк партиции (через родителя, по диапазону ключа), детерминированно (mtime=0)."""
    column = PARTITIONED[table]
    nxt = _add_months(month, 1)
    lo = datetime(month.year, month.month, 1, tzinfo=UTC)
    hi = datetime(nxt.year, nxt.month, 1, tzinfo=UTC)
    query = text(
        f"SELECT to_jsonb(t)::text AS doc, t.{column} AS k, t.id AS id FROM {table} t"  # noqa: S608 — имена из PARTITIONED
        f" WHERE t.{column} >= :lo AND t.{column} < :hi AND (t.{column}, t.id) > (:k, :id)"
        f" ORDER BY t.{column}, t.id LIMIT :n"
    )
    rows_total = 0
    fd, tmp_name = tempfile.mkstemp(prefix=".aicd-export-", suffix=".ndjson.gz")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            key_k: datetime = lo - timedelta(microseconds=1)
            key_id = uuid.UUID(int=0)
            while True:
                batch = (
                    await conn.execute(query, {"lo": lo, "hi": hi, "k": key_k, "id": key_id, "n": _EXPORT_CHUNK})
                ).all()
                for doc, _, _ in batch:
                    gz.write(doc.encode("utf-8") + b"\n")
                rows_total += len(batch)
                if len(batch) < _EXPORT_CHUNK:
                    break
                key_k, key_id = batch[-1][1], batch[-1][2]
        sha, size = await asyncio.to_thread(_sha256_and_size, tmp)
        key = f"global/{table}/{month:%Y-%m}.ndjson.gz"
        ref = await asyncio.to_thread(sink.put_file, key, tmp, sha)
    finally:
        await asyncio.to_thread(tmp.unlink, missing_ok=True)
    return ArchivedPartition(table, partition, ref, sha, size, rows_total)


async def _ensure_partitions(conn: AsyncConnection, report: RetentionReport, settings: Settings, now: datetime) -> None:
    current = _month_start(now.astimezone(UTC).date())
    for table in PARTITIONED:
        if not await _table_exists(conn, table):
            report.skipped_tables[table] = "таблица ещё не создана (Phase 4)"
            continue
        created = (
            await conn.execute(
                text("SELECT maint_ensure_monthly_partitions(:t, :d, :m)"),
                {"t": table, "d": current, "m": settings.retention_partitions_ahead_months},
            )
        ).scalar_one()
        await conn.commit()
        report.partitions_created[table] = int(created)
        months = [m for p in await _partitions(conn, table) if (m := partition_month(p, table)) is not None]
        ahead = sum(1 for m in months if m > current)
        report.partitions_ahead[table] = ahead
        if ahead < 2:
            report.warnings.append(f"{table}: впереди {ahead} партиций (< 2)")


async def _drop_expired_partitions(
    conn: AsyncConnection, report: RetentionReport, periods: RetentionPeriods, now: datetime, sink: ArchiveSink
) -> None:
    for table in PARTITIONED:
        if table in report.skipped_tables:
            continue
        cutoff = partition_cutoff(table, periods, now)
        for partition in await _partitions(conn, table):
            month = partition_month(partition, table)
            if month is None:
                report.warnings.append(f"{partition}: имя партиции не по шаблону, пропущена")
                continue
            if _add_months(month, 1) > cutoff:
                continue
            archived: ArchivedPartition | None = None
            if table in ARCHIVED_BEFORE_DROP:
                archived = await _export_partition(conn, table, partition, month, sink)
                await conn.rollback()  # закрыть читающую транзакцию: архив уже в хранилище
                report.archives.append(archived)
            result = (
                await conn.execute(
                    text("SELECT maint_drop_partition(:t, :p, :c, :ref, :sha)"),
                    {
                        "t": table,
                        "p": partition,
                        "c": cutoff,
                        "ref": archived.storage_ref if archived else None,
                        "sha": archived.sha256 if archived else None,
                    },
                )
            ).scalar_one()
            if result == "dropped":
                meta: dict[str, Any] = {"table": table, "partition": partition, "cutoff": cutoff.isoformat()}
                if archived is not None:
                    meta.update(archive_ref=archived.storage_ref, sha256=archived.sha256, rows=archived.rows)
                await audit.record(
                    conn,
                    action="maintenance.partition_dropped",
                    resource_type="maintenance",
                    resource_id=report.session_id,
                    actor_type="system",
                    actor_id=None,
                    workspace_id=None,
                    metadata=meta,
                )
                report.partitions_dropped.append(partition)
            else:
                report.partitions_skipped[partition] = str(result)
                if result == "skipped_unpublished":
                    report.warnings.append(f"{partition}: есть неопубликованные события, партиция сохранена")
            await conn.commit()


def _batch_deletes(periods: RetentionPeriods, now: datetime) -> list[tuple[str, str, dict[str, Any]]]:
    """(таблица, условие отбора, параметры). Отбор пакета — по ctid (§20.2)."""
    return [
        (
            "run_event_log",
            "run_id IN (SELECT r.id FROM runs r WHERE r.status = ANY(:terminal) AND r.finished_at < :cutoff)",
            {"cutoff": now - timedelta(days=periods.run_event_log_days), "terminal": list(TERMINAL_RUN_STATUSES)},
        ),
        ("processed_events", "processed_at < :cutoff", {"cutoff": now - timedelta(days=periods.processed_events_days)}),
        ("idempotency_keys", "expires_at < :cutoff", {"cutoff": now}),
        (
            "user_sessions",
            "(expires_at < :cutoff OR revoked_at < :cutoff)",
            {"cutoff": now - timedelta(days=periods.user_sessions_days)},
        ),
        ("provider_health", "observed_at < :cutoff", {"cutoff": now - timedelta(days=periods.provider_health_days)}),
    ]


async def _delete_in_batches(
    conn: AsyncConnection, report: RetentionReport, settings: Settings, periods: RetentionPeriods, now: datetime
) -> None:
    for table, condition, params in _batch_deletes(periods, now):
        if not await _table_exists(conn, table):
            report.skipped_tables[table] = "таблица отсутствует"
            continue
        statement = text(
            f"DELETE FROM {table} WHERE ctid = ANY(ARRAY(SELECT ctid FROM {table} WHERE {condition} LIMIT :n))"  # noqa: S608 — фиксированный список
        )
        total = 0
        while True:
            deleted = (await conn.execute(statement, {**params, "n": settings.retention_batch_size})).rowcount
            if deleted:
                await audit.record(
                    conn,
                    action="maintenance.rows_deleted",
                    resource_type="maintenance",
                    resource_id=report.session_id,
                    actor_type="system",
                    actor_id=None,
                    workspace_id=None,
                    metadata={"table": table, "rows": deleted, "cutoff": params["cutoff"].isoformat()},
                )
            await conn.commit()
            total += deleted
            if deleted < settings.retention_batch_size:
                break
            if settings.retention_batch_pause_seconds:
                await asyncio.sleep(settings.retention_batch_pause_seconds)
        report.rows_deleted[table] = total


# Архив run (§20.2): таблица → запрос строк run (детерминированный порядок). Параметры :ws, :run.
RUN_ARCHIVE_EXPORTS: tuple[tuple[str, str], ...] = (
    (
        "chat_threads",
        "SELECT to_jsonb(t)::text FROM chat_threads t WHERE t.workspace_id = :ws AND t.run_id = :run"
        " ORDER BY t.created_at, t.id",
    ),
    (
        "chat_messages",
        "SELECT (to_jsonb(t) - 'search_vector')::text FROM chat_messages t"
        " WHERE t.workspace_id = :ws AND t.run_id = :run ORDER BY t.created_at, t.id",
    ),
    (
        "agent_messages",
        "SELECT to_jsonb(t)::text FROM agent_messages t WHERE t.workspace_id = :ws AND t.run_id = :run"
        " ORDER BY t.created_at, t.id",
    ),
    (
        "message_deliveries",
        "SELECT to_jsonb(d)::text FROM message_deliveries d JOIN agent_messages m"
        " ON m.workspace_id = d.workspace_id AND m.id = d.message_id"
        " WHERE m.workspace_id = :ws AND m.run_id = :run ORDER BY d.message_id, d.consumer_id",
    ),
    (
        "user_directives",
        "SELECT to_jsonb(t)::text FROM user_directives t WHERE t.workspace_id = :ws AND t.run_id = :run"
        " ORDER BY t.created_at, t.id",
    ),
    (
        "directive_impacts",
        "SELECT to_jsonb(t)::text FROM directive_impacts t WHERE t.workspace_id = :ws AND t.run_id = :run"
        " ORDER BY t.directive_id, t.task_id",
    ),
    (
        "directive_acknowledgements",
        "SELECT to_jsonb(a)::text FROM directive_acknowledgements a JOIN user_directives d"
        " ON d.workspace_id = a.workspace_id AND d.id = a.directive_id"
        " WHERE d.workspace_id = :ws AND d.run_id = :run ORDER BY a.created_at, a.id",
    ),
)

_RUN_ARCHIVE_CANDIDATES = text(
    "SELECT r.workspace_id, r.id, r.project_id FROM runs r"
    " WHERE r.status = ANY(:terminal) AND r.finished_at < :cutoff"
    " AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.workspace_id = r.workspace_id AND a.run_id = r.id"
    "                 AND a.kind = 'archive')"
    " AND (EXISTS (SELECT 1 FROM agent_messages m WHERE m.workspace_id = r.workspace_id AND m.run_id = r.id)"
    "   OR EXISTS (SELECT 1 FROM chat_threads t WHERE t.workspace_id = r.workspace_id AND t.run_id = r.id))"
    " ORDER BY r.finished_at, r.id LIMIT :n"
)

# Сообщения поручений остаются как трасса (user_directives → chat_messages RESTRICT): body → ссылка на архив.
_RUN_ARCHIVE_REDACT_DIRECTIVE_CHAT = text(
    "UPDATE chat_messages m SET body = :marker WHERE m.workspace_id = :ws AND m.run_id = :run"
    " AND EXISTS (SELECT 1 FROM user_directives d WHERE d.workspace_id = m.workspace_id AND d.chat_message_id = m.id)"
)
_RUN_ARCHIVE_DELETE_CHAT = text(
    "DELETE FROM chat_messages m WHERE m.workspace_id = :ws AND m.run_id = :run"
    " AND NOT EXISTS (SELECT 1 FROM user_directives d WHERE d.workspace_id = m.workspace_id"
    "                 AND d.chat_message_id = m.id)"
)
_RUN_ARCHIVE_DELETE_THREADS = text(
    "DELETE FROM chat_threads t WHERE t.workspace_id = :ws AND t.run_id = :run"
    " AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.workspace_id = t.workspace_id AND m.thread_id = t.id)"
)
# agent_messages: сохраняются сообщения, на которые ссылаются сохраняемые строки (FK RESTRICT), и вся их
# цепочка reply_to; остальные удаляются одним оператором (ссылки reply_to внутри удаляемого набора допустимы).
_RUN_ARCHIVE_DELETE_MESSAGES = text(
    "WITH RECURSIVE keep(id, reply_to_id) AS ("
    "  SELECT m.id, m.reply_to_id FROM agent_messages m WHERE m.workspace_id = :ws AND m.run_id = :run AND ("
    "    EXISTS (SELECT 1 FROM agent_decisions x WHERE x.workspace_id = m.workspace_id AND x.acp_message_id = m.id)"
    "    OR EXISTS (SELECT 1 FROM directive_acknowledgements x"
    "               WHERE x.workspace_id = m.workspace_id AND x.acp_message_id = m.id)"
    "    OR EXISTS (SELECT 1 FROM chat_messages x WHERE x.workspace_id = m.workspace_id AND x.acp_message_id = m.id)"
    "    OR EXISTS (SELECT 1 FROM agent_messages x WHERE x.workspace_id = m.workspace_id AND x.reply_to_id = m.id"
    "               AND x.run_id <> m.run_id))"
    "  UNION"
    "  SELECT p.id, p.reply_to_id FROM agent_messages p JOIN keep k ON p.workspace_id = :ws AND p.id = k.reply_to_id"
    ")"
    " DELETE FROM agent_messages m WHERE m.workspace_id = :ws AND m.run_id = :run"
    " AND m.id NOT IN (SELECT k.id FROM keep k)"
)


async def _export_run(
    conn: AsyncConnection, workspace_id: uuid.UUID, run_id: uuid.UUID, sink: ArchiveSink
) -> ArchivedRun:
    """NDJSON.gz пакета run: строка = {"table": …, "row": …}; детерминированно (mtime=0)."""
    params = {"ws": workspace_id, "run": run_id}
    counts: dict[str, int] = {}
    fd, tmp_name = tempfile.mkstemp(prefix=".aicd-export-", suffix=".ndjson.gz")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            for table, query in RUN_ARCHIVE_EXPORTS:
                counts[table] = 0
                result = await conn.stream(text(query), params)
                async for (doc,) in result:
                    gz.write(f'{{"table":"{table}","row":{doc}}}\n'.encode())
                    counts[table] += 1
        sha, size = await asyncio.to_thread(_sha256_and_size, tmp)
        key = f"{workspace_id}/runs/{run_id}.ndjson.gz"
        ref = await asyncio.to_thread(sink.put_file, key, tmp, sha)
    finally:
        await asyncio.to_thread(tmp.unlink, missing_ok=True)
    return ArchivedRun(workspace_id, run_id, ref, sha, size, counts)


async def _archive_runs(
    conn: AsyncConnection,
    report: RetentionReport,
    settings: Settings,
    periods: RetentionPeriods,
    now: datetime,
    sink: ArchiveSink,
) -> None:
    """Архив run (§20.2–20.3). Маркер «run заархивирован» — строка artifacts(kind='archive', run_id)."""
    for table in ("agent_messages", "chat_messages"):
        if not await _table_exists(conn, table):
            report.skipped_tables[table] = "таблица ещё не создана"
            return
    cutoff = now - timedelta(days=periods.agent_messages_days)
    deleted = dict.fromkeys(("chat_messages", "chat_threads", "agent_messages"), 0)
    while True:
        candidates = (
            await conn.execute(
                _RUN_ARCHIVE_CANDIDATES,
                {"terminal": list(TERMINAL_RUN_STATUSES), "cutoff": cutoff, "n": settings.retention_batch_size},
            )
        ).all()
        await conn.commit()
        for workspace_id, run_id, project_id in candidates:
            archived = await _export_run(conn, workspace_id, run_id, sink)
            await conn.rollback()  # закрыть читающую транзакцию: архив уже в хранилище
            params = {"ws": workspace_id, "run": run_id}
            redacted = (
                await conn.execute(
                    _RUN_ARCHIVE_REDACT_DIRECTIVE_CHAT, {**params, "marker": f"[архив run: {archived.storage_ref}]"}
                )
            ).rowcount
            removed = {
                "chat_messages": (await conn.execute(_RUN_ARCHIVE_DELETE_CHAT, params)).rowcount,
                "chat_threads": (await conn.execute(_RUN_ARCHIVE_DELETE_THREADS, params)).rowcount,
                "agent_messages": (await conn.execute(_RUN_ARCHIVE_DELETE_MESSAGES, params)).rowcount,
            }
            await conn.execute(
                text(
                    "INSERT INTO artifacts (id, workspace_id, project_id, run_id, kind, storage_ref, sha256,"
                    " size_bytes, mime_type, retention_class) VALUES (:id, :ws, :project, :run, 'archive', :ref,"
                    " :sha, :size, 'application/gzip', 'archive')"
                ),
                {
                    "id": uuid7(),
                    "ws": workspace_id,
                    "project": project_id,
                    "run": run_id,
                    "ref": archived.storage_ref,
                    "sha": archived.sha256,
                    "size": archived.size_bytes,
                },
            )
            await audit.record(
                conn,
                action="maintenance.run_archived",
                resource_type="maintenance",
                resource_id=report.session_id,
                actor_type="system",
                actor_id=None,
                workspace_id=None,
                metadata={
                    "workspace_id": str(workspace_id),
                    "run_id": str(run_id),
                    "archive_ref": archived.storage_ref,
                    "sha256": archived.sha256,
                    "rows_archived": archived.rows,
                    "rows_deleted": removed,
                    "chat_bodies_replaced": redacted,
                    "cutoff": cutoff.isoformat(),
                },
            )
            await conn.commit()
            for table, count in removed.items():
                deleted[table] += count
            report.runs_archived.append(archived)
        if len(candidates) < settings.retention_batch_size:
            break
        if settings.retention_batch_pause_seconds:
            await asyncio.sleep(settings.retention_batch_pause_seconds)
    for table, count in deleted.items():
        report.rows_deleted[table] = report.rows_deleted.get(table, 0) + count


async def run_retention(
    engine: AsyncEngine,
    settings: Settings,
    *,
    now: datetime | None = None,
    sink: ArchiveSink | None = None,
    reason: str = "scheduled retention",
) -> RetentionReport:
    """Один проход retention. `engine` — подключение роли aicd_maintenance (иначе maint_begin откажет)."""
    now = now or utcnow()
    sink = sink or FileSystemArchiveSink(settings.archive_root())
    report = RetentionReport(session_id=uuid7(), started_at=now)
    async with engine.connect() as conn:
        await conn.execute(text("SET lock_timeout = '5s'"))
        await conn.execute(
            text("SELECT maint_begin(:id, :job, :reason)"), {"id": report.session_id, "job": JOB_NAME, "reason": reason}
        )
        await conn.commit()
        try:
            periods = RetentionPeriods.from_settings(settings)
            policy = await _load_policy(conn)
            if policy is not None:
                periods, policy_warnings = periods.merged(policy)
                report.warnings.extend(policy_warnings)
            report.periods = periods
            await conn.commit()

            await _ensure_partitions(conn, report, settings, now)
            await _drop_expired_partitions(conn, report, periods, now, sink)
            await _delete_in_batches(conn, report, settings, periods, now)

            await _archive_runs(conn, report, settings, periods, now, sink)
            report.outbox_unpublished_stale = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL AND created_at < :t"),
                        {"t": now - OUTBOX_STALE_AFTER},
                    )
                ).scalar_one()
            )
            if report.outbox_unpublished_stale:
                report.warnings.append(f"outbox_events: {report.outbox_unpublished_stale} неопубликованных > 15 мин")
            await conn.commit()
        except BaseException:
            report.outcome = "failure"
            await conn.rollback()
            raise
        finally:
            report.finished_at = utcnow()
            try:
                await conn.execute(
                    text("SELECT maint_end(:id, :outcome, CAST(:meta AS jsonb))"),
                    {"id": uuid7(), "outcome": report.outcome, "meta": _json(report.summary())},
                )
                await conn.commit()
            except Exception:
                # Соединение с незакрытой сессией обслуживания не возвращается в пул.
                await conn.invalidate()
                if report.outcome == "success":
                    raise
    return report


def _json(value: Any) -> str:
    return json.dumps(redact_value(value), ensure_ascii=False, default=str)
