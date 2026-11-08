"""P6-03: gate runners на фикстурах pass/fail реальными инструментами (без БД).

Локально (`LocalProcessRunner`) — только для тестов: в production gates исполняются в контейнере
(`AICD_VERIFICATION_RUNNER=local` запрещён). Инструменты: ruff, mypy, pytest(+pytest-cov) из venv,
eslint/tsc/node/npm из /opt/node22, Playwright (глобальный пакет `playwright`, тест без браузера).
Vitest ставится в кэш сессии из npm (`--prefer-offline`); недоступен — тест пропускается с причиной.
PHPStan в окружении нет (composer не смог скачать phpstan/phpstan: github.com недоступен) — проверяется
ERROR(TOOL_UNAVAILABLE), фикстурный прогон пропускается с явной причиной.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aicd_verification.registry import GATE_SPECS
from tests.support.gates import FixtureRepo, materialize, run_spec

VENV_BIN = str(Path(sys.executable).parent)
NODE_BIN = "/opt/node22/bin"
NODE_MODULES = Path("/opt/node22/lib/node_modules")
PATH = f"{VENV_BIN}:{NODE_BIN}:/usr/bin:/bin"


def _repo(tmp_path: Path, tool: str, case: str) -> FixtureRepo:
    return materialize(tool, case, tmp_path / f"{tool}-{case}")


def _link_node_module(repo: FixtureRepo, name: str, target: Path) -> None:
    """node_modules вне git (.gitignore фикстуры): worktree остаётся чистым."""
    nm = repo.path / "node_modules"
    nm.mkdir(exist_ok=True)
    (nm / name).symlink_to(target)


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_ruff_lint(tmp_path: Path, case: str, status: str) -> None:
    repo = _repo(tmp_path, "ruff", case)
    res = await run_spec(GATE_SPECS["ruff-lint"], repo, path=PATH)
    assert res.verdict is not None and res.verdict.status == status, res.stdout
    if status == "FAIL":
        assert any(f.rule_id == "F401" and f.file_path == "pkg/mod.py" and f.line == 1 for f in res.verdict.findings)


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_mypy(tmp_path: Path, case: str, status: str) -> None:
    repo = _repo(tmp_path, "mypy", case)
    res = await run_spec(GATE_SPECS["mypy"], repo, path=PATH)
    assert res.verdict is not None and res.verdict.status == status, res.stdout + res.stderr
    if status == "FAIL":
        (finding,) = res.verdict.findings
        assert finding.rule_id == "mypy/return-value" and finding.file_path == "pkg/mod.py" and finding.line == 2


@pytest.mark.parametrize(
    ("case", "status", "error"),
    [
        ("pass", "PASS", None),
        ("fail", "FAIL", None),
        ("empty", "FAIL", "EMPTY_SUITE"),
        ("skipped", "FAIL", "EMPTY_SUITE"),
    ],
)
async def test_pytest(tmp_path: Path, case: str, status: str, error: str | None) -> None:
    repo = _repo(tmp_path, "pytest", case)
    res = await run_spec(GATE_SPECS["pytest-unit"], repo, path=PATH)
    assert res.verdict is not None
    assert (res.verdict.status, res.verdict.error_code) == (status, error), res.stdout


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_pytest_coverage(tmp_path: Path, case: str, status: str) -> None:
    repo = _repo(tmp_path, "coverage", case)
    res = await run_spec(GATE_SPECS["pytest-coverage"], repo, path=PATH, profile="strict")
    assert res.verdict is not None and res.verdict.status == status, res.stdout[-2000:]
    assert res.verdict.coverage_percent is not None
    if status == "PASS":
        assert res.verdict.coverage_percent == 100.0
    else:
        assert res.verdict.error_code == "THRESHOLD" and res.verdict.coverage_percent < 85
    assert not (repo.path / ".coverage").exists()  # данные coverage — во временном каталоге, не в worktree
    # basic: coverage — только отчёт (порога нет)
    basic = await run_spec(GATE_SPECS["pytest-coverage"], repo, path=PATH, profile="basic")
    assert basic.verdict is not None and basic.verdict.status == "PASS"


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_eslint(tmp_path: Path, case: str, status: str) -> None:
    repo = _repo(tmp_path, "eslint", case)
    res = await run_spec(GATE_SPECS["eslint"], repo, path=PATH)
    assert res.verdict is not None and res.verdict.status == status, res.stdout[-2000:]
    if status == "FAIL":
        rules = {(f.rule_id, f.file_path, f.line) for f in res.verdict.findings}
        assert ("no-unused-vars", "src/add.js", 1) in rules and ("no-undef", "src/add.js", 3) in rules


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_tsc(tmp_path: Path, case: str, status: str) -> None:
    repo = _repo(tmp_path, "tsc", case)
    _link_node_module(repo, "typescript", NODE_MODULES / "typescript")
    res = await run_spec(GATE_SPECS["tsc"], repo, path=PATH)
    assert res.verdict is not None and res.verdict.status == status, res.stdout[-2000:]
    if status == "FAIL":
        assert res.verdict.findings[0].rule_id == "TS2322" and res.verdict.findings[0].file_path == "src/add.ts"


async def test_tsc_without_local_typescript_is_tool_unavailable(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "tsc", "pass")  # node_modules/typescript нет: tsc репозитория не установлен
    res = await run_spec(GATE_SPECS["tsc"], repo, path=PATH)
    assert res.probe_code not in (0, None) and res.verdict is None  # движок → ERROR(TOOL_UNAVAILABLE)


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_npm_build_runs_on_a_copy(tmp_path: Path, case: str, status: str) -> None:
    repo = _repo(tmp_path, "npm-build", case)
    res = await run_spec(GATE_SPECS["npm-build"], repo, path=PATH)
    assert res.verdict is not None and res.verdict.status == status, res.stdout + res.stderr
    assert not (repo.path / "dist").exists()  # сборка шла в копии ($HOME/aicd-build), worktree не изменён


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_playwright_without_browser(tmp_path: Path, case: str, status: str) -> None:
    if not (NODE_MODULES / "playwright" / "cli.js").exists():
        pytest.skip("пакет playwright отсутствует в /opt/node22")
    repo = _repo(tmp_path, "playwright", case)
    _link_node_module(repo, "playwright", NODE_MODULES / "playwright")
    res = await run_spec(GATE_SPECS["playwright"], repo, path=PATH)
    assert res.verdict is not None and res.verdict.status == status, res.stdout[-2000:]
    assert res.verdict.tests is not None and res.verdict.tests["passed" if status == "PASS" else "failed"] == 1


@pytest.fixture(scope="session")
def vitest_modules(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cache = tmp_path_factory.mktemp("vitest-cache")
    npm = shutil.which("npm", path=PATH)
    if npm is None:
        pytest.skip("npm недоступен")
    (cache / "package.json").write_text('{"name": "vitest-cache", "private": true}\n')
    proc = subprocess.run(
        [npm, "install", "--prefer-offline", "--no-audit", "--no-fund", "--ignore-scripts", "vitest@3.2.4"],
        cwd=cache,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PATH": PATH},
        check=False,
    )
    if proc.returncode != 0 or not (cache / "node_modules" / "vitest" / "vitest.mjs").exists():
        pytest.skip(f"vitest не установлен из npm (нет сети/кэша): {proc.stderr[-300:]}")
    return cache / "node_modules"


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_vitest(tmp_path: Path, vitest_modules: Path, case: str, status: str) -> None:
    repo = _repo(tmp_path, "vitest", case)
    (repo.path / "node_modules").symlink_to(vitest_modules)
    res = await run_spec(GATE_SPECS["vitest"], repo, path=PATH)
    assert res.verdict is not None and res.verdict.status == status, res.stdout[-2000:]


async def test_phpstan_missing_tool_is_not_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "phpstan", "fail")
    res = await run_spec(GATE_SPECS["phpstan"], repo, path=PATH)
    if shutil.which("phpstan", path=PATH) is None:
        assert res.probe_code != 0 and res.verdict is None  # не запустился/ненулевой probe → ERROR(TOOL_UNAVAILABLE)
    else:  # pragma: no cover — окружение с phpstan
        assert res.verdict is not None and res.verdict.status == "FAIL"


@pytest.mark.parametrize(("case", "status"), [("pass", "PASS"), ("fail", "FAIL")])
async def test_phpstan_fixtures(tmp_path: Path, case: str, status: str) -> None:
    if shutil.which("phpstan", path=PATH) is None:
        pytest.skip("PHPStan недоступен: composer не скачал phpstan/phpstan (github.com закрыт egress), образа нет")
    repo = _repo(tmp_path, "phpstan", case)  # pragma: no cover
    res = await run_spec(GATE_SPECS["phpstan"], repo, path=PATH)  # pragma: no cover
    assert res.verdict is not None and res.verdict.status == status  # pragma: no cover
