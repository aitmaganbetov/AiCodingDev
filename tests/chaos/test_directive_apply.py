"""P9-03: гибель воркера оркестрации посреди применения поручения (apply) и дубль доставки события.

`directives.apply` — одна транзакция: CHECKPOINTING (checkpoint + объект `checkpoint_state` в хранилище) →
REPLANNING (новая plan_version, задачи-замены) → DISPATCHED. Воркер «умирает» (BaseException — обход
обработчика `except Exception`, который пометил бы поручение FAILED; как при SIGKILL соединение с БД
закрывается и транзакция откатывается) сразу после записи объекта checkpoint в хранилище:

* в БД нет ни следа apply: поручение CONFIRMED, нет checkpoint/новой plan_version/замен, нет событий;
* повторная доставка `directive.state_changed` (двумя воркерами одновременно — дубль at-least-once)
  применяет поручение ровно один раз: один checkpoint, одна новая plan_version, одна цепочка событий;
* объект checkpoint от прерванной попытки остаётся в хранилище без строки `artifacts` (сирота — см. отчёт P9-03).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, BinaryIO

import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration import directives
from aicd_platform.artifacts import ArtifactStore, StoredArtifact
from aicd_platform.config import Settings
from tests.support import db as testdb
from tests.support.orch_api import HookRecorder, add_task, key, make_team, orch_app, running_run
from tests.support.orchestration import connect_admin

pytestmark = pytest.mark.db


class Crash(BaseException):
    """Процесс воркера погиб (не Exception: обработчики приложения его не перехватывают)."""


class CrashAfterPut:
    """Хранилище, «убивающее» воркер сразу после записи объекта (эффект во внешнем хранилище уже есть)."""

    def __init__(self, inner: ArtifactStore) -> None:
        self.inner = inner
        self.written: list[str] = []

    def put(self, workspace_id: uuid.UUID, artifact_id: uuid.UUID, stream: BinaryIO) -> StoredArtifact:
        stored = self.inner.put(workspace_id, artifact_id, stream)
        self.written.append(stored.storage_ref)
        raise Crash("worker killed after checkpoint object was written")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
async def oapp(settings: Settings) -> AsyncIterator[FastAPI]:
    async for application in orch_app(settings, HookRecorder()):
        yield application


async def test_worker_killed_mid_directive_apply_then_duplicate_delivery_applies_once(
    oapp: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, admin: psycopg.Connection
) -> None:
    store: ArtifactStore = oapp.state.aicd.artifacts
    team = await make_team(oapp, app_engine, admin, ("developer",))
    ws = team.tenant.workspace_id
    run = running_run(admin, team.tenant)
    main = add_task(admin, team.tenant, run, "main")
    add_task(admin, team.tenant, run, "review", role="reviewer", depends_on=main)
    dev = team.users["developer"]
    version = admin.execute("SELECT version FROM runs WHERE id = %s", (run,)).fetchone()
    assert version is not None
    r = await dev.client.post(
        f"/api/v1/runs/{run}/directives",
        json={"target": {"type": "run"}, "directive_type": "CHANGE_REQUEST", "message": "добавь проверку",
              "expected_run_version": version[0]},
        headers=dev.headers(idem_key=key()),
    )  # fmt: skip
    assert r.status_code == 202, r.text
    did = uuid.UUID(r.json()["directive_id"])
    await directives.process(worker_engine, ws, did, artifacts=store)  # analyze → AWAITING_CONFIRMATION
    view = await dev.client.get(f"/api/v1/runs/{run}/directives/{did}")
    r = await dev.client.post(f"/api/v1/runs/{run}/directives/{did}/confirm",
                              headers=dev.headers(idem=True, if_match=view.headers["etag"]))  # fmt: skip
    assert r.status_code == 202, r.text

    def state() -> tuple[Any, ...]:
        row = admin.execute(
            "SELECT d.status, d.checkpoint_id, r.plan_version,"
            " (SELECT count(*) FROM checkpoints c WHERE c.run_id = r.id AND c.reason = 'directive'),"
            " (SELECT count(*) FROM tasks t WHERE t.run_id = r.id AND t.plan_version > 1)"
            " FROM user_directives d JOIN runs r ON r.id = d.run_id WHERE d.id = %s",
            (did,),
        ).fetchone()
        assert row is not None
        return tuple(row)

    def transitions() -> list[str]:
        rows = admin.execute(
            "SELECT payload->>'to' FROM outbox_events WHERE event_type = 'directive.state_changed'"
            " AND aggregate_id = %s ORDER BY created_at, id",
            (did,),
        ).fetchall()
        return [row[0] for row in rows]

    assert state() == ("CONFIRMED", None, 1, 0, 0)
    before = transitions()

    crashing = CrashAfterPut(store)
    with pytest.raises(Crash):
        await directives.process(worker_engine, ws, did, artifacts=crashing)
    assert crashing.written, "объект checkpoint записан до гибели воркера"
    assert state() == ("CONFIRMED", None, 1, 0, 0), "apply откатился целиком"
    assert transitions() == before, "события прерванного apply не опубликованы (outbox в той же транзакции)"
    orphan = crashing.written[0]
    assert store.exists(orphan)
    assert admin.execute("SELECT count(*) FROM artifacts WHERE storage_ref = %s", (orphan,)).fetchone() == (0,)

    # Повторная доставка события — двумя воркерами одновременно.
    results = await asyncio.gather(
        directives.process(worker_engine, ws, did, artifacts=store),
        directives.process(worker_engine, ws, did, artifacts=store),
        return_exceptions=True,
    )
    assert not [x for x in results if isinstance(x, BaseException)], results
    status, checkpoint, plan_version, checkpoints, new_tasks = state()
    assert (status, plan_version, checkpoints, new_tasks) == ("DISPATCHED", 2, 1, 2) and checkpoint is not None
    assert transitions() == [*before, "CHECKPOINTING", "REPLANNING", "DISPATCHED"], "каждый переход ровно один раз"
    assert admin.execute(
        "SELECT count(*) FROM directive_acknowledgements WHERE directive_id = %s", (did,)
    ).fetchone() == (2,)
