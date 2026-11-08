"""P9-03 (NFR-REL-01/02): гибель воркера оркестрации/исполнения в разных точках шага задачи.

Настоящий процесс-воркер (`tests/support/chaos_worker.py`, `hang_at`) доходит до точки сбоя и убивается
SIGKILL (без finally/cleanup):

* **mid-lease** — аренда взята, попытка RUNNING, CLI ещё не запущен;
* **mid-gate** — CLI отработал, задача VERIFYING, gate исполняется (`gate_executions` RUNNING).

Затем «новый воркер»: reconciler (после истечения аренды) → попытка FAILED(LEASE_LOST) без расхода
max_attempts → run доводится до COMPLETED. Проверяется: нет потерянных/дублированных эффектов (одна
успешная попытка, нет «висящих» исполнений/gates/аренд), «зомби» со старым fencing token не пишет результат.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from aicd_orchestration.recovery import Reconciler
from aicd_orchestration.worktrees import WorktreeManager
from aicd_platform import leases
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_security.local_runner import LocalProcessRunner
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.phase3 import Slice, create_run, drive, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[2]


def _spawn(s: Slice, database: testdb.TestDatabase, run_id: str, hang_at: str, marker: Path) -> subprocess.Popen[bytes]:
    config = {
        "database_url": database.url_for("aicd_worker"),
        "tmp": str(s.tmp),
        "repository_roots": json.dumps({k: str(v) for k, v in s.settings.repository_roots.items()}),
        "scenario": "agent",
        "workspace_id": s.ws,
        "run_id": run_id,
        "hang_at": hang_at,
        "marker": str(marker),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.support.chaos_worker", json.dumps(config)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _kill_at_marker(proc: subprocess.Popen[bytes], marker: Path, seconds: float = 120) -> None:
    deadline = time.monotonic() + seconds
    try:
        while not marker.exists():
            if proc.poll() is not None:
                err = proc.stderr.read().decode() if proc.stderr else ""
                raise AssertionError(f"воркер завершился до точки сбоя: {err[-3000:]}")
            if time.monotonic() > deadline:
                raise AssertionError("точка сбоя не достигнута")
            time.sleep(0.05)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


def _reconciler(s: Slice) -> Reconciler:
    return Reconciler(
        engine=s.worker,
        artifacts=FileSystemArtifactStore(s.tmp / "artifacts"),
        worktrees=WorktreeManager(s.tmp / "wt"),
        repository_roots=s.settings.repository_roots,
        worker_host_id=None,
        runners=(LocalProcessRunner(),),
    )


def _current_attempt(admin: psycopg.Connection, run_id: str) -> tuple[uuid.UUID, uuid.UUID, int, str]:
    row = admin.execute(
        "SELECT t.id, a.id, a.fencing_token, t.status FROM tasks t JOIN task_attempts a ON a.task_id = t.id"
        " WHERE t.run_id = %s AND a.status = 'RUNNING'",
        (uuid.UUID(run_id),),
    ).fetchone()
    assert row is not None, "попытка, прерванная гибелью воркера, остаётся RUNNING"
    return row[0], row[1], int(row[2]), str(row[3])


@pytest.mark.parametrize(("hang_at", "task_status"), [("lease", "RUNNING"), ("gate", "VERIFYING")])
async def test_worker_killed_mid_step_is_recovered_without_duplicates(
    hang_at: str, task_status: str, world: Slice, database: testdb.TestDatabase
) -> None:
    await install_both(world.worker)
    run_id = await create_run(world)
    marker = world.tmp / f"hang-{hang_at}"
    _kill_at_marker(_spawn(world, database, run_id, hang_at, marker), marker)
    reconciler = _reconciler(world)
    ws = uuid.UUID(world.ws)

    with orch.connect_admin(database) as admin:
        task_id, attempt_id, token, status = _current_attempt(admin, run_id)
        assert status == task_status
        if hang_at == "lease":
            assert admin.execute(
                "SELECT count(*) FROM cli_executions WHERE task_attempt_id = %s", (attempt_id,)
            ).fetchone() == (0,), "CLI не запускался"
        else:
            assert admin.execute(
                "SELECT count(*) FROM gate_executions WHERE task_id = %s AND status = 'RUNNING'", (task_id,)
            ).fetchone() == (1,), "gate прерван посреди исполнения"

        # Пока аренда жива — reconciler не трогает задачу.
        report = await reconciler.reconcile()
        assert task_id not in report.reaped_tasks, report
        assert _current_attempt(admin, run_id)[1] == attempt_id

        admin.execute(
            "UPDATE worker_leases SET acquired_at = now() - interval '5 minutes', lease_until = now() - interval '1 second'"
            " WHERE resource_type = 'task' AND resource_id = %s AND released_at IS NULL",
            (task_id,),
        )
        report = await reconciler.reconcile()
        assert task_id in report.reaped_tasks, report
        assert admin.execute(
            "SELECT status, failure_category FROM task_attempts WHERE id = %s", (attempt_id,)
        ).fetchone() == ("FAILED", "LEASE_LOST")
        assert admin.execute("SELECT status, attempt_count FROM tasks WHERE id = %s", (task_id,)).fetchone() == (
            "RETRY_WAIT",
            0,
        ), "LEASE_LOST не расходует max_attempts"

    # «Зомби» с прежним токеном больше не владеет задачей.
    async with transaction(world.worker, DbContext(workspace_id=ws, actor_type="system")) as conn:
        assert not await leases.is_current(conn, "task", task_id, token)
        stale = leases.Lease("task", task_id, "zombie", token, datetime.now(UTC) + timedelta(hours=1))
        assert not await leases.heartbeat(conn, stale)

    final = await drive(world, run_id)
    if final != "COMPLETED":
        with orch.connect_admin(database) as admin:
            tasks = admin.execute(
                "SELECT task_key, status, attempt_count FROM tasks WHERE run_id = %s", (uuid.UUID(run_id),)
            ).fetchall()
        raise AssertionError(f"run {final}: {tasks}")
    with orch.connect_admin(database) as admin:
        attempts = admin.execute(
            "SELECT a.status, a.failure_category FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
            " WHERE t.id = %s ORDER BY a.attempt_no",
            (task_id,),
        ).fetchall()
        assert attempts == [("FAILED", "LEASE_LOST"), ("SUCCEEDED", None)]
        r = uuid.UUID(run_id)
        assert admin.execute(
            "SELECT count(*) FROM gate_executions WHERE run_id = %s AND status IN ('PENDING','RUNNING')"
            " AND superseded_at IS NULL",
            (r,),
        ).fetchone() == (0,), "нет «висящих» gate после восстановления"
        assert admin.execute(
            "SELECT count(*) FROM cli_executions e JOIN task_attempts a ON a.id = e.task_attempt_id"
            " JOIN tasks t ON t.id = a.task_id WHERE t.run_id = %s AND e.status IN ('QUEUED','STARTING','RUNNING','EXITED')",
            (r,),
        ).fetchone() == (0,)
        assert admin.execute(
            "SELECT count(*) FROM worker_leases l JOIN tasks t ON t.id = l.resource_id WHERE t.run_id = %s"
            " AND l.released_at IS NULL",
            (r,),
        ).fetchone() == (0,)
        # Ровно один переход задачи в SUCCEEDED (нет дублированных эффектов в журнале событий).
        assert admin.execute(
            "SELECT count(*) FROM outbox_events WHERE aggregate_id = %s AND event_type = 'task.state_changed'"
            " AND payload->>'to' = 'SUCCEEDED'",
            (task_id,),
        ).fetchone() == (1,)
