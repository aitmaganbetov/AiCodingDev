"""GitHub App без БД (P8-02, P8-03, P8-09): JWT RS256, installation token с минимальными правами, подпись
вебхука, state установки, безопасный git push. Внешний GitHub — локальный двойник `tests.support.github_double`."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from aicd_cli_adapters.profiles import FORBIDDEN_ENV
from aicd_integrations_github import install_state, permissions, webhooks
from aicd_integrations_github.api import AppJwtSigner, AppKeyError, GitHubClient, GitHubError, RepoRef, parse_remote_url
from aicd_security import git as git_guard
from tests.conftest import init_git_repo
from tests.support.github_double import GitHubDouble, git

SECRET = b"webhook-secret-current"
OLD_SECRET = b"webhook-secret-previous"


@pytest.fixture(scope="module")
def double(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[GitHubDouble, str, Path]]:
    root = tmp_path_factory.mktemp("gh-unit")
    src = root / "src"
    init_git_repo(src)
    gh = GitHubDouble(root / "remote")
    url = gh.start()
    gh.add_installation(77, "acme")
    gh.add_repo(77, "acme", "app", 1001, src)
    gh.add_installation(78, "other")
    gh.add_repo(78, "other", "secret", 2002, src)
    yield gh, url, src
    gh.stop()


@pytest.fixture
async def client(double: tuple[GitHubDouble, str, Path]) -> AsyncIterator[GitHubClient]:
    gh, url, _ = double
    c = GitHubClient(api_url=url, signer=AppJwtSigner(gh.app_id, gh.private_key_pem))
    yield c
    await c.aclose()


# --- JWT App -------------------------------------------------------------------------------------------


def test_jwt_is_rs256_short_lived_and_verifiable(double: tuple[GitHubDouble, str, Path]) -> None:
    gh, _, _ = double
    token = AppJwtSigner(gh.app_id, gh.private_key_pem).issue(now=1_000_000)
    header_b64, payload_b64, _ = token.split(".")
    header = json.loads(webhooks_b64(header_b64))
    payload = json.loads(webhooks_b64(payload_b64))
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload["iss"] == str(gh.app_id)
    assert payload["exp"] - payload["iat"] <= 600, "JWT GitHub App живёт не более 10 минут"
    gh._verify_jwt({"authorization": f"Bearer {AppJwtSigner(gh.app_id, gh.private_key_pem).issue()}"})
    assert "key=***" in repr(AppJwtSigner(gh.app_id, gh.private_key_pem))


def webhooks_b64(text: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_signer_rejects_bad_keys() -> None:
    with pytest.raises(AppKeyError):
        AppJwtSigner(1, "not a pem")
    with pytest.raises(AppKeyError):
        AppJwtSigner(0, "x")


async def test_foreign_key_jwt_is_rejected(double: tuple[GitHubDouble, str, Path]) -> None:
    gh, url, _ = double
    other = GitHubDouble(Path("/nonexistent"))
    c = GitHubClient(api_url=url, signer=AppJwtSigner(gh.app_id, other.private_key_pem))
    try:
        with pytest.raises(GitHubError) as err:
            await c.installation(77)
        assert err.value.status == 401
    finally:
        await c.aclose()


# --- installation token: минимальные права и конкретный репозиторий (SC-GH-04, P8-09) ------------------


async def test_installation_token_is_repo_scoped_and_minimal(
    double: tuple[GitHubDouble, str, Path], client: GitHubClient
) -> None:
    gh, _, _ = double
    gh.token_requests.clear()
    operations: tuple[permissions.Operation, ...] = ("read", "push", "pr_create", "pr_comment", "status", "merge")
    for operation in operations:
        token = await client.installation_token(77, operation, repository_ids=[1001])
        assert token.permissions == dict(permissions.TOKEN_SCOPES[operation])
        assert token.value() not in repr(token) and token.value() not in str(token)
    assert len(gh.token_requests) == 5, "pr_comment использует кэшированный токен pr_create (та же область)"
    for request in gh.token_requests:
        assert request["repository_ids"] == [1001], "токен — только на один репозиторий"
        assert set(request["permissions"]) <= set(permissions.APP_PERMISSIONS)
        assert not set(request["permissions"]) & permissions.FORBIDDEN_PERMISSIONS
    writes = {op: {k for k, v in scope.items() if v == "write"} for op, scope in permissions.TOKEN_SCOPES.items()}
    assert writes == {
        "read": set(),
        "push": {"contents"},
        "pr_create": {"pull_requests"},
        "pr_comment": {"pull_requests"},
        "status": {"statuses"},
        "merge": {"contents"},
    }
    # Кэш: повторный запрос той же операции не обращается к GitHub.
    await client.installation_token(77, "push", repository_ids=[1001])
    assert len(gh.token_requests) == 5
    with pytest.raises(ValueError, match="конкретных репозиториев"):
        await client.installation_token(77, "read")


async def test_token_of_one_installation_cannot_reach_other_repo(
    double: tuple[GitHubDouble, str, Path], client: GitHubClient
) -> None:
    with pytest.raises(GitHubError) as err:
        await client.installation_token(77, "read", repository_ids=[2002])
    assert err.value.status == 422
    token = await client.installation_token(77, "read", repository_ids=[1001])
    with pytest.raises(GitHubError) as err:
        await client.repository(token, RepoRef("other", "secret"))
    assert err.value.status == 404


async def test_read_token_cannot_write(double: tuple[GitHubDouble, str, Path], client: GitHubClient) -> None:
    gh, _, _ = double
    token = await client.installation_token(77, "read", repository_ids=[1001])
    with pytest.raises(GitHubError) as err:
        await client.create_pull(token, RepoRef("acme", "app"), title="t", head="main", base="main", body="")
    assert err.value.status == 403
    assert gh.created_pulls == 0


def test_app_permission_policy() -> None:
    assert dict(permissions.APP_PERMISSIONS) == {
        "contents": "write",
        "pull_requests": "write",
        "checks": "read",
        "statuses": "write",
        "metadata": "read",
    }
    assert permissions.excess_permissions(dict(permissions.APP_PERMISSIONS)) == []
    assert permissions.missing_permissions(dict(permissions.APP_PERMISSIONS)) == []
    assert permissions.excess_permissions({**permissions.APP_PERMISSIONS, "workflows": "write"}) == ["workflows:write"]
    assert permissions.excess_permissions({"administration": "read"}) == ["administration:read"]
    assert permissions.excess_permissions({"checks": "write"}) == ["checks:write"]
    assert permissions.missing_permissions({"metadata": "read"}) == [
        "checks:read",
        "contents:write",
        "pull_requests:write",
        "statuses:write",
    ]


def test_agent_cli_environment_rejects_github_tokens() -> None:
    """P8-09: токен GitHub не может попасть в окружение CLI агента (build_env отклоняет такие переменные)."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "AICD_GIT_ASKPASS_TOKEN", "GITHUB_APP_PRIVATE_KEY_SECRET"):
        assert FORBIDDEN_ENV.search(name), name


