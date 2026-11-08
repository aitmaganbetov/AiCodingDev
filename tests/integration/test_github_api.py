"""P8-02, P8-03, P8-05, P8-09: API GitHub-интеграции на тестовом двойнике GitHub.

* установка GitHub App: state (CSRF, однократность, привязка к пользователю/workspace), проверка прав установки —
  на воркере (OQ-47 (2): API отвечает 202 + проверка `github.verification.requested`, ключ App читает только воркер);
* привязка GitHub-репозитория к проекту (FR-PRJ-03): доступ установки к репозиторию проверяет воркер;
* вебхук: подпись по сырому телу, дедупликация, replay, allowlist, лимит тела, ротация секрета, аудит;
* запрос PR / merge (approval MERGE, four-eyes), RBAC-матрица всех GitHub-операций, 404 для чужого workspace;
* соответствие ответов контракту и fuzz (Schemathesis).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hypothesis
import psycopg
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_api.main import create_app
from aicd_integrations_github import webhooks
from aicd_integrations_github.api import AppJwtSigner, GitHubClient
from aicd_orchestration.github_flow import GitHubWorkflow
from aicd_platform.config import Settings
from aicd_platform.ids import uuid7
from aicd_worker import github as worker_github
from aicd_worker.runtime import Runtime
from tests.conftest import UserSession, login, make_client
from tests.support import db as testdb
from tests.support import github as ghs
from tests.support.contract import OPENAPI_PATH, validate_response
from tests.support.github_double import GitHubDouble
from tests.support.orch_api import Team, key, make_team
from tests.support.orchestration import connect_admin

pytestmark = pytest.mark.db

SECRET = "wh-secret-" + secrets.token_hex(8)
PREVIOUS = "wh-previous-" + secrets.token_hex(8)
ROLES = ("admin", "admin2", "developer", "reviewer", "viewer")


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@dataclass
class Env:
    gh: GitHubDouble
    url: str
    root: Path  # корень репозиториев (settings.repository_roots["main"])
    local: Path  # локальная рабочая копия acme/app
    base_sha: str


@pytest.fixture(scope="module")
def env(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    local = repo_root / f"gh-{secrets.token_hex(4)}"
    base = ghs.init_repo(local)
    gh, url = ghs.double_with_repo(tmp_path_factory.mktemp("gh-remote"), local)
    gh.add_installation(88, "evilcorp", permissions={"contents": "write", "workflows": "write", "metadata": "read"})
    gh.add_installation(89, "acme2")
    yield Env(gh, url, repo_root, local, base)
    gh.stop()


@pytest.fixture(scope="module")
async def gapp(settings: Settings, env: Env, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[FastAPI]:
    secrets_dir = tmp_path_factory.mktemp("gh-secrets")
    (secrets_dir / "key.pem").write_text(env.gh.private_key_pem)
    (secrets_dir / "wh").write_text(SECRET)
    (secrets_dir / "wh-prev").write_text(PREVIOUS)
    configured = settings.model_copy(
        update={
            "github_app_id": env.gh.app_id,
            "github_app_slug": "aicd-test",
            "github_app_private_key_file": str(secrets_dir / "key.pem"),
            "github_webhook_secret_file": str(secrets_dir / "wh"),
            "github_webhook_secret_previous_file": str(secrets_dir / "wh-prev"),
            "github_api_url": env.url,
            "github_web_url": "https://github.test",
            "github_git_url": env.url,
            "github_webhook_max_bytes": 64 * 1024,
        }
    )
    application = create_app(configured)
    async with application.router.lifespan_context(application):
        yield application


async def _run_verifications(admin: psycopg.Connection, worker_engine: AsyncEngine, env: Env) -> list[str]:
    """Воркер: события `github.verification.requested` из outbox → `aicd_worker.github.handle_event` (ключ App —
    только здесь). Возвращает исходы обработки."""
    client = GitHubClient(api_url=env.url, signer=AppJwtSigner(env.gh.app_id, env.gh.private_key_pem))
    workflow = GitHubWorkflow(worker_engine, client, repository_roots={"main": env.root}, git_url=env.url)
    rt = Runtime(engine=worker_engine, orchestrator=None, loop=asyncio.get_running_loop(),  # type: ignore[arg-type]
                 dispatch_execution=_noop, github=workflow)  # fmt: skip
    rows = admin.execute(
        "SELECT e.id, e.workspace_id, e.aggregate_id, e.payload FROM outbox_events e"
        " JOIN github_verification_requests v ON v.id = e.aggregate_id"
        " WHERE e.event_type = 'github.verification.requested' AND v.status = 'PENDING' ORDER BY e.id"
    ).fetchall()
    outcomes = []
    try:
        for event_id, ws, aggregate_id, payload in rows:
            event = {"event_id": str(event_id), "event_type": "github.verification.requested",
                     "workspace_id": str(ws), "aggregate_id": str(aggregate_id), "payload": payload}  # fmt: skip
            assert worker_github.is_github_event(event)
            outcomes.append(await worker_github.handle_event(rt, event))
    finally:
        await client.aclose()
    return outcomes


async def _noop(message: dict[str, Any]) -> None:
    del message


async def _verification(user: UserSession, ws: str, accepted: Any) -> dict[str, Any]:
    """Статус проверки после обработки воркером (getGitHubVerification).

    connectGitHubInstallation — 201 с установкой PENDING и `verification_id`; attachRepository (github) — 202 + Location.
    """
    if accepted.status_code == 201:
        assert accepted.json()["status"] == "PENDING" or accepted.json()["verification_id"], accepted.text
        location = f"/api/v1/integrations/github/verifications/{accepted.json()['verification_id']}"
    else:
        assert accepted.status_code == 202, accepted.text
        location = accepted.headers["location"]
        assert location == f"/api/v1/integrations/github/verifications/{accepted.json()['id']}"
    r = await user.client.get(location, headers={"X-Workspace-Id": ws})
    validate_response("getGitHubVerification", r)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def _team(app: FastAPI, engine: AsyncEngine, admin: psycopg.Connection) -> Team:
    return await make_team(app, engine, admin, ROLES)


async def _connect(team: Team, who: str, installation_id: int) -> Any:
    """Запрос подключения (201, установка PENDING); проверку выполняет `_run_verifications`."""
    user = team.users[who]
    r = await user.client.post("/api/v1/integrations/github/installations/setup", headers=user.headers(team.ws))
    validate_response("startGitHubInstallation", r)
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    assert r.json()["install_url"].startswith("https://github.test/apps/aicd-test/installations/new?state=")
    r = await user.client.post(
        "/api/v1/integrations/github/installations",
        json={"installation_id": installation_id, "state": state, "setup_action": "install"},
        headers=user.headers(team.ws, idem=True),
    )
    validate_response("connectGitHubInstallation", r)
    return r, state


# --- установка GitHub App ---------------------------------------------------------------------------------


async def test_installation_flow_state_and_permissions(
    gapp: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, admin: psycopg.Connection, env: Env
) -> None:
    team = await _team(gapp, app_engine, admin)
    owner = team.users["owner"]
    requests_before = len(env.gh.requests)
    r, state = await _connect(team, "owner", 89)
    assert r.status_code == 201 and r.json()["status"] == "PENDING" and r.json()["installation_id"] == 89
    pending = await _verification(owner, team.ws, r)
    assert pending["status"] == "PENDING" and pending["kind"] == "installation" and pending["result_id"] is None
    assert len(env.gh.requests) == requests_before, "API не обращается к GitHub API (ключ App — только у воркера)"
    assert await _run_verifications(admin, worker_engine, env) == ["succeeded"]
    done = await _verification(owner, team.ws, r)
    assert done["status"] == "SUCCEEDED" and done["completed_at"] is not None and done["error"] is None
    body = (
        await owner.client.get("/api/v1/integrations/github/installations", headers={"X-Workspace-Id": team.ws})
    ).json()["items"][0]
    assert body["id"] == done["result_id"] == r.json()["id"]
    assert body["installation_id"] == 89 and body["status"] == "ACTIVE" and body["account_login"] == "acme2"
    assert body["permissions"] == {"contents": "write", "pull_requests": "write", "checks": "read",
                                   "statuses": "write", "metadata": "read"}  # fmt: skip
    # Повтор события (дубль доставки) не меняет результат.
    assert await _run_verifications(admin, worker_engine, env) == []
    # state однократен: тот же state с новым Idempotency-Key отклоняется.
    r = await owner.client.post(
        "/api/v1/integrations/github/installations", json={"installation_id": 89, "state": state},
        headers=owner.headers(team.ws, idem=True),
    )  # fmt: skip
    validate_response("connectGitHubInstallation", r)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "state_used"
    # state другого пользователя (CSRF/подмена) и подделанный state — отказ.
    r = await team.users["admin"].client.post(
        "/api/v1/integrations/github/installations", json={"installation_id": 89, "state": state},
        headers=team.users["admin"].headers(team.ws, idem=True),
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "invalid_state"
    r = await owner.client.post(
        "/api/v1/integrations/github/installations", json={"installation_id": 89, "state": "v1.x.y"},
        headers=owner.headers(team.ws, idem=True),
    )  # fmt: skip
    assert r.status_code == 422
    # Установка с лишними правами (workflows) не подключается (SC-GH-04) — FAILED на воркере.
    r_excess, _ = await _connect(team, "owner", 88)
    # Несуществующая установка.
    r_missing, _ = await _connect(team, "admin", 999_999)
    # Та же установка в другом workspace — 409 сразу в API (UNIQUE installation_id).
    other = await _team(gapp, app_engine, admin)
    r_conflict, _ = await _connect(other, "owner", 89)
    assert r_conflict.status_code == 409 and r_conflict.json()["code"] == "conflict"
    assert sorted(await _run_verifications(admin, worker_engine, env)) == ["failed"] * 2
    excess = await _verification(owner, team.ws, r_excess)
    assert excess["status"] == "FAILED" and excess["error"]["code"] == "permissions"
    assert "workflows:write" in excess["error"]["detail"]
    assert (await _verification(team.users["admin"], team.ws, r_missing))["error"]["code"] == "not_found"
    # Отклонённые установки не подключаются (REVOKED).
    statuses: dict[int, str] = dict(
        admin.execute(
            "SELECT installation_id, status FROM github_installations WHERE workspace_id = %s",
            (team.tenant.workspace_id,),
        ).fetchall()
    )
    assert statuses == {89: "ACTIVE", 88: "REVOKED", 999_999: "REVOKED"}
    # Проверка чужого workspace — 404.
    r = await other.users["owner"].client.get(
        f"/api/v1/integrations/github/verifications/{excess['id']}", headers={"X-Workspace-Id": other.ws}
    )
    validate_response("getGitHubVerification", r)
    assert r.status_code == 404
    r = await owner.client.get("/api/v1/integrations/github/installations", headers={"X-Workspace-Id": team.ws})
    validate_response("listGitHubInstallations", r)
    [item] = [i for i in r.json()["items"] if i["installation_id"] == 89]
    r = await owner.client.delete(
        f"/api/v1/integrations/github/installations/{item['id']}", headers=owner.headers(team.ws)
    )
    validate_response("disconnectGitHubInstallation", r)
    assert r.status_code == 204
    status = admin.execute("SELECT status FROM github_installations WHERE id = %s", (item["id"],)).fetchone()
    assert status == ("REVOKED",)
    # Повторное подключение отключённой установки этого workspace — реактивация.
    r, _ = await _connect(team, "owner", 89)
    assert await _run_verifications(admin, worker_engine, env) == ["succeeded"]
    again = await _verification(owner, team.ws, r)
    assert again["status"] == "SUCCEEDED" and again["result_id"] == item["id"]
    actions = {
        row[0]
        for row in admin.execute(
            "SELECT action || ':' || outcome FROM audit_events WHERE workspace_id = %s AND action LIKE 'github.%%'",
            (team.tenant.workspace_id,),
        ).fetchall()
    }
    assert {"github.installation.connect:success", "github.installation.disconnect:success",
            "github.installation.connect:denied", "github.verification.requested:success"} <= actions  # fmt: skip
    admin.execute("UPDATE github_installations SET status = 'REVOKED' WHERE installation_id = 89")


def test_api_process_never_loads_github_app_key() -> None:
    """OQ-47 (2): у процесса API нет пути к ключу App, а подключение установки и привязка репозитория работают
    (202): ключ читает только воркер (SECURITY_ARCHITECTURE §6.1)."""
    import aicd_api.routers.github as api_github

    assert not hasattr(api_github, "github_client")
    source = Path(api_github.__file__).read_text(encoding="utf-8")
    assert "build_client" not in source and "github_private_key_pem" not in source
    assert "installation_token" not in source and ".installation(" not in source


async def test_attach_github_repository(
    gapp: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, admin: psycopg.Connection, env: Env
) -> None:
    team = await _team(gapp, app_engine, admin)
    owner = team.users["owner"]
    project = str(team.tenant.project_id)
    payload = {"provider": "github", "remote_url": "https://github.test/acme/app", "local_path": str(env.local)}
    r = await owner.client.post(f"/api/v1/projects/{project}/repositories", json=payload,
                                headers=owner.headers(team.ws, idem=True))  # fmt: skip
    validate_response("attachRepository", r)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "no_installation"
    _new_installation(admin, team, env)
    for bad, code in (
        ({**payload, "remote_url": "https://evil.example/acme/app"}, "invalid"),
        ({k: v for k, v in payload.items() if k != "local_path"}, "required"),
        ({**payload, "local_path": "/etc"}, "invalid_repository"),
    ):
        r = await owner.client.post(f"/api/v1/projects/{project}/repositories", json=bad,
                                    headers=owner.headers(team.ws, idem=True))  # fmt: skip
        validate_response("attachRepository", r)
        assert r.status_code == 422 and r.json()["errors"][0]["code"] == code, r.text
    r = await team.users["developer"].client.post(
        f"/api/v1/projects/{project}/repositories", json=payload, headers=team.users["developer"].headers(team.ws, idem=True)
    )  # fmt: skip
    assert r.status_code == 403
    # Недоступный установке репозиторий — FAILED not_accessible на воркере.
    missing = await owner.client.post(
        f"/api/v1/projects/{project}/repositories", json={**payload, "remote_url": "https://github.test/acme/missing"},
        headers=owner.headers(team.ws, idem=True),
    )  # fmt: skip
    validate_response("attachRepository", missing)
    requests_before = len(env.gh.requests)
    r = await owner.client.post(f"/api/v1/projects/{project}/repositories", json=payload,
                                headers=owner.headers(team.ws, idem=True))  # fmt: skip
    validate_response("attachRepository", r)
    pending = await _verification(owner, team.ws, r)
    assert pending["status"] == "PENDING" and pending["kind"] == "repository" and pending["project_id"] == project
    assert len(env.gh.requests) == requests_before, "API не обращается к GitHub API"
    assert sorted(await _run_verifications(admin, worker_engine, env)) == ["failed", "succeeded"]
    assert (await _verification(owner, team.ws, missing))["error"]["code"] == "not_accessible"
    done = await _verification(owner, team.ws, r)
    assert done["status"] == "SUCCEEDED"
    listed = await owner.client.get(f"/api/v1/projects/{project}/repositories")
    assert listed.status_code == 200, listed.text
    [repo] = [i for i in listed.json()["items"] if i["id"] == done["result_id"]]
    assert repo["provider"] == "github" and repo["remote_url"] == "https://github.test/acme/app"
    row = admin.execute(
        "SELECT full_name, github_repo_id, github_installation_id IS NOT NULL FROM repositories WHERE id = %s",
        (done["result_id"],),
    ).fetchone()
    assert row == ("acme/app", 1001, True)
    jobs = admin.execute("SELECT kind FROM indexing_jobs WHERE repository_id = %s", (done["result_id"],)).fetchall()
    assert jobs == [("full",)], "первичная индексация поставлена воркером"
    # Проверка доступа шла токеном только на чтение и только для этого репозитория.
    request = env.gh.token_requests[-1]
    assert request["repositories"] == ["app"] and "write" not in request["permissions"].values()


# --- вебхук (P8-03) -------------------------------------------------------------------------------------


def _webhook_body(installation_id: int = 77, number: int = 1) -> bytes:
    return json.dumps(
        {"action": "closed", "installation": {"id": installation_id}, "repository": {"id": 1001, "full_name": "acme/app"},
         "pull_request": {"number": number, "merged": True, "head": {"ref": "aicd/x", "sha": "a" * 40}}}
    ).encode()  # fmt: skip


async def _post_webhook(
    app: FastAPI, body: bytes, *, event: str = "pull_request", guid: str | None = None, signature: str | None = "ok",
    secret: str = SECRET, ctype: str = "application/json",
) -> Any:  # fmt: skip
    headers = {"Content-Type": ctype, "X-GitHub-Event": event, "X-GitHub-Delivery": guid or str(uuid.uuid4())}
    if signature == "ok":
        headers["X-Hub-Signature-256"] = webhooks.sign(body, secret.encode())
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    async with make_client(app, ip="198.51.100.7") as client:
        return await client.post("/api/v1/integrations/github/webhook", content=body, headers=headers)


def _delivery(admin: psycopg.Connection, guid: str) -> tuple[Any, ...] | None:
    return admin.execute(
        "SELECT status, signature_valid, payload_storage_key FROM github_webhook_deliveries WHERE delivery_guid = %s",
        (guid,),
    ).fetchone()


def _events(admin: psycopg.Connection, guid: str) -> int:
    row = admin.execute(
        "SELECT count(*) FROM outbox_events WHERE event_type = 'github.webhook.received' AND payload->>'delivery_guid' = %s",
        (guid,),
    ).fetchone()
    return int(row[0]) if row else 0


async def test_webhook_signature_dedup_replay_and_limits(gapp: FastAPI, admin: psycopg.Connection) -> None:
    body = _webhook_body()
    guid = str(uuid.uuid4())
    r = await _post_webhook(gapp, body, guid=guid)
    validate_response("receiveGitHubWebhook", r)
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    assert _delivery(admin, guid) == ("RECEIVED", True, None), "payload не сохраняется (подсказка, SC-GH-03)"
    assert _events(admin, guid) == 1
    event = admin.execute(
        "SELECT workspace_id, payload FROM outbox_events WHERE payload->>'delivery_guid' = %s", (guid,)
    ).fetchone()
    assert event is not None and event[0] is None and event[1]["hint"]["pr_number"] == 1
    assert "merged" not in json.dumps(event[1]), "состояние из payload не переносится"
    # Replay той же доставки (даже с верной подписью) — 200 duplicate, без новой обработки.
    r = await _post_webhook(gapp, body, guid=guid)
    validate_response("receiveGitHubWebhook", r)
    assert r.status_code == 200 and r.json() == {"status": "duplicate"} and _events(admin, guid) == 1

    cases: dict[str, dict[str, Any]] = {
        "spoofed": {"signature": "sha256=" + "0" * 64},
        "missing": {"signature": None},
        "wrong_secret": {"secret": "attacker-secret"},
        "sha1_header": {"signature": "sha1=" + "0" * 40},
    }
    for name, kwargs in cases.items():
        g = str(uuid.uuid4())
        r = await _post_webhook(gapp, body, guid=g, **kwargs)
        validate_response("receiveGitHubWebhook", r)
        assert r.status_code == 401, name
        assert _delivery(admin, g) == ("REJECTED", False, None), name
        assert _events(admin, g) == 0, name
    # Изменённое тело при подписи исходного.
    g = str(uuid.uuid4())
    async with make_client(gapp, ip="198.51.100.7") as client:
        r = await client.post(
            "/api/v1/integrations/github/webhook", content=_webhook_body(number=2),
            headers={"Content-Type": "application/json", "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": g,
                     "X-Hub-Signature-256": webhooks.sign(body, SECRET.encode())},
        )  # fmt: skip
    assert r.status_code == 401 and _events(admin, g) == 0
    # Подделка с GUID будущей доставки не блокирует подлинную.
    g = str(uuid.uuid4())
    assert (await _post_webhook(gapp, body, guid=g, signature="sha256=" + "1" * 64)).status_code == 401
    r = await _post_webhook(gapp, body, guid=g)
    assert r.status_code == 202 and _events(admin, g) == 1
    # Ротация: предыдущий секрет ещё действует.
    g = str(uuid.uuid4())
    assert (await _post_webhook(gapp, body, guid=g, secret=PREVIOUS)).status_code == 202
    # Событие вне allowlist — 202 ignored без обработки.
    g = str(uuid.uuid4())
    r = await _post_webhook(gapp, b'{"action":"requested"}', event="workflow_run", guid=g)
    validate_response("receiveGitHubWebhook", r)
    assert (
        r.json() == {"status": "ignored"} and _delivery(admin, g) == ("IGNORED", True, None) and _events(admin, g) == 0
    )
    # Слишком большое тело — 413 до проверки подписи и без записи.
    g = str(uuid.uuid4())
    big = b'{"x":"' + b"a" * (70 * 1024) + b'"}'
    r = await _post_webhook(gapp, big, guid=g)
    validate_response("receiveGitHubWebhook", r)
    assert r.status_code == 413 and _delivery(admin, g) is None
    # Некорректные заголовки / тип содержимого.
    r = await _post_webhook(gapp, body, guid="not-a-uuid")
    validate_response("receiveGitHubWebhook", r)
    assert r.status_code == 400
    r = await _post_webhook(gapp, body, ctype="application/x-www-form-urlencoded")
    assert r.status_code == 415
    r = await _post_webhook(gapp, b"[1]", event="ping")
    assert r.status_code == 400
    audits: dict[str, int] = dict(
        admin.execute(
            "SELECT outcome, count(*) FROM audit_events WHERE action LIKE 'github.webhook.%%' AND workspace_id IS NULL"
            " AND actor_type = 'github' GROUP BY outcome"
        ).fetchall()
    )
    assert audits.get("denied", 0) >= 6 and audits.get("success", 0) >= 3


# --- PR / merge через API и RBAC-матрица (P8-04, P8-05, P8-09) -----------------------------------------------


@dataclass
class PrWorld:
    team: Team
    repo: uuid.UUID
    done: ghs.CompletedRun
    ci: ghs.CompletedRun
    pr: uuid.UUID


def _new_installation(admin: psycopg.Connection, team: Team, env: Env) -> uuid.UUID:
    """Своя установка App (account acme, доступ к acme/app) для workspace команды: installation_id глобально уникален."""
    number = secrets.randbelow(10**8) + 10_000
    env.gh.add_installation(number, "acme").repository_ids.add(1001)
    return ghs.installation(admin, team.tenant, number, "acme")


async def _world(app: FastAPI, engine: AsyncEngine, admin: psycopg.Connection, env: Env) -> PrWorld:
    team = await _team(app, engine, admin)
    inst = _new_installation(admin, team, env)
    ref = f"main/{env.local.name}"
    repo = ghs.github_repository(admin, team.tenant, installation_row=inst, full_name="acme/app", repo_id=1001,
                                 local_ref=ref)  # fmt: skip
    done = ghs.completed_run(admin, team.tenant, repository_id=repo, local=env.local, base_sha=env.base_sha,
                             files={f"src/{secrets.token_hex(3)}.py": "x = 1\n"})  # fmt: skip
    ci = ghs.completed_run(admin, team.tenant, repository_id=repo, local=env.local, base_sha=env.base_sha,
                           files={".github/workflows/ci.yml": "on: push\n"})  # fmt: skip
    pr = uuid7()
    admin.execute(
        "INSERT INTO pull_requests (id, workspace_id, repository_id, run_id, pr_number, head_branch, base_branch,"
        " head_sha, state, url) VALUES (%s, %s, %s, %s, %s, %s, 'main', %s, 'OPEN', %s)",
        (pr, team.tenant.workspace_id, repo, done.run, secrets.randbelow(10**6) + 100, done.branch, done.head_sha,
         "https://github.test/acme/app/pull/100"),
    )  # fmt: skip
    return PrWorld(team, repo, done, ci, pr)


async def test_pull_request_request_and_ci_config_guard(
    gapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection, env: Env
) -> None:
    w = await _world(gapp, app_engine, admin, env)
    dev = w.team.users["developer"]
    r = await dev.client.post(f"/api/v1/runs/{w.done.run}/pull-requests", json={"title": "Feature"},
                              headers=dev.headers(idem=True))  # fmt: skip
    validate_response("requestPullRequest", r)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["head_branch"] == w.done.branch and job["head_sha"] == w.done.head_sha and job["base_branch"] == "main"
    assert job["status"] == "QUEUED" and job["repository_full_name"] == "acme/app"
    events = admin.execute(
        "SELECT payload FROM outbox_events WHERE event_type = 'github.pr.requested' AND run_id = %s", (w.done.run,)
    ).fetchall()
    assert len(events) == 1 and events[0][0]["requested_by"] == dev.user_id
    # Изменения CI-конфигурации — 403 policy_denied + approval MANUAL_REVIEW (SC-GH-05).
    r = await dev.client.post(f"/api/v1/runs/{w.ci.run}/pull-requests", json={}, headers=dev.headers(idem=True))
    validate_response("requestPullRequest", r)
    assert r.status_code == 403 and r.json()["code"] == "policy_denied"
    approval = admin.execute(
        "SELECT action_type, status, subject->>'kind', subject->'paths' FROM approvals WHERE id = %s",
        (r.json()["approval_id"],),
    ).fetchone()
    assert approval == ("MANUAL_REVIEW", "PENDING", "ci_config_push", [".github/workflows/ci.yml"])
    # После APPROVED (решает другой пользователь) запрос PR проходит.
    reviewer = w.team.users["reviewer"]
    r2 = await reviewer.client.post(
        f"/api/v1/approvals/{r.json()['approval_id']}/decision", json={"decision": "APPROVED"},
        headers=reviewer.headers(idem=True, if_match='"v0"'),
    )  # fmt: skip
    assert r2.status_code == 200, r2.text
    r = await dev.client.post(f"/api/v1/runs/{w.ci.run}/pull-requests", json={}, headers=dev.headers(idem=True))
    assert r.status_code == 202, r.text
    # Незавершённый run / неизвестная задача.
    r = await dev.client.post(
        f"/api/v1/runs/{w.done.run}/pull-requests", json={"task_id": str(uuid.uuid4())}, headers=dev.headers(idem=True)
    )
    validate_response("requestPullRequest", r)
    assert r.status_code == 422


async def test_merge_request_four_eyes(
    gapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection, env: Env
) -> None:
    w = await _world(gapp, app_engine, admin, env)
    project = w.team.tenant.project_id
    requester = w.team.users["admin"]
    r = await requester.client.post(
        f"/api/v1/projects/{project}/pull-requests/{w.pr}/merge-request", json={"merge_method": "squash"},
        headers=requester.headers(idem=True),
    )  # fmt: skip
    validate_response("requestPullRequestMerge", r)
    assert r.status_code == 202, r.text
    approval_id = r.json()["approval_id"]
    subject = admin.execute(
        "SELECT action_type, required_role, subject FROM approvals WHERE id = %s", (approval_id,)
    ).fetchone()
    assert subject is not None and subject[0] == "MERGE" and subject[1] == "admin"
    assert subject[2]["head_sha"] == w.done.head_sha and subject[2]["merge_method"] == "squash"
    # Повтор того же предмета — тот же PENDING approval.
    r = await requester.client.post(
        f"/api/v1/projects/{project}/pull-requests/{w.pr}/merge-request", json={"merge_method": "squash"},
        headers=requester.headers(idem=True),
    )  # fmt: skip
    assert r.json()["approval_id"] == approval_id
    # Four-eyes: инициатор не утверждает; developer/reviewer — ниже admin.
    for who, code in (("admin", "four_eyes_violation"), ("developer", "forbidden"), ("reviewer", "forbidden")):
        user = w.team.users[who]
        r = await user.client.post(f"/api/v1/approvals/{approval_id}/decision", json={"decision": "APPROVED"},
                                   headers=user.headers(idem=True, if_match='"v0"'))  # fmt: skip
        assert r.status_code == 403 and r.json()["code"] == code, who
    second = w.team.users["admin2"]
    r = await second.client.post(f"/api/v1/approvals/{approval_id}/decision", json={"decision": "APPROVED"},
                                 headers=second.headers(idem=True, if_match='"v0"'))  # fmt: skip
    assert r.status_code == 200, r.text
    events = admin.execute(
        "SELECT payload->>'status', payload->'subject'->>'scope' FROM outbox_events WHERE event_type = 'approval.decided'"
        " AND aggregate_id = %s",
        (approval_id,),
    ).fetchall()
    assert events == [("APPROVED", "github")], "решение уходит воркеру GitHub через outbox"
    r = await requester.client.get(f"/api/v1/projects/{project}/pull-requests?run_id={w.done.run}")
    validate_response("listPullRequests", r)
    assert [i["id"] for i in r.json()["items"]] == [str(w.pr)]


# operationId → (метод, путь, тело, роли с доступом) — каждая GitHub-операция API.
def _operations(w: PrWorld) -> list[tuple[str, str, str, dict[str, Any] | None, set[str]]]:
    project = w.team.tenant.project_id
    all_roles = {"owner", "admin", "admin2", "developer", "reviewer", "viewer"}
    managers = {"owner", "admin", "admin2"}
    developers = managers | {"developer"}
    return [
        ("listGitHubInstallations", "GET", "/api/v1/integrations/github/installations", None, all_roles),
        ("startGitHubInstallation", "POST", "/api/v1/integrations/github/installations/setup", None, managers),
        ("connectGitHubInstallation", "POST", "/api/v1/integrations/github/installations",
         {"installation_id": 1, "state": "v1.a.b"}, managers),
        ("disconnectGitHubInstallation", "DELETE", f"/api/v1/integrations/github/installations/{uuid.uuid4()}", None,
         managers),
        ("getGitHubVerification", "GET", f"/api/v1/integrations/github/verifications/{uuid.uuid4()}", None, all_roles),
        ("attachRepository", "POST", f"/api/v1/projects/{project}/repositories",
         {"provider": "github", "remote_url": "https://github.test/acme/nope", "local_path": "/nope"}, managers),
        ("requestPullRequest", "POST", f"/api/v1/runs/{w.done.run}/pull-requests", {"task_id": str(uuid.uuid4())},
         developers),
        ("listPullRequests", "GET", f"/api/v1/projects/{project}/pull-requests", None, all_roles),
        ("requestPullRequestMerge", "POST", f"/api/v1/projects/{project}/pull-requests/{uuid.uuid4()}/merge-request",
         {}, developers),
    ]  # fmt: skip


async def _call(user: UserSession, ws: str, method: str, path: str, body: dict[str, Any] | None) -> Any:
    headers = user.headers(ws, idem=method == "POST")
    if method == "GET":
        return await user.client.get(path, headers={"X-Workspace-Id": ws})
    if method == "DELETE":
        return await user.client.delete(path, headers=headers)
    return await user.client.post(path, json=body or {}, headers=headers)


async def test_rbac_matrix_and_cross_workspace(
    gapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection, env: Env
) -> None:
    """P8-09: каждая GitHub-операция × роль; посторонний — 404 (AC-12)."""
    w = await _world(gapp, app_engine, admin, env)
    stranger = await login(gapp, app_engine)
    for op_id, method, path, body, allowed in _operations(w):
        for role, user in w.team.users.items():
            r = await _call(user, w.team.ws, method, path, body)
            validate_response(op_id, r)
            if role in allowed:
                assert r.status_code != 403 or r.json().get("code") == "policy_denied", (op_id, role, r.text)
            else:
                assert r.status_code == 403 and r.json()["code"] == "forbidden", (op_id, role, r.status_code)
        r = await _call(stranger, w.team.ws, method, path, body)
        validate_response(op_id, r)
        assert r.status_code == 404, (op_id, "чужой workspace", r.status_code)
    denied = admin.execute(
        "SELECT count(*) FROM audit_events WHERE workspace_id = %s AND action = 'authz.denied'",
        (w.team.tenant.workspace_id,),
    ).fetchone()
    assert denied is not None and denied[0] > 0, "отказ на небезопасных методах аудируется"
    await stranger.client.aclose()


def _cases(operation_id: str, count: int) -> list[Any]:
    schema = schemathesis.openapi.from_path(OPENAPI_PATH)
    op = schema.find_operation_by_id(operation_id)
    drawn: list[Any] = []

    @hypothesis.settings(
        max_examples=count, deadline=None, database=None, suppress_health_check=list(HealthCheck), derandomize=True
    )
    @given(case=op.as_strategy())
    def collect(case: Any) -> None:
        drawn.append(case)

    collect()
    return drawn


@pytest.mark.parametrize(
    "operation_id",
    [
        "listGitHubInstallations",
        "startGitHubInstallation",
        "connectGitHubInstallation",
        "disconnectGitHubInstallation",
        "getGitHubVerification",
        "requestPullRequest",
        "listPullRequests",
        "requestPullRequestMerge",
        "receiveGitHubWebhook",
    ],
)
async def test_contract_fuzz_never_5xx_and_responses_conform(
    gapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection, env: Env, operation_id: str
) -> None:
    w = await _world(gapp, app_engine, admin, env)
    owner = w.team.users["owner"]
    real = {"run_id": str(w.done.run), "project_id": str(w.team.tenant.project_id), "pull_request_id": str(w.pr)}
    for n, case in enumerate(_cases(operation_id, 20)):
        path = case.formatted_path
        if n % 2 == 0:
            for name, value in (case.path_parameters or {}).items():
                path = path.replace(str(value), real.get(name, str(value)), 1)
        headers = {k: v for k, v in (case.headers or {}).items() if k.lower() not in ("cookie", "x-csrf-token")}
        headers.setdefault("X-Workspace-Id", w.team.ws)
        method = case.method.upper()
        if method == "GET":
            r = await owner.client.get(f"/api/v1{path}", headers=headers, params=case.query or None)
        elif method == "DELETE":
            headers["X-CSRF-Token"] = owner.csrf
            r = await owner.client.delete(f"/api/v1{path}", headers=headers)
        else:
            headers["X-CSRF-Token"] = owner.csrf
            if operation_id != "receiveGitHubWebhook":
                headers.setdefault("Idempotency-Key", key())
            body = (
                case.body if isinstance(case.body, dict | list | str | int | float | bool) else None
            )  # NotSet — без тела
            r = await owner.client.post(f"/api/v1{path}", headers=headers, json=body, params=case.query or None)
        assert r.status_code < 500, f"{operation_id} {path} {case.body!r}: {r.status_code} {r.text}"
        validate_response(operation_id, r)
