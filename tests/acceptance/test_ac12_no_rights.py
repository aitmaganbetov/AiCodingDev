"""AC-12 (P9-01): пользователь без прав.

* не член workspace B: чтение run/задач/чата/approvals/SSE B, отправка поручения, решение по approval → `404`
  (не раскрывает существование; ответ идентичен несуществующему ресурсу); попытка учтена метрикой безопасности
  `authz_denied_total{reason="tenant"}` (API_DESIGN §17: 404 на чужие ресурсы — в метрики, не в audit);
* член B с ролью `viewer`: чтение разрешено, отправка поручения и утверждение критического действия → `403`,
  каждая попытка — запись `audit_events(action='authz.denied', outcome='denied')` с правом и маршрутом;
* SSE-поток run B для не члена недоступен.
Полный обход операций с чужими ID — tests/security/test_cross_tenant_api.py, RLS — tests/security/test_rls.py.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.approvals import Approvals
from aicd_platform.db import DbContext, transaction
from tests.conftest import UserSession, login
from tests.support import db as testdb
from tests.support.contract import validate_response
from tests.support.orchestration import connect_admin
from tests.support.phase3 import Slice, create_run, world

__all__ = ["world"]  # фикстура pytest

pytestmark = pytest.mark.db


def _tenant_denials() -> float:
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name.endswith("authz_denied_total") and sample.labels.get("reason") == "tenant":
                return float(sample.value)
    return 0.0


async def _request_merge_approval(s: Slice, run_id: str) -> uuid.UUID:
    async with transaction(s.worker, DbContext(workspace_id=uuid.UUID(s.ws), actor_type="system")) as conn:
        approval_id = await Approvals().request(
            conn,
            workspace_id=uuid.UUID(s.ws),
            project_id=uuid.UUID(s.project),
            run_id=uuid.UUID(run_id),
            task_id=None,
            action_type="MERGE",
            risk_level="high",
            subject={"pr": 7, "head_sha": "c" * 40},
            requested_by_type="system",
        )
    return uuid.UUID(str(approval_id))


def _directive(run_version: int) -> dict[str, Any]:
    return {
        "target": {"type": "run"},
        "directive_type": "CHANGE_REQUEST",
        "message": "Поменять подход",
        "expected_run_version": run_version,
    }


async def _denied_audits(database: testdb.TestDatabase, user: UserSession) -> list[dict[str, Any]]:
    with connect_admin(database) as conn:
        cur = conn.execute(
            "SELECT outcome, metadata FROM audit_events WHERE action = 'authz.denied' AND actor_id = %s"
            " ORDER BY created_at",
            (uuid.UUID(user.user_id),),
        )
        return [{"outcome": r[0], "metadata": r[1]} for r in cur.fetchall()]


async def test_ac12_non_member_gets_404_and_viewer_gets_403_with_audit(
    app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase, world: Slice
) -> None:
    run_id = await create_run(world)
    approval_id = await _request_merge_approval(world, run_id)
    run_version = (await world.user.client.get(f"/api/v1/runs/{run_id}")).json()["version"]
    tasks = (await world.user.client.get(f"/api/v1/runs/{run_id}/tasks")).json()["tasks"]

    viewer = await login(app, app_engine)
    added = await world.user.client.post(
        f"/api/v1/workspaces/{world.ws}/members",
        json={"email": viewer.email, "role": "viewer"},
        headers=world.user.headers(idem=True),
    )
    assert added.status_code == 201, added.text
    stranger = await login(app, app_engine)
    try:
        # --- не член workspace: 404 на чтение, поручение, решение по approval, SSE ---
        before = _tenant_denials()
        missing_run = uuid.uuid4()
        reads = [
            ("getRun", f"/api/v1/runs/{run_id}", f"/api/v1/runs/{missing_run}"),
            ("getRunTasks", f"/api/v1/runs/{run_id}/tasks", f"/api/v1/runs/{missing_run}/tasks"),
            ("getRunChat", f"/api/v1/runs/{run_id}/chat", f"/api/v1/runs/{missing_run}/chat"),
            ("getApproval", f"/api/v1/approvals/{approval_id}", f"/api/v1/approvals/{uuid.uuid4()}"),
        ]
        for op, foreign, absent in reads:
            r_foreign, r_absent = await stranger.client.get(foreign), await stranger.client.get(absent)
            validate_response(op, r_foreign)
            assert r_foreign.status_code == r_absent.status_code == 404, (op, r_foreign.text)
            assert r_foreign.json()["code"] == r_absent.json()["code"], "ответ не раскрывает существование"
        if tasks:
            assert (await stranger.client.get(f"/api/v1/tasks/{tasks[0]['id']}")).status_code == 404
        directive = await stranger.client.post(
            f"/api/v1/runs/{run_id}/directives", json=_directive(run_version), headers=stranger.headers(idem=True)
        )
        validate_response("createDirective", directive)
        assert directive.status_code == 404
        decision = await stranger.client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"decision": "APPROVED", "reason": "чужой"},
            headers=stranger.headers(idem=True, if_match='"v0"'),
        )
        validate_response("decideApproval", decision)
        assert decision.status_code == 404
        sse = await asyncio.wait_for(stranger.client.get(f"/api/v1/runs/{run_id}/events"), 10)
        assert sse.status_code == 404, "SSE-поток чужого run недоступен"
        assert _tenant_denials() - before >= len(reads) + 3, "попытки не члена учтены метрикой безопасности"
        assert await _denied_audits(database, stranger) == [], "404 не пишет audit (защита от флуда, API_DESIGN §17)"

        # --- viewer: чтение — да; поручение и утверждение критического действия — 403 + audit ---
        seen = await viewer.client.get(f"/api/v1/runs/{run_id}")
        validate_response("getRun", seen)
        assert seen.status_code == 200
        denied_directive = await viewer.client.post(
            f"/api/v1/runs/{run_id}/directives", json=_directive(run_version), headers=viewer.headers(idem=True)
        )
        validate_response("createDirective", denied_directive)
        assert denied_directive.status_code == 403 and denied_directive.json()["code"] == "forbidden"
        denied_decision = await viewer.client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"decision": "APPROVED", "reason": "viewer"},
            headers=viewer.headers(idem=True, if_match='"v0"'),
        )
        validate_response("decideApproval", denied_decision)
        assert denied_decision.status_code == 403
        audits = await _denied_audits(database, viewer)
        assert len(audits) >= 2 and all(a["outcome"] == "denied" for a in audits)
        assert all(a["metadata"]["role"] == "viewer" and a["metadata"]["method"] == "POST" for a in audits)
        routes = {a["metadata"]["route"] for a in audits}
        assert any("directives" in str(r) for r in routes) and any("approvals" in str(r) for r in routes), routes

        # Состояние B не изменилось: approval в ожидании, поручений нет.
        approval = await world.user.client.get(f"/api/v1/approvals/{approval_id}")
        assert approval.json()["status"] == "PENDING"
        listed = await world.user.client.get(f"/api/v1/runs/{run_id}/directives")
        assert listed.status_code == 200 and listed.json()["items"] == []
    finally:
        for session in (viewer, stranger):
            client: httpx.AsyncClient = session.client
            await client.aclose()