# --- вебхуки (SC-GH-01) ----------------------------------------------------------------------------------


def test_webhook_signature_checks() -> None:
    body = b'{"action":"opened"}'
    good = webhooks.sign(body, SECRET)
    assert webhooks.signature_valid(body, good, [SECRET])
    assert webhooks.signature_valid(body, webhooks.sign(body, OLD_SECRET), [SECRET, OLD_SECRET]), "ротация"
    assert not webhooks.signature_valid(body, webhooks.sign(body, b"wrong"), [SECRET, OLD_SECRET])
    assert not webhooks.signature_valid(body + b" ", good, [SECRET]), "изменённое тело"
    assert not webhooks.signature_valid(body, None, [SECRET])
    assert not webhooks.signature_valid(body, "", [SECRET])
    assert not webhooks.signature_valid(body, good.replace("sha256=", "sha1="), [SECRET])
    assert not webhooks.signature_valid(body, "sha256=zz", [SECRET])
    assert not webhooks.signature_valid(body, good, []), "без секрета подпись не принимается"


def test_webhook_headers_and_hint() -> None:
    with pytest.raises(webhooks.WebhookRejectedError):
        webhooks.parse_delivery_guid(None)
    with pytest.raises(webhooks.WebhookRejectedError):
        webhooks.parse_delivery_guid("not-a-uuid")
    with pytest.raises(webhooks.WebhookRejectedError):
        webhooks.parse_event("Pull-Request")
    guid = uuid.uuid4()
    assert webhooks.parse_delivery_guid(str(guid)) == guid
    body = json.dumps(
        {
            "action": "closed",
            "installation": {"id": 77},
            "repository": {"id": 1001, "full_name": "acme/app"},
            "pull_request": {"number": 3, "head": {"ref": "aicd/x", "sha": "a" * 40}, "merged": True},
            "sender": {"login": "attacker"},
        }
    ).encode()
    hint = webhooks.extract_hint("pull_request", body)
    assert hint.action == "closed" and hint.installation_id == 77
    assert hint.data == {
        "repository_id": 1001,
        "repository_full_name": "acme/app",
        "pr_number": 3,
        "head_ref": "aicd/x",
        "head_sha": "a" * 40,
    }, "в подсказку попадают только ограниченные поля (merged не доверяется)"
    with pytest.raises(webhooks.WebhookRejectedError):
        webhooks.extract_hint("ping", b"[1,2]")
    with pytest.raises(webhooks.WebhookRejectedError):
        webhooks.extract_hint("ping", b"{not json")
    assert "push" in webhooks.ALLOWED_EVENTS and "workflow_run" not in webhooks.ALLOWED_EVENTS


