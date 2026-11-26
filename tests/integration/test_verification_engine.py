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


# --- шаг prepare: зависимости проекта для gates (QUALITY_GATES §4.3) ------------------------------

PREPARE_NETWORK = "aicd-prep-egress-test"
_PY_PROJECT = {
    "pyproject.toml": (
        "[project]\nname = 'x'\nversion = '0'\n"
        "[tool.mypy]\nmypy_path = 'pkg'\n"
        "[tool.hatch.build.targets.wheel]\npackages = ['pkg/x']\n"
    ),
    "pkg/x/__init__.py": "",
    "pkg/x/mod.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
    "tests/test_mod.py": "from x.mod import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n",
}


def _prepare_verifier(env: Env, runner: SandboxRunner, root: Path) -> VerificationEngine:
    return env.verifier(
        runner,
        image=PY_IMAGE,
        images=default_gate_images(),
        prepare_root=root,
        prepare_network=PREPARE_NETWORK,
    )


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason=f"Docker daemon / image {PY_IMAGE} недоступны")
async def test_prepare_without_lockfile_blocks_dependent_gates(env: Env, tmp_path: Path) -> None:
    """Без lock-файла установка недетерминирована: зависимые gates — ERROR, инструмент не запускается.

    Проверяется fail-closed: `DEPENDENCIES_UNAVAILABLE` ≠ `PASS`, и контейнер pytest вообще не
    создаётся (ни probe, ни прогон) — подложить результат нечем.
    """
    sha = commit(env.repo, _PY_PROJECT)  # uv.lock отсутствует
    recorder = _RecordingDockerRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(env.tmp), str(tmp_path)], timeout_grace_seconds=1
    )
    async with recorder as runner:
        outcome = await env.verify(_prepare_verifier(env, runner, tmp_path / "deps"), sha)

    rows = gate_rows(env)
    assert rows["pytest-unit"]["status"] == "ERROR"
    assert rows["pytest-unit"]["error_message"].startswith("DEPENDENCIES_UNAVAILABLE")
    assert "PREPARE_NO_LOCKFILE" in rows["pytest-unit"]["error_message"]
    assert not outcome.passed
    assert all(s.labels.get("aicd.gate") != "pytest-unit" for s in recorder.specs)


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason=f"Docker daemon / image {PY_IMAGE} недоступны")
async def test_prepare_cache_is_mounted_read_only_with_dependency_env(env: Env, tmp_path: Path) -> None:
    """Готовый кэш (по ключу содержимого) монтируется в gate ro, PATH/PYTHONPATH указывают на него."""
    from aicd_verification import prepare as prepare_step

    sha = commit(env.repo, {**_PY_PROJECT, "uv.lock": "version = 1\nrequires-python = '>=3.12'\n"})
    images = default_gate_images()
    deps_root = tmp_path / "deps"
    key, _digests = prepare_step.cache_key(
        workspace_id=str(env.tenant.workspace_id),
        spec=prepare_step.PYTHON_SPEC,
        image=images["prepare-python"],
        repo=env.repo,
        files=("uv.lock", "pyproject.toml"),
    )
    cache = deps_root / key  # маркер прошлой успешной подготовки → prepare не запускается (cache hit)
    cache.mkdir(parents=True)
    (cache / prepare_step.MARKER_NAME).write_text('{"cache": "hit"}', encoding="utf-8")

    recorder = _RecordingDockerRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(env.tmp), str(tmp_path)], timeout_grace_seconds=1
    )
    async with recorder as runner:
        await env.verify(_prepare_verifier(env, runner, deps_root), sha)

    # Контейнер prepare не создавался: ключ кэша совпал.
    assert all(s.labels.get("aicd.kind") != prepare_step.PREPARE_KIND for s in recorder.specs)
    pytest_specs = [s for s in recorder.specs if s.labels.get("aicd.gate") == "pytest-unit"]
    assert pytest_specs, [s.labels for s in recorder.specs]
    for spec in pytest_specs:
        deps = [m for m in spec.mounts if m.target == prepare_step.DEPS_DIR]
        assert len(deps) == 1 and deps[0].read_only and deps[0].source == cache
        assert spec.env["PATH"].startswith(f"{prepare_step.DEPS_DIR}/venv/bin:")
        assert spec.env["PYTHONPATH"] == "/workspace/pkg"
        assert spec.network == "none"  # gate остаётся без сети, egress есть только у prepare
    # Каталог кэша пуст (его содержимое в этом тесте не создаётся), поэтому gate — честный ERROR,
    # а не PASS: смонтированный кэш сам по себе вердикта не даёт.
    rows = gate_rows(env)
    assert rows["pytest-unit"]["status"] == "ERROR"
    assert rows["pytest-unit"]["error_message"].startswith("TOOL_UNAVAILABLE")


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason=f"Docker daemon / image {PY_IMAGE} недоступны")
async def test_project_without_dependency_manifest_gets_platform_tools_only(env: Env, tmp_path: Path) -> None:
    """Нет ни lock-файла, ни манифеста зависимостей: не PREPARE_NO_LOCKFILE, а окружение инструментов платформы."""
    from aicd_verification import prepare as prepare_step

    sha = commit(env.repo, {"tests/test_plain.py": "def test_ok() -> None:\n    assert True\n"})
    images = default_gate_images()
    deps_root = tmp_path / "deps"
    bare = prepare_step.bare_spec(prepare_step.PYTHON_SPEC)
    assert bare is not None
    key, _digests = prepare_step.cache_key(
        workspace_id=str(env.tenant.workspace_id),
        spec=bare,
        image=images["prepare-python"],
        repo=env.repo,
        files=bare.manifests,
    )
    cache = deps_root / key  # готовый кэш: сам prepare (нужна сеть) в тесте не запускается
    cache.mkdir(parents=True)
    (cache / prepare_step.MARKER_NAME).write_text('{"cache": "hit"}', encoding="utf-8")

    recorder = _RecordingDockerRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(env.tmp), str(tmp_path)], timeout_grace_seconds=1
    )
    async with recorder as runner:
        await env.verify(_prepare_verifier(env, runner, deps_root), sha)

    pytest_specs = [s for s in recorder.specs if s.labels.get("aicd.gate") == "pytest-unit"]
    assert pytest_specs, [s.labels for s in recorder.specs]
    assert all(any(m.target == prepare_step.DEPS_DIR and m.source == cache for m in s.mounts) for s in pytest_specs)
    assert "PREPARE_NO_LOCKFILE" not in (gate_rows(env)["pytest-unit"]["error_message"] or "")


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason=f"Docker daemon / image {PY_IMAGE} недоступны")
async def test_gates_without_needs_deps_never_get_the_dependency_cache(env: Env, tmp_path: Path) -> None:
    """Кэш зависимостей не попадает в gates, которым он не нужен (SCOPE, ruff): лишних монтирований нет."""
    from aicd_verification import prepare as prepare_step

    sha = commit(env.repo, {**_PY_PROJECT, "uv.lock": "version = 1\n"})
    recorder = _RecordingDockerRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(env.tmp), str(tmp_path)], timeout_grace_seconds=1
    )
    async with recorder as runner:
        await env.verify(_prepare_verifier(env, runner, tmp_path / "deps"), sha)
    for spec in recorder.specs:
        if spec.labels.get("aicd.gate") in ("scope", "ruff-lint", "ruff-format"):
            assert all(m.target != prepare_step.DEPS_DIR for m in spec.mounts), spec.labels


