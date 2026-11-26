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


def test_next_attempt_continues_the_previous_one_and_diff_still_covers_the_whole_task(tmp_path: Path) -> None:
    """Дерево следующей попытки создаётся от коммита предыдущей; база для diff и проверок — прежняя."""
    repo = tmp_path / "repo"
    init_git_repo(repo)
    base = _head(repo)
    mgr = WorktreeManager(tmp_path / "worktrees")
    run_id, task_id = uuid7(), uuid7()
    first = mgr.create(repo, worktree_id=uuid7(), run_id=run_id, task_id=task_id, base_commit_sha=base)
    (first.path / "feature.txt").write_text("сделано первой попыткой\n")
    done = mgr.commit_snapshot(first.path, "attempt 1")
    assert done is not None

    second = mgr.create(
        repo, worktree_id=uuid7(), run_id=run_id, task_id=task_id, base_commit_sha=base, start_commit_sha=done
    )
    assert second.base_commit_sha == base, "база не сдвинулась: проверяется всё изменение задачи"
    assert (second.path / "feature.txt").read_text() == "сделано первой попыткой\n", (
        "работа предыдущей попытки на месте"
    )
    assert mgr.commit_snapshot(second.path, "nothing new") is None
    (second.path / "fix.txt").write_text("исправление\n")
    diff = mgr.diff(second.path, base)
    assert b"feature.txt" in diff and b"fix.txt" in diff
    assert _head(repo) == base, "основная ветка не меняется"


def test_resume_commit_must_descend_from_the_base(tmp_path: Path) -> None:
    """Чужой коммит (не потомок базы run) продолжением быть не может: diff от базы потерял бы смысл."""
    repo = tmp_path / "repo"
    init_git_repo(repo)
    base = _head(repo)
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo), "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--orphan", "other"], check=True, env=env)
    (repo / "x.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "unrelated"],
        check=True,
        env=env,
    )
    unrelated = _head(repo)
    mgr = WorktreeManager(tmp_path / "wt")
    for bad in (unrelated, "deadbeef", "0" * 40):
        with pytest.raises(WorktreeError):
            mgr.create(
                repo, worktree_id=uuid7(), run_id=uuid7(), task_id=None, base_commit_sha=base, start_commit_sha=bad
            )


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
