"""Согласованная работа попыток задачи: следующая продолжает сделанное и знает, почему итог не принят.

До этого каждая попытка получала чистое дерево от базового коммита run и промпт без единого слова о
предыдущей: ту же работу выполняли заново — в том числе другой провайдер, — а упавший gate до агента не
доходил. Сценарий — реальные gates на Fake CLI: синтетический ключ в изменении трижды проваливает проверку.
"""

from __future__ import annotations

import itertools
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from aicd_orchestration import coordination
from aicd_platform.db import DbContext, transaction
from tests.support.phase3 import Slice, create_run, drive, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant], check=False
        ).returncode
        == 0
    )


async def test_retry_continues_previous_attempt_and_is_told_why_it_failed(world: Slice) -> None:
    await install_both(world.worker)
    run_id = uuid.UUID(await create_run(world, objective="AKIAABCDEFGHIJKLMNOP"))
    assert await drive(world, str(run_id)) == "FAILED"

    async with transaction(world.worker, DbContext(workspace_id=uuid.UUID(world.ws), actor_type="system")) as conn:
        task_id = (
            await conn.execute(text("SELECT id FROM tasks WHERE run_id = :r AND task_key = 'main'"), {"r": run_id})
        ).scalar_one()
        attempts = (
            await conn.execute(
                text("SELECT attempt_no, provider, failure_category FROM task_attempts WHERE task_id = :t ORDER BY 1"),
                {"t": task_id},
            )
        ).all()
        trees = (
            await conn.execute(
                text(
                    "SELECT w.base_commit_sha, w.head_commit_sha, r.local_root_ref FROM worktrees w"
                    " JOIN repositories r ON r.id = w.repository_id WHERE w.task_id = :t ORDER BY w.created_at, w.id"
                ),
                {"t": task_id},
            )
        ).all()
        prompts = (
            await conn.execute(
                text(
                    "SELECT payload FROM agent_activity_events WHERE task_id = :t"
                    " AND event_type = 'agent.prompt_assembled' ORDER BY id"
                ),
                {"t": task_id},
            )
        ).all()
        history = await coordination.previous_attempts(conn, task_id)

    assert [a.failure_category for a in attempts] == ["GATE_FAILED"] * 3
    assert len({a.provider for a in attempts}) == 1, "задачу ведёт один провайдер — второй её не повторяет"

    # Каждое следующее дерево начинается с коммита предыдущей попытки, а база для diff и gates — база run.
    assert len(trees) == 3 and len({t.base_commit_sha for t in trees}) == 1
    repo = world.settings.repository_roots["main"] / str(trees[0].local_root_ref).split("/", 1)[1]
    first = trees[0].head_commit_sha
    assert first and first != trees[0].base_commit_sha, "первая попытка оставила изменение"
    for earlier, later in itertools.pairwise(trees):
        assert _is_ancestor(repo, earlier.head_commit_sha, later.head_commit_sha), "работа не выброшена"

    # Промпт первой попытки о предыдущих молчит; вторая и третья получают раздел coordination.
    refs = [[f["ref"] for f in p.payload["fragments"] if f["kind"] == "coordination"] for p in prompts]
    assert "coordination:previous_attempts" not in refs[0]
    assert all("coordination:previous_attempts" in r for r in refs[1:]), refs

    # В справке — кто делал, каким коммитом закончил и какой gate не прошёл.
    assert [h.attempt_no for h in history] == [1, 2, 3]
    assert history[0].commit_sha == first and history[0].failure_category == "GATE_FAILED"
    assert history[0].failed_gates, "причина отказа доходит до следующей попытки"
    assert coordination.resume_point(history) == trees[-1].head_commit_sha
