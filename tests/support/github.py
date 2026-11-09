"""Фабрики для тестов GitHub-интеграции (P8): установка, GitHub-репозиторий, завершённый run с проверенным
коммитом в локальной копии и на «GitHub» (двойник `github_double`)."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

from aicd_integrations_github.permissions import APP_PERMISSIONS
from aicd_platform.ids import uuid7
from tests.support import quality as q
from tests.support.github_double import GitHubDouble, git
from tests.support.orchestration import Tenant, drive_run, lease_task, new_run, new_task, update

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"]}


def init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    env = {**GIT_ENV, "HOME": str(path)}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    (path / "README.md").write_text("base\n")
    git(path, "add", ".")
    git(path, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
    return git(path, "rev-parse", "HEAD")


def commit_on_branch(path: Path, branch: str, files: dict[str, str], base: str = "main") -> str:
    git(path, "branch", branch, base)
    tmp = path.parent / f".wt-{uuid.uuid4().hex[:8]}"
    git(path, "worktree", "add", "-q", str(tmp), branch)
    try:
        for name, content in files.items():
            target = tmp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(tmp, "add", ".")
        git(tmp, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", f"change {branch}")
        return git(tmp, "rev-parse", "HEAD")
    finally:
        git(path, "worktree", "remove", "--force", str(tmp))


def installation(
    conn: psycopg.Connection, tenant: Tenant, installation_id: int, account: str, status: str = "ACTIVE"
) -> uuid.UUID:
    row = uuid7()
    conn.execute(
        "INSERT INTO github_installations (id, workspace_id, installation_id, account_login, account_type,"
        " permissions, status, installed_by) VALUES (%s, %s, %s, %s, 'organization', %s, %s, %s)",
        (
            row,
            tenant.workspace_id,
            installation_id,
            account,
            json.dumps(dict(APP_PERMISSIONS)),
            status,
            tenant.owner_id,
        ),
    )
    return row


def github_repository(
    conn: psycopg.Connection,
    tenant: Tenant,
    *,
    installation_row: uuid.UUID,
    full_name: str,
    repo_id: int,
    local_ref: str,
) -> uuid.UUID:
    row = uuid7()
    conn.execute(
        "INSERT INTO repositories (id, workspace_id, project_id, provider, remote_url, default_branch, local_root_ref,"
        " github_installation_id, github_repo_id, full_name) VALUES (%s, %s, %s, 'github', %s, 'main', %s, %s, %s, %s)",
        (row, tenant.workspace_id, tenant.project_id, f"https://github.test/{full_name}", local_ref, installation_row,
         repo_id, full_name),
    )  # fmt: skip
    return row


@dataclass(frozen=True)
class CompletedRun:
    tenant: Tenant
    run: uuid.UUID
    task: uuid.UUID
    branch: str
    base_sha: str
    head_sha: str


def completed_run(
    conn: psycopg.Connection,
    tenant: Tenant,
    *,
    repository_id: uuid.UUID,
    local: Path,
    base_sha: str,
    files: dict[str, str],
) -> CompletedRun:
    """COMPLETED run: задача SUCCEEDED, worktree с коммитом `files` в локальной копии и PASS gate на нём."""
    t = dataclasses.replace(tenant, repository_id=repository_id)
    run = new_run(conn, t)
    update(conn, "runs", run, base_commit_sha=base_sha)
    drive_run(conn, run, "DISCOVERY", "PLANNED")
    update(conn, "runs", run, approved_plan_version=1)
    drive_run(conn, run, "RUNNING")
    task = new_task(conn, t, run)
    worktree = uuid7()
    branch = f"aicd/{run.hex[-12:]}/{task.hex[-12:]}-{worktree.hex[-8:]}"
    head = commit_on_branch(local, branch, files)
    conn.execute(
        "INSERT INTO worktrees (id, workspace_id, repository_id, run_id, task_id, branch_name, base_commit_sha,"
        " head_commit_sha, path_ref, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'LOCKED')",
        (worktree, t.workspace_id, repository_id, run, task, branch, base_sha, head, str(worktree)),
    )
    lease_task(conn, task)
    update(conn, "tasks", task, status="RUNNING")
    update(conn, "tasks", task, status="VERIFYING")
    gate = q.ensure_gate(conn, "ruff-lint", "LINT")
    ev = q.evidence(conn, t, run, head)
    q.gate_execution(conn, t, run, gate, "LINT", "PASS", head, evidence_id=ev, task=task)
    update(conn, "tasks", task, status="SUCCEEDED", finished_at="now")
    drive_run(conn, run, "VERIFYING")
    update(conn, "runs", run, status="COMPLETED", finished_at="now")
    return CompletedRun(t, run, task, branch, base_sha, head)


def double_with_repo(
    root: Path, local: Path, *, installation_id: int = 77, account: str = "acme", name: str = "app", repo_id: int = 1001
) -> tuple[GitHubDouble, str]:
    gh = GitHubDouble(root)
    url = gh.start()
    gh.add_installation(installation_id, account)
    gh.add_repo(installation_id, account, name, repo_id, local)
    return gh, url