async def test_superseded_execution_is_not_restarted(env: Env) -> None:
    """Параллельная верификация той же задачи: строка gate завершена и `superseded_at` проставлен,
    пока gate ждал семафор. Повторный старт такой строки нарушал CHECK `ck_gate_executions_finished`
    (IntegrityError в `_set_running`), gate получал ERROR(INFRA) и задача уходила на бесконечный повтор.
    """
    from aicd_verification.engine import _GateRun, _TaskContext

    execution_id = uuid7()
    with connect_admin(env.database) as conn:
        gate_id = conn.execute("SELECT id FROM quality_gates WHERE name = 'scope' LIMIT 1").fetchone()[0]  # type: ignore[index]
        conn.execute(
            "INSERT INTO gate_executions (id, workspace_id, run_id, task_id, gate_id, gate_type, required,"
            " status, attempt_no, commit_sha, finished_at, superseded_at)"
            " VALUES (%s, %s, %s, %s, %s, 'SCOPE', true, 'ERROR', 1, %s, now(), now())",
            (execution_id, env.tenant.workspace_id, env.run_id, env.task_id, gate_id, "0" * 40),
        )

    verifier = env.verifier()
    task = _TaskContext(env.tenant.workspace_id, env.run_id, env.task_id, uuid7(), "0" * 40, env.repo)
    gate = _GateRun(gate_id, "scope", "SCOPE", True, 60, GATE_SPECS["scope"], None, execution_id=execution_id)

    assert await verifier._set_running(task, gate) is False
    assert gate.status == "ERROR"  # вердикт принадлежит другой верификации, здесь он не перезаписан
    with connect_admin(env.database) as conn:
        row = conn.execute("SELECT status, started_at FROM gate_executions WHERE id = %s", (execution_id,)).fetchone()
    assert row is not None and row[0] == "ERROR" and row[1] is None


