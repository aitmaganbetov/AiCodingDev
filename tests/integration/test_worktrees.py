"""P2-09: worktree на задачу, diff от платформы, изоляция путей."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aicd_orchestration.worktrees import WorktreeError, WorktreeManager
from aicd_platform.ids import uuid7
from tests.conftest import init_git_repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_create_diff_commit_remove(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    # Вредоносный хук в репозитории не должен исполняться платформой.
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text('#!/bin/sh\ntouch "$GIT_DIR/../HOOK_RAN"\n')
    hook.chmod(0o755)
    base = _head(repo)
    mgr = WorktreeManager(tmp_path / "worktrees")
    run_id, task_id = uuid7(), uuid7()
    info = mgr.create(repo, worktree_id=uuid7(), run_id=run_id, task_id=task_id, base_commit_sha=base)
    assert info.branch_name == f"aicd/{run_id.hex[-12:]}/{task_id.hex[-12:]}-{info.worktree_id.hex[-8:]}"
    assert (info.path / "README.md").exists() and not (repo / "HOOK_RAN").exists()
    (info.path / "new.txt").write_text("hello\n")
    (info.path / "README.md").write_text("changed\n")
    diff = mgr.diff(info.path, base)
    assert b"new.txt" in diff and b"+changed" in diff
    sha = mgr.commit_snapshot(info.path, "WIP: safe point")
    assert sha and len(sha) == 40 and _head(repo) == base, "основная ветка не меняется"
    assert mgr.commit_snapshot(info.path, "no changes") is None
    assert mgr.find_orphans(set()) == [info.worktree_id]
    assert mgr.find_orphans({info.worktree_id}) == []
    mgr.remove(repo, info.path)
    assert not info.path.exists()


@pytest.mark.parametrize("base", ["main", "deadbeef", "0" * 40])
def test_base_commit_must_be_existing_full_sha(tmp_path: Path, base: str) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    with pytest.raises(WorktreeError):
        WorktreeManager(tmp_path / "wt").create(
            repo, worktree_id=uuid7(), run_id=uuid7(), task_id=None, base_commit_sha=base
        )


def test_paths_outside_root_rejected(tmp_path: Path) -> None:
    mgr = WorktreeManager(tmp_path / "wt")
    with pytest.raises(WorktreeError):
        mgr.diff(tmp_path, "a" * 40)


def test_root_inside_repo_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    with pytest.raises(WorktreeError):
        WorktreeManager(repo / "wt").create(
            repo, worktree_id=uuid7(), run_id=uuid7(), task_id=None, base_commit_sha=_head(repo)
        )
