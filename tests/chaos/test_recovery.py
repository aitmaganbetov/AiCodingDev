"""P3-12 / P3-16 (chaos v1): гибель воркера во время исполнения CLI и восстановление.

Настоящий процесс-воркер (`tests/support/chaos_worker.py`) запускает задачу на Fake CLI и убивается SIGKILL,
пока CLI работает. Процесс CLI (своя группа процессов) переживает воркер — как при падении контейнера воркера.
Проверяется DATA_FLOW §5/§7:
* пока аренда жива, reconciler не трогает исполнение (нет ложного «краха» живой задачи);
* после истечения аренды reaper убивает осиротевший CLI, исполнение → CRASHED, попытка → FAILED(LEASE_LOST),
  задача → RETRY_WAIT (не FAILED: LEASE_LOST не расходует max_attempts);
* worktree упавшей попытки → ORPHANED → REMOVED (diff, если был, сохранён в artifacts);
* run затем штатно завершается (COMPLETED) новой попыткой, без дублей и «зомби»-записей.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text

from aicd_orchestration.recovery import Reconciler, reap
from aicd_orchestration.worktrees import WorktreeManager
from aicd_platform import leases
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_security.local_runner import LocalProcessRunner
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.phase3 import Slice, create_run, drive, install_both, install_cli, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[2]


def _spawn_worker(s: Slice, database: testdb.TestDatabase, run_id: str, scenario: str) -> subprocess.Popen[bytes]:
    config = {
        "database_url": database.url_for("aicd_worker"),
        "tmp": str(s.tmp),
        "repository_roots": json.dumps({k: str(v) for k, v in s.settings.repository_roots.items()}),
        "scenario": scenario,
        "workspace_id": s.ws,
        "run_id": run_id,
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.support.chaos_worker", json.dumps(config)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _wait_running_execution(
    admin: psycopg.Connection, run_id: str, worker: subprocess.Popen[bytes]
) -> dict[str, object]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        row = admin.execute(
            "SELECT e.id, e.pid_ref, e.status, a.task_id, a.id AS attempt_id FROM cli_executions e"
            " JOIN task_attempts a ON a.id = e.task_attempt_id JOIN tasks t ON t.id = a.task_id"
            " WHERE t.run_id = %s AND e.status = 'RUNNING' AND e.pid_ref IS NOT NULL",
            (uuid.UUID(run_id),),
        ).fetchone()
        if row is not None:
            return {"id": row[0], "pid_ref": row[1], "task_id": row[3], "attempt_id": row[4]}
        if worker.poll() is not None:
            err = worker.stderr.read().decode() if worker.stderr else ""
            raise AssertionError(f"воркер завершился до старта CLI: {err[-2000:]}")
        time.sleep(0.1)
    raise AssertionError("исполнение не перешло в RUNNING")


def _alive(pid_ref: str) -> bool:
    pid = int(pid_ref.split(":")[1])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # зомби (уже завершён, но не собран родителем) — не «жив»
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split(") ")[-1].startswith("Z"))


async def test_worker_killed_while_cli_running_is_recovered(world: Slice, database: testdb.TestDatabase) -> None:
    await install_both(world.worker)
    run_id = await create_run(world)
    worker = _spawn_worker(world, database, run_id, scenario="timeout")  # CLI «думает» час
    store = FileSystemArtifactStore(world.tmp / "artifacts")
    reconciler = Reconciler(
        engine=world.worker,
        artifacts=store,
        worktrees=WorktreeManager(world.tmp / "wt"),
        repository_roots=world.settings.repository_roots,
        worker_host_id=None,  # исполнитель теста не регистрирует хост
        runners=(LocalProcessRunner(),),
    )
    with orch.connect_admin(database) as admin:
        try:
            execution = _wait_running_execution(admin, run_id, worker)
            pid_ref = str(execution["pid_ref"])
            assert _alive(pid_ref)

            # 1. Воркер жив и держит аренду: reconciler не имеет права трогать исполнение.
            report = await reconciler.reconcile()
            assert execution["id"] not in report.crashed_executions and not report.reaped_tasks
            assert admin.execute("SELECT status FROM cli_executions WHERE id = %s", (execution["id"],)).fetchone() == (
                "RUNNING",
            )

            # 2. SIGKILL воркера (без finally/cleanup) — CLI остаётся сиротой.
            worker.send_signal(signal.SIGKILL)
            worker.wait(timeout=10)
            assert _alive(pid_ref), "процесс CLI должен пережить воркер (иначе сценарий не проверяет reconciler)"
            # Аренда истекает (вместо ожидания TTL сдвигаем lease_until в прошлое).
            admin.execute(
                "UPDATE worker_leases SET acquired_at = now() - interval '1 minute', lease_until = now() - interval '1 second'"
                " WHERE resource_type = 'task' AND resource_id = %s AND released_at IS NULL",
                (execution["task_id"],),
            )

            report = await reconciler.reconcile()
        finally:
            if worker.poll() is None:
                worker.kill()

        assert report.reaped_tasks == [execution["task_id"]] and not report.deferred_tasks
        assert execution["id"] in report.crashed_executions
        for _ in range(50):
            if not _alive(pid_ref):
                break
            await asyncio.sleep(0.05)
        assert not _alive(pid_ref), "осиротевший CLI убит до перевода задачи"
        assert admin.execute(
            "SELECT status, failure_category, finished_at IS NOT NULL FROM cli_executions WHERE id = %s",
            (execution["id"],),
        ).fetchone() == ("CRASHED", "PROCESS_FAILED", True)
        assert admin.execute(
            "SELECT status, failure_category FROM task_attempts WHERE id = %s", (execution["attempt_id"],)
        ).fetchone() == ("FAILED", "LEASE_LOST")
        task = admin.execute(
            "SELECT status, attempt_count FROM tasks WHERE id = %s", (execution["task_id"],)
        ).fetchone()
        assert task is not None and task[0] == "RETRY_WAIT" and task[1] == 0, "LEASE_LOST не расходует попытки"
        assert admin.execute(
            "SELECT count(*) FROM worker_leases WHERE resource_id = %s AND released_at IS NULL", (execution["task_id"],)
        ).fetchone() == (0,)
        # «Зомби»-воркер со старым fencing token больше не может записать результат (DATA_FLOW §5).
        old_token = admin.execute(
            "SELECT fencing_token FROM task_attempts WHERE id = %s", (execution["attempt_id"],)
        ).fetchone()
        assert old_token is not None
        async with transaction(world.worker, DbContext(workspace_id=uuid.UUID(world.ws), actor_type="system")) as conn:
            assert not await leases.is_current(conn, "task", execution["task_id"], int(old_token[0]))  # type: ignore[arg-type]
        worktrees = admin.execute("SELECT status FROM worktrees WHERE run_id = %s", (uuid.UUID(run_id),)).fetchall()
        assert worktrees == [("REMOVED",)] and report.removed_worktrees
        assert not any((world.tmp / "wt").iterdir()), "каталог осиротевшего worktree удалён"
        assert admin.execute("SELECT reason FROM checkpoints WHERE run_id = %s", (uuid.UUID(run_id),)).fetchall() == [
            ("plan_approved",)
        ], "checkpoint при запуске утверждённого плана (DATA_FLOW §7)"

    # 3. Новый воркер доводит run до конца: повтор после LEASE_LOST, затем независимое ревью.
    world.scenario["name"] = "agent"
    assert await drive(world, run_id) == "COMPLETED"
    async with transaction(world.worker, DbContext(workspace_id=uuid.UUID(world.ws), actor_type="system")) as conn:
        attempts = (
            await conn.execute(
                text(
                    "SELECT a.status, a.failure_category FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
                    " WHERE t.run_id = :r AND t.task_key = 'main' ORDER BY a.attempt_no"
                ),
                {"r": uuid.UUID(run_id)},
            )
        ).all()
    assert [tuple(a) for a in attempts] == [("FAILED", "LEASE_LOST"), ("SUCCEEDED", None)]


async def test_reaper_defers_when_execution_cannot_be_confirmed_dead(
    world: Slice, database: testdb.TestDatabase
) -> None:
    """Исполнение на «чужом» хосте: reaper не переводит задачу, пока процесс не подтверждён мёртвым."""
    await install_cli(world.worker)
    with orch.connect_admin(database) as admin:
        tenant = orch.seed_tenant(admin)
        run = orch.new_run(admin, tenant)
        orch.drive_run(admin, run, "DISCOVERY", "PLANNED", "RUNNING")
        task = orch.new_task(admin, tenant, run)
        orch.lease_task(admin, task, token=7)
        attempt = orch.new_attempt(admin, tenant, task, token=7)
        orch.update(admin, "tasks", task, status="LEASED")
        orch.update(admin, "tasks", task, status="RUNNING")
        execution = uuid7()
        admin.execute(
            "INSERT INTO cli_executions (id, workspace_id, task_attempt_id, installation_id, command_profile,"
            " request_hash, timeout_seconds, fencing_token)"
            " SELECT %s, %s, %s, i.id, 'exec', repeat('a', 64), 60, 7 FROM cli_installations i WHERE i.enabled LIMIT 1",
            (execution, tenant.workspace_id, attempt),
        )
        admin.execute(
            "UPDATE cli_executions SET status = 'STARTING', pid_ref = 'other-host:4242:4242' WHERE id = %s",
            (execution,),
        )
        admin.execute("UPDATE cli_executions SET status = 'RUNNING', started_at = now() WHERE id = %s", (execution,))
        admin.execute(  # аренда умершего воркера: истекла секунду назад
            "INSERT INTO worker_leases (id, resource_type, resource_id, workspace_id, worker_id, fencing_token,"
            " acquired_at, heartbeat_at, lease_until) VALUES (%s, 'task', %s, %s, 'other-host:1:x', 7,"
            " now() - interval '1 minute', now() - interval '1 minute', now() - interval '1 second')",
            (uuid7(), task, tenant.workspace_id),
        )
        report = await reap(world.worker, runners=(LocalProcessRunner(),))
        assert task in report.deferred_tasks and task not in report.reaped_tasks
        assert admin.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() == ("RUNNING",)
        assert admin.execute(
            "SELECT count(*) FROM worker_leases WHERE resource_id = %s AND released_at IS NULL", (task,)
        ).fetchone() == (1,), "аренда не освобождена: вторая копия CLI не стартует"