# --- офлайн-БД уязвимостей для gates SCA (runbook vulndb-update.md) ---------------------------------


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason=f"Docker daemon / image {PY_IMAGE} недоступны")
async def test_sca_gate_gets_fresh_offline_db_read_only_and_rejects_missing_one(env: Env, tmp_path: Path) -> None:
    """Свежая БД монтируется в gate SCA только для чтения; отсутствующая — ERROR(TOOL_UNAVAILABLE), не PASS."""
    from datetime import UTC, datetime

    from aicd_verification.security_gates import CONTAINER_DB_DIR, OSV_MANIFEST_FILE

    with connect_admin(env.database) as conn:
        profile = conn.execute("SELECT id FROM quality_profiles WHERE workspace_id = %s", (env.tenant.workspace_id,))
        profile_id = profile.fetchone()[0]  # type: ignore[index]
        for name in ("osv-scanner-sca", "trivy-vuln"):
            conn.execute(
                "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type, required, timeout_seconds)"
                " VALUES (%s, %s, %s, %s, 'SCA', true, 300)",
                (uuid7(), env.tenant.workspace_id, profile_id, name),
            )
    sha = commit(env.repo, {**_PY_PROJECT, "uv.lock": "version = 1\nrequires-python = '>=3.12'\n"})
    vulndb = tmp_path / "vulndb"  # есть только БД OSV; БД Trivy не выложена
    (vulndb / "osv" / "osv-scalibr" / "PyPI").mkdir(parents=True)
    (vulndb / "osv" / "osv-scalibr" / "PyPI" / "all.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    (vulndb / "osv" / OSV_MANIFEST_FILE).write_text(
        json.dumps({"updated_at": datetime.now(UTC).isoformat(), "ecosystems": ["PyPI"]}), encoding="utf-8"
    )
    recorder = _RecordingDockerRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(env.tmp), str(tmp_path)], timeout_grace_seconds=1
    )
    async with recorder as runner:
        verifier = env.verifier(
            runner, image=PY_IMAGE, images=default_gate_images(SECURITY_GATE_IMAGES), vulndb_root=vulndb
        )
        await env.verify(verifier, sha)

    osv_specs = [s for s in recorder.specs if s.labels.get("aicd.gate") == "osv-scanner-sca"]
    assert osv_specs, sorted({s.labels.get("aicd.gate", "") for s in recorder.specs})
    for spec in osv_specs:
        db = [m for m in spec.mounts if m.target == CONTAINER_DB_DIR]
        assert len(db) == 1 and db[0].read_only and db[0].source == vulndb / "osv"
        assert spec.network == "none"
    rows = gate_rows(env)
    assert "is not configured" not in (rows["osv-scanner-sca"]["error_message"] or "")
    assert rows["trivy-vuln"]["status"] == "ERROR"
    assert rows["trivy-vuln"]["error_message"].startswith("TOOL_UNAVAILABLE: offline trivy vulnerability DB")
    assert all(s.labels.get("aicd.gate") != "trivy-vuln" for s in recorder.specs)


# --- порог severity по новым находкам относительно base_commit (QUALITY_GATES §2.2) ---------------


@pytest.mark.docker
@pytest.mark.skipif(not _semgrep_image_present(), reason=f"Docker daemon / образ {SEMGREP.ref} недоступны")
@pytest.mark.parametrize("adds_vulnerability", [False, True])
async def test_scanner_blocks_only_on_findings_new_relative_to_base(env: Env, adds_vulnerability: bool) -> None:
    """Находки, уже бывшие на base commit, показываются, но не блокируют; новая находка блокирует."""
    fixtures = ROOT / "tests" / "fixtures" / "security" / "semgrep"
    vulnerable = (fixtures / "vulnerable" / "vuln_handlers.py").read_text()
    base = commit(env.repo, {"src/legacy_handlers.py": vulnerable}, "legacy code with findings")
    with connect_admin(env.database) as conn:
        conn.execute("UPDATE runs SET base_commit_sha = %s, version = version + 1 WHERE id = %s", (base, env.run_id))
        profile = conn.execute("SELECT id FROM quality_profiles WHERE workspace_id = %s", (env.tenant.workspace_id,))
        conn.execute(
            "INSERT INTO quality_gates (id, workspace_id, profile_id, name, gate_type, required, timeout_seconds)"
            " VALUES (%s, %s, %s, 'semgrep-sast', 'SAST', true, 300)",
            (uuid7(), env.tenant.workspace_id, profile.fetchone()[0]),  # type: ignore[index]
        )
    change = {"src/safe_tasks.py": (fixtures / "clean" / "safe_tasks.py").read_text()}
    if adds_vulnerability:
        change["src/new_handlers.py"] = vulnerable
    sha = commit(env.repo, change, "task change")
    async with DockerSandboxRunner(
        f"unix://{DOCKER_SOCK}", allowed_bind_roots=[str(env.tmp)], timeout_grace_seconds=1
    ) as runner:
        await env.verify(env.verifier(runner, image=PY_IMAGE, images=default_gate_images(SECURITY_GATE_IMAGES)), sha)

    row = gate_rows(env)["semgrep-sast"]
    if adds_vulnerability:
        assert row["status"] == "FAIL", row
        assert "already present at base" in row["error_message"] and "src/new_handlers.py" in row["error_message"]
        assert "legacy_handlers.py" not in row["error_message"].split("):", 1)[1]
    else:
        assert row["status"] == "PASS", row["error_message"]
    assert not list(env.tmp.glob("aicd-verify-base-*"))  # checkout base commit удалён
