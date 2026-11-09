"""OQ-51 (b), миграция 0016: обновление офлайн-БД уязвимостей только по утверждённому approval VULN_DB_UPDATE.

Запрос/авторизация/итог — функции `sys_vulndb_*` под ролью aicd_maintenance (`scripts.vulndb.approval`), решение —
API approvals (four-eyes, step-up, admin+). Авторизация однократна, предмет (виды БД, экосистемы, реестр) привязан
хэшем; истёкшее/неутверждённое approval — отказ.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from scripts.vulndb.approval import PlatformDb, authorize, build_subject, report, request
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import db as testdb
from tests.support.contract import validate_response
from tests.support.orch_api import HookRecorder, Team, key, make_team, orch_app
from tests.support.orchestration import connect_admin

pytestmark = pytest.mark.db

MIGRATION = (
    Path(__file__).resolve().parents[2] / "infrastructure/migrations/versions/0016_backup_grants_vulndb_approval.py"
)


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
async def oapp(settings: object) -> AsyncIterator[FastAPI]:
    async for application in orch_app(settings, HookRecorder()):  # type: ignore[arg-type]
        yield application


async def _decide(team: Team, who: str, approval_id: uuid.UUID, if_match: str = '"v0"') -> httpx.Response:
    user = team.users[who]
    r = await user.client.post(
        f"/api/v1/approvals/{approval_id}/decision", json={"decision": "APPROVED"},
        headers=user.headers(idem_key=key(), if_match=if_match),
    )  # fmt: skip
    validate_response("decideApproval", r)
    return r


def _sqlstate(exc: pytest.ExceptionInfo[psycopg.Error]) -> str | None:
    return exc.value.sqlstate


async def test_vulndb_update_requires_approved_platform_approval(
    oapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection, database: testdb.TestDatabase
) -> None:
    team = await make_team(oapp, app_engine, admin, ("admin", "developer"))
    db = PlatformDb(dsn=database.url_for("aicd_maintenance"))
    subject = build_subject(["PyPI", "npm"], "mirror.gcr.io/aquasec/trivy-db:2")
    assert subject["egress_hosts"] == ["mirror.gcr.io", "osv-vulnerabilities.storage.googleapis.com"]
    ws, project = team.tenant.workspace_id, team.tenant.project_id

    with pytest.raises(psycopg.Error) as exc:  # инициатор — не член workspace
        request(db, workspace_id=ws, project_id=project, requested_by_email="nobody@example.test", subject=subject)
    assert _sqlstate(exc) == "42501"

    approval_id = request(db, workspace_id=ws, project_id=project, requested_by_email=team.users["owner"].email,
                          subject=subject)  # fmt: skip
    assert request(db, workspace_id=ws, project_id=project, requested_by_email=team.users["owner"].email,
                   subject=subject) == approval_id, "повтор того же предмета — тот же PENDING"  # fmt: skip
    row = admin.execute(
        "SELECT status, required_role, risk_level, requested_by, expires_at - requested_at FROM approvals WHERE id = %s",
        (approval_id,),
    ).fetchone()
    assert row is not None and row[:4] == ("PENDING", "admin", "high", uuid.UUID(team.users["owner"].user_id))
    assert row[4].total_seconds() == 24 * 3600
    assert admin.execute(
        "SELECT count(*) FROM outbox_events WHERE aggregate_id = %s AND event_type = 'approval.requested'",
        (approval_id,),
    ).fetchone() == (1,)

    with pytest.raises(psycopg.Error) as exc:  # ещё не утверждено
        authorize(db, approval_id, subject, "op@host")
    assert _sqlstate(exc) == "42501"

    # Видно в API approvals; four-eyes: инициатор не утверждает; developer — ниже admin.
    r = await team.users["admin"].client.get(f"/api/v1/approvals/{approval_id}")
    validate_response("getApproval", r)
    assert r.json()["action_type"] == "VULN_DB_UPDATE" and r.json()["can_decide"] is True
    r = await _decide(team, "owner", approval_id)
    assert r.status_code == 403 and r.json()["code"] == "four_eyes_violation"
    r = await _decide(team, "developer", approval_id)
    assert r.status_code == 403
    r = await _decide(team, "admin", approval_id)  # свежий вход — step-up не требуется
    assert r.status_code == 200, r.text

    with pytest.raises(psycopg.Error) as exc:  # предмет другой (добавлена экосистема)
        authorize(db, approval_id, build_subject(["PyPI", "npm", "Go"], "mirror.gcr.io/aquasec/trivy-db:2"), "op@host")
    assert _sqlstate(exc) == "22023"
    granted = authorize(db, approval_id, subject, "op@host")
    assert granted["decided_by"] == team.users["admin"].user_id
    assert granted["requested_by"] == team.users["owner"].user_id
    with pytest.raises(psycopg.Error) as exc:  # однократно
        authorize(db, approval_id, subject, "op@host")
    assert _sqlstate(exc) == "42501"
    report(db, approval_id, "success", {"osv": {"sha256": ["0" * 64]}})
    actions = [
        r[0]
        for r in admin.execute(
            "SELECT action FROM audit_events WHERE resource_id = %s ORDER BY created_at, id", (approval_id,)
        ).fetchall()
    ]
    assert actions == ["approval.request", "approval.decide", "vulndb.update_authorized", "vulndb.update_finished"]

    # Downgrade 0016 не уничтожает историю approvals этого типа.
    spec = importlib.util.spec_from_file_location("m0016", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    guard = migration.DOWNGRADE_VULNDB.split("END $$;")[0] + "END $$;"
    with psycopg.connect(database.superuser_url) as conn, pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        conn.execute(guard)


async def test_expired_approval_is_refused(
    oapp: FastAPI, app_engine: AsyncEngine, admin: psycopg.Connection, database: testdb.TestDatabase
) -> None:
    team = await make_team(oapp, app_engine, admin, ("admin",))
    db = PlatformDb(dsn=database.url_for("aicd_maintenance"))
    subject = build_subject(["PyPI"], None)
    approval_id = request(db, workspace_id=team.tenant.workspace_id, project_id=team.tenant.project_id,
                          requested_by_email=team.users["owner"].email, subject=subject)  # fmt: skip
    assert (await _decide(team, "admin", approval_id)).status_code == 200
    admin.execute(
        "UPDATE approvals SET requested_at = requested_at - interval '2 days', expires_at = now() - interval '1 hour',"
        " version = version + 1 WHERE id = %s",
        (approval_id,),
    )
    with pytest.raises(psycopg.Error) as exc:
        authorize(db, approval_id, subject, "op@host")
    assert _sqlstate(exc) == "42501"
    with pytest.raises(psycopg.Error) as exc:  # итог без авторизации
        report(db, approval_id, "success", {})
    assert _sqlstate(exc) == "42501"