# --- state установки (CSRF) ------------------------------------------------------------------------------


def test_install_state() -> None:
    key = os.urandom(32)
    ws, user = uuid.uuid4(), uuid.uuid4()
    token, state = install_state.issue(key, ws, user, 600)
    assert install_state.verify(key, token, workspace_id=ws, user_id=user).nonce == state.nonce
    for kwargs in ({"workspace_id": uuid.uuid4(), "user_id": user}, {"workspace_id": ws, "user_id": uuid.uuid4()}):
        with pytest.raises(install_state.InstallStateError, match="mismatch"):
            install_state.verify(key, token, **kwargs)
    with pytest.raises(install_state.InstallStateError, match="signature"):
        install_state.verify(os.urandom(32), token, workspace_id=ws, user_id=user)
    head, payload, mac = token.split(".")
    with pytest.raises(install_state.InstallStateError):
        install_state.verify(key, f"{head}.{payload}x.{mac}", workspace_id=ws, user_id=user)
    expired, _ = install_state.issue(key, ws, user, -10)
    with pytest.raises(install_state.InstallStateError, match="expired"):
        install_state.verify(key, expired, workspace_id=ws, user_id=user)


def test_parse_remote_url() -> None:
    assert parse_remote_url("https://github.com/acme/app.git", "https://github.com") == RepoRef("acme", "app")
    assert parse_remote_url("https://github.com/acme/app", "https://github.com/").full_name == "acme/app"
    for bad in (
        "https://evil.example/acme/app",
        "https://github.com/acme",
        "https://github.com/acme/app/tree/main",
        "https://user:pw@github.com/acme/app",
        "https://github.com/acme/..",
        "https://github.com/acme/app?x=1",
    ):
        with pytest.raises(ValueError, match="owner"):
            parse_remote_url(bad, "https://github.com")


# --- git push: токен не в argv/URL/.git/config (SC-GH-04) --------------------------------------------------


