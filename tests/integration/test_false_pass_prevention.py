"""P6-08/P6-09/P6-12 с реальным PostgreSQL (миграция 0008): findings/test_results/evidence_links, guard
завершения run (`run_completion_verdict`) и сценарии ложного прохождения (AC-11, QUALITY_GATES §7).

Гейты исполняет `VerificationEngine` (LocalProcessRunner; MIGRATION — в контейнере postgres по digest).
Чистая часть набора false-pass — tests/unit/test_verdict_rules.py.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_orchestration.approvals import Approvals
from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import DbContext, create_engine, transaction
from aicd_platform.ids import uuid7
from aicd_security.docker_runner import DockerSandboxRunner
from aicd_security.local_runner import LocalProcessRunner
from aicd_security.sandbox import SandboxRunner
from aicd_verification.engine import VerificationEngine, VerificationOutcome
from aicd_verification.images import DEFAULT_GATE_IMAGES
from aicd_verification.verdict import CompletionVerdict, run_completion_verdict
from tests.integration.test_gate_runners_docker import DOCKER_SOCK, _image_available
from tests.support import db as testdb
from tests.support.gates import FixtureRepo, git, materialize
from tests.support.orchestration import Tenant, connect_admin, drive_run, new_task, seed_tenant

pytestmark = pytest.mark.db

VENV_BIN = str(Path(sys.executable).parent)
PATH = f"{VENV_BIN}:/usr/bin:/bin"
GATES = (
    ("scope", "SCOPE"),
    ("ruff-lint", "LINT"),
    ("pytest-unit", "UNIT"),
    ("pytest-coverage", "COVERAGE"),
    ("migrations", "MIGRATION"),
)


@dataclass
class Env:
    database: testdb.TestDatabase
    engine: AsyncEngine
    store: FileSystemArtifactStore
    tenant: Tenant
    run_id: uuid.UUID
    task_id: uuid.UUID
    tmp: Path

    def verifier(self, runner: SandboxRunner | None = None, *, path: str = PATH, **kw: Any) -> VerificationEngine:
        return VerificationEngine(
            engine=self.engine,
            store=self.store,
            runner=runner or LocalProcessRunner(),
            local_env={"PATH": path},
            staging_root=self.tmp,
            **kw,
        )

    async def verify(
        self, verifier: VerificationEngine, repo: FixtureRepo, sha: str | None = None
    ) -> VerificationOutcome:
        return await verifier.verify_task(
            workspace_id=self.tenant.workspace_id,
            run_id=self.run_id,
            task_id=self.task_id,
            worktree_path=repo.path,
            commit_sha=sha or repo.head,
            quality_profile="strict",
        )

    async def completion(self, *, store: bool = True) -> CompletionVerdict:
        async with transaction(self.engine, DbContext(workspace_id=self.tenant.workspace_id, actor_type="system")) as c:
            return await run_completion_verdict(c, self.run_id, store=self.store if store else None)

    def rows(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        with connect_admin(self.database) as conn:
            cur = conn.execute(sql, params)
            cols = [c.name for c in cur.description or ()]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def gates(self) -> dict[str, dict[str, Any]]:
        rows = self.rows(
            "SELECT q.name, g.* FROM gate_executions g JOIN quality_gates q ON q.id = g.gate_id"
            " WHERE g.task_id = %s AND g.superseded_at IS NULL",
            self.task_id,
        )
        return {r["name"]: r for r in rows}


async def _env(database: testdb.TestDatabase, tmp_path: Path, base: str, gates: tuple[tuple[str, str], ...]) -> Env:
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn)
        profile = uuid7()
        conn.execute(
            "INSERT INTO quality_profiles (id, workspace_id, code, version, definition) VALUES (%s, %s, 'strict', 1, '{}')",
            (profile, tenant.workspace_id),
        )
        for name, gate_type in gates:
            conn.execute(
                "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type, required, timeout_seconds)"
                " VALUES (%s, %s, %s, %s, %s, true, 600)",
                (uuid7(), tenant.workspace_id, profile, name, gate_type),
            )
        run = uuid7()
        conn.execute(
            "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
            " quality_profile, quality_profile_id, documentation_mode, base_commit_sha)"
            " VALUES (%s, %s, %s, %s, %s, 'DEVELOP', 'objective', 'strict', %s, 'SMART', %s)",
            (run, tenant.workspace_id, tenant.project_id, tenant.repository_id, tenant.owner_id, profile, base),
        )
        drive_run(conn, run, "DISCOVERY", "PLANNED", "RUNNING")
        task = new_task(conn, tenant, run)
    engine = create_engine(database.url_for("aicd_worker"))
    return Env(database, engine, FileSystemArtifactStore(tmp_path / "artifacts"), tenant, run, task, tmp_path)


@pytest.fixture
async def area(tmp_path: Path) -> Path:
    path = (tmp_path / "area").resolve()
    path.mkdir()
    return path


@pytest.fixture
async def coverage_env(database: testdb.TestDatabase, area: Path) -> AsyncIterator[tuple[Env, FixtureRepo]]:
    repo = materialize("coverage", "pass", area / "repo")
    env = await _env(database, area, repo.base, GATES)
    yield env, repo
    await env.engine.dispose()


async def test_green_run_completes_with_test_results_and_links(coverage_env: tuple[Env, FixtureRepo]) -> None:
    env, repo = coverage_env
    outcome = await env.verify(env.verifier(), repo)
    gates = env.gates()
    assert outcome.passed, outcome.summary
    assert gates["pytest-coverage"]["status"] == "PASS" and gates["migrations"]["status"] == "NOT_APPLICABLE"
    assert gates["ruff-lint"]["status"] == "PASS"
    results = {
        r["suite"]: r for r in env.rows("SELECT * FROM test_results WHERE workspace_id = %s", env.tenant.workspace_id)
    }
    assert results["pytest-coverage"]["coverage_percent"] == 100 and results["pytest-unit"]["passed"] == 2
    links = env.rows(
        "SELECT target_type, relation FROM evidence_links WHERE evidence_id = %s", gates["pytest-unit"]["evidence_id"]
    )
    assert {(r["target_type"], r["relation"]) for r in links} == {
        ("gate_execution", "produced_by"),
        ("task", "verifies"),
    }
    verdict = await env.completion()
    assert verdict.ok, verdict.reasons


async def test_tool_missing_writes_error_code_and_blocks_completion(
    coverage_env: tuple[Env, FixtureRepo], area: Path
) -> None:
    env, repo = coverage_env
    bare = area / "bin"
    bare.mkdir()
    (bare / "python3").symlink_to(sys.executable)  # python есть, ruff нет
    await env.verify(env.verifier(path=f"{bare}:/usr/bin:/bin"), repo)
    ruff = env.gates()["ruff-lint"]
    assert ruff["status"] == "ERROR" and ruff["error_code"] == "TOOL_UNAVAILABLE"
    assert "profile strict: MANUAL_REVIEW" in ruff["error_message"]
    verdict = await env.completion()
    assert not verdict.ok and any("ruff-lint[LINT] is ERROR" in r for r in verdict.reasons)


async def test_artifact_tampering_detected_by_completion_guard(coverage_env: tuple[Env, FixtureRepo]) -> None:
    env, repo = coverage_env
    assert (await env.verify(env.verifier(), repo)).passed
    ev = env.rows(
        "SELECT a.storage_ref FROM evidence e JOIN artifacts a ON a.id = e.artifact_id WHERE e.id = %s",
        env.gates()["pytest-unit"]["evidence_id"],
    )[0]
    path = env.store._path(env.store._key(ev["storage_ref"]))  # подмена объекта в хранилище (FP-14)
    assert path.exists(), path
    path.chmod(0o644)
    path.write_bytes(b"forged: 10 passed\n")
    verdict = await env.completion()
    assert not verdict.ok and any("sha256 mismatch" in r or "unreadable" in r for r in verdict.reasons), verdict.reasons
    assert (await env.completion(store=False)).ok  # без повторного чтения хеши в БД согласованы


async def test_gate_on_different_commit_and_open_finding_block_completion(
    coverage_env: tuple[Env, FixtureRepo],
) -> None:
    env, repo = coverage_env
    assert (await env.verify(env.verifier(), repo)).passed
    other = "f" * 40
    with connect_admin(env.database) as conn:
        conn.execute("UPDATE gate_executions SET commit_sha = %s WHERE id = %s", (other, env.gates()["scope"]["id"]))
    verdict = await env.completion()
    assert not verdict.ok and any("not the final commit" in r or "commit differs" in r for r in verdict.reasons)
    with connect_admin(env.database) as conn:
        conn.execute(
            "UPDATE gate_executions SET commit_sha = %s WHERE id = %s", (repo.head, env.gates()["scope"]["id"])
        )
        conn.execute(
            "INSERT INTO findings (id, workspace_id, run_id, task_id, source, severity, rule_id, title, fingerprint)"
            " VALUES (%s, %s, %s, %s, 'manual', 'critical', 'manual/blocker', 'blocker', %s)",
            (uuid7(), env.tenant.workspace_id, env.run_id, env.task_id, "e" * 64),
        )
    verdict = await env.completion()
    assert not verdict.ok and verdict.reasons == ("open critical finding manual/blocker (OPEN)",)


async def test_not_applicable_without_reason_is_rejected_by_db(coverage_env: tuple[Env, FixtureRepo]) -> None:
    env, repo = coverage_env
    await env.verify(env.verifier(), repo)
    with connect_admin(env.database) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE gate_executions SET not_applicable_reason = NULL WHERE id = %s", (env.gates()["migrations"]["id"],)
        )


@pytest.fixture
async def ruff_env(database: testdb.TestDatabase, area: Path) -> AsyncIterator[tuple[Env, FixtureRepo]]:
    repo = materialize("ruff", "fail", area / "repo")
    env = await _env(database, area, repo.base, (("scope", "SCOPE"), ("ruff-lint", "LINT")))
    yield env, repo
    await env.engine.dispose()


async def test_findings_lifecycle_open_then_verified_fixed(ruff_env: tuple[Env, FixtureRepo]) -> None:
    env, repo = ruff_env
    outcome = await env.verify(
        env.verifier(path=f"{os.path.dirname(sys.executable)}:/root/.local/bin:/usr/bin:/bin"), repo
    )
    assert not outcome.passed
    found = env.rows("SELECT * FROM findings WHERE run_id = %s", env.run_id)
    assert [(f["rule_id"], f["file_path"], f["line"], f["status"], f["source"]) for f in found] == [
        ("F401", "pkg/mod.py", 1, "OPEN", "gate")
    ]
    ruff = env.gates()["ruff-lint"]
    assert found[0]["gate_execution_id"] == ruff["id"] and found[0]["evidence_id"] == ruff["evidence_id"]
    assert not (await env.completion()).ok

    (repo.path / "pkg" / "mod.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    git(repo.path, "add", "-A")
    git(repo.path, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "fix")
    fixed_sha = git(repo.path, "rev-parse", "HEAD")
    outcome = await env.verify(env.verifier(), repo, fixed_sha)
    assert outcome.passed, outcome.summary
    (row,) = env.rows("SELECT * FROM findings WHERE run_id = %s", env.run_id)
    assert row["status"] == "VERIFIED_FIXED" and row["fixed_in_commit_sha"] == fixed_sha
    assert row["evidence_id"] == env.gates()["ruff-lint"]["evidence_id"]
    assert (await env.completion()).ok


def _docker_ready() -> bool:
    return Path(DOCKER_SOCK).exists() and all(_image_available(DEFAULT_GATE_IMAGES[k]) for k in ("postgres", "python"))


@pytest.mark.docker
@pytest.mark.skipif(not _docker_ready(), reason="Docker daemon / закреплённые образы postgres, python недоступны")
async def test_destructive_migration_requires_approval(database: testdb.TestDatabase, area: Path) -> None:
    repo = materialize("migrations", "destructive", area / "repo")
    env = await _env(database, area, repo.base, (("scope", "SCOPE"), ("migrations", "MIGRATION")))
    try:
        async with DockerSandboxRunner(
            f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(area)], require_image_digest=True, timeout_grace_seconds=1
        ) as runner:
            verifier = env.verifier(
                runner, image=DEFAULT_GATE_IMAGES["python"], images=DEFAULT_GATE_IMAGES, approvals=Approvals()
            )
            outcome = await env.verify(verifier, repo)
            mig = env.gates()["migrations"]
            assert not outcome.passed and mig["status"] == "FAIL", mig["error_message"]
            assert mig["error_message"].startswith("APPROVAL_REQUIRED")
            (approval,) = env.rows("SELECT * FROM approvals WHERE run_id = %s", env.run_id)
            assert approval["action_type"] == "DESTRUCTIVE_MIGRATION" and approval["status"] == "PENDING"
            (finding,) = env.rows("SELECT * FROM findings WHERE run_id = %s", env.run_id)
            assert finding["rule_id"] == "migration/destructive-drop-column" and finding["severity"] == "high"
            ev = env.rows("SELECT environment FROM evidence WHERE id = %s", mig["evidence_id"])[0]["environment"]
            assert ev["image"] == DEFAULT_GATE_IMAGES["postgres"] and ev["network"] == "none"

            again = await env.verify(verifier, repo)  # повтор до решения: approval не дублируется
            assert not again.passed and len(env.rows("SELECT id FROM approvals WHERE run_id = %s", env.run_id)) == 1

            with connect_admin(env.database) as conn:
                conn.execute(
                    "UPDATE approvals SET status = 'APPROVED', decided_by = %s, decided_at = now(), version = version + 1"
                    " WHERE id = %s",
                    (env.tenant.owner_id, approval["id"]),
                )
            approved = await env.verify(verifier, repo)
        assert approved.passed, approved.summary
        assert env.gates()["migrations"]["status"] == "PASS"
        (finding,) = env.rows("SELECT * FROM findings WHERE run_id = %s", env.run_id)
        assert finding["severity"] == "info" and finding["title"].startswith("approved (")
        assert (await env.completion()).ok
    finally:
        await env.engine.dispose()
