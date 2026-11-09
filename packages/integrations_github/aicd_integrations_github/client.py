"""HTTP-клиент GitHub REST API (P8-02…P8-05): JWT App → installation token → операции с репозиторием.

* Базовый URL — конфигурация оператора (`github_api_url`); запросы — только через `httpx.AsyncClient` с
  `trust_env=False`, без редиректов, с тайм-аутами и (если задан) через egress-proxy (SC-NET-01).
* Installation token: запрашивается на операцию для одного репозитория (`repository_ids`/`repositories`)
  с минимальными правами (`permissions.TOKEN_SCOPES`), кэшируется в памяти до `expires_at − 5 мин`
  (TTL у GitHub ≤ 1 ч). Значение не логируется (`InstallationToken.__repr__`), в БД/на диск не пишется.
* Ответ GitHub — недоверенные данные: числа/строки, которые попадают в БД, проверяются вызывающим.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

import httpx

from aicd_integrations_github.app_auth import AppJwtSigner
from aicd_integrations_github.permissions import TOKEN_SCOPES, Operation
from aicd_platform.redaction import redact_text

API_VERSION: Final = "2022-11-28"
USER_AGENT: Final = "AiCodingDev-GitHub-App"
TOKEN_REFRESH_MARGIN_SECONDS: Final = 300
MAX_RESPONSE_BYTES: Final = 5 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_OWNER = re.compile(r"^[A-Za-z0-9-]{1,39}$")


class GitHubError(RuntimeError):
    """Ошибка GitHub API. `status` — HTTP-код (0 — сетевая ошибка); сообщение без секретов."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {redact_text(message)[:500]}")
        self.status = status


