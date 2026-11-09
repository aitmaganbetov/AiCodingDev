"""P3-14: Verification Engine v0 с реальным PostgreSQL (AC-11 false-pass prevention, AC-14 evidence).

Процессы gates исполняются `LocalProcessRunner` (фиксированный argv, чистое окружение); отдельный
тест `docker` запускает SCOPE в verification-контейнере через `DockerSandboxRunner`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_platform.artifacts import FileSystemArtifactStore
from aicd_platform.db import create_engine
from aicd_platform.ids import uuid7
from aicd_security.docker_runner import DockerSandboxRunner
from aicd_security.local_runner import LocalProcessRunner
from aicd_security.sandbox import ContainerSpec, SandboxRunner
from aicd_verification.engine import PROBE_TIMEOUT_SECONDS, VerificationEngine, VerificationOutcome, declared_gate_env
from aicd_verification.gates import V0_GATE_DEFINITIONS
from aicd_verification.images import default_gate_images
from aicd_verification.registry import GATE_SPECS
from aicd_verification.security_gates import SECURITY_GATE_IMAGES
from aicd_verification.tools.images import SEMGREP
from tests.conftest import init_git_repo
from tests.support import db as testdb
from tests.support.orchestration import Tenant, connect_admin, drive_run, new_task, seed_tenant

pytestmark = pytest.mark.db

FAKE_AWS_KEY = "AKIA" + "Q7ZK4M2XW9TR3BNV"  # не настоящий ключ
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": os.environ["PATH"],
}
VENV_BIN = str(Path(sys.executable).parent)
DOCKER_SOCK = "/var/run/docker.sock"
PY_IMAGE = "python:3.12-slim-bookworm"
ROOT = Path(__file__).resolve().parents[2]
# Окружение контейнера gate, которое задаёт сам движок (VerificationEngine._spec_for).
ENGINE_ENV = {"CI", "TZ", "LANG", "HOME", "PYTHONDONTWRITEBYTECODE", "RUFF_CACHE_DIR", "npm_config_cache",
              "npm_config_update_notifier"}  # fmt: skip


@dataclass
class Env:
    database: testdb.TestDatabase
    engine: AsyncEngine
    store: FileSystemArtifactStore
    tenant: Tenant
    run_id: uuid.UUID
    task_id: uuid.UUID
    repo: Path
    base: str
    tmp: Path

    def verifier(
        self, runner: SandboxRunner | None = None, *, path: str | None = None, **kwargs: Any
    ) -> VerificationEngine:
        return VerificationEngine(
            engine=self.engine,
            store=self.store,
            runner=runner or LocalProcessRunner(),
            local_env={"PATH": path or f"{VENV_BIN}:/usr/bin:/bin"},
            staging_root=self.tmp,
            **kwargs,
        )

    async def verify(self, verifier: VerificationEngine, commit: str) -> VerificationOutcome:
        return await verifier.verify_task(
            workspace_id=self.tenant.workspace_id,
            run_id=self.run_id,
            task_id=self.task_id,
            worktree_path=self.repo,
            commit_sha=commit,
            quality_profile="strict",
        )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=GIT_ENV
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def _seed(database: testdb.TestDatabase, base: str) -> tuple[Tenant, uuid.UUID, uuid.UUID]:
    """Tenant + профиль strict уровня workspace с gates v0 (глобального seed gates в 0003 нет)."""
    with connect_admin(database) as conn:
        tenant = seed_tenant(conn)
        profile = uuid7()
        conn.execute(
            "INSERT INTO quality_profiles (id, workspace_id, code, version, definition)"
            " VALUES (%s, %s, 'strict', 1, '{}')",
            (profile, tenant.workspace_id),
        )
        for name, gate_type, required, timeout in V0_GATE_DEFINITIONS:
            conn.execute(
                "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type, required, timeout_seconds)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uuid7(), tenant.workspace_id, profile, name, gate_type, required, timeout),
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
    return tenant, run, task


@pytest.fixture
async def env(database: testdb.TestDatabase, tmp_path: Path) -> AsyncIterator[Env]:
    area = (tmp_path / "area").resolve()
    area.mkdir()
    repo = area / "repo"
    init_git_repo(repo)
    base = git(repo, "rev-parse", "HEAD")
    tenant, run, task = _seed(database, base)
    engine = create_engine(database.url_for("aicd_worker"))
    store = FileSystemArtifactStore(tmp_path / "artifacts")
    yield Env(database, engine, store, tenant, run, task, repo, base, area)
    await engine.dispose()


def gate_rows(env: Env) -> dict[str, dict[str, Any]]:
    with connect_admin(env.database) as conn:
        cur = conn.execute(
            "SELECT q.name, g.* FROM gate_executions g JOIN quality_gates q ON q.id = g.gate_id"
            " WHERE g.task_id = %s AND g.superseded_at IS NULL",
            (env.task_id,),
        )
        cols = [c.name for c in cur.description or ()]
        return {row[0]: dict(zip(cols, row, strict=True)) for row in cur.fetchall()}


def evidence_of(env: Env, evidence_id: uuid.UUID) -> dict[str, Any]:
    with connect_admin(env.database) as conn:
        cur = conn.execute(
            "SELECT e.*, a.storage_ref, a.sha256, a.size_bytes, a.kind AS artifact_kind, a.retention_class"
            " FROM evidence e JOIN artifacts a ON a.id = e.artifact_id WHERE e.id = %s",
            (evidence_id,),
        )
        cols = [c.name for c in cur.description or ()]
        row = cur.fetchone()
        assert row is not None
        return dict(zip(cols, row, strict=True))


def gate_events(env: Env) -> list[dict[str, Any]]:
    with connect_admin(env.database) as conn:
        rows = conn.execute(
            "SELECT payload FROM outbox_events WHERE run_id = %s AND event_type = 'gate.completed' ORDER BY id",
            (env.run_id,),
        ).fetchall()
    return [r[0] for r in rows]


def artifact_bytes(env: Env, ev: dict[str, Any]) -> bytes:
    return b"".join(env.store.open(ev["storage_ref"], expected_sha256=ev["sha256"]))


async def test_clean_change_scope_pass_with_complete_evidence(env: Env) -> None:
    sha = commit(env.repo, {"README.md": "test\nmore docs\n"})
    outcome = await env.verify(env.verifier(), sha)

    assert outcome.passed, outcome.summary
    rows = gate_rows(env)
    assert rows["scope"]["status"] == "PASS" and rows["scope"]["required"]
    assert rows["scope"]["started_at"] is not None and rows["scope"]["finished_at"] is not None
    for name in ("ruff-lint", "ruff-format", "pytest-unit", "npm-unit"):  # нет Python/JS в репозитории
        assert rows[name]["status"] == "NOT_APPLICABLE"
        assert "detector:" in rows[name]["not_applicable_reason"]
    assert set(outcome.gate_execution_ids) == {r["id"] for r in rows.values()}

    # AC-14: evidence полно и artifact совпадает с хранилищем по sha256.
    ev = evidence_of(env, rows["scope"]["evidence_id"])
    assert ev["verification_status"] == "VERIFIED" and ev["verified_at"] is not None
    assert ev["producer"] == "verification_engine" and ev["kind"] == "scan_report"
    assert ev["commit_sha"] == sha and ev["exit_code"] == 0
    assert ev["command"].startswith("python3 -I -S -c '<aicd-scope-scanner sha256:")
    assert ev["started_at"] <= ev["finished_at"]
    assert ev["environment"]["runner"] == "local"
    assert ev["environment"]["tool_versions"]["aicd-scope"].startswith("Python 3")
    data = artifact_bytes(env, ev)
    assert hashlib.sha256(data).hexdigest() == ev["sha256"] and len(data) == ev["size_bytes"]
    assert ev["retention_class"] == "evidence"
    assert b'"violations_total": 0' in data

    events = gate_events(env)
    assert len(events) == 5
    scope_event = next(e for e in events if e["gate_type"] == "SCOPE")
    assert scope_event["status"] == "PASS" and scope_event["commit_sha"] == sha
    assert scope_event["evidence_id"] == str(rows["scope"]["evidence_id"])
    assert set(scope_event) <= {
        "gate_execution_id",
        "gate_type",
        "task_id",
        "status",
        "required",
        "commit_sha",
        "evidence_id",
        "findings",
        "duration_ms",
        "tool",
    }

    # Повторная доставка на том же SHA: результат переиспользуется, второго PASS нет (FP-18).
    again = await env.verify(env.verifier(), sha)
    assert again.passed and set(again.gate_execution_ids) == set(outcome.gate_execution_ids)
    assert len(gate_events(env)) == 5


async def test_secret_in_diff_fails_scope_without_leaking_value(env: Env) -> None:
    sha = commit(env.repo, {"config/settings.txt": f"aws_key = {FAKE_AWS_KEY}\n"})
    outcome = await env.verify(env.verifier(), sha)

    assert not outcome.passed
    rows = gate_rows(env)
    assert rows["scope"]["status"] == "FAIL"
    ev = evidence_of(env, rows["scope"]["evidence_id"])
    assert ev["verification_status"] == "VERIFIED" and ev["exit_code"] == 1
    data = artifact_bytes(env, ev)
    assert b"secret-scan/aws-access-key-id" in data and b"config/settings.txt" in data
    # Значение секрета нигде не сохранено: ни в артефакте, ни в evidence/gate/outbox.
    assert FAKE_AWS_KEY.encode() not in data
    assert FAKE_AWS_KEY not in json.dumps(ev, default=str)
    assert FAKE_AWS_KEY not in json.dumps(rows, default=str)
    events = gate_events(env)
    assert FAKE_AWS_KEY not in json.dumps(events)
    scope_event = next(e for e in events if e["gate_type"] == "SCOPE")
    assert scope_event["status"] == "FAIL" and scope_event["findings"]["critical"] == 1
    assert not list(env.tmp.glob("aicd-verify-*/diff.patch"))  # staging с diff удалён


async def test_required_tool_missing_is_error_not_pass(env: Env, tmp_path: Path) -> None:
    sha = commit(env.repo, {"pyproject.toml": "[project]\nname = 'x'\n", "pkg/mod.py": "X = 1\n"})
    bare_bin = tmp_path / "bin"  # только python3, без ruff
    bare_bin.mkdir()
    (bare_bin / "python3").symlink_to(sys.executable)
    outcome = await env.verify(env.verifier(path=f"{bare_bin}:/usr/bin:/bin"), sha)

    rows = gate_rows(env)
    assert rows["scope"]["status"] == "PASS"
    assert rows["ruff-lint"]["status"] == "ERROR" and rows["ruff-lint"]["required"]
    assert rows["ruff-lint"]["error_message"].startswith("TOOL_UNAVAILABLE")
    assert outcome.passed is False  # AC-11: отсутствие инструмента ≠ PASS
    assert "ruff-lint[LINT,required]=ERROR" in outcome.summary


async def test_python_gates_pass_and_fail_on_real_tools(env: Env) -> None:
    files = {
        "pyproject.toml": "[project]\nname = 'x'\n",
        "pkg/__init__.py": "",
        "pkg/mod.py": "def add(a, b):\n    return a + b\n",
        "tests/test_mod.py": "from pkg.mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    }
    sha = commit(env.repo, files)
    outcome = await env.verify(env.verifier(), sha)
    rows = gate_rows(env)
    assert rows["ruff-lint"]["status"] == "PASS", rows["ruff-lint"]["error_message"]
    assert rows["pytest-unit"]["status"] == "PASS", rows["pytest-unit"]["error_message"]
    assert outcome.passed, outcome.summary
    ev = evidence_of(env, rows["pytest-unit"]["evidence_id"])
    assert ev["kind"] == "test_report" and ev["command"].startswith("python3 -m pytest")
    assert "pytest" in ev["environment"]["tool_versions"]["pytest"]

    # Новый commit с упавшим тестом: прежние строки замещены (FP-13), UNIT = FAIL.
    sha2 = commit(env.repo, {"pkg/mod.py": "def add(a, b):\n    return a - b\n"})
    outcome2 = await env.verify(env.verifier(), sha2)
    rows2 = gate_rows(env)
    assert rows2["pytest-unit"]["status"] == "FAIL" and rows2["pytest-unit"]["commit_sha"] == sha2
    assert rows2["pytest-unit"]["attempt_no"] == 2
    assert not outcome2.passed


async def test_gate_timeout_is_error(env: Env) -> None:
    sha = commit(env.repo, {"tests/test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(30)\n"})
    outcome = await env.verify(env.verifier(timeout_cap_seconds=2), sha)
    rows = gate_rows(env)
    assert rows["pytest-unit"]["status"] == "ERROR"
    assert rows["pytest-unit"]["error_message"].startswith("TIMEOUT")
    assert rows["pytest-unit"]["evidence_id"] is not None  # частичный вывод сохранён
    assert not outcome.passed


async def test_sha_mismatch_and_dirty_worktree_never_pass(env: Env) -> None:
    sha = commit(env.repo, {"README.md": "v2\n"})
    outcome = await env.verify(env.verifier(), env.base)  # HEAD ≠ проверяемый SHA
    assert not outcome.passed
    assert gate_rows(env)["scope"]["error_message"].startswith("SHA_MISMATCH")

    commit(env.repo, {"tests/test_x.py": "def test_x():\n    assert True\n"})
    head = git(env.repo, "rev-parse", "HEAD")
    (env.repo / "tests" / "test_x.py").write_text("def test_x():\n    assert False\n")  # незакоммичено
    outcome = await env.verify(env.verifier(), head)
    rows = gate_rows(env)
    assert rows["pytest-unit"]["status"] == "ERROR"
    assert rows["pytest-unit"]["error_message"].startswith("WORKTREE_DIRTY")
    assert not outcome.passed
    assert sha  # первый коммит использован как «чужой» HEAD


def _docker_available() -> bool:
    if not Path(DOCKER_SOCK).exists():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker", timeout=3) as c:
            return c.get("/_ping").status_code == 200 and c.get(f"/images/{PY_IMAGE}/json").status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason=f"Docker daemon / image {PY_IMAGE} недоступны")
async def test_scope_gate_in_verification_container(env: Env) -> None:
    sha = commit(env.repo, {"src/app.py": f"KEY = '{FAKE_AWS_KEY}'\n"})
    async with DockerSandboxRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(env.tmp)], timeout_grace_seconds=1
    ) as runner:
        outcome = await env.verify(env.verifier(runner, image=PY_IMAGE), sha)
    rows = gate_rows(env)
    assert rows["scope"]["status"] == "FAIL", rows["scope"]["error_message"]
    # Python есть, но конфигурации ruff нет → NOT_APPLICABLE; тестов нет → NOT_APPLICABLE.
    assert rows["ruff-lint"]["status"] == "NOT_APPLICABLE"
    assert not outcome.passed
    ev = evidence_of(env, rows["scope"]["evidence_id"])
    assert ev["environment"]["runner"] == "docker" and ev["environment"]["network"] == "none"
    assert ev["environment"]["image"] == PY_IMAGE
    assert str(ev["environment"]["image_id"]).startswith("sha256:")
    assert ev["environment"]["workspace_mount"] == "read-only"
    data = artifact_bytes(env, ev)
    assert b"secret-scan/aws-access-key-id" in data and FAKE_AWS_KEY.encode() not in data


class _RecordingDockerRunner(DockerSandboxRunner):
    """DockerSandboxRunner, запоминающий спецификации контейнеров (env, сеть) для проверки."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.specs: list[ContainerSpec] = []

    async def start(self, spec: ContainerSpec) -> Any:
        self.specs.append(spec)
        return await super().start(spec)


