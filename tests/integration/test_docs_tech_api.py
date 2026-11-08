"""Стек проекта, политика документации, источники и безопасный импорт (P5-07, P5-09, P5-10; AC-10).

Требует PostgreSQL с миграцией 0007 (technologies, documentation_*, sys_plan_docs_download).
Загрузка документации — через `security.ssrf` с явной тестовой политикой: имя `docs.example.com`
резолвится тестовым резолвером в 127.0.0.1 (разрешён только тестовым allowlist), транспорт — тестовый
(https-цель 443 → локальный http-сервер); в production 127.0.0.1 недоступен.
"""

from __future__ import annotations

import io
import ipaddress
import os
import secrets
import shutil
import subprocess
import threading
import uuid
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_code_intelligence.docs.importer import DocumentationImporter
from aicd_code_intelligence.docs.policy import ApprovalState
from aicd_code_intelligence.technologies.detect import detect_at_commit
from aicd_code_intelligence.technologies.store import ensure_technology_version, persist_detection
from aicd_orchestration import external_actions
from aicd_orchestration.docs_ports import DOCS_DOWNLOAD_TTL, DocsApprovals, DocsLedger
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_security.ssrf import PinnedTransport, SsrfFetcher, SsrfPolicy, Target
from tests.conftest import UserSession, login
from tests.support import db as testdb
from tests.support import orchestration as orch
from tests.support.contract import validate_response

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stacks"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": os.environ["PATH"],
}


def _git_fixture(tmp_path: Path, name: str) -> tuple[Path, str]:
    repo = tmp_path / f"{name}-{secrets.token_hex(3)}"
    shutil.copytree(FIXTURES / name, repo)
    run = {"check": True, "env": GIT_ENV, "capture_output": True, "text": True}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], **run)  # type: ignore[call-overload]
    subprocess.run(["git", "-C", str(repo), "add", "-A"], **run)  # type: ignore[call-overload]
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "i"], **run)  # type: ignore[call-overload]
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], **run).stdout.strip()  # type: ignore[call-overload]
    return repo, sha


def _worker_ctx(ws: str) -> DbContext:
    return DbContext(workspace_id=uuid.UUID(ws), actor_type="system")


async def _detect_and_store(worker_engine: AsyncEngine, tmp_path: Path, ws: str, project: str, fixture: str) -> None:
    repo, sha = _git_fixture(tmp_path, fixture)
    detected = detect_at_commit(repo, sha)
    async with transaction(worker_engine, _worker_ctx(ws)) as conn:
        await persist_detection(
            conn, workspace_id=uuid.UUID(ws), project_id=uuid.UUID(project), commit_sha=sha, detected=detected
        )


async def _add_member(owner: UserSession, ws: str, other: UserSession, role: str) -> None:
    r = await owner.client.post(
        f"/api/v1/workspaces/{ws}/members", json={"email": other.email, "role": role}, headers=owner.headers(idem=True)
    )
    assert r.status_code == 201, r.text


# --- P5-07: GET /projects/{id}/technologies -----------------------------------------------------


