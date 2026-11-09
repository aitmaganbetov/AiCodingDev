"""Сборка объектов-сирот хранилища артефактов (P9-03 / P3-18; DATABASE_DESIGN §20.4).

Объект-сирота — объект хранилища артефактов (FS или S3) с ключом формата `object_key`
(`<workspace_id>/<YYYY>/<MM>/<artifact_id>`), для которого нет строки `artifacts` с этим `storage_ref`.
Возникает, когда объект записан, а транзакция с его строкой откатилась (гибель воркера посреди
`directives.apply`/checkpoint, сбой INSERT после `put`) или когда строка удалена, а удаление объекта не
удалось (retention). Архивы retention (`archive/…`) и прочие префиксы не рассматриваются.

* Только объекты старше `grace` (по умолчанию 24 ч): запись в процессе (объект уже есть, строка ещё не
  закоммичена) не трогается.
* Проверка строк — пачками по `storage_ref` под ролью `aicd_maintenance` (SELECT на `artifacts`) в сессии
  обслуживания (`maint_begin`/`maint_end`, §19.6); каждый пакет удалений — `maintenance.orphan_objects_deleted`
  в аудите (число, байты, dry-run). `dry_run` — только подсчёт (аудит с `dry_run=true`), без удаления.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform import audit
from aicd_platform.artifacts import StoredObject
from aicd_platform.clock import utcnow
from aicd_platform.ids import uuid7

log = logging.getLogger("aicd.maintenance.artifact_gc")

JOB_NAME = "orphan_objects"
DEFAULT_GRACE = timedelta(hours=24)
BATCH = 500


class ObjectStore(Protocol):
    def iter_objects(self) -> Iterator[StoredObject]: ...

    def delete(self, storage_ref: str) -> bool: ...


@dataclass
class OrphanReport:
    session_id: uuid.UUID
    dry_run: bool
    grace_seconds: int
    scanned: int = 0
    too_recent: int = 0
    orphans: int = 0
    orphan_bytes: int = 0
    deleted: int = 0
    failed: int = 0
    orphan_refs: list[str] = field(default_factory=list)  # первые 100 — для отчёта оператору

    def summary(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "grace_seconds": self.grace_seconds,
            "scanned": self.scanned,
            "too_recent": self.too_recent,
            "orphans": self.orphans,
            "orphan_bytes": self.orphan_bytes,
            "deleted": self.deleted,
            "failed": self.failed,
        }


def _batches(store: ObjectStore, cutoff: datetime, report: OrphanReport) -> Iterator[list[StoredObject]]:
    batch: list[StoredObject] = []
    for obj in store.iter_objects():
        report.scanned += 1
        if obj.modified_at >= cutoff:
            report.too_recent += 1
            continue
        batch.append(obj)
        if len(batch) >= BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


async def sweep_orphan_objects(
    engine: AsyncEngine,
    store: ObjectStore,
    *,
    grace: timedelta = DEFAULT_GRACE,
    dry_run: bool = False,
    now: datetime | None = None,
    reason: str = "orphan artifact objects sweep",
) -> OrphanReport:
    """Один проход. `engine` — подключение роли aicd_maintenance (иначе maint_begin откажет)."""
    now = now or utcnow()
    cutoff = now - grace
    report = OrphanReport(session_id=uuid7(), dry_run=dry_run, grace_seconds=int(grace.total_seconds()))
    outcome = "failure"
    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT maint_begin(:id, :job, :reason)"), {"id": report.session_id, "job": JOB_NAME, "reason": reason}
        )
        await conn.commit()
        try:
            batches = _batches(store, cutoff, report)
            while True:
                batch = await asyncio.to_thread(next, batches, None)
                if batch is None:
                    break
                refs = [o.storage_ref for o in batch]
                known = set(
                    (
                        await conn.execute(
                            text("SELECT storage_ref FROM artifacts WHERE storage_ref = ANY(:refs)"), {"refs": refs}
                        )
                    )
                    .scalars()
                    .all()
                )
                await conn.commit()
                orphans = [o for o in batch if o.storage_ref not in known]
                if not orphans:
                    continue
                deleted = failed = 0
                if not dry_run:
                    for obj in orphans:
                        try:
                            deleted += int(await asyncio.to_thread(store.delete, obj.storage_ref))
                        except Exception as exc:  # noqa: BLE001 — один объект не останавливает сборку
                            failed += 1
                            log.warning(
                                "orphan delete failed", extra={"ref": obj.storage_ref, "error": type(exc).__name__}
                            )
                report.orphans += len(orphans)
                report.orphan_bytes += sum(o.size_bytes for o in orphans)
                report.deleted += deleted
                report.failed += failed
                report.orphan_refs.extend(o.storage_ref for o in orphans[: max(0, 100 - len(report.orphan_refs))])
                await audit.record(
                    conn,
                    action="maintenance.orphan_objects_deleted",
                    resource_type="maintenance",
                    resource_id=report.session_id,
                    actor_type="system",
                    actor_id=None,
                    workspace_id=None,
                    metadata={
                        "dry_run": dry_run,
                        "orphans": len(orphans),
                        "bytes": sum(o.size_bytes for o in orphans),
                        "deleted": deleted,
                        "failed": failed,
                        "cutoff": cutoff.isoformat(),
                    },
                )
                await conn.commit()
            outcome = "success"
        except BaseException:
            await conn.rollback()
            raise
        finally:
            try:
                await conn.execute(
                    text("SELECT maint_end(:id, :outcome, CAST(:meta AS jsonb))"),
                    {"id": uuid7(), "outcome": outcome, "meta": json.dumps(report.summary())},
                )
                await conn.commit()
            except Exception:
                await conn.invalidate()
                if outcome == "success":
                    raise
    log.info("orphan objects sweep", extra=report.summary())
    return report
