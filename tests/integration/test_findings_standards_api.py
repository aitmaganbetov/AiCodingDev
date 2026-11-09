"""Phase 7 (OQ-43 (c), (d)): жизненный цикл findings, вердикт человека по контролю, применимость контролей
проекта и guard завершения run по блокирующим контролям.

Каждый ответ сверяется с контрактом; RBAC по ролям (в т.ч. ACCEPTED_RISK — admin+), 404 для чужого workspace,
Idempotency-Key (повтор без побочных эффектов), If-Match/ETag, audit-события, Schemathesis-fuzz (нет 5xx,
ответы соответствуют контракту). Guard завершения: FAIL блокирующего контроля (явного или по умолчанию
платформы) не даёт run `COMPLETED`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import hypothesis
import psycopg
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7
from aicd_standards import api as standards
from aicd_verification.verdict import run_completion_verdict
from tests.conftest import UserSession, login, make_client
from tests.support import db as testdb
from tests.support import quality as q
from tests.support.contract import OPENAPI_PATH, validate_response
from tests.support.orch_api import Team, key, make_team, running_run
from tests.support.orchestration import Tenant, connect_admin, seed_tenant

pytestmark = pytest.mark.db

SECRET = "hunter2-very-secret"
ADR, LINT, SCOPE, SECRETS = "ARCH-42010-01", "QM-25010-01", "SEC-27001-01", "SEC-27002-01"
ROLES = ("admin", "developer", "reviewer", "viewer")


@dataclass
class Seed:
    team: Team
    run: uuid.UUID
    critical_run: uuid.UUID
    commit: str
    evidence: uuid.UUID
    pending_evidence: uuid.UUID
    other_evidence: uuid.UUID  # evidence другого run
    findings: dict[str, uuid.UUID] = field(default_factory=dict)
    controls: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def tenant(self) -> Tenant:
        return self.team.tenant

    def user(self, role: str) -> UserSession:
        return self.team.users[role]


def _finding(
    conn: psycopg.Connection, tenant: Tenant, run: uuid.UUID, severity: str, title: str, refs: list[str] | None = None
) -> uuid.UUID:
    fid = q.finding(conn, tenant, run, severity=severity, rule=f"rule.{severity}")
    conn.execute("UPDATE findings SET title = %s, control_refs = %s WHERE id = %s", (title, refs or [], fid))
    return fid


@pytest.fixture(scope="module")
def admin(database: testdb.TestDatabase) -> Iterator[psycopg.Connection]:
    with connect_admin(database) as conn:
        yield conn


@pytest.fixture(scope="module")
async def seed(
    app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase, admin: psycopg.Connection
) -> AsyncIterator[Seed]:
    team = await make_team(app, app_engine, admin, ROLES)
    tenant = team.tenant
    run = running_run(admin, tenant)
    critical_profile = admin.execute(
        "SELECT id FROM quality_profiles WHERE workspace_id IS NULL AND code = 'critical' ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]  # type: ignore[index]
    critical_run = uuid7()
    admin.execute(
        "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
        " quality_profile, quality_profile_id, documentation_mode)"
        " VALUES (%s, %s, %s, %s, %s, 'DEVELOP', 'objective', 'critical', %s, 'SMART')",
        (critical_run, tenant.workspace_id, tenant.project_id, tenant.repository_id, tenant.owner_id, critical_profile),
    )
    commit = q.sha1()
    s = Seed(
        team, run, critical_run, commit,
        evidence=q.evidence(admin, tenant, run, commit),
        pending_evidence=q.evidence(admin, tenant, run, commit, status="PENDING", kind="diff"),
        other_evidence=q.evidence(admin, tenant, critical_run, commit),
    )  # fmt: skip
    gate = q.ensure_gate(admin, "scope", "SCOPE")
    q.gate_execution(admin, tenant, run, gate, "SCOPE", "PASS", commit, evidence_id=s.evidence)
    s.findings["critical"] = _finding(admin, tenant, run, "critical", f"token={SECRET} в конфиге")
    s.findings["high"] = _finding(admin, tenant, run, "high", "SQL-инъекция", ["CWE-INJ-01"])
    for i in range(3):
        s.findings[f"medium{i}"] = _finding(admin, tenant, run, "medium", f"medium {i}")
    s.findings["low"] = _finding(admin, tenant, run, "low", "низкая")
    s.findings["crit_in_critical"] = _finding(admin, tenant, critical_run, "critical", "critical profile")
    s.findings["high_in_critical"] = _finding(admin, tenant, critical_run, "high", "critical profile high")
    s.controls = {
        code: admin.execute("SELECT id FROM standard_controls WHERE control_code = %s", (code,)).fetchone()[0]  # type: ignore[index]
        for code in (ADR, LINT, SCOPE, SECRETS)
    }
    # Утверждённая применимость (ADR — document_check, LINT/SCOPE — automated_gate); SECRETS — черновик.
    user_ctx = DbContext(user_id=tenant.owner_id, workspace_id=tenant.workspace_id)
    async with transaction(app_engine, user_ctx) as c:
        for code in (ADR, LINT, SCOPE):
            await standards.approve_project_control(
                c, workspace_id=tenant.workspace_id, project_id=tenant.project_id, control_id=s.controls[code],
                user_id=tenant.owner_id, applicability="applicable", rationale=None,
            )  # fmt: skip
    worker = await _worker(database)
    try:
        async with transaction(worker, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as c:
            await standards.propose_project_controls(c, workspace_id=tenant.workspace_id, project_id=tenant.project_id)
            await standards.evaluate_run(c, run)
    finally:
        await worker.dispose()
    yield s
    for session in team.users.values():
        await session.client.aclose()


async def _worker(database: testdb.TestDatabase) -> AsyncEngine:
    from aicd_platform.db import create_engine

    return create_engine(database.url_for("aicd_worker"), pool_size=2)


def _etag(r: Any) -> str:
    tag: str = r.headers["etag"]
    return tag


# --- findings: список, карточка -----------------------------------------------------------------------


async def test_list_run_findings_filters_and_pagination(seed: Seed) -> None:
    client = seed.user("viewer").client
    r = await client.get(f"/api/v1/runs/{seed.run}/findings")
    validate_response("listRunFindings", r)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [f["severity"] for f in items][:2] == ["critical", "high"] and len(items) == 6
    assert SECRET not in r.text and "[REDACTED]" in items[0]["title"]
    high = items[1]
    assert high["control_refs"] == ["CWE-INJ-01"] and high["run_id"] == str(seed.run) and high["version"] >= 1
    # Фильтры: повторяемые severity/status, контроль; курсор привязан к фильтрам.
    med = await client.get(f"/api/v1/runs/{seed.run}/findings", params=[("severity", "medium"), ("severity", "low")])
    assert {f["severity"] for f in med.json()["items"]} == {"medium", "low"}
    ref = await client.get(f"/api/v1/runs/{seed.run}/findings", params={"control_ref": "CWE-INJ-01"})
    assert [f["id"] for f in ref.json()["items"]] == [str(seed.findings["high"])]
    seen: list[str] = []
    cursor = None
    while True:
        page = await client.get(
            f"/api/v1/runs/{seed.run}/findings", params={"limit": 2, **({"cursor": cursor} if cursor else {})}
        )
        validate_response("listRunFindings", page)
        seen += [f["id"] for f in page.json()["items"]]
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == [f["id"] for f in items]
    first = await client.get(f"/api/v1/runs/{seed.run}/findings", params={"limit": 1})
    bad = await client.get(
        f"/api/v1/runs/{seed.run}/findings",
        params={"limit": 1, "status": "OPEN", "cursor": first.json()["next_cursor"]},
    )
    validate_response("listRunFindings", bad)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_cursor"


async def test_get_finding_etag_and_not_modified(seed: Seed) -> None:
    client = seed.user("viewer").client
    r = await client.get(f"/api/v1/findings/{seed.findings['low']}")
    validate_response("getFinding", r)
    assert r.status_code == 200 and _etag(r) == f'"v{r.json()["version"]}"'
    assert r.json()["status"] == "OPEN" and r.json()["resolved_by"] is None
    again = await client.get(f"/api/v1/findings/{seed.findings['low']}", headers={"If-None-Match": _etag(r)})
    assert again.status_code == 304
    missing = await client.get(f"/api/v1/findings/{uuid7()}")
    validate_response("getFinding", missing)
    assert missing.status_code == 404


# --- findings: решения человека -----------------------------------------------------------------------


async def _transition(
    user: UserSession, finding: uuid.UUID, to_status: str, *, reason: str = "разобрано", if_match: str | None,
    idem: str | None = None,
) -> Any:  # fmt: skip
    r = await user.client.post(
        f"/api/v1/findings/{finding}/transitions",
        json={"to_status": to_status, "reason": reason},
        headers=user.headers(idem_key=idem or key(), if_match=if_match),
    )
    validate_response("transitionFinding", r)
    return r


async def _current_etag(user: UserSession, finding: uuid.UUID) -> str:
    return _etag(await user.client.get(f"/api/v1/findings/{finding}"))


async def test_transition_lifecycle_etag_idempotency_and_audit(seed: Seed, admin: psycopg.Connection) -> None:
    reviewer, fid = seed.user("reviewer"), seed.findings["medium0"]
    tag = await _current_etag(reviewer, fid)
    idem = key()
    ack = await _transition(reviewer, fid, "ACKNOWLEDGED", if_match=tag, idem=idem)
    assert ack.status_code == 200, ack.text
    body = ack.json()
    assert body["status"] == "ACKNOWLEDGED" and body["resolved_by"]["id"] == reviewer.user_id
    assert _etag(ack) == f'"v{body["version"]}"' and _etag(ack) != tag
    # Повтор с тем же ключом — сохранённый ответ, без второго перехода и второго audit.
    replay = await _transition(reviewer, fid, "ACKNOWLEDGED", if_match=tag, idem=idem)
    assert replay.status_code == 200 and replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == body
    reuse = await _transition(reviewer, fid, "FALSE_POSITIVE", if_match=tag, idem=idem)
    assert reuse.status_code == 422 and reuse.json()["code"] == "idempotency_key_reuse"
    # If-Match: устаревший → 412 с актуальным ETag; без него → 428; без ключа → 400.
    stale = await _transition(reviewer, fid, "FALSE_POSITIVE", if_match=tag)
    assert stale.status_code == 412 and stale.headers["etag"] == _etag(ack)
    missing = await _transition(reviewer, fid, "FALSE_POSITIVE", if_match=None)
    assert missing.status_code == 428
    no_key = await reviewer.client.post(
        f"/api/v1/findings/{fid}/transitions", json={"to_status": "OPEN", "reason": "x"},
        headers=reviewer.headers(if_match=_etag(ack)),
    )  # fmt: skip
    validate_response("transitionFinding", no_key)
    assert no_key.status_code == 400 and no_key.json()["code"] == "idempotency_key_missing"
    # Недопустимый переход FSM (ACKNOWLEDGED → OPEN не из правил Verification Engine) → 409.
    bad = await _transition(reviewer, fid, "OPEN", if_match=_etag(ack))
    assert bad.status_code == 409 and bad.json()["code"] == "invalid_state_transition"
    assert bad.json()["current_status"] == "ACKNOWLEDGED"
    # Ложное срабатывание (reviewer), затем повторное открытие — решение снимается, причина сохраняется.
    fp = await _transition(reviewer, fid, "FALSE_POSITIVE", reason=f"тест; password={SECRET}", if_match=_etag(ack))
    assert fp.status_code == 200 and fp.json()["status"] == "FALSE_POSITIVE"
    assert SECRET not in fp.text and "[REDACTED]" in fp.json()["resolution_reason"]
    reopen = await _transition(reviewer, fid, "OPEN", reason="воспроизводится", if_match=_etag(fp))
    assert reopen.status_code == 200 and reopen.json()["status"] == "OPEN" and reopen.json()["resolved_by"] is None
    events = admin.execute(
        "SELECT metadata->>'from', metadata->>'to', actor_id FROM audit_events"
        " WHERE action = 'finding.transition' AND resource_id = %s ORDER BY created_at, id",
        (fid,),
    ).fetchall()
    assert [(e[0], e[1]) for e in events] == [
        ("OPEN", "ACKNOWLEDGED"), ("ACKNOWLEDGED", "FALSE_POSITIVE"), ("FALSE_POSITIVE", "OPEN"),
    ]  # fmt: skip
    assert {str(e[2]) for e in events} == {reviewer.user_id}


async def test_accepted_risk_rbac_and_profile_limit(seed: Seed, admin: psycopg.Connection) -> None:
    # ACCEPTED_RISK — admin и выше: reviewer/developer получают 403 (отказ аудируется), viewer — 403 на triage.
    fid = seed.findings["medium1"]
    for role in ("reviewer", "developer", "viewer"):
        user = seed.user(role)
        r = await _transition(user, fid, "ACCEPTED_RISK", if_match=await _current_etag(user, fid))
        assert r.status_code == 403 and r.json()["code"] == "forbidden", role
    denied = admin.execute(
        "SELECT count(*) FROM audit_events WHERE action = 'authz.denied' AND actor_id = %s"
        " AND metadata->>'permission' = 'finding:triage:ACCEPTED_RISK'",
        (uuid.UUID(seed.user("reviewer").user_id),),
    ).fetchone()
    assert denied == (1,)
    viewer = seed.user("viewer")
    r = await _transition(viewer, fid, "ACKNOWLEDGED", if_match=await _current_etag(viewer, fid))
    assert r.status_code == 403
    adm = seed.user("admin")
    ok = await _transition(
        adm, fid, "ACCEPTED_RISK", reason="риск принят до релиза", if_match=await _current_etag(adm, fid)
    )
    assert ok.status_code == 200 and ok.json()["status"] == "ACCEPTED_RISK"
    assert ok.json()["resolved_by"]["id"] == adm.user_id
    # Профиль critical: critical-находку принять как риск нельзя (422), high — можно.
    owner = seed.user("owner")
    crit = seed.findings["crit_in_critical"]
    r = await _transition(owner, crit, "ACCEPTED_RISK", if_match=await _current_etag(owner, crit))
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/to_status"
    high = seed.findings["high_in_critical"]
    r = await _transition(owner, high, "ACCEPTED_RISK", if_match=await _current_etag(owner, high))
    assert r.status_code == 200
    # FIXED/VERIFIED_FIXED человек не выставляет — схема запроса (422).
    r = await owner.client.post(
        f"/api/v1/findings/{crit}/transitions", json={"to_status": "VERIFIED_FIXED", "reason": "x"},
        headers=owner.headers(idem=True, if_match=await _current_etag(owner, crit)),
    )  # fmt: skip
    validate_response("transitionFinding", r)
    assert r.status_code == 422


# --- вердикт человека по контролю -------------------------------------------------------------------


async def _review(
    user: UserSession, s: Seed, code: str, status: str = "PASS", evidence: list[uuid.UUID] | None = None,
    *, notes: str = "проверено вручную", run: uuid.UUID | None = None, idem: str | None = None,
) -> Any:  # fmt: skip
    r = await user.client.post(
        f"/api/v1/runs/{run or s.run}/compliance/{s.controls[code]}/review",
        json={"status": status, "notes": notes, "evidence_ids": [str(e) for e in (evidence or [s.evidence])]},
        headers=user.headers(idem_key=idem or key()),
    )
    validate_response("reviewComplianceControl", r)
    return r


async def _compliance(user: UserSession, s: Seed) -> dict[str, dict[str, Any]]:
    r = await user.client.get(f"/api/v1/runs/{s.run}/compliance", params={"limit": 200})
    validate_response("getRunCompliance", r)
    return {row["control_code"]: row for row in r.json()["items"]}


async def test_compliance_review_flow(seed: Seed, admin: psycopg.Connection, database: testdb.TestDatabase) -> None:
    reviewer, viewer = seed.user("reviewer"), seed.user("viewer")
    before = await _compliance(viewer, seed)
    assert before[ADR]["status"] == "MANUAL_REVIEW" and before[LINT]["status"] == "UNVERIFIED"
    assert before[SCOPE]["status"] == "PASS" and before[SCOPE]["blocking"] is True  # security в strict
    assert before[LINT]["blocking"] is False and before[ADR]["notes"]
    # viewer — 403; член другого workspace — 404.
    r = await _review(viewer, seed, ADR)
    assert r.status_code == 403
    idem = key()
    ok = await _review(reviewer, seed, ADR, evidence=[seed.evidence, seed.pending_evidence],
                       notes=f"ADR проверен; token={SECRET}", idem=idem)  # fmt: skip
    assert ok.status_code == 200, ok.text
    row = ok.json()
    assert row["status"] == "PASS" and row["reviewed_by"]["id"] == reviewer.user_id
    assert row["evidence_id"] == str(seed.evidence) and SECRET not in ok.text
    replay = await _review(reviewer, seed, ADR, evidence=[seed.evidence, seed.pending_evidence],
                           notes=f"ADR проверен; token={SECRET}", idem=idem)  # fmt: skip
    assert replay.headers["idempotency-replayed"] == "true" and replay.json() == row
    # Переопределение UNVERIFIED автоматического контроля; PASS gate не переопределяется (409).
    override = await _review(reviewer, seed, LINT, "FAIL", [seed.pending_evidence], notes="линтер падает локально")
    assert override.status_code == 200 and override.json()["status"] == "FAIL"
    conflict = await _review(reviewer, seed, SCOPE)
    assert conflict.status_code == 409 and conflict.json()["code"] == "invalid_state_transition"
    # Неутверждённый контроль — 409; evidence другого run / PASS без VERIFIED / повтор evidence — 422.
    assert (await _review(reviewer, seed, SECRETS)).status_code == 409
    for evidence in ([seed.other_evidence], [seed.pending_evidence], [seed.evidence, seed.evidence]):
        bad = await _review(reviewer, seed, LINT, "PASS", evidence)
        assert bad.status_code == 422, evidence
    after = await _compliance(viewer, seed)
    assert after[ADR]["status"] == "PASS" and after[ADR]["result_id"] == row["result_id"]
    assert after[LINT]["status"] == "FAIL"
    audit = admin.execute(
        "SELECT metadata->>'control_code', metadata->>'from', metadata->>'to' FROM audit_events"
        " WHERE action = 'compliance.review' AND workspace_id = %s ORDER BY created_at, id",
        (seed.tenant.workspace_id,),
    ).fetchall()
    assert [tuple(a) for a in audit] == [(ADR, "MANUAL_REVIEW", "PASS"), (LINT, "UNVERIFIED", "FAIL")]
    # Повторная оценка движком сохраняет вердикты человека (и переопределение UNVERIFIED).
    worker = await _worker(database)
    try:
        async with transaction(worker, DbContext(workspace_id=seed.tenant.workspace_id, actor_type="system")) as c:
            await standards.evaluate_run(c, seed.run)
    finally:
        await worker.dispose()
    final = await _compliance(viewer, seed)
    assert final[ADR]["result_id"] == row["result_id"] and final[LINT]["status"] == "FAIL"
    assert final[LINT]["reviewed_by"]["id"] == reviewer.user_id


# --- применимость контролей проекта ----------------------------------------------------------------


async def test_project_controls_list_and_approval(seed: Seed, admin: psycopg.Connection) -> None:
    project = seed.tenant.project_id
    viewer = seed.user("viewer")
    r = await viewer.client.get(f"/api/v1/projects/{project}/controls", params={"limit": 200})
    validate_response("listProjectControls", r)
    body = r.json()
    assert r.status_code == 200 and len(body["items"]) == 33 and body["quality_profile"] == "strict"
    assert "не является" in body["disclaimer"]
    rows = {i["control_code"]: i for i in body["items"]}
    assert rows[ADR]["approval_state"] == "approved" and rows[ADR]["applicability"] == "APPLICABLE"
    assert rows[SECRETS]["approval_state"] == "draft" and rows[SECRETS]["applicability"] == "PENDING_REVIEW"
    assert rows[SECRETS]["effective_blocking"] is True and rows[SECRETS]["blocking"] is None
    drafts = await viewer.client.get(f"/api/v1/projects/{project}/controls", params={"approval_state": "draft"})
    assert {i["approval_state"] for i in drafts.json()["items"]} == {"draft"}
    page = await viewer.client.get(f"/api/v1/projects/{project}/controls", params={"limit": 10})
    nxt = await viewer.client.get(
        f"/api/v1/projects/{project}/controls", params={"limit": 10, "cursor": page.json()["next_cursor"]}
    )
    validate_response("listProjectControls", nxt)
    assert [i["control_id"] for i in page.json()["items"] + nxt.json()["items"]] == [
        i["control_id"] for i in body["items"][:20]
    ]
    path = f"/api/v1/projects/{project}/controls/{seed.controls[SECRETS]}/approval"
    # admin+ (project:update): developer/reviewer — 403.
    for role in ("developer", "reviewer"):
        user = seed.user(role)
        r = await user.client.post(path, json={"applicability": "applicable"}, headers=user.headers(idem=True))
        validate_response("approveProjectControl", r)
        assert r.status_code == 403, role
    adm = seed.user("admin")
    r = await adm.client.post(path, json={"applicability": "tailored"}, headers=adm.headers(idem=True))
    validate_response("approveProjectControl", r)
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/rationale"
    r = await adm.client.post(
        path, json={"applicability": "tailored", "rationale": "только сервис API", "blocking": False,
                    "agent_role": "devops"},
        headers=adm.headers(idem=True),
    )  # fmt: skip
    validate_response("approveProjectControl", r)
    assert r.status_code == 200, r.text
    ctl = r.json()
    assert ctl["approval_state"] == "approved" and ctl["applicability"] == "APPLICABLE"
    assert ctl["proposed_applicability"] == "tailored" and ctl["approved_by"]["id"] == adm.user_id
    assert ctl["blocking"] is False and ctl["effective_blocking"] is False and ctl["agent_role"] == "devops"
    missing = await adm.client.post(
        f"/api/v1/projects/{project}/controls/{uuid7()}/approval", json={"applicability": "applicable"},
        headers=adm.headers(idem=True),
    )  # fmt: skip
    validate_response("approveProjectControl", missing)
    assert missing.status_code == 404
    audit = admin.execute(
        "SELECT metadata->>'control_code', metadata->'from'->>'state' FROM audit_events"
        " WHERE action = 'project_control.approve' AND resource_id = %s",
        (seed.controls[SECRETS],),
    ).fetchall()
    assert [tuple(a) for a in audit] == [(SECRETS, "draft")]


# --- tenant-изоляция ----------------------------------------------------------------------------------


async def test_non_members_get_404_and_anonymous_401(app: FastAPI, app_engine: AsyncEngine, seed: Seed) -> None:
    outsider = await login(app, app_engine)
    gets = {
        "listRunFindings": f"/api/v1/runs/{seed.run}/findings",
        "getFinding": f"/api/v1/findings/{seed.findings['low']}",
        "listProjectControls": f"/api/v1/projects/{seed.tenant.project_id}/controls",
    }
    posts = {
        "transitionFinding": (
            f"/api/v1/findings/{seed.findings['low']}/transitions",
            {"to_status": "ACKNOWLEDGED", "reason": "x"},
        ),
        "reviewComplianceControl": (
            f"/api/v1/runs/{seed.run}/compliance/{seed.controls[ADR]}/review",
            {"status": "PASS", "notes": "x", "evidence_ids": [str(seed.evidence)]},
        ),
        "approveProjectControl": (
            f"/api/v1/projects/{seed.tenant.project_id}/controls/{seed.controls[ADR]}/approval",
            {"applicability": "applicable"},
        ),
    }
    try:
        await outsider.create_workspace()
        for op_id, path in gets.items():
            r = await outsider.client.get(path)
            validate_response(op_id, r)
            assert r.status_code == 404, op_id
        for op_id, (path, body) in posts.items():
            r = await outsider.client.post(path, json=body, headers=outsider.headers(idem=True, if_match='"v1"'))
            validate_response(op_id, r)
            assert r.status_code == 404 and r.json()["code"] == "not_found", op_id
    finally:
        await outsider.client.aclose()
    async with make_client(app) as anon:
        for op_id, path in gets.items():
            r = await anon.get(path)
            validate_response(op_id, r)
            assert r.status_code == 401


# --- guard завершения run: блокирующие контроли ---------------------------------------------------------


async def test_completion_guard_rejects_failed_blocking_control(
    database: testdb.TestDatabase, worker_engine: AsyncEngine
) -> None:
    """QUALITY_GATES §5.3 п.6: run с единственным обязательным gate PASS (полное evidence) проходит guard,
    пока FAIL контроля безопасности (блокирующий по умолчанию в strict) не появится в compliance_results;
    явное `blocking = false` снимает блокировку."""
    commit = q.sha1()
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn)
        profile = q.tenant_profile(conn, tenant)
        gate = q.ensure_gate(conn, "scope", "SCOPE", profile=(profile, tenant.workspace_id))
        run = uuid7()
        conn.execute(
            "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
            " quality_profile, quality_profile_id, documentation_mode)"
            " VALUES (%s, %s, %s, %s, %s, 'DEVELOP', 'objective', 'strict', %s, 'SMART')",
            (run, tenant.workspace_id, tenant.project_id, tenant.repository_id, tenant.owner_id, profile),
        )
        ev = uuid7()  # evidence процесса с полным набором полей AC-14
        conn.execute(
            "INSERT INTO evidence (id, workspace_id, run_id, kind, artifact_id, producer, verification_status,"
            " commit_sha, command, exit_code, environment, started_at, finished_at, verified_at)"
            " VALUES (%s, %s, %s, 'command_result', %s, 'verification_engine', 'VERIFIED', %s, 'scope --check', 0,"
            ' \'{"runner": "local", "tool_versions": {"scope": "1"}}\', now(), now(), now())',
            (ev, tenant.workspace_id, run, q.artifact(conn, tenant, run), commit),
        )
        q.gate_execution(conn, tenant, run, gate, "SCOPE", "PASS", commit, evidence_id=ev)
        control = conn.execute("SELECT id FROM standard_controls WHERE control_code = %s", (SECRETS,)).fetchone()[0]  # type: ignore[index]

    async def verdict() -> Any:
        async with transaction(worker_engine, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as c:
            return await run_completion_verdict(c, run)

    baseline = await verdict()
    assert baseline.ok, baseline.reasons
    with connect_admin(database) as conn:
        conn.execute(
            "INSERT INTO compliance_results (id, workspace_id, run_id, control_id, status, evidence_id)"
            " VALUES (%s, %s, %s, %s, 'FAIL', %s)",
            (uuid7(), tenant.workspace_id, run, control, ev),
        )
    blocked = await verdict()
    assert not blocked.ok and blocked.reasons == (f"blocking control {SECRETS} is FAIL",)
    with connect_admin(database) as conn:
        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(tenant.owner_id),))
        conn.execute(
            "INSERT INTO project_controls (workspace_id, project_id, control_id, applicability, blocking, approved_by,"
            " approved_at) VALUES (%s, %s, %s, 'applicable', false, %s, now())",
            (tenant.workspace_id, tenant.project_id, control, tenant.owner_id),
        )
    assert (await verdict()).ok
    with connect_admin(database) as conn:
        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(tenant.owner_id),))
        conn.execute(
            "UPDATE project_controls SET blocking = true WHERE project_id = %s AND control_id = %s",
            (tenant.project_id, control),
        )
    assert not (await verdict()).ok


# --- fuzz по контракту ---------------------------------------------------------------------------------


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
        "listRunFindings",
        "getFinding",
        "listProjectControls",
        "transitionFinding",
        "reviewComplianceControl",
        "approveProjectControl",
    ],
)
async def test_contract_fuzz_never_5xx_and_responses_conform(seed: Seed, operation_id: str) -> None:
    real = {
        "run_id": str(seed.run),
        "finding_id": str(seed.findings["medium2"]),
        "project_id": str(seed.tenant.project_id),
        "control_id": str(seed.controls[ADR]),
    }
    owner = seed.user("owner")
    for n, case in enumerate(_cases(operation_id, 25)):
        path = case.formatted_path
        if n % 2 == 0:  # половина случаев — на существующих ресурсах, чтобы fuzz доходил до запросов к БД
            for name, value in (case.path_parameters or {}).items():
                path = path.replace(str(value), real.get(name, str(value)), 1)
        headers = {k: v for k, v in (case.headers or {}).items() if k.lower() not in ("cookie", "x-csrf-token")}
        if case.method.upper() == "GET":
            r = await owner.client.get(f"/api/v1{path}", headers=headers, params=case.query or None)
        else:
            headers["X-CSRF-Token"] = owner.csrf
            r = await owner.client.post(f"/api/v1{path}", headers=headers, json=case.body, params=case.query or None)
        assert r.status_code < 500, f"{operation_id} {path} {case.query!r} {case.body!r}: {r.status_code} {r.text}"
        if r.status_code != 304:
            validate_response(operation_id, r)