async def test_technologies_endpoint(
    app: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, user: UserSession, tmp_path: Path
) -> None:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    url = f"/api/v1/projects/{project['id']}/technologies"
    r = await user.client.get(url)
    validate_response("listTechnologies", r)
    assert r.status_code == 200 and r.json() == {"items": [], "next_cursor": None}

    await _detect_and_store(worker_engine, tmp_path, ws["id"], project["id"], "python")
    r = await user.client.get(url)
    validate_response("listTechnologies", r)
    items = {(i["ecosystem"], i["name"]): i for i in r.json()["items"]}
    fastapi = items[("python", "fastapi")]
    assert fastapi["version_label"] == "0.115.0" and fastapi["detected_from"] == "uv.lock"
    assert fastapi["support_status"] == "unknown" and fastapi["confirmed_by"] is None
    assert items[("lang", "python")]["version_label"] == "3.12.7"

    # Пагинация по курсору без дублей и пропусков.
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = await user.client.get(url, params=params)
        validate_response("listTechnologies", page)
        seen += [f"{i['ecosystem']}:{i['name']}" for i in page.json()["items"]]
        cursor = page.json()["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(f"{e}:{n}" for e, n in items) and len(seen) == len(set(seen))

    # Повторное определение (другой стек на новом commit) заменяет автоматически найденные строки.
    await _detect_and_store(worker_engine, tmp_path, ws["id"], project["id"], "php")
    names = {i["name"] for i in (await user.client.get(url, params={"limit": 200})).json()["items"]}
    assert "laravel/framework" in names and "fastapi" not in names

    # Изоляция: не член workspace — 404; viewer — 200; неверный X-Workspace-Id — 404.
    stranger = await login(app, app_engine)
    r = await stranger.client.get(url)
    validate_response("listTechnologies", r)
    assert r.status_code == 404
    viewer = await login(app, app_engine)
    await _add_member(user, ws["id"], viewer, "viewer")
    assert (await viewer.client.get(url)).status_code == 200
    other_ws = await stranger.create_workspace()
    assert (await user.client.get(url, headers={"X-Workspace-Id": other_ws["id"]})).status_code == 404
    bad = await user.client.get(url, params={"cursor": "garbage"})
    validate_response("listTechnologies", bad)
    assert bad.status_code == 422
    for s in (stranger, viewer):
        await s.client.aclose()


# --- P5-09: documentation-policy --------------------------------------------------------------


async def test_documentation_policy_get_patch(app: FastAPI, app_engine: AsyncEngine, user: UserSession) -> None:
    ws = await user.create_workspace()
    project = await user.create_project(ws["id"])
    url = f"/api/v1/projects/{project['id']}/documentation-policy"
    r = await user.client.get(url)
    validate_response("getDocumentationPolicy", r)
    body = r.json()
    assert body["documentation_mode"] == "SMART" and body["project_id"] == project["id"], "default SMART"
    tag = r.headers["etag"]
    assert tag == f'"v{body["settings_version"]}"'
    assert (await user.client.get(url, headers={"If-None-Match": tag})).status_code == 304

    missing = await user.client.patch(url, json={"documentation_mode": "OFF"}, headers=user.headers())
    validate_response("updateDocumentationPolicy", missing)
    assert missing.status_code == 428
    r = await user.client.patch(url, json={"documentation_mode": "OFF"}, headers=user.headers(if_match=tag))
    validate_response("updateDocumentationPolicy", r)
    assert r.status_code == 200 and r.json()["documentation_mode"] == "OFF" and r.headers["etag"] != tag
    stale = await user.client.patch(url, json={"documentation_mode": "STRICT"}, headers=user.headers(if_match=tag))
    validate_response("updateDocumentationPolicy", stale)
    assert stale.status_code == 412
    invalid = await user.client.patch(
        url, json={"documentation_mode": "AUTO"}, headers=user.headers(if_match=r.headers["etag"])
    )
    validate_response("updateDocumentationPolicy", invalid)
    assert invalid.status_code == 422
    extra = await user.client.patch(
        url, json={"documentation_mode": "OFF", "x": 1}, headers=user.headers(if_match=r.headers["etag"])
    )
    assert extra.status_code == 422

    # Проект видит новый режим (общая строка project_settings); ETag проекта не меняется.
    proj = (await user.client.get(f"/api/v1/projects/{project['id']}")).json()
    assert proj["settings"]["documentation_mode"] == "OFF" and proj["version"] == project["version"]

    audit = await user.client.get(
        "/api/v1/audit-events",
        headers={"X-Workspace-Id": ws["id"]},
        params={"action": "project.documentation_policy.update"},
    )
    assert audit.status_code == 200 and len(audit.json()["items"]) == 1

    dev = await login(app, app_engine)
    await _add_member(user, ws["id"], dev, "developer")
    assert (await dev.client.get(url)).status_code == 200
    denied = await dev.client.patch(
        url, json={"documentation_mode": "SMART"}, headers=dev.headers(if_match=r.headers["etag"])
    )
    validate_response("updateDocumentationPolicy", denied)
    assert denied.status_code == 403
    stranger = await login(app, app_engine)
    r404 = await stranger.client.get(url)
    validate_response("getDocumentationPolicy", r404)
    assert r404.status_code == 404
    assert (
        await stranger.client.patch(url, json={"documentation_mode": "OFF"}, headers=stranger.headers(if_match='"v1"'))
    ).status_code == 404
    for s in (dev, stranger):
        await s.client.aclose()


# --- P5-10: источники и refresh ---------------------------------------------------------------


async def _technology_version(worker_engine: AsyncEngine, ws: str) -> str:
    async with transaction(worker_engine, _worker_ctx(ws)) as conn:
        return str(await ensure_technology_version(conn, "python", "fastapi", "0.115.0"))


async def _register(user: UserSession, ws: str, tv: str, uri: str, **extra: Any) -> httpx.Response:
    body = {
        "technology_version_id": tv,
        "source_type": "url",
        "uri_or_file_ref": uri,
        "trust_level": "official",
        **extra,
    }
    return await user.client.post("/api/v1/documentation/sources", json=body, headers=user.headers(ws, idem=True))


async def test_documentation_sources_and_refresh(
    app: FastAPI, app_engine: AsyncEngine, worker_engine: AsyncEngine, user: UserSession
) -> None:
    ws = (await user.create_workspace())["id"]
    tv = await _technology_version(worker_engine, ws)
    uri = f"https://docs-{secrets.token_hex(3)}.example.com/guide/"
    r = await _register(user, ws, tv, uri, license_info="MIT", refresh_policy="scheduled")
    validate_response("registerDocumentationSource", r)
    assert r.status_code == 201, r.text
    source = r.json()
    assert source["latest_snapshot"] is None and source["refresh_policy"] == "scheduled"
    assert r.headers["location"].endswith(source["id"])

    dup = await _register(user, ws, tv, uri)
    validate_response("registerDocumentationSource", dup)
    assert dup.status_code == 409
    for bad in (
        "http://docs.example.com/",
        "https://user:pw@docs.example.com/",
        "https://169.254.169.254/latest/",
        "https://docs.internal/",
        "https://2130706433/",
        "https://docs.example.com:8443/",
    ):
        rb = await _register(user, ws, tv, bad)
        validate_response("registerDocumentationSource", rb)
        assert rb.status_code == 422 and rb.json()["errors"][0]["code"] == "ssrf_blocked", bad
    unknown_tv = await _register(user, ws, str(uuid.uuid4()), "https://docs.example.org/x")
    validate_response("registerDocumentationSource", unknown_tv)
    assert unknown_tv.status_code == 404
    file_src = await user.client.post(
        "/api/v1/documentation/sources",
        json={
            "technology_version_id": tv,
            "source_type": "file",
            "uri_or_file_ref": "https://docs.example.org/manual.pdf",
            "trust_level": "internal",
        },
        headers=user.headers(ws, idem=True),
    )
    validate_response("registerDocumentationSource", file_src)
    assert file_src.status_code == 201 and file_src.json()["source_type"] == "file"

    listing = await user.client.get("/api/v1/documentation/sources", headers={"X-Workspace-Id": ws})
    validate_response("listDocumentationSources", listing)
    assert {s["id"] for s in listing.json()["items"]} == {source["id"], file_src.json()["id"]}
    filtered = await user.client.get(
        "/api/v1/documentation/sources", headers={"X-Workspace-Id": ws}, params={"enabled": "false"}
    )
    assert filtered.json()["items"] == []
    page = await user.client.get("/api/v1/documentation/sources", headers={"X-Workspace-Id": ws}, params={"limit": 1})
    assert len(page.json()["items"]) == 1 and page.json()["next_cursor"]

    # Refresh: явный запрос человека = согласие; PLANNED docs_download в ledger.
    refresh_url = f"/api/v1/documentation/sources/{source['id']}/refresh"
    headers = user.headers(idem=True)
    r = await user.client.post(refresh_url, headers=headers)
    validate_response("refreshDocumentationSource", r)
    assert r.status_code == 202, r.text
    accepted = r.json()
    assert accepted["status"] == "QUEUED" and accepted["source_id"] == source["id"]
    replay = await user.client.post(refresh_url, headers=headers)
    assert replay.status_code == 202 and replay.json() == accepted
    async with transaction(worker_engine, _worker_ctx(ws)) as conn:
        action = await external_actions.get(conn, uuid.UUID(accepted["external_action_id"]))
        events = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE event_type = 'documentation_source.refresh_requested'"
                    " AND aggregate_id = :id"
                ),
                {"id": source["id"]},
            )
        ).scalar_one()
    assert action is not None and action.status == "PLANNED" and action.action_type == "docs_download"
    assert action.run_id is None and action.target == uri and action.request["source_id"] == source["id"]
    assert events == 1
    second = await user.client.post(refresh_url, headers=user.headers(idem=True))
    assert second.status_code == 202 and second.json()["external_action_id"] != accepted["external_action_id"]

    # Отключённый источник ⇒ 409; источник-артефакт по сети не загружается.
    disabled = await _register(user, ws, tv, "https://docs.example.net/off", enabled=False)
    assert disabled.status_code == 201
    r = await user.client.post(
        f"/api/v1/documentation/sources/{disabled.json()['id']}/refresh", headers=user.headers(idem=True)
    )
    validate_response("refreshDocumentationSource", r)
    assert r.status_code == 409

    # RBAC: reviewer читает, но не обновляет; developer обновляет, но не регистрирует.
    reviewer = await login(app, app_engine)
    await _add_member(user, ws, reviewer, "reviewer")
    assert (
        await reviewer.client.get("/api/v1/documentation/sources", headers={"X-Workspace-Id": ws})
    ).status_code == 200
    r = await reviewer.client.post(refresh_url, headers=reviewer.headers(idem=True))
    validate_response("refreshDocumentationSource", r)
    assert r.status_code == 403
    dev = await login(app, app_engine)
    await _add_member(user, ws, dev, "developer")
    assert (await dev.client.post(refresh_url, headers=dev.headers(idem=True))).status_code == 202
    r = await _register(dev, ws, tv, "https://docs.example.net/dev")
    validate_response("registerDocumentationSource", r)
    assert r.status_code == 403

    # Изоляция: чужой пользователь не видит и не обновляет источник (404), список его workspace пуст.
    stranger = await login(app, app_engine)
    other_ws = (await stranger.create_workspace())["id"]
    r = await stranger.client.post(refresh_url, headers=stranger.headers(idem=True))
    validate_response("refreshDocumentationSource", r)
    assert r.status_code == 404
    r = await stranger.client.post(refresh_url, headers=stranger.headers(other_ws, idem=True))
    assert r.status_code == 404
    assert (await stranger.client.get("/api/v1/documentation/sources", headers={"X-Workspace-Id": other_ws})).json()[
        "items"
    ] == []
    assert (
        await stranger.client.get("/api/v1/documentation/sources", headers={"X-Workspace-Id": ws})
    ).status_code == 404
    assert (
        await user.client.post(f"/api/v1/documentation/sources/{uuid.uuid4()}/refresh", headers=user.headers(idem=True))
    ).status_code == 404
    for s in (reviewer, dev, stranger):
        await s.client.aclose()


