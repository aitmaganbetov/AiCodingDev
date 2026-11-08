"""Repository Explorer (FR-PRJ-05): дерево и файлы по commit, безопасность путей, лимиты, изоляция workspace."""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_projects.explorer import MAX_FILE_BYTES
from tests.conftest import UserSession, login
from tests.support.contract import validate_response

pytestmark = pytest.mark.db

_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": os.environ["PATH"],
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        check=True,
        env=_ENV,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repo(path: Path) -> tuple[str, str]:
    """Два commit: первый — README, второй — дерево с подкаталогом, бинарным, большим файлом и symlink."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=_ENV)
    (path / "README.md").write_text("v1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "first")
    first = _git(path, "rev-parse", "HEAD")
    (path / "README.md").write_text("v2\nстрока <b>два</b>\n")
    (path / "src" / "pkg").mkdir(parents=True)
    (path / "src" / "pkg" / "mod.py").write_text("print('hi')\n")
    (path / "src" / "app.py").write_text("x = 1\n")
    (path / "logo.bin").write_bytes(b"\x89PNG\x00\x01\x02" * 10)
    (path / "big.txt").write_text("я" * (MAX_FILE_BYTES // 2 + 100))  # 2 байта на символ → больше лимита
    os.symlink("README.md", path / "link")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "second")
    second = _git(path, "rev-parse", "HEAD")
    return first, second


async def _setup(user: UserSession, repo_root: Path) -> tuple[str, dict[str, Any], str, str]:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    repo_dir = repo_root / f"explore-{secrets.token_hex(3)}"
    first, second = _make_repo(repo_dir)
    r = await user.client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"provider": "local", "local_path": str(repo_dir)},
        headers=user.headers(idem=True),
    )
    assert r.status_code == 201, r.text
    base = f"/api/v1/projects/{project['id']}/repositories/{r.json()['id']}"
    return base, r.json(), first, second


async def test_tree_listing_default_commit_and_navigation(user: UserSession, repo_root: Path) -> None:
    base, repo, _first, second = await _setup(user, repo_root)
    r = await user.client.get(f"{base}/tree")
    validate_response("listRepositoryTree", r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commit_sha"] == second and body["path"] == "" and body["repository_id"] == repo["id"]
    names = [(i["name"], i["type"]) for i in body["items"]]
    assert names == [
        ("src", "directory"),
        ("README.md", "file"),
        ("big.txt", "file"),
        ("link", "symlink"),
        ("logo.bin", "file"),
    ]
    assert body["items"][0]["size"] is None and body["items"][1]["size"] == len("v2\nстрока <b>два</b>\n".encode())

    r = await user.client.get(f"{base}/tree", params={"path": "src"})
    validate_response("listRepositoryTree", r)
    assert [(i["path"], i["type"]) for i in r.json()["items"]] == [("src/pkg", "directory"), ("src/app.py", "file")]
    r = await user.client.get(f"{base}/tree", params={"path": "src/pkg/", "commit": "main"})
    assert r.status_code == 200 and [i["path"] for i in r.json()["items"]] == ["src/pkg/mod.py"]


async def test_tree_pagination_is_pinned_to_commit(user: UserSession, repo_root: Path) -> None:
    base, _repo, _first, second = await _setup(user, repo_root)
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = await user.client.get(f"{base}/tree", params=params)
        validate_response("listRepositoryTree", r)
        assert r.status_code == 200 and r.json()["commit_sha"] == second
        seen += [i["name"] for i in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        if not cursor:
            break
    assert seen == ["src", "README.md", "big.txt", "link", "logo.bin"]
    bad = await user.client.get(f"{base}/tree", params={"cursor": "garbage", "path": "src"})
    validate_response("listRepositoryTree", bad)
    assert bad.status_code == 422


async def test_view_file_at_specific_commit(user: UserSession, repo_root: Path) -> None:
    base, _repo, first, second = await _setup(user, repo_root)
    r = await user.client.get(f"{base}/file", params={"path": "README.md", "commit": first})
    validate_response("getRepositoryFile", r)
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "v1\n" and r.json()["commit_sha"] == first and not r.json()["truncated"]
    r = await user.client.get(f"{base}/file", params={"path": "README.md", "commit": second[:10]})
    validate_response("getRepositoryFile", r)
    body = r.json()
    assert body["commit_sha"] == second and body["content"] == "v2\nстрока <b>два</b>\n"
    assert body["encoding"] == "utf-8" and body["is_binary"] is False and body["type"] == "file"
    # Файл отсутствует в первом commit.
    r = await user.client.get(f"{base}/file", params={"path": "src/app.py", "commit": first})
    validate_response("getRepositoryFile", r)
    assert r.status_code == 404
    r = await user.client.get(f"{base}/tree", params={"commit": first})
    assert [i["name"] for i in r.json()["items"]] == ["README.md"]


async def test_binary_large_and_symlink_files(user: UserSession, repo_root: Path) -> None:
    base, _repo, _first, _second = await _setup(user, repo_root)
    r = await user.client.get(f"{base}/file", params={"path": "logo.bin"})
    validate_response("getRepositoryFile", r)
    assert r.json()["is_binary"] is True and r.json()["content"] is None and r.json()["size"] == 70

    r = await user.client.get(f"{base}/file", params={"path": "big.txt"})
    validate_response("getRepositoryFile", r)
    body = r.json()
    assert body["truncated"] is True and body["is_binary"] is False and body["max_bytes"] == MAX_FILE_BYTES
    assert body["size"] == 2 * (MAX_FILE_BYTES // 2 + 100)
    assert set(body["content"]) == {"я"} and len(body["content"].encode()) <= MAX_FILE_BYTES

    r = await user.client.get(f"{base}/file", params={"path": "link"})
    validate_response("getRepositoryFile", r)
    assert r.json()["type"] == "symlink" and r.json()["content"] == "README.md"

    r = await user.client.get(f"{base}/file", params={"path": "src"})
    validate_response("getRepositoryFile", r)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "not_a_file"
    r = await user.client.get(f"{base}/tree", params={"path": "README.md"})
    validate_response("listRepositoryTree", r)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "not_a_directory"


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd",
        "/etc/passwd",
        "src/../README.md",
        "./README.md",
        ".git/config",
        "src/.GIT/x",
        "a//b",
        "a\\b",
        "x\x01",
    ],
)
async def test_unsafe_paths_rejected(user: UserSession, repo_root: Path, path: str) -> None:
    base, _repo, _first, _second = await _setup(user, repo_root)
    for op, url in (("getRepositoryFile", f"{base}/file"), ("listRepositoryTree", f"{base}/tree")):
        r = await user.client.get(url, params={"path": path})
        validate_response(op, r)
        assert r.status_code == 422, (op, path, r.text)
        assert str(repo_root) not in r.text


@pytest.mark.parametrize(
    ("commit", "status"),
    [("--output=/tmp/x", 422), ("HEAD~1", 422), ("main@{0}", 422), ("deadbeefdeadbeef", 404), ("nosuchbranch", 404)],
)
async def test_commit_validation(user: UserSession, repo_root: Path, commit: str, status: int) -> None:
    base, _repo, _first, _second = await _setup(user, repo_root)
    r = await user.client.get(f"{base}/tree", params={"commit": commit})
    validate_response("listRepositoryTree", r)
    assert r.status_code == status, r.text


async def test_workspace_isolation(app: FastAPI, app_engine: AsyncEngine, user: UserSession, repo_root: Path) -> None:
    base, _repo, _first, _second = await _setup(user, repo_root)
    other = await login(app, app_engine)
    try:
        r = await other.client.get(f"{base}/tree")
        validate_response("listRepositoryTree", r)
        assert r.status_code in (403, 404)
        r = await other.client.get(f"{base}/file", params={"path": "README.md"})
        assert r.status_code in (403, 404) and "v2" not in r.text
        # Репозиторий другого проекта того же пользователя под чужим project_id — 404.
        ws = await user.create_workspace()
        foreign_project = await user.create_project(ws["id"])
        repo_id = base.rsplit("/", 1)[-1]
        r = await user.client.get(f"/api/v1/projects/{foreign_project['id']}/repositories/{repo_id}/tree")
        validate_response("listRepositoryTree", r)
        assert r.status_code == 404
    finally:
        await other.client.aclose()
