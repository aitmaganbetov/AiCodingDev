"""Unit: шаг `prepare` — зависимости проекта для gates (QUALITY_GATES §4.3, OQ-43 (b)).

Проверяется то, от чего зависит честность вердикта: ключ кэша по содержимому и изоляция по
workspace, запрет недетерминированной установки без lock-файла, корни исходников только из
объявленной конфигурации репозитория (без исполнения кода проекта) и окружение gate-контейнера.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aicd_verification import prepare


def write_project(root: Path, *, lock: str = "version = 1\n", pyproject: str | None = None) -> Path:
    repo = root / "repo"
    (repo / "packages" / "platform").mkdir(parents=True)
    (repo / "apps" / "api").mkdir(parents=True)
    (repo / "uv.lock").write_text(lock, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        pyproject
        if pyproject is not None
        else (
            "[tool.mypy]\n"
            'mypy_path = "packages/platform:apps/api:packages/missing"\n'
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["packages/platform/aicd_platform", "apps/api/aicd_api"]\n'
        ),
        encoding="utf-8",
    )
    return repo


def test_lockfile_is_required() -> None:
    """Без lock-файла установка недетерминирована: шаг не выполняется (PREPARE_NO_LOCKFILE)."""
    assert prepare.find_lockfile(Path("/nonexistent"), prepare.PYTHON_SPEC) is None


def test_lockfile_is_found_in_the_repository_root(tmp_path: Path) -> None:
    repo = write_project(tmp_path)
    assert prepare.find_lockfile(repo, prepare.PYTHON_SPEC) == "uv.lock"


def test_cache_key_is_content_addressed_and_isolated_per_workspace(tmp_path: Path) -> None:
    repo = write_project(tmp_path)
    files = ("uv.lock", "pyproject.toml")

    def key(**overrides: object) -> str:
        args: dict[str, object] = {
            "workspace_id": "ws-1",
            "spec": prepare.PYTHON_SPEC,
            "image": "uv@sha256:" + "a" * 64,
            "repo": repo,
            "files": files,
        }
        args.update(overrides)
        return prepare.cache_key(**args)[0]  # type: ignore[arg-type]

    base = key()
    assert base.startswith(prepare.CACHE_PREFIX)
    assert key() == base  # детерминирован
    # Кэш изолирован по workspace: нет отравления между тенантами (§4.3, «Безопасность»).
    assert key(workspace_id="ws-2") != base
    # Другой образ toolchain — другой каталог зависимостей.
    assert key(image="uv@sha256:" + "b" * 64) != base
    # Изменение lock-файла инвалидирует кэш.
    (repo / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    assert key() != base


def test_cache_key_records_file_digests(tmp_path: Path) -> None:
    repo = write_project(tmp_path)
    _, digests = prepare.cache_key(
        workspace_id="ws-1",
        spec=prepare.PYTHON_SPEC,
        image="uv@sha256:" + "a" * 64,
        repo=repo,
        files=("uv.lock", "pyproject.toml", "absent.txt"),
    )
    assert set(digests) == {"uv.lock", "pyproject.toml"}  # отсутствующий файл не попадает в evidence
    assert all(len(v) == 64 for v in digests.values())


def test_source_roots_come_only_from_declared_configuration(tmp_path: Path) -> None:
    """`--no-install-project` не ставит проект: first-party пакеты видны по объявленным корням."""
    repo = write_project(tmp_path)
    assert prepare.source_roots(repo) == ("packages/platform", "apps/api")


def test_source_roots_reject_escaping_and_missing_paths(tmp_path: Path) -> None:
    repo = write_project(
        tmp_path,
        pyproject=(
            "[tool.pytest.ini_options]\n"
            'pythonpath = ["../outside", "/etc", "packages/platform", "glob/*", "packages/absent"]\n'
        ),
    )
    assert prepare.source_roots(repo) == ("packages/platform",)


def test_source_roots_survive_a_broken_pyproject(tmp_path: Path) -> None:
    repo = write_project(tmp_path, pyproject="[tool.mypy\n")
    assert prepare.source_roots(repo) == ()


def test_gate_env_puts_the_dependency_venv_first(tmp_path: Path) -> None:
    env = prepare.gate_env("python", "/workspace", ("packages/platform", "apps/api"))
    assert env["PATH"].startswith(f"{prepare.DEPS_DIR}/venv/bin:")
    assert env["VIRTUAL_ENV"] == f"{prepare.DEPS_DIR}/venv"
    assert env["PYTHONPATH"] == "/workspace/packages/platform:/workspace/apps/api"
    assert env["PYTHONNOUSERSITE"] == "1"


def test_gate_env_is_empty_for_an_unregistered_toolchain() -> None:
    assert prepare.gate_env("node", "/workspace", ()) == {}
    assert prepare.spec_for("node") is None


def test_python_prepare_never_runs_project_or_package_scripts() -> None:
    """Install-скрипты и build-backend проекта в prepare не исполняются (§4.3)."""
    assert "--no-install-project" in prepare.PYTHON_SPEC.argv
    assert "--frozen" in prepare.PYTHON_SPEC.argv
    # Загрузка интерпретатора из сети запрещена: python берётся только из образа.
    assert prepare.PYTHON_SPEC.env["UV_PYTHON_DOWNLOADS"] == "never"
    # Инструменты сборки платформы закреплены по версии (их нет в lock-файле проекта).
    assert all(any("==" in a for a in argv) for argv in prepare.PYTHON_SPEC.extra_argv)


def test_source_roots_reach_child_processes_through_a_pth_file() -> None:
    """Дочерние процессы тестов проекта запускаются с очищенным окружением и теряют PYTHONPATH:
    корни исходников дополнительно кладутся в site-packages venv файлом `.pth`."""
    argv = prepare.src_pth_argv("/workspace", ("src", "packages/platform"))
    assert argv[0] == f"{prepare.DEPS_DIR}/venv/bin/python3"
    assert argv[1] == "-c"
    assert argv[3:] == ("/workspace/src", "/workspace/packages/platform")
    # Корни передаются только аргументами argv: в текст скрипта данные репозитория не подставляются.
    assert "/workspace" not in argv[2]
    # В evidence команда показывается по sha256 скрипта, а не его текстом.
    display = prepare.src_pth_display("/workspace", ("src",))
    assert display[2] == f"<aicd-prepare-src-pth sha256:{prepare.SRC_PTH_DRIVER_SHA256}>"
    assert display[3:] == ("/workspace/src",)


def test_pth_driver_writes_the_declared_roots(tmp_path: Path) -> None:
    """Скрипт `.pth` исполняется как есть: проверяется его поведение, а не только argv."""
    import subprocess
    import sys

    site = tmp_path / "site"
    site.mkdir()
    script = prepare.SRC_PTH_DRIVER.replace('sysconfig.get_paths()["purelib"]', repr(str(site)))
    subprocess.run([sys.executable, "-c", script, "/workspace/src", ""], check=True, capture_output=True)
    assert (site / prepare.SRC_PTH_NAME).read_text(encoding="utf-8") == "/workspace/src\n"


def test_project_without_manifest_gets_platform_tools_only(tmp_path: Path) -> None:
    """Нет манифеста — нет зависимостей проекта: пустое окружение + инструменты платформы с фиксированными версиями."""
    (tmp_path / "tests").mkdir()
    assert not prepare.declares_dependencies(tmp_path, prepare.PYTHON_SPEC)
    bare = prepare.bare_spec(prepare.PYTHON_SPEC)
    assert bare is not None and bare.argv[:2] == ("uv", "venv")
    installed = [a for argv in bare.extra_argv for a in argv if "==" in a]
    assert any(a.startswith("pytest==") for a in installed) and any(a.startswith("pytest-cov==") for a in installed)
    assert all("==" in a for argv in bare.extra_argv for a in argv[6:])  # только закреплённые версии


@pytest.mark.parametrize("name", ["pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py"])
def test_declared_dependencies_still_require_a_lockfile(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("", encoding="utf-8")
    assert prepare.declares_dependencies(tmp_path, prepare.PYTHON_SPEC)