# --- P5-10: импортёр end-to-end ----------------------------------------------------------------


class _Docs(BaseHTTPRequestHandler):
    routes: ClassVar[dict[str, tuple[str, bytes]]] = {}

    def do_GET(self) -> None:
        ctype, body = self.routes.get(self.path, ("text/plain", b""))
        self.send_response(200 if self.path in self.routes else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — сигнатура базового класса
        return


@pytest.fixture(scope="module")
def docs_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Docs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


class _Resolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        if host not in self.mapping:
            raise OSError("NXDOMAIN")
        return [self.mapping[host]]


class _LocalHttpTransport:
    """Тестовый транспорт: проверенная https-цель (pinned IP) обслуживается локальным http-сервером."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.inner = PinnedTransport()

    def stream(self, target: Target, headers: Mapping[str, str], policy: SsrfPolicy) -> Any:
        return self.inner.stream(replace(target, scheme="http", port=self.port), headers, policy)


def _importer(worker_engine: AsyncEngine, tmp_path: Path, port: int, ip: str = "127.0.0.1") -> DocumentationImporter:
    policy = SsrfPolicy(extra_allowed_networks=(ipaddress.ip_network("127.0.0.1/32"),))
    fetcher = SsrfFetcher(policy, resolver=_Resolver({"docs.example.com": ip}), transport=_LocalHttpTransport(port))
    return DocumentationImporter(worker_engine, FileSystemArtifactStore(tmp_path / "artifacts"), fetcher, DocsLedger())


async def _refresh(user: UserSession, source_id: str) -> uuid.UUID:
    r = await user.client.post(f"/api/v1/documentation/sources/{source_id}/refresh", headers=user.headers(idem=True))
    assert r.status_code == 202, r.text
    return uuid.UUID(r.json()["external_action_id"])


async def test_importer_end_to_end(
    worker_engine: AsyncEngine, user: UserSession, docs_server: int, tmp_path: Path
) -> None:
    ws = (await user.create_workspace())["id"]
    tv = await _technology_version(worker_engine, ws)
    md = b"Intro\n\n# Install\n\npip install demo\n\n## Usage\n\nSPDX-License-Identifier: MIT\n"
    _Docs.routes["/guide.md"] = ("text/markdown", md)
    src = (await _register(user, ws, tv, "https://docs.example.com/guide.md")).json()
    importer = _importer(worker_engine, tmp_path, docs_server)

    action_id = await _refresh(user, src["id"])
    outcome = await importer.run(
        workspace_id=uuid.UUID(ws), source_id=uuid.UUID(src["id"]), external_action_id=action_id, fencing_token=1
    )
    assert outcome.status == "READY" and outcome.sections == 3 and outcome.license == "MIT"
    async with transaction(worker_engine, _worker_ctx(ws)) as conn:
        snap = (
            (
                await conn.execute(
                    text("SELECT * FROM documentation_snapshots WHERE id = :id"), {"id": outcome.snapshot_id}
                )
            )
            .mappings()
            .one()
        )
        sections = (
            (
                await conn.execute(
                    text(
                        "SELECT section_path, heading, body_ref FROM documentation_sections WHERE snapshot_id = :id ORDER BY section_path"
                    ),
                    {"id": outcome.snapshot_id},
                )
            )
            .mappings()
            .all()
        )
        action = await external_actions.get(conn, action_id)
        refreshed = (
            await conn.execute(
                text("SELECT last_refreshed_at FROM documentation_sources WHERE id = :id"), {"id": src["id"]}
            )
        ).scalar_one()
    assert (
        snap["status"] == "READY" and snap["mime_type"] == "text/markdown" and snap["external_action_id"] == action_id
    )
    assert [s["heading"] for s in sections] == [None, "Install", "Usage"]
    assert all(s["body_ref"].startswith("artifact:") for s in sections)
    assert action is not None and action.status == "CONFIRMED" and action.external_ref == str(outcome.snapshot_id)
    assert refreshed is not None
    listing = await user.client.get("/api/v1/documentation/sources", headers={"X-Workspace-Id": ws})
    validate_response("listDocumentationSources", listing)
    latest = next(s for s in listing.json()["items"] if s["id"] == src["id"])["latest_snapshot"]
    assert latest["id"] == str(outcome.snapshot_id) and latest["status"] == "READY"

    # Неизменённое содержимое: новый снимок не создаётся, ledger подтверждён ссылкой на существующий.
    again = await importer.run(
        workspace_id=uuid.UUID(ws),
        source_id=uuid.UUID(src["id"]),
        external_action_id=await _refresh(user, src["id"]),
        fencing_token=1,
    )
    assert again.status == "UNCHANGED" and again.snapshot_id == outcome.snapshot_id

    # Вредоносный архив ⇒ QUARANTINED, секции не создаются.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../escape.md", b"# pwned")
    _Docs.routes["/bundle.zip"] = ("application/zip", buf.getvalue())
    zsrc = (await _register(user, ws, tv, "https://docs.example.com/bundle.zip")).json()
    q = await importer.run(
        workspace_id=uuid.UUID(ws),
        source_id=uuid.UUID(zsrc["id"]),
        external_action_id=await _refresh(user, zsrc["id"]),
        fencing_token=1,
    )
    assert q.status == "QUARANTINED" and q.sections == 0 and q.error and "ARCHIVE_PATH" in q.error

    # Имя резолвится в metadata-адрес ⇒ FAILED, ledger FAILED, соединения не было.
    blocked = _importer(worker_engine, tmp_path, docs_server, ip="169.254.169.254")
    action_id = await _refresh(user, src["id"])
    f = await blocked.run(
        workspace_id=uuid.UUID(ws), source_id=uuid.UUID(src["id"]), external_action_id=action_id, fencing_token=1
    )
    assert f.status == "FAILED" and f.error == "SSRF_BLOCKED"
    async with transaction(worker_engine, _worker_ctx(ws)) as conn:
        failed = await external_actions.get(conn, action_id)
        status = (
            await conn.execute(text("SELECT status FROM documentation_snapshots WHERE id = :id"), {"id": f.snapshot_id})
        ).scalar_one()
    assert failed is not None and failed.status == "FAILED" and status == "FAILED"


async def test_docs_approvals_port_reuses_pending_and_reports_state(
    worker_engine: AsyncEngine, database: testdb.TestDatabase
) -> None:
    """DocsApprovals: повторный запрос того же предмета — тот же PENDING approval; TTL 7 дней; состояние."""
    with orch.connect_admin(database) as admin:
        tenant = orch.seed_tenant(admin)
    port = DocsApprovals()
    subject = {"kind": "docs_download", "uri": "https://docs.example.com/guide", "reason": "нужна справка"}
    async with transaction(worker_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as conn:
        first = await port.request_docs_download(
            conn,
            workspace_id=tenant.workspace_id,
            project_id=tenant.project_id,
            run_id=None,
            task_id=None,
            subject=subject,
            requested_by_role=None,
        )
        again = await port.request_docs_download(
            conn,
            workspace_id=tenant.workspace_id,
            project_id=tenant.project_id,
            run_id=None,
            task_id=None,
            subject=subject,
            requested_by_role=None,
        )
        assert first == again
        assert await port.approval_state(conn, first) == ApprovalState.PENDING
        ttl = (
            await conn.execute(text("SELECT expires_at - now() FROM approvals WHERE id = :a"), {"a": first})
        ).scalar_one()
        assert abs(ttl - DOCS_DOWNLOAD_TTL).total_seconds() < 60
        assert await port.approval_state(conn, uuid.uuid4()) == ApprovalState.NONE
