"""P6-11: API качества, compliance, отчёта run и evidence (ТЗ §8, §13, §14; FR-QG-07, FR-STD-05, AC-14).

Run засевается результатами верификации всех терминальных статусов gate (PASS/FAIL/ERROR/SKIPPED/
NOT_APPLICABLE), VERIFIED evidence с sha256 артефакта, находками в разных состояниях жизненного цикла и
вердиктами контролей (`aicd_standards.store.evaluate_run`). Каждый ответ сверяется с контрактом; отчёт —
по содержанию (вердикты, трассировка standard → control → check → evidence → verdict, предупреждение
«не сертификация», поля evidence AC-14) в JSON и Markdown; RBAC по ролям, 404 для чужого workspace,
Schemathesis-fuzz (нет 5xx, ответы соответствуют контракту).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import hypothesis
import psycopg
import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.db import DbContext, create_engine, transaction
from aicd_platform.ids import uuid7
from aicd_standards import api as standards
from tests.conftest import UserSession, login, make_client
from tests.support import db as testdb
from tests.support import quality as q
from tests.support.contract import OPENAPI_PATH, validate_response
from tests.support.orchestration import Tenant, connect_admin, drive_run, lease_task, new_run, seed_tenant

pytestmark = pytest.mark.db

OPERATIONS = ("getRunQuality", "getRunCompliance", "getRunReport", "listTaskEvidence", "getEvidence", "listRunEvidence")
SCOPE, SECRETS = "SEC-27001-01", "SEC-27002-01"
SECRET = "hunter2-very-secret"
EVIL_TITLE = "Шаг | <script>alert(1)</script> [x](javascript:alert(1)) **bold**"


@dataclass
class Seed:
    owner: UserSession
    tenant: Tenant
    run: uuid.UUID
    empty_run: uuid.UUID
    task: uuid.UUID
    other_task: uuid.UUID
    commit: str
    gates: dict[str, uuid.UUID] = field(default_factory=dict)
    evidence: dict[str, uuid.UUID] = field(default_factory=dict)
    findings: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def ws(self) -> str:
        return str(self.tenant.workspace_id)


def _task(conn: psycopg.Connection, tenant: Tenant, run: uuid.UUID, key: str, title: str) -> uuid.UUID:
    task = uuid7()
    conn.execute(
        "INSERT INTO tasks (id, workspace_id, run_id, plan_version, task_key, agent_role, title, instructions,"
        " complexity, risk_level) VALUES (%s, %s, %s, 1, %s, 'developer', %s, 'do it', 2, 'low')",
        (task, tenant.workspace_id, run, key, title),
    )
    return task


def _process_evidence(
    conn: psycopg.Connection, tenant: Tenant, run: uuid.UUID, task: uuid.UUID | None, commit: str, kind: str
) -> uuid.UUID:
    """Evidence Verification Engine с полным набором полей AC-14 (и «секретом» в команде/окружении)."""
    ev = uuid7()
    conn.execute(
        "INSERT INTO evidence (id, workspace_id, run_id, task_id, kind, artifact_id, producer, verification_status,"
        " commit_sha, command, exit_code, environment, started_at, finished_at, verified_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'verification_engine', 'VERIFIED', %s, %s, 1, %s::jsonb,"
        " now() - interval '1 minute', now(), now())",
        (
            ev,
            tenant.workspace_id,
            run,
            task,
            kind,
            q.artifact(conn, tenant, run, "gate_log"),
            commit,
            f"gitleaks detect --token={SECRET}",
            '{"runner": "docker", "image": "aicd/verify@sha256:' + "a" * 64 + '",'
            ' "tool_versions": {"gitleaks": "8.18"}, "network": "none", "api_token": "' + SECRET + '"}',
        ),
    )
    return ev


def _finding(
    conn: psycopg.Connection,
    tenant: Tenant,
    run: uuid.UUID,
    severity: str,
    title: str,
    *,
    gate: tuple[uuid.UUID, uuid.UUID] | None = None,
    task: uuid.UUID | None = None,
) -> uuid.UUID:
    fid = uuid7()
    conn.execute(
        "INSERT INTO findings (id, workspace_id, run_id, task_id, source, source_gate_id, gate_execution_id, severity,"
        " rule_id, title, file_path, line, fingerprint) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'src/app.py', 7, %s)",
        (
            fid,
            tenant.workspace_id,
            run,
            task,
            "gate" if gate else "agent_review",
            gate[1] if gate else None,
            gate[0] if gate else None,
            severity,
            f"rule.{severity}",
            title,
            q.sha256(),
        ),
    )
    return fid


@pytest.fixture(scope="module")
async def seed(app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase) -> AsyncIterator[Seed]:
    owner = await login(app, app_engine)
    commit = q.sha1()
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn, owner_id=uuid.UUID(owner.user_id))
        run = new_run(conn, tenant)
        drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
        task = _task(conn, tenant, run, "t1", EVIL_TITLE)
        lease_task(conn, task)
        other_task = _task(conn, tenant, run, "t2", "Вторая задача")
        s = Seed(owner, tenant, run, new_run(conn, tenant), task, other_task, commit)
        profile = (q.tenant_profile(conn, tenant), tenant.workspace_id)
        # PASS (SCOPE, VERIFIED evidence задачи), FAIL (SECRETS + находка), ERROR, SKIPPED, NOT_APPLICABLE.
        scope_id = q.ensure_gate(conn, "scope", "SCOPE", profile=profile)
        s.evidence["scope"] = _process_evidence(conn, tenant, run, task, commit, "command_result")
        s.gates["PASS"] = q.gate_execution(
            conn, tenant, run, scope_id, "SCOPE", "PASS", commit, evidence_id=s.evidence["scope"], task=task
        )
        leaks_id = q.ensure_gate(conn, "gitleaks", "SECRETS", profile=profile)
        s.evidence["secrets"] = _process_evidence(conn, tenant, run, None, commit, "scan_report")
        s.gates["FAIL"] = q.gate_execution(
            conn, tenant, run, leaks_id, "SECRETS", "FAIL", commit, evidence_id=s.evidence["secrets"]
        )
        s.gates["ERROR"] = q.gate_execution(
            conn, tenant, run, q.ensure_gate(conn, "semgrep", "SAST", profile=profile), "SAST", "ERROR", commit,
            error_code="TIMEOUT",
        )  # fmt: skip
        s.gates["SKIPPED"] = q.gate_execution(
            conn, tenant, run, q.ensure_gate(conn, "pytest-unit", "UNIT", profile=profile), "UNIT", "SKIPPED", commit,
            task=task, reason=f"dependency failed; password={SECRET}",
        )  # fmt: skip
        s.gates["NOT_APPLICABLE"] = q.gate_execution(
            conn, tenant, run, q.ensure_gate(conn, "checkov", "IAC", profile=profile), "IAC", "NOT_APPLICABLE", commit,
            reason="no IaC files",
        )  # fmt: skip
        # Evidence другой задачи, связанное с первой через evidence_links (verifies).
        s.evidence["linked"] = _process_evidence(conn, tenant, run, other_task, commit, "test_report")
        conn.execute(
            "INSERT INTO evidence_links (id, workspace_id, evidence_id, target_type, target_id, relation)"
            " VALUES (%s, %s, %s, 'task', %s, 'verifies'), (%s, %s, %s, 'gate_execution', %s, 'produced_by')",
            (
                uuid7(), tenant.workspace_id, s.evidence["linked"], task,
                uuid7(), tenant.workspace_id, s.evidence["scope"], s.gates["PASS"],
            ),
        )  # fmt: skip
        s.evidence["pending"] = q.evidence(conn, tenant, run, commit, status="PENDING", kind="diff")
        conn.execute("UPDATE evidence SET task_id = NULL WHERE id = %s", (s.evidence["pending"],))
        s.findings["high"] = _finding(conn, tenant, run, "high", "leaked key", gate=(s.gates["FAIL"], leaks_id))
        s.findings["critical"] = _finding(conn, tenant, run, "critical", f"token={SECRET} in config", task=task)
        s.findings["medium"] = _finding(conn, tenant, run, "medium", "acknowledged")
        s.findings["low"] = _finding(conn, tenant, run, "low", "false alarm")
        s.findings["info"] = _finding(conn, tenant, run, "info", "accepted")
    # Решения человека по находкам (роль app, resolved_by = пользователь сессии — триггер 0008).
    user_ctx = DbContext(user_id=tenant.owner_id, workspace_id=tenant.workspace_id)
    async with transaction(app_engine, user_ctx) as c:
        await c.execute(text("UPDATE findings SET status = 'ACKNOWLEDGED' WHERE id = :f"), {"f": s.findings["medium"]})
        for key, status in (("low", "FALSE_POSITIVE"), ("info", "ACCEPTED_RISK")):
            await c.execute(
                text(
                    "UPDATE findings SET status = :s, resolved_by = :u, resolution_reason = 'проверено' WHERE id = :f"
                ),
                {"s": status, "u": tenant.owner_id, "f": s.findings[key]},
            )
        for code, applicability, rationale in ((SCOPE, "applicable", None), (SECRETS, "applicable", None),
                                               ("SQL-9075-01", "not_applicable", "БД во внешнем сервисе")):  # fmt: skip
            control = (
                await c.execute(text("SELECT id FROM standard_controls WHERE control_code = :c"), {"c": code})
            ).scalar_one()
            await standards.approve_project_control(
                c, workspace_id=tenant.workspace_id, project_id=tenant.project_id, control_id=control,
                user_id=tenant.owner_id, applicability=applicability, rationale=rationale,
            )  # fmt: skip
    worker = create_engine(database.url_for("aicd_worker"), pool_size=2)
    try:
        async with transaction(worker, DbContext(workspace_id=tenant.workspace_id, actor_type="system")) as c:
            await standards.evaluate_run(c, run)
    finally:
        await worker.dispose()
    yield s
    await owner.client.aclose()


async def _member(
    app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase, s: Seed, role: str
) -> UserSession:
    member = await login(app, app_engine)
    with connect_admin(database) as conn:
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (%s, %s, %s)",
            (s.tenant.workspace_id, member.user_id, role),
        )
    return member


def _paths(s: Seed) -> dict[str, str]:
    return {
        "getRunQuality": f"/api/v1/runs/{s.run}/quality",
        "getRunCompliance": f"/api/v1/runs/{s.run}/compliance",
        "getRunReport": f"/api/v1/runs/{s.run}/report",
        "listTaskEvidence": f"/api/v1/tasks/{s.task}/evidence",
        "getEvidence": f"/api/v1/evidence/{s.evidence['scope']}",
        "listRunEvidence": f"/api/v1/runs/{s.run}/evidence",
    }


# --- quality ----------------------------------------------------------------------------------------


async def test_quality_report_verdict_gates_and_findings(seed: Seed) -> None:
    r = await seed.owner.client.get(f"/api/v1/runs/{seed.run}/quality")
    validate_response("getRunQuality", r)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == str(seed.run) and body["quality_profile"] == "strict"
    # FAIL/SKIPPED обязательных gates ⇒ не PASS.
    assert body["verdict"] == "FAIL"
    by_id = {g["id"]: g for g in body["gate_executions"]}
    for status, ge in seed.gates.items():
        assert by_id[str(ge)]["status"] == status
        assert by_id[str(ge)]["commit_sha"] == seed.commit
    assert by_id[str(seed.gates["PASS"])]["task_id"] == str(seed.task)
    assert by_id[str(seed.gates["PASS"])]["evidence_id"] == str(seed.evidence["scope"])
    assert by_id[str(seed.gates["ERROR"])]["error_code"] == "TIMEOUT"
    skipped_reason = by_id[str(seed.gates["SKIPPED"])]["not_applicable_reason"]
    assert SECRET not in skipped_reason and "[REDACTED]" in skipped_reason
    # Открытые находки (OPEN/ACKNOWLEDGED/FIXED) по severity; жизненный цикл — по всем.
    assert body["findings_summary"] == {"info": 0, "low": 0, "medium": 1, "high": 1, "critical": 1}
    assert body["findings_by_status"] == {
        "OPEN": 2, "ACKNOWLEDGED": 1, "FIXED": 0, "VERIFIED_FIXED": 0, "FALSE_POSITIVE": 1, "ACCEPTED_RISK": 1,
    }  # fmt: skip
    assert body["findings_total"] == 5
    assert [f["severity"] for f in body["findings"]] == ["critical", "high", "medium", "low", "info"]
    critical = body["findings"][0]
    assert critical["status"] == "OPEN" and SECRET not in critical["title"] and critical["task_id"] == str(seed.task)
    assert body["findings"][1]["gate_execution_id"] == str(seed.gates["FAIL"])


async def test_quality_verdict_pass_and_pending(seed: Seed, database: testdb.TestDatabase) -> None:
    pending = await seed.owner.client.get(f"/api/v1/runs/{seed.empty_run}/quality")
    validate_response("getRunQuality", pending)
    assert pending.json()["verdict"] == "PENDING" and pending.json()["gate_executions"] == []
    with connect_admin(database) as conn:
        run = new_run(conn, seed.tenant)
        pid = conn.execute(
            "SELECT id FROM quality_profiles WHERE workspace_id = %s", (seed.tenant.workspace_id,)
        ).fetchone()[0]  # type: ignore[index]
        profile = (pid, seed.tenant.workspace_id)
        ev = q.evidence(conn, seed.tenant, run, seed.commit)
        q.gate_execution(conn, seed.tenant, run, q.ensure_gate(conn, "scope", "SCOPE", profile=profile), "SCOPE",
                         "PASS", seed.commit, evidence_id=ev)  # fmt: skip
        q.gate_execution(conn, seed.tenant, run, q.ensure_gate(conn, "checkov", "IAC", profile=profile), "IAC",
                         "NOT_APPLICABLE", seed.commit)  # fmt: skip
        only_na = new_run(conn, seed.tenant)
        q.gate_execution(conn, seed.tenant, only_na, q.ensure_gate(conn, "checkov", "IAC", profile=profile), "IAC",
                         "NOT_APPLICABLE", seed.commit)  # fmt: skip
    ok = await seed.owner.client.get(f"/api/v1/runs/{run}/quality")
    validate_response("getRunQuality", ok)
    assert ok.json()["verdict"] == "PASS"
    empty = await seed.owner.client.get(f"/api/v1/runs/{only_na}/quality")
    assert empty.json()["verdict"] == "FAIL", "одни NOT_APPLICABLE — пустая верификация, не PASS"


# --- compliance -------------------------------------------------------------------------------------


async def test_compliance_matrix_page_trace_and_disclaimer(seed: Seed) -> None:
    client = seed.owner.client
    r = await client.get(f"/api/v1/runs/{seed.run}/compliance", params={"limit": 200})
    validate_response("getRunCompliance", r)
    body = r.json()
    assert r.status_code == 200 and body["next_cursor"] is None
    assert body["disclaimer"] == standards.DISCLAIMER and "не является" in body["disclaimer"]
    assert len(body["items"]) == 33 and sum(body["summary"].values()) == 33
    rows = {row["control_code"]: row for row in body["items"]}
    scope = rows[SCOPE]
    assert scope["status"] == "PASS" and scope["applicability"] == "APPLICABLE"
    assert scope["verification_method"] == "automated_gate" and scope["gate_type"] == "SCOPE"
    assert scope["gate_execution_id"] == str(seed.gates["PASS"])
    assert scope["evidence_id"] == str(seed.evidence["scope"]) and scope["agent_role"] == "security"
    assert scope["standard_code"] == "ISO/IEC 27001" and scope["edition"] and scope["clause_ref"]
    assert rows[SECRETS]["status"] == "FAIL" and rows[SECRETS]["evidence_id"] == str(seed.evidence["secrets"])
    assert rows["SQL-9075-01"]["status"] == "NOT_APPLICABLE"
    assert rows["SQL-9075-01"]["applicability"] == "NOT_APPLICABLE"
    assert rows["QM-25010-01"]["applicability"] == "PENDING_REVIEW" and rows["QM-25010-01"]["status"] == "UNVERIFIED"
    assert body["summary"]["PASS"] >= 1 and body["summary"]["FAIL"] >= 1

    # Cursor-пагинация: страницы по 7 без пропусков и повторов, порядок — стандарт, контроль.
    seen: list[str] = []
    cursor = None
    while True:
        page = await client.get(
            f"/api/v1/runs/{seed.run}/compliance", params={"limit": 7, **({"cursor": cursor} if cursor else {})}
        )
        validate_response("getRunCompliance", page)
        seen += [row["control_id"] for row in page.json()["items"]]
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == [row["control_id"] for row in body["items"]]
    # Фильтры; курсор привязан к фильтрам.
    fail = await client.get(f"/api/v1/runs/{seed.run}/compliance", params={"status": "FAIL"})
    assert {row["status"] for row in fail.json()["items"]} == {"FAIL"}
    std = await client.get(f"/api/v1/runs/{seed.run}/compliance", params={"standard_code": "ISO/IEC 27001"})
    assert {row["standard_code"] for row in std.json()["items"]} == {"ISO/IEC 27001"}
    first = await client.get(f"/api/v1/runs/{seed.run}/compliance", params={"limit": 1})
    bad = await client.get(
        f"/api/v1/runs/{seed.run}/compliance",
        params={"limit": 1, "status": "PASS", "cursor": first.json()["next_cursor"]},
    )
    validate_response("getRunCompliance", bad)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_cursor"


# --- evidence ---------------------------------------------------------------------------------------


async def test_evidence_fields_links_and_redaction(seed: Seed) -> None:
    r = await seed.owner.client.get(f"/api/v1/evidence/{seed.evidence['scope']}")
    validate_response("getEvidence", r)
    ev = r.json()
    # AC-14: commit, команда, exit code, время, окружение, sha256 артефакта.
    assert ev["commit_sha"] == seed.commit and ev["exit_code"] == 1
    assert ev["started_at"] and ev["finished_at"] and ev["verification_status"] == "VERIFIED"
    assert ev["producer"] == "verification_engine" and ev["task_id"] == str(seed.task)
    assert re.fullmatch(r"[0-9a-f]{64}", ev["artifact"]["sha256"])
    assert ev["artifact"]["kind"] == "gate_log"
    assert ev["artifact"]["download_url"] == f"/api/v1/artifacts/{ev['artifact']['id']}/download"
    assert ev["environment"]["runner"] == "docker" and ev["environment"]["tool_versions"] == {"gitleaks": "8.18"}
    assert SECRET not in r.text and ev["environment"]["api_token"] == "[REDACTED]"
    assert ev["command"].startswith("gitleaks detect --token=") and "[REDACTED]" in ev["command"]
    assert {"target_type": "gate_execution", "target_id": str(seed.gates["PASS"]), "relation": "produced_by"} in ev[
        "links"
    ]
    missing = await seed.owner.client.get(f"/api/v1/evidence/{uuid7()}")
    validate_response("getEvidence", missing)
    assert missing.status_code == 404


async def test_task_evidence_list_filter_and_pagination(seed: Seed) -> None:
    client = seed.owner.client
    r = await client.get(f"/api/v1/tasks/{seed.task}/evidence")
    validate_response("listTaskEvidence", r)
    ids = [e["id"] for e in r.json()["items"]]
    # Своё evidence задачи и связанное с ней через evidence_links; evidence run без задачи — нет.
    assert ids == [str(seed.evidence["scope"]), str(seed.evidence["linked"])]
    first = await client.get(f"/api/v1/tasks/{seed.task}/evidence", params={"limit": 1})
    validate_response("listTaskEvidence", first)
    second = await client.get(
        f"/api/v1/tasks/{seed.task}/evidence", params={"limit": 1, "cursor": first.json()["next_cursor"]}
    )
    assert [e["id"] for e in first.json()["items"] + second.json()["items"]] == ids
    assert second.json()["next_cursor"] is None
    none = await client.get(f"/api/v1/tasks/{seed.task}/evidence", params={"verification_status": "REJECTED"})
    validate_response("listTaskEvidence", none)
    assert none.json() == {"items": [], "next_cursor": None}
    other = await client.get(f"/api/v1/tasks/{seed.other_task}/evidence", params={"verification_status": "VERIFIED"})
    assert [e["id"] for e in other.json()["items"]] == [str(seed.evidence["linked"])]


async def test_run_evidence_list_filters_and_pagination(seed: Seed) -> None:
    """OQ-45 (c): все evidence run — keyset по (created_at, id), фильтры статуса/задачи/вида."""
    client = seed.owner.client
    url = f"/api/v1/runs/{seed.run}/evidence"
    r = await client.get(url)
    validate_response("listRunEvidence", r)
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()["items"]]
    expected = [seed.evidence[k] for k in ("scope", "secrets", "linked", "pending")]
    assert set(ids) == {str(e) for e in expected} and len(ids) == len(set(ids))
    assert SECRET not in r.text
    pages: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await client.get(url, params=params)
        validate_response("listRunEvidence", page)
        pages += [e["id"] for e in page.json()["items"]]
        cursor = page.json()["next_cursor"]
        if not cursor:
            break
    assert pages == ids
    verified = await client.get(url, params={"verification_status": "VERIFIED"})
    assert str(seed.evidence["pending"]) not in [e["id"] for e in verified.json()["items"]]
    by_task = await client.get(url, params={"task_id": str(seed.other_task)})
    assert [e["id"] for e in by_task.json()["items"]] == [str(seed.evidence["linked"])]
    by_kind = await client.get(url, params={"kind": "scan_report"})
    assert [e["id"] for e in by_kind.json()["items"]] == [str(seed.evidence["secrets"])]
    empty = await client.get(f"/api/v1/runs/{seed.empty_run}/evidence")
    validate_response("listRunEvidence", empty)
    assert empty.json() == {"items": [], "next_cursor": None}
    # Курсор привязан к фильтрам; испорченный курсор — 422; несуществующий run — 404.
    first = await client.get(url, params={"limit": 1})
    mixed = await client.get(url, params={"limit": 1, "cursor": first.json()["next_cursor"], "kind": "diff"})
    validate_response("listRunEvidence", mixed)
    assert mixed.status_code == 422
    missing = await client.get(f"/api/v1/runs/{uuid7()}/evidence")
    validate_response("listRunEvidence", missing)
    assert missing.status_code == 404


# --- report -----------------------------------------------------------------------------------------


async def test_report_json_contents(seed: Seed) -> None:
    r = await seed.owner.client.get(f"/api/v1/runs/{seed.run}/report", headers={"Accept": "application/json"})
    validate_response("getRunReport", r)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    report = r.json()
    assert report["run"]["id"] == str(seed.run) and report["generated_at"].endswith("Z")
    assert report["quality"]["verdict"] == "FAIL" and len(report["findings"]) == 5
    assert report["disclaimer"] == standards.DISCLAIMER
    assert sum(report["compliance_summary"].values()) == 33
    assert {t["id"] for t in report["tasks"]} == {str(seed.task), str(seed.other_task)}
    assert {e["id"] for e in report["evidence"]} == {str(v) for v in seed.evidence.values()}
    for ev in report["evidence"]:  # AC-14: процессные evidence несут все поля
        if ev["kind"] in ("command_result", "scan_report", "test_report"):
            assert ev["commit_sha"] and ev["command"] and ev["exit_code"] is not None
            assert ev["started_at"] and ev["finished_at"] and ev["environment"].get("runner")
        assert re.fullmatch(r"[0-9a-f]{64}", ev["artifact"]["sha256"])
    # Трассировка standard → control → applicability → роль → check → evidence → verdict.
    trace = {row["control"]["code"]: row for row in report["compliance_matrix"]}
    assert len(trace) == 33
    scope = trace[SCOPE]
    assert scope["standard"]["code"] == "ISO/IEC 27001" and scope["control"]["clause_ref"]
    assert scope["applicability"] == "APPLICABLE" and scope["agent_role"] == "security"
    assert scope["check"] == {
        "method": "automated_gate", "gate_type": "SCOPE", "gate_execution_id": str(seed.gates["PASS"]),
        "gate_status": "PASS", "commit_sha": seed.commit,
    }  # fmt: skip
    assert scope["evidence"]["id"] == str(seed.evidence["scope"])
    assert scope["evidence"]["verification_status"] == "VERIFIED"
    assert re.fullmatch(r"[0-9a-f]{64}", scope["evidence"]["artifact_sha256"])
    assert scope["verdict"]["status"] == "PASS"
    assert trace[SECRETS]["verdict"]["status"] == "FAIL" and trace[SECRETS]["check"]["gate_status"] == "FAIL"
    assert trace["QM-25010-01"]["evidence"] is None and trace["QM-25010-01"]["verdict"]["status"] == "UNVERIFIED"
    assert SECRET not in r.text
    # Отчёт детерминирован: при тех же данных совпадает всё, кроме момента формирования.
    again = (await seed.owner.client.get(f"/api/v1/runs/{seed.run}/report")).json()
    assert {**again, "generated_at": None} == {**report, "generated_at": None}


async def test_report_markdown_same_data_escaped(seed: Seed) -> None:
    r = await seed.owner.client.get(f"/api/v1/runs/{seed.run}/report", headers={"Accept": "text/markdown"})
    validate_response("getRunReport", r)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.headers["vary"] == "Accept"
    md = r.text
    assert md.startswith(f"# Отчёт run {seed.run}")
    assert "## Вердикт качества: FAIL" in md
    for status in seed.gates:
        assert re.search(rf"\| {status} \|", md.replace("NOT\\_APPLICABLE", "NOT_APPLICABLE")), status
    assert seed.commit in md and "TIMEOUT" in md
    assert "не является" in md and "## Standards & Compliance" in md
    assert "SEC-27001-01" in md.replace("\\", "") and "automated\\_gate" in md
    with_sha = [line for line in md.splitlines() if str(seed.evidence["scope"]) in line and "gitleaks" in line]
    assert with_sha and re.search(r"\| [0-9a-f]{64} \|$", with_sha[0])
    assert SECRET not in md
    # Свободный текст экранирован: ни сырого HTML, ни ссылки, ни разрыва таблицы.
    assert "<script>" not in md and "&lt;script&gt;" in md
    assert "\\[x\\](javascript:alert(1))" in md and "\\*\\*bold\\*\\*" in md and "Шаг \\| " in md
    prefer_json = await seed.owner.client.get(
        f"/api/v1/runs/{seed.run}/report", headers={"Accept": "application/json, text/markdown;q=0.5"}
    )
    assert prefer_json.headers["content-type"].startswith("application/json")


# --- RBAC и tenant-изоляция ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "developer", "reviewer", "viewer"])
async def test_every_role_reads_quality_and_evidence(
    app: FastAPI, app_engine: AsyncEngine, database: testdb.TestDatabase, seed: Seed, role: str
) -> None:
    member = await _member(app, app_engine, database, seed, role)
    try:
        for op_id, path in _paths(seed).items():
            r = await member.client.get(path)
            validate_response(op_id, r)
            assert r.status_code == 200, f"{role} {op_id}: {r.text}"
    finally:
        await member.client.aclose()


async def test_non_members_get_404_and_anonymous_401(app: FastAPI, app_engine: AsyncEngine, seed: Seed) -> None:
    outsider = await login(app, app_engine)
    try:
        await outsider.create_workspace()  # член другого workspace
        for op_id, path in _paths(seed).items():
            r = await outsider.client.get(path)
            validate_response(op_id, r)
            assert r.status_code == 404, f"{op_id}: чужой ресурс неотличим от отсутствующего"
            assert r.json()["code"] == "not_found"
    finally:
        await outsider.client.aclose()
    async with make_client(app) as anon:
        for op_id, path in _paths(seed).items():
            r = await anon.get(path)
            validate_response(op_id, r)
            assert r.status_code == 401
    # Член workspace с чужим X-Workspace-Id — тоже 404.
    r = await seed.owner.client.get(_paths(seed)["getEvidence"], headers={"X-Workspace-Id": str(uuid7())})
    assert r.status_code == 404


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


@pytest.mark.parametrize("operation_id", OPERATIONS)
async def test_contract_fuzz_never_5xx_and_responses_conform(seed: Seed, operation_id: str) -> None:
    real = {
        "run_id": str(seed.run),
        "task_id": str(seed.task),
        "evidence_id": str(seed.evidence["scope"]),
    }
    for n, case in enumerate(_cases(operation_id, 25)):
        path = case.formatted_path
        if n % 2 == 0:  # половина случаев — на существующих ресурсах, чтобы fuzz доходил до запросов к БД
            for name, value in (case.path_parameters or {}).items():
                path = path.replace(str(value), real.get(name, str(value)), 1)
        headers = {k: v for k, v in (case.headers or {}).items() if k.lower() not in ("cookie", "x-csrf-token")}
        r = await seed.owner.client.get(f"/api/v1{path}", headers=headers, params=case.query or None)
        assert r.status_code < 500, f"{operation_id} {path} {case.query!r}: {r.status_code} {r.text}"
        if r.status_code != 304:
            validate_response(operation_id, r)
