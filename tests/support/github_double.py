"""Локальный тестовый двойник GitHub (REST API + git smart HTTP) для P8-02…P8-05, P8-09.

Это **тестовая фикстура**, не код продукта: продукт ходит в него настоящим `GitHubClient` (httpx) и настоящим
`git push` (GIT_ASKPASS), меняется только базовый URL. Двойник реализует ровно используемые эндпоинты и
проверяет то, что проверяет GitHub:

* JWT App: RS256-подпись публичным ключом App, `iss`, срок ≤ 10 мин;
* installation token: выдаётся только для репозиториев установки и с правами ⊆ правам установки; каждый
  запрос с токеном проверяет срок, репозиторий и право операции (`metadata`, `pull_requests`, `contents`, …);
* git push: Basic `x-access-token:<token>` с `contents:write` для этого репозитория (`git http-backend`);
* merge: `sha` должен совпадать с head PR (409), PR должен быть mergeable с учётом защиты ветки (405).

Счётчики (`created_pulls`, `merges`, `token_requests`, …) — для проверок «ровно один PR», «merge не вызывался»,
«токен с минимальными правами».
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aicd_integrations_github.permissions import APP_PERMISSIONS

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"}
_RANK = {"read": 1, "write": 2}


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> str:
    env = {**GIT_ENV, "PATH": os.environ["PATH"]}
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input_bytes, capture_output=True, env=env, check=True)
    return proc.stdout.decode().strip()


def generate_app_key() -> tuple[str, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, key.public_key()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class FakeRepo:
    id: int
    owner: str
    name: str
    bare: Path
    default_branch: str = "main"
    protection: dict[str, tuple[str, ...]] = field(default_factory=dict)  # ветка → required contexts

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class FakeInstallation:
    id: int
    account: str
    account_type: str = "Organization"
    permissions: dict[str, str] = field(default_factory=lambda: dict(APP_PERMISSIONS))
    repository_ids: set[int] = field(default_factory=set)
    suspended: bool = False
    deleted: bool = False


@dataclass
class IssuedToken:
    installation_id: int
    permissions: dict[str, str]
    repository_ids: set[int]
    expires_at: float


@dataclass
class FakePull:
    number: int
    repo_id: int
    head: str
    base: str
    title: str
    body: str
    head_sha: str
    state: str = "open"
    merged: bool = False
    merge_commit_sha: str | None = None
    mergeable_state_override: str | None = None
    created_at: float = field(default_factory=time.time)


class GitHubDouble:
    def __init__(self, root: Path, *, app_id: int = 4242) -> None:
        self.root = root
        self.app_id = app_id
        self.private_key_pem, self.public_key = generate_app_key()
        self.lock = threading.RLock()
        self.installations: dict[int, FakeInstallation] = {}
        self.repos: dict[int, FakeRepo] = {}
        self.tokens: dict[str, IssuedToken] = {}
        self.pulls: list[FakePull] = []
        self.reviews: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.statuses: dict[tuple[int, str], dict[str, str]] = {}
        self.check_runs: dict[tuple[int, str], dict[str, str]] = {}
        # Счётчики/журналы для проверок тестов
        self.created_pulls = 0
        self.merges = 0
        self.pushes = 0
        self.token_requests: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.auth_failures: list[str] = []
        self.fail_next: dict[str, int] = {}  # "METHOD path-regex" → HTTP-код единожды
        # Chaos (P9-03): "METHOD path-regex" → Hold — успешный ответ задерживается после применения эффекта
        # (PR создан, push принят, merge выполнен), пока тест не убьёт воркер и не отпустит hold.
        self.hold_after: dict[str, Hold] = {}
        self._server: ThreadingHTTPServer | None = None

    # --- настройка ------------------------------------------------------------------------------------

    def add_installation(self, installation_id: int, account: str, **kwargs: Any) -> FakeInstallation:
        inst = FakeInstallation(installation_id, account, **kwargs)
        self.installations[installation_id] = inst
        return inst

    def add_repo(self, installation_id: int, owner: str, name: str, repo_id: int, source: Path) -> FakeRepo:
        """Bare-репозиторий «на GitHub», заполненный из локального клона `source` (ветка main)."""
        bare = self.root / owner / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(source), str(bare)],
            check=True,
            env={**GIT_ENV, "PATH": os.environ["PATH"]},
        )
        git(bare, "config", "http.receivepack", "true")
        repo = FakeRepo(repo_id, owner, name, bare)
        self.repos[repo_id] = repo
        self.installations[installation_id].repository_ids.add(repo_id)
        return repo

    def repo_by_name(self, owner: str, name: str) -> FakeRepo | None:
        for repo in self.repos.values():
            if repo.owner.lower() == owner.lower() and repo.name.lower() == name.lower():
                return repo
        return None

    def branch_sha(self, repo: FakeRepo, branch: str) -> str | None:
        try:
            return git(repo.bare, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
        except subprocess.CalledProcessError:
            return None

    def pulls_of(self, repo_id: int) -> list[FakePull]:
        return [p for p in self.pulls if p.repo_id == repo_id]

    def all_token_values(self) -> list[str]:
        return list(self.tokens)

    # --- сервер ---------------------------------------------------------------------------------------

    def start(self) -> str:
        double = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _dispatch(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, headers, payload = double.handle(self.command, self.path, dict(self.headers.items()), body)
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):  # клиент убит во время hold (chaos)
                    self.close_connection = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # --- обработка ------------------------------------------------------------------------------------

    def handle(
        self, method: str, raw_path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        result = self._handle(method, raw_path, headers, body)
        if not self.hold_after or not 200 <= result[0] < 300:
            return result
        path = urlsplit(raw_path).path
        with self.lock:
            hold = None
            for key in list(self.hold_after):
                m, _, rx = key.partition(" ")
                if m == method and re.fullmatch(rx, path):
                    hold = self.hold_after.pop(key)
                    break
        if hold is not None:
            hold.reached.set()
            hold.release.wait(hold.timeout)
        return result

    def _handle(
        self, method: str, raw_path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(raw_path)
        path, query = parts.path, parse_qs(parts.query)
        headers = {k.lower(): v for k, v in headers.items()}
        with self.lock:
            self.requests.append((method, path))
            for key, code in list(self.fail_next.items()):
                m, _, rx = key.partition(" ")
                if m == method and re.fullmatch(rx, path):
                    del self.fail_next[key]
                    return _json(code, {"message": "injected failure"})
        if ".git/" in path:
            return self._git(method, path, parts.query, headers, body)
        try:
            with self.lock:
                return self._api(method, path, query, headers, body)
        except _Http as exc:
            return _json(exc.status, {"message": exc.message})

    # JWT App
    def _verify_jwt(self, headers: dict[str, str]) -> None:
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise _Http(401, "A JSON web token could not be decoded")
        token = auth[7:]
        try:
            h, p, s = token.split(".")
            header, payload = json.loads(_unb64(h)), json.loads(_unb64(p))
            self.public_key.verify(_unb64(s), f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except (ValueError, InvalidSignature) as exc:
            self.auth_failures.append("jwt")
            raise _Http(401, "A JSON web token could not be decoded") from exc
        now = time.time()
        if header.get("alg") != "RS256" or str(payload.get("iss")) != str(self.app_id):
            raise _Http(401, "'Issuer' claim ('iss') must be an Integer")
        if not (payload["iat"] <= now + 60 and payload["exp"] > now and payload["exp"] - payload["iat"] <= 660):
            raise _Http(401, "'Expiration time' claim ('exp') is too far in the future")

    def _token(self, headers: dict[str, str], repo: FakeRepo, permission: str, level: str) -> IssuedToken:
        auth = headers.get("authorization", "")
        value = auth[7:] if auth.startswith(("Bearer ", "token ")) else ""
        issued = self.tokens.get(value)
        if issued is None or issued.expires_at < time.time():
            self.auth_failures.append("token")
            raise _Http(401, "Bad credentials")
        inst = self.installations[issued.installation_id]
        if inst.suspended or inst.deleted or repo.id not in issued.repository_ids:
            raise _Http(404, "Not Found")
        if _RANK.get(issued.permissions.get(permission, ""), 0) < _RANK[level]:
            self.auth_failures.append(f"scope:{permission}:{level}")
            raise _Http(403, "Resource not accessible by integration")
        return issued

    def _api(
        self, method: str, path: str, query: dict[str, list[str]], headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        data = json.loads(body) if body else {}
        if m := re.fullmatch(r"/app/installations/(\d+)", path):
            self._verify_jwt(headers)
            inst = self.installations.get(int(m.group(1)))
            if inst is None or inst.deleted:
                raise _Http(404, "Not Found")
            return _json(200, {
                "id": inst.id, "app_id": self.app_id,
                "account": {"login": inst.account, "type": inst.account_type},
                "repository_selection": "selected", "permissions": inst.permissions,
                "suspended_at": "2026-01-01T00:00:00Z" if inst.suspended else None,
            })  # fmt: skip
        if m := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path):
            self._verify_jwt(headers)
            inst = self.installations.get(int(m.group(1)))
            if inst is None or inst.deleted or inst.suspended:
                raise _Http(404, "Not Found")
            self.token_requests.append({"installation_id": inst.id, **data})
            perms = data.get("permissions") or dict(inst.permissions)
            for name, level in perms.items():
                if _RANK.get(inst.permissions.get(name, ""), 0) < _RANK.get(level, 9):
                    raise _Http(422, f"The permissions requested are not granted to this installation: {name}")
            ids: set[int] = set()
            for rid in data.get("repository_ids") or []:
                if rid not in inst.repository_ids:
                    raise _Http(422, "There is at least one repository that does not exist or is not accessible")
                ids.add(int(rid))
            for name in data.get("repositories") or []:
                repo = self.repo_by_name(inst.account, name)
                if repo is None or repo.id not in inst.repository_ids:
                    raise _Http(422, "There is at least one repository that does not exist or is not accessible")
                ids.add(repo.id)
            if not ids:
                ids = set(inst.repository_ids)
            value = "ghs_" + secrets.token_hex(18)
            self.tokens[value] = IssuedToken(inst.id, dict(perms), ids, time.time() + 3600)
            expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            return _json(201, {
                "token": value, "expires_at": expires, "permissions": perms,
                "repositories": [{"id": i, "full_name": self.repos[i].full_name} for i in sorted(ids)],
            })  # fmt: skip
        m = re.fullmatch(r"/repos/([^/]+)/([^/]+)(/.*)?", path)
        if m is None:
            raise _Http(404, "Not Found")
        repo = self.repo_by_name(unquote(m.group(1)), unquote(m.group(2)))
        if repo is None:
            raise _Http(404, "Not Found")
        rest = m.group(3) or ""
        if rest == "" and method == "GET":
            self._token(headers, repo, "metadata", "read")
            return _json(200, {
                "id": repo.id, "full_name": repo.full_name, "default_branch": repo.default_branch,
                "html_url": f"https://github.test/{repo.full_name}",
            })  # fmt: skip
        if (b := re.fullmatch(r"/branches/(.+)", rest)) and method == "GET":
            self._token(headers, repo, "metadata", "read")
            name = unquote(b.group(1))
            sha = self.branch_sha(repo, name)
            if sha is None:
                raise _Http(404, "Branch not found")
            contexts = repo.protection.get(name)
            return _json(200, {
                "name": name, "commit": {"sha": sha}, "protected": contexts is not None,
                "protection": {"required_status_checks": {"contexts": list(contexts or ())}},
            })  # fmt: skip
        if rest == "/pulls" and method == "GET":
            self._token(headers, repo, "pull_requests", "read")
            head = (query.get("head") or [""])[0]
            owner, _, branch = head.partition(":")
            items = [p for p in self.pulls_of(repo.id) if not head or (owner == repo.owner and p.head == branch)]
            return _json(200, [self._pull_json(repo, p) for p in items])
        if rest == "/pulls" and method == "POST":
            self._token(headers, repo, "pull_requests", "write")
            head, base = str(data["head"]), str(data["base"])
            sha = self.branch_sha(repo, head)
            if sha is None or self.branch_sha(repo, base) is None:
                raise _Http(422, "Validation Failed")
            if any(p.head == head and p.state == "open" for p in self.pulls_of(repo.id)):
                raise _Http(422, "A pull request already exists")
            pull = FakePull(
                len(self.pulls) + 1, repo.id, head, base, str(data["title"]), str(data.get("body", "")), sha
            )
            self.pulls.append(pull)
            self.created_pulls += 1
            return _json(201, self._pull_json(repo, pull))
        if (p := re.fullmatch(r"/pulls/(\d+)(/.*)?", rest)) is not None:
            found = next((x for x in self.pulls_of(repo.id) if x.number == int(p.group(1))), None)
            if found is None:
                raise _Http(404, "Not Found")
            pull = found
            sub = p.group(2) or ""
            if sub == "" and method == "GET":
                self._token(headers, repo, "pull_requests", "read")
                return _json(200, self._pull_json(repo, pull))
            if sub == "/reviews" and method == "GET":
                self._token(headers, repo, "pull_requests", "read")
                return _json(200, self.reviews.get((repo.id, pull.number), []))
            if sub == "/reviews" and method == "POST":
                self._token(headers, repo, "pull_requests", "write")
                if data.get("event") != "COMMENT":
                    raise _Http(422, "only COMMENT reviews are expected from AiCodingDev")
                review = {"id": secrets.randbelow(10**9) + 1, "body": data.get("body", ""), "state": "COMMENTED"}
                self.reviews.setdefault((repo.id, pull.number), []).append(review)
                return _json(200, review)
            if sub == "/merge" and method == "PUT":
                self._token(headers, repo, "contents", "write")
                if pull.merged or pull.state != "open":
                    raise _Http(405, "Pull Request is not mergeable")
                if data.get("sha") != pull.head_sha:
                    raise _Http(409, "Head branch was modified. Review and try the merge again.")
                if self._mergeable_state(repo, pull) not in ("clean", "unstable", "has_hooks"):
                    raise _Http(405, "Required status check is expected.")
                git(repo.bare, "update-ref", f"refs/heads/{pull.base}", pull.head_sha)
                pull.merged, pull.state, pull.merge_commit_sha = True, "closed", pull.head_sha
                self.merges += 1
                return _json(200, {"sha": pull.head_sha, "merged": True, "message": "Pull Request successfully merged"})
        if (s := re.fullmatch(r"/statuses/([0-9a-f]{40})", rest)) and method == "POST":
            self._token(headers, repo, "statuses", "write")
            self.statuses.setdefault((repo.id, s.group(1)), {})[str(data["context"])] = str(data["state"])
            return _json(201, {"state": data["state"], "context": data["context"]})
        if (s := re.fullmatch(r"/commits/([0-9a-f]{40})/status", rest)) and method == "GET":
            self._token(headers, repo, "statuses", "read")
            current = self.statuses.get((repo.id, s.group(1)), {})
            return _json(200, {"statuses": [{"context": c, "state": st} for c, st in current.items()]})
        if (s := re.fullmatch(r"/commits/([0-9a-f]{40})/check-runs", rest)) and method == "GET":
            self._token(headers, repo, "checks", "read")
            runs = self.check_runs.get((repo.id, s.group(1)), {})
            return _json(
                200, {"check_runs": [{"name": n, "status": "completed", "conclusion": c} for n, c in runs.items()]}
            )
        raise _Http(404, "Not Found")

    def _mergeable_state(self, repo: FakeRepo, pull: FakePull) -> str:
        if pull.mergeable_state_override:
            return pull.mergeable_state_override
        required = repo.protection.get(pull.base) or ()
        statuses = self.statuses.get((repo.id, pull.head_sha), {})
        checks = self.check_runs.get((repo.id, pull.head_sha), {})
        if any(statuses.get(c) != "success" and checks.get(c) != "success" for c in required):
            return "blocked"
        return "clean"

    def _pull_json(self, repo: FakeRepo, pull: FakePull) -> dict[str, Any]:
        state = self._mergeable_state(repo, pull)
        return {
            "number": pull.number,
            "html_url": f"https://github.test/{repo.full_name}/pull/{pull.number}",
            "state": pull.state,
            "merged": pull.merged,
            "mergeable": None if pull.merged else state not in ("dirty",),
            "mergeable_state": state,
            "merge_commit_sha": pull.merge_commit_sha,
            "merged_at": "2026-11-08T10:00:00Z" if pull.merged else None,
            "closed_at": "2026-11-08T10:00:00Z" if pull.state == "closed" else None,
            "title": pull.title,
            "body": pull.body,
            "head": {"ref": pull.head, "sha": pull.head_sha},
            "base": {"ref": pull.base},
        }

    # git smart HTTP через `git http-backend` (Basic x-access-token:<token>, contents:write для push)
    def _git(
        self, method: str, path: str, query: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        m = re.fullmatch(r"/([^/]+)/([^/]+)\.git(/.*)", path)
        repo = self.repo_by_name(m.group(1), m.group(2)) if m else None
        if m is None or repo is None:
            return 404, {}, b"not found"
        push = "git-receive-pack" in path or "service=git-receive-pack" in query
        auth = headers.get("authorization", "")
        token = None
        if auth.startswith("Basic "):
            user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            token = password if user == "x-access-token" else None
        with self.lock:
            issued = self.tokens.get(token or "")
            allowed = (
                issued is not None
                and issued.expires_at > time.time()
                and repo.id in issued.repository_ids
                and _RANK.get(issued.permissions.get("contents", ""), 0) >= _RANK["write" if push else "read"]
            )
        if not allowed:
            if token is not None:
                self.auth_failures.append("git")
            return 401, {"WWW-Authenticate": 'Basic realm="GitHub"'}, b"auth required"
        env = {
            **GIT_ENV,
            "PATH": os.environ["PATH"],
            "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": f"/{repo.owner}/{repo.name}.git{m.group(3)}",
            "REQUEST_METHOD": method,
            "QUERY_STRING": query,
            "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "x-access-token",
            "REMOTE_ADDR": "127.0.0.1",
        }
        if headers.get("content-encoding"):
            env["HTTP_CONTENT_ENCODING"] = headers["content-encoding"]
        if headers.get("git-protocol"):
            env["GIT_PROTOCOL"] = headers["git-protocol"]
        proc = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env, check=False)
        head, _, payload = proc.stdout.partition(b"\r\n\r\n")
        status, out_headers = 200, {}
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                out_headers[name] = value.strip()
        if push and method == "POST" and status == 200:
            with self.lock:
                self.pushes += 1
        return status, out_headers, payload


@dataclass
class Hold:
    """Задержка ответа после применения эффекта: `reached` — эффект применён, `release` — отпустить ответ."""

    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    timeout: float = 60.0


class _Http(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _json(status: int, body: Any) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps(body).encode()


def iter_tokens(double: GitHubDouble) -> Iterator[str]:
    yield from double.tokens
