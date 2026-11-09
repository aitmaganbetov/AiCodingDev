"""P6-03/P6-05: gates в verification-контейнерах (`DockerSandboxRunner`, сеть none, worktree read-only)
на образах, закреплённых по digest (`aicd_verification.images`), с `require_image_digest=True` (профиль server).

* ruff и eslint — фикстуры pass/fail;
* MIGRATION — эфемерная PostgreSQL 17 (postgres:17-alpine по digest): upgrade→downgrade→upgrade,
  неполный откат, ошибка миграции, деструктивная операция → предмет approval.
Пропускаются, если Docker или образ недоступны (образы не скачиваются тестом).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aicd_security.docker_runner import DockerSandboxRunner
from aicd_verification.images import DEFAULT_GATE_IMAGES
from aicd_verification.registry import GATE_SPECS
from tests.support.gates import materialize, run_spec

DOCKER_SOCK = "/var/run/docker.sock"
pytestmark = pytest.mark.docker


def _image_available(ref: str) -> bool:
    if not Path(DOCKER_SOCK).exists():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker", timeout=3) as c:
            return c.get("/_ping").status_code == 200 and c.get(f"/images/{ref}/json").status_code == 200
    except httpx.HTTPError:
        return False


def needs(key: str) -> pytest.MarkDecorator:
    ref = DEFAULT_GATE_IMAGES[key]
    return pytest.mark.skipif(not _image_available(ref), reason=f"Docker daemon / образ {ref} недоступны")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


def runner(tmp_path: Path) -> DockerSandboxRunner:
    return DockerSandboxRunner(
        f"unix://{DOCKER_SOCK}",
        allowed_bind_roots=[str(tmp_path)],
        require_image_digest=True,
        timeout_grace_seconds=1,
    )


@needs("ruff")
@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_ruff_in_pinned_container(root: Path, case: str, status: str) -> None:
    repo = materialize("ruff", case, root / "repo")
    async with runner(root) as r:
        res = await run_spec(GATE_SPECS["ruff-lint"], repo, runner=r, image=DEFAULT_GATE_IMAGES["ruff"])
    assert res.verdict is not None and res.verdict.status == status, res.stdout + res.stderr
    if status == "FAIL":
        assert res.verdict.findings[0].file_path == "pkg/mod.py"


@needs("eslint")
@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_eslint_in_pinned_container(root: Path, case: str, status: str) -> None:
    repo = materialize("eslint", case, root / "repo")
    async with runner(root) as r:
        res = await run_spec(GATE_SPECS["eslint"], repo, runner=r, image=DEFAULT_GATE_IMAGES["eslint"])
    assert res.verdict is not None and res.verdict.status == status, res.stdout[-2000:] + res.stderr
    if status == "FAIL":
        assert {f.file_path for f in res.verdict.findings} == {"src/add.js"}  # пути /workspace/… нормализованы


@needs("eslint")
async def test_unpinned_image_is_rejected_by_server_policy(root: Path) -> None:
    repo = materialize("eslint", "pass", root / "repo")
    async with runner(root) as r:
        res = await run_spec(GATE_SPECS["eslint"], repo, runner=r, image="pipelinecomponents/eslint:latest")
    assert res.probe_code is None and res.verdict is None  # контейнер не создан → движок даст ERROR, не PASS


@needs("postgres")
@pytest.mark.parametrize(
    ("case", "status", "rule"),
    [
        ("pass", "PASS", None),
        ("broken_down", "FAIL", "migration/downgrade-incomplete"),
        ("failing", "FAIL", "migration/upgrade1-failed"),
    ],
)
async def test_migrations_on_ephemeral_postgres(root: Path, case: str, status: str, rule: str | None) -> None:
    repo = materialize("migrations", case, root / "repo")
    async with runner(root) as r:
        res = await run_spec(GATE_SPECS["migrations"], repo, runner=r, image=DEFAULT_GATE_IMAGES["postgres"])
    assert res.verdict is not None, res.stdout + res.stderr
    assert res.verdict.status == status, res.stdout + res.stderr
    assert res.verdict.approval is None
    if rule:
        assert any(f.rule_id == rule for f in res.verdict.findings), res.verdict.findings
    else:
        assert "upgrade→downgrade→upgrade ok" in res.verdict.message


@needs("postgres")
async def test_destructive_migration_applies_but_requires_approval(root: Path) -> None:
    repo = materialize("migrations", "destructive", root / "repo")
    async with runner(root) as r:
        res = await run_spec(GATE_SPECS["migrations"], repo, runner=r, image=DEFAULT_GATE_IMAGES["postgres"])
    assert res.verdict is not None and res.verdict.status == "PASS", res.stdout + res.stderr
    assert res.verdict.approval is not None  # движок превратит в FAIL до APPROVED approval
    (finding,) = [f for f in res.verdict.findings if f.rule_id.startswith("migration/destructive-")]
    assert finding.rule_id == "migration/destructive-drop-column" and finding.severity == "high"
    assert finding.file_path == "db/migrations/0002_drop_email.up.sql" and finding.line == 1
