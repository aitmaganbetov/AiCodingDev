"""AC-04 (P9-01): Codex реализует изменение в отдельном worktree задачи; Claude выполняет независимое ревью.

Политика L3 (профиль strict) требует независимого ревью, оба провайдера доступны. Роли закреплены настройками
провайдеров workspace (codex — developer, claude — reviewer), чтобы сценарий «Codex реализует → Claude
ревьюирует» был детерминирован. Проверяется:
* изменения только в worktree задачи: ветка `main` и рабочая копия исходного репозитория не тронуты, коммит
  разработчика лежит в ветке `aicd/...` worktree, файлы изменения отсутствуют в рабочей копии репозитория;
* ревью назначено Claude, маркировка независимости — `independent` (≠ провайдер автора изменения).
Ветка «Claude недоступен → fallback, independent=false» — tests/e2e/test_routing_review.py
(`human_review` при strict, `self_review` после явного SELF_REVIEW_FALLBACK при basic).
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from aicd_security.paths import resolve_ref
from tests.acceptance.support import rows
from tests.support.phase3 import Slice, create_run, drive, install_both, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}
    ).stdout.strip()


async def _restrict_roles(s: Slice, code: str, roles: list[str]) -> None:
    listed = (await s.user.client.get("/api/v1/providers", headers={"X-Workspace-Id": s.ws})).json()["items"]
    p = next(x for x in listed if x["code"] == code)
    version = p["workspace_settings"]["settings_version"]
    r = await s.user.client.patch(
        f"/api/v1/providers/{p['provider_id']}/settings",
        json={"allowed_roles": roles},
        headers=s.user.headers(s.ws, if_match=f'"v{version}"'),
    )
    assert r.status_code == 200, r.text


async def test_ac04_codex_implements_in_task_worktree_and_claude_reviews_independently(world: Slice) -> None:
    await install_both(world.worker)
    await _restrict_roles(world, "codex", ["developer"])
    await _restrict_roles(world, "claude", ["reviewer"])
    (repo_row,) = await rows(
        world, "SELECT local_root_ref FROM repositories WHERE project_id = :p", p=uuid.UUID(world.project)
    )
    repo = resolve_ref(repo_row["local_root_ref"], world.settings.repository_roots)
    main_before = _git(repo, "rev-parse", "refs/heads/main")
    tracked_before = set(_git(repo, "ls-files").splitlines())
    assert _git(repo, "status", "--porcelain") == ""

    run_id = await create_run(world)
    assert await drive(world, run_id) == "COMPLETED"

    attempts = {
        a["task_key"]: a
        for a in await rows(
            world,
            "SELECT t.task_key, t.agent_role, a.provider FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
            " WHERE t.run_id = :r AND a.status = 'SUCCEEDED'",
            r=uuid.UUID(run_id),
        )
    }
    assert attempts["main"]["agent_role"] == "developer" and attempts["main"]["provider"] == "codex"
    assert attempts["review"]["agent_role"] == "reviewer" and attempts["review"]["provider"] == "claude"
    review: list[dict[str, Any]] = await rows(
        world,
        "SELECT d.selected_provider, d.complexity_level, d.score_breakdown FROM routing_decisions d"
        " JOIN tasks t ON t.id = d.task_id WHERE t.run_id = :r AND t.task_key = 'review'",
        r=uuid.UUID(run_id),
    )
    assert review[-1]["selected_provider"] == "claude" and review[-1]["complexity_level"] >= 3
    assert review[-1]["score_breakdown"]["review_independence"] == "independent", "evidence независимости ревью"

    # Изменения — только в worktree задачи.
    worktrees = await rows(
        world,
        "SELECT w.branch_name, w.base_commit_sha, w.head_commit_sha, w.path_ref, t.task_key FROM worktrees w"
        " JOIN tasks t ON t.id = w.task_id WHERE w.run_id = :r",
        r=uuid.UUID(run_id),
    )
    developer = next(w for w in worktrees if w["task_key"] == "main")
    assert developer["branch_name"].startswith("aicd/") and developer["head_commit_sha"] != developer["base_commit_sha"]
    assert _git(repo, "rev-parse", "refs/heads/main") == main_before, "ветка main исходного репозитория не изменена"
    assert _git(repo, "status", "--porcelain") == "", "рабочая копия исходного репозитория не изменена"
    assert set(_git(repo, "ls-files").splitlines()) == tracked_before
    changed = _git(repo, "diff", "--name-only", developer["base_commit_sha"], developer["head_commit_sha"]).splitlines()
    assert changed, "коммит разработчика содержит изменения"
    assert all(not (repo / f).exists() for f in changed if f not in tracked_before), (
        "новые файлы изменения существуют только в worktree/коммите задачи"
    )
    assert (
        _git(repo, "branch", "--contains", developer["head_commit_sha"], "--format=%(refname:short)")
        == (developer["branch_name"])
    ), "коммит изменения достижим только из ветки worktree задачи"