def _semgrep_image_present() -> bool:
    if not _docker_available():
        return False
    with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker", timeout=3) as c:
        return c.get(f"/images/{SEMGREP.ref}/json").status_code == 200


@pytest.mark.docker
@pytest.mark.skipif(not _semgrep_image_present(), reason=f"Docker daemon / образ {SEMGREP.ref} недоступны")
async def test_semgrep_sast_in_container_offline_gets_declared_env(env: Env) -> None:
    """Объявленные gate переменные (офлайн-флаги semgrep) доходят до контейнера: без них `semgrep --version` в
    сети `none` ждёт проверку версии дольше PROBE_TIMEOUT_SECONDS (60 с) и SAST даёт ERROR TOOL_UNAVAILABLE."""
    with connect_admin(env.database) as conn:
        profile = conn.execute("SELECT id FROM quality_profiles WHERE workspace_id = %s", (env.tenant.workspace_id,))
        conn.execute(
            "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type, required, timeout_seconds)"
            " VALUES (%s, %s, %s, 'semgrep-sast', 'SAST', true, 300)",
            (uuid7(), env.tenant.workspace_id, profile.fetchone()[0]),  # type: ignore[index]
        )
    clean = (ROOT / "tests" / "fixtures" / "security" / "semgrep" / "clean" / "safe_tasks.py").read_text()
    sha = commit(env.repo, {"src/safe_tasks.py": clean})
    started = time.monotonic()
    recorder = _RecordingDockerRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(env.tmp)], timeout_grace_seconds=1
    )
    async with recorder as runner:
        await env.verify(env.verifier(runner, image=PY_IMAGE, images=default_gate_images(SECURITY_GATE_IMAGES)), sha)
    elapsed = time.monotonic() - started
    rows = gate_rows(env)
    assert rows["semgrep-sast"]["status"] == "PASS", rows["semgrep-sast"]["error_message"]
    semgrep_specs = [s for s in recorder.specs if s.labels.get("aicd.gate") == "semgrep-sast"]
    assert len(semgrep_specs) == 2  # probe + запуск
    for spec in semgrep_specs:
        assert spec.network == "none"
        assert spec.env["SEMGREP_ENABLE_VERSION_CHECK"] == "0" and spec.env["SEMGREP_SEND_METRICS"] == "off"
        assert spec.env["HOME"] == "/home/verify"  # зарезервированные значения движка не переопределены
        assert not set(spec.env) - {*declared_gate_env(GATE_SPECS["semgrep-sast"]), *ENGINE_ENV}
    assert elapsed < PROBE_TIMEOUT_SECONDS, f"SAST офлайн занял {elapsed:.0f} с"
