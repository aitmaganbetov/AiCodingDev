"""ST-CLI-02 / ST-INJ-07 / ST-SBX-11 (ТЗ §20 «command injection», SC-CLI-01/02, SC-INJ-07, SC-SBX-11; TH-06, TH-07, TH-11).

* Недоверенный текст задачи (`objective`) с `$(…)`, `;`, переводами строк и `--флагами` доходит до CLI только
  через файл/stdin — настоящий процесс Fake CLI не получает его как аргумент и не исполняет shell.
* Имена веток и SHA из недоверенных источников проверяются до вызова git (`--upload-pack=…`, `-o`, `$(…)`).
* Платформенный git над недоверенным репозиторием не исполняет `core.fsmonitor`, hooks, `core.sshCommand`
  и filter-драйверы `.gitattributes`, записанные агентом в репозиторий/worktree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import worktree_resolver
from aicd_platform.ids import uuid7
from aicd_security import git as git_guard
from aicd_security.local_runner import LocalProcessRunner
from tests.conftest import init_git_repo
from tests.support.cli import MemorySink, make_request

pytestmark = pytest.mark.security


@pytest.mark.parametrize(
    "objective",
    [
        "$(touch {marker})",
        "`touch {marker}`",
        "ok; touch {marker}",
        "ok && touch {marker} || true",
        "--scenario=env_dump\ntouch {marker}",
        "--dangerously-bypass-approvals-and-sandbox $(touch {marker})",
    ],
)
async def test_objective_never_reaches_argv_or_shell(tmp_path: Path, objective: str) -> None:
    marker = tmp_path / "pwned"
    wid = uuid7()
    (tmp_path / str(wid)).mkdir()
    adapter = FakeCLIExecutor(
        scenario="success_diff",
        runner=LocalProcessRunner(),
        artifact_sink=MemorySink(),
        worktree_path=worktree_resolver(tmp_path),
        env_source={"PATH": os.environ["PATH"]},
    )
    request = make_request(worktree_id=wid, objective=objective.format(marker=marker))
    handle = await adapter.execute(request)
    events = [e async for e in adapter.stream_events(handle.execution_id)]
    result = await adapter.collect_result(handle.execution_id)
    assert not marker.exists(), "objective исполнен shell"
    # Сценарий не подменён «флагом» из objective: процесс отработал success_diff и записал objective как данные.
    assert result.status == "completed" and result.exit_code == 0
    written = (tmp_path / str(wid) / "FAKE_CHANGE.md").read_text(encoding="utf-8")
    assert objective.format(marker=marker) in written
    assert not any("PATH" in str(e.payload.get("text", "")) for e in events), "env_dump не выполнялся"


@pytest.mark.parametrize(
    "name",
    [
        "--upload-pack=touch /tmp/x",
        "-o",
        "-bad",
        "main;rm -rf /",
        "$(id)",
        "`id`",
        "a b",
        "a\nb",
        "a..b",
        "x@{1}",
        "refs/heads/../../etc",
        "branch.lock",
        "trailing/",
        "",
    ],
)
def test_untrusted_branch_names_rejected(name: str) -> None:
    with pytest.raises(git_guard.GitRepositoryError):
        git_guard.validate_branch_name(name)


def test_valid_branch_names_pass() -> None:
    for name in ("main", "aicd/0190f5a0/task-1", "release-1.2", "feature/x_y"):
        assert git_guard.validate_branch_name(name) == name


def _poison(repo: Path, marker: Path) -> None:
    """Агент записал в репозиторий исполняемую git-конфигурацию (TH-11, AB-8)."""
    cmd = f"touch {marker}"
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(
            f'[core]\n\tfsmonitor = "{cmd}"\n\tsshCommand = "{cmd}"\n\thooksPath = .githooks\n'
            f'[filter "evil"]\n\tclean = {cmd}\n\tsmudge = {cmd}\n\trequired = true\n'
            f'[diff "evil"]\n\ttextconv = {cmd}\n'
        )
    hooks = repo / ".githooks"
    hooks.mkdir()
    for hook in ("post-checkout", "pre-commit", "post-index-change", "reference-transaction"):
        script = hooks / hook
        script.write_text(f"#!/bin/sh\n{cmd}\n", encoding="utf-8")
        script.chmod(0o755)
        (repo / ".git" / "hooks" / hook).write_text(f"#!/bin/sh\n{cmd}\n", encoding="utf-8")
        (repo / ".git" / "hooks" / hook).chmod(0o755)
    (repo / ".gitattributes").write_text("* filter=evil diff=evil\n", encoding="utf-8")
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")


def test_platform_git_ignores_malicious_repository_config(tmp_path: Path) -> None:
    """Defence in depth: даже при отравленном `.git/config` (агенту недоступен — `.git` ro, SC-SBX-02) платформенный
    git не исполняет fsmonitor/hooks/sshCommand, а filter/textconv-драйверы не выбираются (атрибуты — из пустого
    дерева, `GIT_ATTR_SOURCE`)."""
    repo = tmp_path / "repo"
    init_git_repo(repo)
    marker = tmp_path / "pwned"
    _poison(repo, marker)
    info = git_guard.inspect_repository(repo)
    assert info.head_sha
    git_guard.run_git(repo, "status", "--porcelain")
    git_guard.run_git(repo, "diff", "HEAD")
    git_guard.run_git(repo, "add", "--all")
    git_guard.run_git(repo, "log", "-1", "--format=%H")
    git_guard.run_git_bytes(repo, "cat-file", "-p", "HEAD:README.md", max_output_bytes=1024)
    assert not marker.exists(), "платформенный git исполнил конфигурацию из недоверенного репозитория"
    # Контроль: «обычный» git с той же конфигурацией её исполняет — фикстура действительно вредоносна.
    subprocess.run(["git", "-C", str(repo), "status"], capture_output=True, check=False)
    assert marker.exists(), "фикстура не воспроизводит атаку"


def test_worktree_gitlink_redirect_is_refused(tmp_path: Path) -> None:
    """TH-11: агент пишет в worktree (rw) `.git` → `gitdir: <свой каталог>` с вредоносной конфигурацией; платформа
    отказывается выполнять git в таком worktree (diff/commit/очистка), код агента не исполняется на хосте."""
    from aicd_orchestration.worktrees import WorktreeError, WorktreeManager

    repo = tmp_path / "repo"
    init_git_repo(repo)
    base = git_guard.run_git(repo, "rev-parse", "HEAD")
    manager = WorktreeManager(tmp_path / "worktrees")
    info = manager.create(repo, worktree_id=uuid7(), run_id=uuid7(), task_id=None, base_commit_sha=base)
    assert manager.diff(info.path, base) is not None  # исходный gitlink принимается
    marker = tmp_path / "pwned"
    evil = info.path / "evil-gitdir"
    subprocess.run(["git", "init", "-q", "--bare", str(evil)], check=True, capture_output=True)
    (evil / "worktrees").mkdir()
    with (evil / "config").open("a", encoding="utf-8") as fh:
        fh.write(f'[filter "evil"]\n\tclean = touch {marker}\n\trequired = true\n')
    (evil / "info").mkdir(exist_ok=True)
    (evil / "info" / "attributes").write_text("* filter=evil\n", encoding="utf-8")
    (info.path / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    (info.path / ".git").write_text(f"gitdir: {evil}\n", encoding="utf-8")
    for action in (lambda: manager.diff(info.path, base), lambda: manager.commit_snapshot(info.path, "x"),
                   lambda: manager.clear_agent_io(info.path)):  # fmt: skip
        with pytest.raises(WorktreeError, match="gitlink"):
            action()
    # Симлинк вместо файла и ссылка на «чужой» worktree того же корня — тоже отказ.
    (info.path / ".git").unlink()
    (info.path / ".git").symlink_to(evil)
    with pytest.raises(WorktreeError, match="gitlink"):
        manager.diff(info.path, base)
    assert not marker.exists()


def test_worktree_gitattributes_filters_do_not_run(tmp_path: Path) -> None:
    """`.gitattributes` агента в worktree ссылается на драйвер, определённый в конфигурации репозитория
    пользователя (например, LFS-подобный) — платформенные diff/commit его не вызывают."""
    from aicd_orchestration.worktrees import WorktreeManager

    repo = tmp_path / "repo"
    init_git_repo(repo)
    marker = tmp_path / "pwned"
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(f'[filter "user"]\n\tclean = touch {marker}\n[diff "user"]\n\ttextconv = touch {marker}\n')
    base = git_guard.run_git(repo, "rev-parse", "HEAD")
    manager = WorktreeManager(tmp_path / "worktrees")
    info = manager.create(repo, worktree_id=uuid7(), run_id=uuid7(), task_id=None, base_commit_sha=base)
    (info.path / ".gitattributes").write_text("* filter=user diff=user\n", encoding="utf-8")
    (info.path / "README.md").write_text("changed by agent\n", encoding="utf-8")
    diff = manager.diff(info.path, base)
    assert b"changed by agent" in diff
    assert manager.commit_snapshot(info.path, "snapshot")
    assert not marker.exists()


def test_git_argument_values_are_not_shell_interpreted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    marker = tmp_path / "pwned"
    with pytest.raises(git_guard.GitRepositoryError):
        git_guard.run_git(repo, "rev-parse", "--verify", f"$(touch {marker})")
    git_guard.run_git(repo, "log", "--format=%H", "-1", "--", f"; touch {marker}")
    assert not marker.exists()