class GitHubNotConfiguredError(RuntimeError):
    """GitHub App не настроен (нет app_id/ключа) — интеграция недоступна."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @classmethod
    def parse(cls, full_name: str) -> RepoRef:
        owner, _, name = full_name.partition("/")
        if not _OWNER.fullmatch(owner) or not _NAME.fullmatch(name) or name in (".", ".."):
            raise ValueError("ожидалось имя репозитория вида owner/name")
        return cls(owner, name)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def path(self) -> str:
        return f"/repos/{quote(self.owner, safe='')}/{quote(self.name, safe='')}"


def parse_remote_url(url: str, web_url: str) -> RepoRef:
    """`https://github.com/<owner>/<repo>[.git]` → RepoRef (хост — только настроенный `github_web_url`)."""
    base = web_url.rstrip("/") + "/"
    if not url.startswith(base) or any(c in url for c in "?#@\\ ") or len(url) > 2048:
        raise ValueError(f"ожидался URL вида {base}<owner>/<repo>")
    rest = url[len(base) :].removesuffix("/").removesuffix(".git")
    if rest.count("/") != 1:
        raise ValueError(f"ожидался URL вида {base}<owner>/<repo>")
    return RepoRef.parse(rest)


class InstallationToken:
    """Installation access token. Значение доступно только через `value()`; repr/str его не раскрывают."""

    __slots__ = ("_value", "expires_at", "permissions", "repository_ids")

    def __init__(
        self, value: str, *, expires_at: float, permissions: Mapping[str, str], repository_ids: tuple[int, ...]
    ) -> None:
        self._value = value
        self.expires_at = expires_at
        self.permissions = dict(permissions)
        self.repository_ids = repository_ids

    def value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"InstallationToken(expires_at={self.expires_at}, permissions={self.permissions}, value=***)"

    __str__ = __repr__

    def fresh(self, now: float) -> bool:
        return self.expires_at - now > TOKEN_REFRESH_MARGIN_SECONDS


@dataclass(frozen=True)
class Installation:
    installation_id: int
    account_login: str
    account_type: str  # user | organization
    repository_selection: str  # all | selected
    permissions: dict[str, str]
    app_id: int | None
    suspended: bool


@dataclass(frozen=True)
class RepoInfo:
    id: int
    full_name: str
    default_branch: str
    html_url: str


@dataclass(frozen=True)
class PullInfo:
    number: int
    url: str
    state: str  # open | closed
    merged: bool
    head_ref: str
    head_sha: str
    base_ref: str
    mergeable: bool | None
    mergeable_state: str | None
    merge_commit_sha: str | None
    merged_at: str | None
    closed_at: str | None
    body: str


@dataclass(frozen=True)
class BranchInfo:
    name: str
    sha: str
    protected: bool
    required_contexts: tuple[str, ...]


@dataclass
class _Cache:
    tokens: dict[tuple[int, tuple[int, ...], tuple[str, ...], tuple[tuple[str, str], ...]], InstallationToken] = field(
        default_factory=dict
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _check_sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", value):
        raise GitHubError(502, "в ответе GitHub некорректный SHA")
    return value


def _pull(data: Mapping[str, Any]) -> PullInfo:
    try:
        head, base = data["head"], data["base"]
        url = str(data["html_url"])
        if not url.startswith("https://"):
            raise GitHubError(502, "в ответе GitHub некорректный URL PR")
        return PullInfo(
            number=int(data["number"]),
            url=url,
            state=str(data["state"]),
            merged=bool(data.get("merged") or data.get("merged_at")),
            head_ref=str(head["ref"]),
            head_sha=_check_sha(head["sha"]),
            base_ref=str(base["ref"]),
            mergeable=data.get("mergeable"),
            mergeable_state=data.get("mergeable_state"),
            merge_commit_sha=data.get("merge_commit_sha"),
            merged_at=data.get("merged_at"),
            closed_at=data.get("closed_at"),
            body=str(data.get("body") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubError(502, "неожиданный формат PR в ответе GitHub") from exc


class GitHubClient:
    def __init__(
        self,
        *,
        api_url: str,
        signer: AppJwtSigner | None,
        proxy_url: str | None = None,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._signer = signer
        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            proxy=proxy_url,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                     "User-Agent": USER_AGENT},
        )  # fmt: skip
        self._cache = _Cache()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def configured(self) -> bool:
        return self._signer is not None

    @property
    def app_id(self) -> int | None:
        """ID GitHub App подписанта (проверка `foreign_app` установки); без ключа — None."""
        return self._signer.app_id if self._signer is not None else None

    def _jwt(self) -> str:
        if self._signer is None:
            raise GitHubNotConfiguredError("GitHub App не настроен")
        return self._signer.issue()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        ok: Sequence[int] = (200,),
    ) -> Any:
        try:
            response = await self._http.request(
                method, path, json=json, params=params, headers={"Authorization": f"Bearer {auth}"}
            )
        except httpx.HTTPError as exc:
            raise GitHubError(0, f"{type(exc).__name__}") from None
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise GitHubError(502, "ответ GitHub слишком большой")
        if response.status_code not in ok:
            message = ""
            try:
                body = response.json()
                message = str(body.get("message", "")) if isinstance(body, dict) else ""
            except ValueError:
                message = ""
            raise GitHubError(response.status_code, message or response.reason_phrase)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError(502, "ответ GitHub не JSON") from exc

    # --- App / installations ------------------------------------------------------------------------

    async def installation(self, installation_id: int) -> Installation:
        data = await self._request("GET", f"/app/installations/{int(installation_id)}", auth=self._jwt())
        try:
            account = data["account"]
            account_type = "organization" if str(account["type"]).lower() == "organization" else "user"
            return Installation(
                installation_id=int(data["id"]),
                account_login=str(account["login"]),
                account_type=account_type,
                repository_selection=str(data.get("repository_selection") or "selected"),
                permissions={str(k): str(v) for k, v in (data.get("permissions") or {}).items()},
                app_id=int(data["app_id"]) if data.get("app_id") is not None else None,
                suspended=data.get("suspended_at") is not None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError(502, "неожиданный формат установки в ответе GitHub") from exc

    async def installation_token(
        self,
        installation_id: int,
        operation: Operation,
        *,
        repository_ids: Sequence[int] = (),
        repositories: Sequence[str] = (),
    ) -> InstallationToken:
        """Token одной установки для заданных репозиториев с минимальными правами операции (SC-GH-04)."""
        if not repository_ids and not repositories:
            raise ValueError("installation token запрашивается только для конкретных репозиториев")
        scope = dict(TOKEN_SCOPES[operation])
        key = (
            int(installation_id),
            tuple(sorted(int(i) for i in repository_ids)),
            tuple(sorted(repositories)),
            tuple(sorted(scope.items())),
        )
        async with self._cache.lock:
            cached = self._cache.tokens.get(key)
            if cached is not None and cached.fresh(time.time()):
                return cached
            body: dict[str, Any] = {"permissions": scope}
            if repository_ids:
                body["repository_ids"] = [int(i) for i in repository_ids]
            else:
                body["repositories"] = list(repositories)
            data = await self._request(
                "POST", f"/app/installations/{int(installation_id)}/access_tokens", auth=self._jwt(), json=body,
                ok=(201,),
            )  # fmt: skip
            try:
                value = str(data["token"])
                expires = _parse_time(str(data["expires_at"]))
                granted = {str(k): str(v) for k, v in (data.get("permissions") or {}).items()}
                repo_ids = tuple(int(r["id"]) for r in (data.get("repositories") or []))
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError(502, "неожиданный формат installation token") from exc
            token = InstallationToken(value, expires_at=expires, permissions=granted, repository_ids=repo_ids)
            self._cache.tokens[key] = token
            return token

    def forget_tokens(self) -> None:
        self._cache.tokens.clear()

    # --- Репозиторий --------------------------------------------------------------------------------

    async def repository(self, token: InstallationToken, repo: RepoRef) -> RepoInfo:
        data = await self._request("GET", repo.path, auth=token.value())
        try:
            return RepoInfo(
                id=int(data["id"]),
                full_name=str(data["full_name"]),
                default_branch=str(data["default_branch"]),
                html_url=str(data["html_url"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError(502, "неожиданный формат репозитория в ответе GitHub") from exc

    async def branch(self, token: InstallationToken, repo: RepoRef, branch: str) -> BranchInfo | None:
        try:
            data = await self._request("GET", f"{repo.path}/branches/{quote(branch, safe='')}", auth=token.value())
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        try:
            protection = data.get("protection") or {}
            checks = protection.get("required_status_checks") or {}
            contexts = [str(c) for c in (checks.get("contexts") or [])]
            contexts += [str(c["context"]) for c in (checks.get("checks") or []) if c.get("context")]
            return BranchInfo(
                name=str(data["name"]),
                sha=_check_sha(data["commit"]["sha"]),
                protected=bool(data.get("protected")),
                required_contexts=tuple(sorted(set(contexts))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError(502, "неожиданный формат ветки в ответе GitHub") from exc

    async def find_pulls(self, token: InstallationToken, repo: RepoRef, head_branch: str) -> list[PullInfo]:
        """PR (любого состояния) из ветки `head_branch` этого же репозитория — сверка ledger (ADR-0012)."""
        data = await self._request(
            "GET",
            f"{repo.path}/pulls",
            auth=token.value(),
            params={"head": f"{repo.owner}:{head_branch}", "state": "all", "per_page": 100},
        )
        if not isinstance(data, list):
            raise GitHubError(502, "неожиданный формат списка PR")
        return [p for p in (_pull(item) for item in data) if p.head_ref == head_branch]

    async def create_pull(
        self, token: InstallationToken, repo: RepoRef, *, title: str, head: str, base: str, body: str
    ) -> PullInfo:
        data = await self._request(
            "POST",
            f"{repo.path}/pulls",
            auth=token.value(),
            json={
                "title": title[:256],
                "head": head,
                "base": base,
                "body": body[:60000],
                "maintainer_can_modify": False,
            },
            ok=(201,),
        )
        return _pull(data)

    async def pull(self, token: InstallationToken, repo: RepoRef, number: int) -> PullInfo:
        return _pull(await self._request("GET", f"{repo.path}/pulls/{int(number)}", auth=token.value()))

    async def reviews(self, token: InstallationToken, repo: RepoRef, number: int) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"{repo.path}/pulls/{int(number)}/reviews", auth=token.value(), params={"per_page": 100}
        )
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def create_review(self, token: InstallationToken, repo: RepoRef, number: int, *, body: str) -> int:
        """Ревью-комментарий (event=COMMENT): не одобряет и не блокирует PR — решение остаётся за людьми."""
        data = await self._request(
            "POST",
            f"{repo.path}/pulls/{int(number)}/reviews",
            auth=token.value(),
            json={"event": "COMMENT", "body": body[:60000]},
        )
        return int(data["id"])

    async def commit_statuses(self, token: InstallationToken, repo: RepoRef, sha: str) -> dict[str, str]:
        """Комбинированный статус коммита: context → state (success/failure/error/pending)."""
        data = await self._request("GET", f"{repo.path}/commits/{_check_sha(sha)}/status", auth=token.value())
        statuses = data.get("statuses") if isinstance(data, dict) else None
        return {str(s["context"]): str(s["state"]) for s in (statuses or []) if isinstance(s, dict)}

    async def check_runs(self, token: InstallationToken, repo: RepoRef, sha: str) -> dict[str, str]:
        """Check runs коммита: name → conclusion (или status, пока не завершён)."""
        data = await self._request(
            "GET", f"{repo.path}/commits/{_check_sha(sha)}/check-runs", auth=token.value(), params={"per_page": 100}
        )
        runs = data.get("check_runs") if isinstance(data, dict) else None
        result: dict[str, str] = {}
        for run in runs or []:
            if isinstance(run, dict) and run.get("name"):
                result[str(run["name"])] = str(run.get("conclusion") or run.get("status") or "pending")
        return result

    async def set_status(
        self, token: InstallationToken, repo: RepoRef, sha: str, *, state: str, context: str, description: str
    ) -> None:
        if state not in ("success", "failure", "error", "pending"):
            raise ValueError(state)
        await self._request(
            "POST",
            f"{repo.path}/statuses/{_check_sha(sha)}",
            auth=token.value(),
            json={"state": state, "context": context, "description": description[:140]},
            ok=(201,),
        )

    async def merge(
        self, token: InstallationToken, repo: RepoRef, number: int, *, sha: str, method: str, title: str
    ) -> str:
        """Merge PR при совпадении head SHA (`sha` — защита от TOCTOU: GitHub отвечает 409 при сдвиге ветки).

        Параметров обхода branch protection нет: GitHub сам отклоняет merge, если требования не выполнены (405).
        """
        if method not in ("merge", "squash", "rebase"):
            raise ValueError(method)
        data = await self._request(
            "PUT",
            f"{repo.path}/pulls/{int(number)}/merge",
            auth=token.value(),
            json={"sha": _check_sha(sha), "merge_method": method, "commit_title": title[:256]},
        )
        if not isinstance(data, dict) or not data.get("merged"):
            raise GitHubError(409, "GitHub не выполнил merge")
        return _check_sha(data.get("sha"))


def _parse_time(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