def test_push_ref_guards() -> None:
    sha = "a" * 40
    for kwargs in (
        {"branch": "main"},
        {"branch": "aicd/../x"},
        {"remote_url": "https://x-access-token:t@github.com/a/b.git"},
        {"remote_url": "ssh://github.com/a/b.git"},
        {"sha": "HEAD"},
    ):
        args: dict[str, Any] = {"remote_url": "https://github.com/a/b.git", "sha": sha, "branch": "aicd/r/t", **kwargs}
        with pytest.raises(git_guard.GitRepositoryError):
            git_guard.push_ref(Path("/nonexistent"), token="t", **args)


def _work_clone(src: Path, tmp_path: Path) -> tuple[Path, str]:
    work = tmp_path / "work"
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp_path),
    }
    subprocess.run(["git", "clone", "-q", str(src), str(work)], check=True, env=env)
    (work / "f.txt").write_text("change\n")
    git(work, "add", ".")
    git(work, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "c")
    return work, git(work, "rev-parse", "HEAD")


def _exists(path: str) -> bool:
    return Path(path).exists()


def _read(path: Path) -> str:
    return path.read_text()


async def test_push_uses_askpass_and_leaves_no_token(
    double: tuple[GitHubDouble, str, Path], client: GitHubClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh, url, src = double
    work, sha = _work_clone(src, tmp_path)
    token = await client.installation_token(77, "push", repository_ids=[1001])
    seen: list[tuple[list[str], dict[str, str]]] = []
    real_run = subprocess.run

    def spy(argv: list[str], **kwargs: Any) -> Any:
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    await asyncio.to_thread(
        git_guard.push_ref, work, remote_url=f"{url}/acme/app.git", sha=sha, branch="aicd/run/task-1",
        token=token.value(),
    )  # fmt: skip
    assert gh.branch_sha(gh.repos[1001], "aicd/run/task-1") == sha
    argv, env = next((a, e) for a, e in seen if "push" in a)
    assert token.value() not in " ".join(argv), "токен не в argv"
    assert all("@" not in a for a in argv if a.startswith("http")), "URL без учётных данных"
    assert env["AICD_GIT_ASKPASS_TOKEN"] == token.value() and "credential.helper=" in argv
    assert not _exists(env["GIT_ASKPASS"]), "одноразовый askpass удалён"
    assert token.value() not in _read(work / ".git" / "config")
    # Без токена с contents:write push отклоняется GitHub.
    read = await client.installation_token(77, "read", repository_ids=[1001])
    with pytest.raises(git_guard.GitRepositoryError) as err:
        await asyncio.to_thread(
            git_guard.push_ref, work, remote_url=f"{url}/acme/app.git", sha=sha, branch="aicd/run/task-2",
            token=read.value(),
        )  # fmt: skip
    assert read.value() not in err.value.detail


async def test_force_push_over_foreign_history_is_rejected(
    double: tuple[GitHubDouble, str, Path], client: GitHubClient, tmp_path: Path
) -> None:
    """Push без `+`: перезапись ветки несвязанной историей отклоняется как non-fast-forward (SC-GH-05)."""
    gh, url, _ = double
    a, b = tmp_path / "a", tmp_path / "b"
    for repo in (a, b):
        init_git_repo(repo)
        (repo / "x").write_text(str(repo) + str(time.time()))
        git(repo, "add", ".")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "x")
    token = await client.installation_token(77, "push", repository_ids=[1001])
    git_guard.push_ref(a, remote_url=f"{url}/acme/app.git", sha=git(a, "rev-parse", "HEAD"), branch="aicd/ff/1",
                       token=token.value())  # fmt: skip
    with pytest.raises(git_guard.GitRepositoryError):
        git_guard.push_ref(b, remote_url=f"{url}/acme/app.git", sha=git(b, "rev-parse", "HEAD"), branch="aicd/ff/1",
                           token=token.value())  # fmt: skip
    assert gh.branch_sha(gh.repos[1001], "aicd/ff/1") == git(a, "rev-parse", "HEAD")
