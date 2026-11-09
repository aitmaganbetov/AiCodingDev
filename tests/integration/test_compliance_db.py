"""P6-10: применимость и compliance evaluation на PostgreSQL под ролями приложения (RLS, триггеры 0008).

Сценарий: стек проекта → черновики применимости (worker) → утверждение человеком (app) → результаты
gates/evidence/findings run → вердикты контролей с трассировкой standard → control → gate → evidence →
verdict (`compliance_matrix`), история вердиктов через `superseded_at`, связи `evidence_links`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.db import DbContext, create_engine, transaction
from aicd_platform.ids import uuid7
from aicd_standards import api as standards
from aicd_standards import store
from tests.support import db as testdb
from tests.support import quality as q
from tests.support.orchestration import Tenant, connect_admin, drive_run, new_run, seed_tenant

pytestmark = pytest.mark.db

SCOPE = "SEC-27001-01"
SECRETS = "SEC-27002-01"
INJECTION = "CWE-INJ-01"
AUTH_REVIEW = "ASVS-V2-01"
SQL = "SQL-9075-01"


@dataclass
class Env:
    tenant: Tenant
    run: uuid.UUID
    commit: str
    worker: AsyncEngine
    app: AsyncEngine
    controls: dict[str, uuid.UUID]

    def worker_ctx(self) -> DbContext:
        return DbContext(workspace_id=self.tenant.workspace_id, actor_type="system")

    def user_ctx(self) -> DbContext:
        return DbContext(user_id=self.tenant.owner_id, workspace_id=self.tenant.workspace_id)


def _stack(conn: psycopg.Connection, tenant: Tenant, pairs: list[tuple[str, str]]) -> None:
    for ecosystem, name in pairs:
        tech, tv = uuid7(), uuid7()
        conn.execute(
            "INSERT INTO technologies (id, name, ecosystem) VALUES (%s, %s, %s)"
            " ON CONFLICT ON CONSTRAINT uq_technologies_ecosystem_name DO NOTHING",
            (tech, name, ecosystem),
        )
        tech = conn.execute(
            "SELECT id FROM technologies WHERE ecosystem = %s AND name = %s", (ecosystem, name)
        ).fetchone()[0]  # type: ignore[index]
        conn.execute(
            "INSERT INTO technology_versions (id, technology_id, version_label) VALUES (%s, %s, %s)",
            (tv, tech, f"t-{tv.hex[-8:]}"),
        )
        conn.execute(
            "INSERT INTO project_technologies (workspace_id, project_id, technology_version_id, detected_from)"
            " VALUES (%s, %s, %s, 'pyproject.toml')",
            (tenant.workspace_id, tenant.project_id, tv),
        )


@pytest.fixture(scope="module")
async def env(database: testdb.TestDatabase) -> AsyncIterator[Env]:
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn)
        _stack(conn, tenant, [("lang", "python"), ("python", "fastapi"), ("python", "sqlalchemy")])
        run = new_run(conn, tenant)
        drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
        controls = {r[0]: r[1] for r in conn.execute("SELECT control_code, id FROM standard_controls").fetchall()}
    worker = create_engine(database.url_for("aicd_worker"), pool_size=2)
    app = create_engine(database.url_for("aicd_app"), pool_size=2)
    try:
        yield Env(tenant, run, q.sha1(), worker, app, controls)
    finally:
        await worker.dispose()
        await app.dispose()


async def _statuses(env: Env) -> dict[str, tuple[str, int]]:
    """Текущий статус и число строк истории по каждому контролю run."""
    async with transaction(env.worker, env.worker_ctx()) as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.control_code, max(cr.status) FILTER (WHERE cr.superseded_at IS NULL), count(*)"
                    " FROM compliance_results cr JOIN standard_controls c ON c.id = cr.control_id"
                    " WHERE cr.run_id = :r GROUP BY c.control_code"
                ),
                {"r": env.run},
            )
        ).all()
    return {r[0]: (r[1], r[2]) for r in rows}


async def test_compliance_evaluation_end_to_end(database: testdb.TestDatabase, env: Env) -> None:
    ws, project = env.tenant.workspace_id, env.tenant.project_id
    # 1. Система предлагает применимость по стеку — только черновики.
    async with transaction(env.worker, env.worker_ctx()) as conn:
        proposals = await standards.propose_project_controls(conn, workspace_id=ws, project_id=project)
    by_code = {p.control_code: p for p in proposals}
    assert len(proposals) == len(env.controls) == 33
    assert by_code[SQL].applicability == "applicable" and by_code[AUTH_REVIEW].applicability == "applicable"
    assert by_code["ASVS-V14-01"].applicability == "not_applicable"  # контейнеров в стеке нет

    # 2. Без утверждения человеком всё UNVERIFIED (черновик не учитывается).
    async with transaction(env.worker, env.worker_ctx()) as conn:
        report = await standards.evaluate_run(conn, env.run)
    assert report.summary()["UNVERIFIED"] == 33
    assert {s for s, _n in (await _statuses(env)).values()} == {"UNVERIFIED"}

    # 3. Человек утверждает применимость (SQL — неприменим по решению, с обоснованием).
    async with transaction(env.app, env.user_ctx()) as conn:
        for code in (SCOPE, SECRETS, INJECTION, AUTH_REVIEW):
            await standards.approve_project_control(
                conn,
                workspace_id=ws,
                project_id=project,
                control_id=env.controls[code],
                user_id=env.tenant.owner_id,
                applicability="applicable",
                rationale=None,
            )
        await standards.approve_project_control(
            conn,
            workspace_id=ws,
            project_id=project,
            control_id=env.controls[SQL],
            user_id=env.tenant.owner_id,
            applicability="not_applicable",
            rationale="БД только во внешнем сервисе",
        )

    # 4. Результаты верификации run (как от Verification Engine).
    with connect_admin(database) as conn:
        profile = (q.tenant_profile(conn, env.tenant), env.tenant.workspace_id)
        scope_ev = q.evidence(conn, env.tenant, env.run, env.commit)
        scope_gate = q.gate_execution(
            conn, env.tenant, env.run, q.ensure_gate(conn, "scope", "SCOPE", profile=profile), "SCOPE", "PASS", env.commit,
            evidence_id=scope_ev,
        )  # fmt: skip
        secrets_gate_id = q.ensure_gate(conn, "gitleaks", "SECRETS", profile=profile)
        secrets_ev = q.evidence(conn, env.tenant, env.run, env.commit, kind="scan_report")
        secrets_gate = q.gate_execution(
            conn, env.tenant, env.run, secrets_gate_id, "SECRETS", "FAIL", env.commit, evidence_id=secrets_ev
        )
        leak = q.finding(conn, env.tenant, env.run, severity="high", gate=(secrets_gate, secrets_gate_id))
        sast_gate_id = q.ensure_gate(conn, "semgrep", "SAST", profile=profile)
        sast_ev = q.evidence(conn, env.tenant, env.run, env.commit, kind="scan_report")
        sast_gate = q.gate_execution(
            conn, env.tenant, env.run, sast_gate_id, "SAST", "PASS", env.commit, evidence_id=sast_ev
        )
        q.finding(conn, env.tenant, env.run, severity="medium", gate=(sast_gate, sast_gate_id), cwe="CWE-89")

    # 5. Оценка run: вердикты по правилам QUALITY_GATES §8.2.
    async with transaction(env.worker, env.worker_ctx()) as conn:
        report = await standards.evaluate_run(conn, env.run)
    verdicts = {v.control.control_code: v for v in report.verdicts}
    assert verdicts[SCOPE].status == "PASS" and verdicts[SCOPE].evidence_id == scope_ev
    assert verdicts[SECRETS].status == "FAIL" and verdicts[SECRETS].finding_ids == (leak,)
    assert verdicts[SECRETS].evidence_id == secrets_ev and verdicts[SECRETS].gate_execution_id == secrets_gate
    assert verdicts[INJECTION].status == "PASS"  # CWE-89 medium ниже порога high контроля
    assert verdicts[AUTH_REVIEW].status == "MANUAL_REVIEW"
    assert verdicts[SQL].status == "NOT_APPLICABLE" and "внешнем сервисе" in verdicts[SQL].notes
    assert verdicts["QM-25010-01"].status == "UNVERIFIED"  # не утверждён
    statuses = await _statuses(env)
    assert statuses[SCOPE] == ("PASS", 2) and statuses[SECRETS] == ("FAIL", 2)  # прежний вердикт — в истории
    assert statuses["QM-25010-01"] == ("UNVERIFIED", 1)  # вердикт не изменился — строка та же

    # 6. Повторная оценка без изменений идемпотентна.
    async with transaction(env.worker, env.worker_ctx()) as conn:
        _ws, _p, inp = await store.load_run_input(conn, env.run)
        counts = await store.persist_report(conn, ws, standards.evaluate(inp))
    assert counts == {"inserted": 0, "unchanged": 33}

    # 7. Трассировка в отчёте (роль api): standard → control → applicability → роль → gate → evidence → verdict.
    async with transaction(env.app, env.user_ctx()) as conn:
        matrix = await standards.compliance_matrix(conn, env.run)
        links = (
            await conn.execute(
                text(
                    "SELECT l.relation, l.evidence_id FROM evidence_links l JOIN compliance_results cr"
                    " ON cr.id = l.target_id WHERE l.target_type = 'compliance_result' AND cr.run_id = :r"
                    " AND cr.superseded_at IS NULL"
                ),
                {"r": env.run},
            )
        ).all()
    assert len(matrix) == 33
    row = next(r for r in matrix if r["control"]["code"] == SCOPE)
    assert row["standard"] == {"code": "ISO/IEC 27001", "edition": "2022"}
    assert row["applicability"] == {"value": "applicable", "approved_by": env.tenant.owner_id}
    assert row["agent_role"] == "security"
    assert row["check"]["gate_type"] == "SCOPE" and row["check"]["gate_execution_id"] == scope_gate
    assert row["check"]["gate_status"] == "PASS" and row["check"]["commit_sha"] == env.commit
    assert row["evidence"]["id"] == scope_ev and row["evidence"]["verification_status"] == "VERIFIED"
    assert row["evidence"]["sha256"] is not None
    assert row["verdict"]["status"] == "PASS"
    assert next(r for r in matrix if r["control"]["code"] == "QM-25010-01")["applicability"]["value"] == "undecided"
    assert ("supports", scope_ev) in links and ("refutes", secrets_ev) in links

    # 8. Повторное предложение не трогает утверждённые строки; система не может их изменить.
    async with transaction(env.worker, env.worker_ctx()) as conn:
        await standards.propose_project_controls(conn, workspace_id=ws, project_id=project)
        pcs = await standards.load_project_controls(conn, project)
    assert pcs[env.controls[SQL]].applicability == "not_applicable"
    assert pcs[env.controls[SQL]].approved_by == env.tenant.owner_id
    assert pcs[env.controls["QM-25010-01"]].approved_by is None
    with pytest.raises(Exception, match="only by a user"):
        async with transaction(env.worker, env.worker_ctx()) as conn:
            await conn.execute(
                text("UPDATE project_controls SET applicability = 'applicable' WHERE control_id = :c"),
                {"c": env.controls[SQL]},
            )


async def test_evaluate_unknown_run_is_not_found(env: Env) -> None:
    async with transaction(env.worker, env.worker_ctx()) as conn:
        with pytest.raises(standards.RunNotFoundError):
            await standards.evaluate_run(conn, uuid7())
    # Run другого workspace невидим (RLS) — тоже «не найден».
    async with transaction(env.worker, DbContext(workspace_id=uuid7(), actor_type="system")) as conn:
        with pytest.raises(standards.RunNotFoundError):
            await standards.evaluate_run(conn, env.run)
