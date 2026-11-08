"""Определение стека по файлам commit (P5-07, FR-PRJ-04): фикстуры Python / TypeScript / PHP.

Файлы читаются из объектов Git (`ls-tree`/`cat-file`) без checkout рабочего дерева.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from aicd_code_intelligence.technologies.detect import (
    CONF_LOCK,
    DetectedTechnology,
    GitCommitReader,
    MappingReader,
    detect_at_commit,
    detect_stack,
    summarize,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stacks"
ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent", "PATH": os.environ["PATH"]}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        check=True,
        capture_output=True,
        text=True,
        env=ENV,
    ).stdout.strip()


def make_repo(tmp_path: Path, fixture: str) -> tuple[Path, str]:
    repo = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, repo)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=ENV)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo, git(repo, "rev-parse", "HEAD")


def by_key(items: list[DetectedTechnology]) -> dict[tuple[str, str], DetectedTechnology]:
    return {(t.ecosystem, t.name): t for t in items}


def test_python_fixture(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, "python")
    found = by_key(detect_at_commit(repo, sha))
    assert found[("lang", "python")].version_label == "3.12.7"
    fastapi = found[("python", "fastapi")]
    assert fastapi.version_label == "0.115.0" and fastapi.confidence == CONF_LOCK
    assert fastapi.detected_from == "uv.lock" and any(e.ref() == "pyproject.toml:6" for e in fastapi.evidence)
    assert found[("python", "sqlalchemy")].version_label == "2.0.30", "имя нормализовано (SQLAlchemy[asyncio])"
    assert found[("python", "pytest")].version_label == "8.3.3", "dependency-groups"
    assert found[("python", "ruff")].version_label == "0.6.9"
    assert ("python", "starlette") not in found, "транзитивные записи lockfile не входят в стек"
    assert found[("docker", "python")].version_label == "3.12-slim"
    assert ("ci", "github-actions") in found


def test_typescript_fixture(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, "typescript")
    found = by_key(detect_at_commit(repo, sha))
    assert found[("lang", "typescript")].version_label == "5.5.4"
    assert found[("npm", "next")].version_label == "14.2.15"
    assert found[("npm", "react")].version_label == "18.3.1"
    assert found[("npm", "vitest")].version_label == "^2.0.0" and found[("npm", "vitest")].confidence < CONF_LOCK
    assert ("npm", "loose-envify") not in found and ("npm", "postcss") not in found
    assert found[("runtime", "node")].version_label == "20.18.0"
    assert found[("docker", "node")].version_label == "20-alpine"
    assert found[("docker", "nginx")].version_label == "1.27-alpine"
    assert ("docker", "build") not in found, "ссылка на стадию сборки — не образ"


def test_php_fixture(tmp_path: Path) -> None:
    repo, sha = make_repo(tmp_path, "php")
    found = by_key(detect_at_commit(repo, sha))
    assert found[("lang", "php")].version_label == "^8.2"
    laravel = found[("composer", "laravel/framework")]
    assert laravel.version_label == "11.9.2" and {e.path for e in laravel.evidence} >= {"composer.lock", "artisan"}
    assert found[("composer", "phpunit/phpunit")].version_label == "11.3.6"
    assert ("composer", "symfony/console") not in found and ("composer", "ext-json") not in found
    assert ("ci", "gitlab-ci") in found


def test_detection_reads_given_commit_not_worktree(tmp_path: Path) -> None:
    repo, first = make_repo(tmp_path, "php")
    lock = repo / "composer.lock"
    lock.write_text(lock.read_text().replace("v11.9.2", "v11.30.0"))
    git(repo, "commit", "-qam", "bump")
    second = git(repo, "rev-parse", "HEAD")
    lock.write_text("{ not json")  # незакоммиченное изменение рабочего дерева игнорируется
    assert by_key(detect_at_commit(repo, first))[("composer", "laravel/framework")].version_label == "11.9.2"
    assert by_key(detect_at_commit(repo, second))[("composer", "laravel/framework")].version_label == "11.30.0"


def test_symlinks_and_large_files_not_read(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, "python")
    (repo / "sub").mkdir()
    os.symlink("/etc/passwd", repo / "sub" / "requirements.txt")
    (repo / "big").mkdir()
    (repo / "big" / "requirements.txt").write_text("evilpkg==1.0\n" + "#" * (6 * 1024 * 1024))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "hostile")
    found = by_key(detect_at_commit(repo, git(repo, "rev-parse", "HEAD")))
    assert ("python", "evilpkg") not in found
    assert ("python", "root") not in found


def test_reader_rejects_non_sha(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, "python")
    for bad in ("HEAD", "--output=/tmp/x", "main"):
        with pytest.raises(ValueError, match="SHA"):
            GitCommitReader(repo, bad)


def test_malformed_manifests_do_not_crash() -> None:
    files = {
        "pyproject.toml": "[project\nbroken",
        "package.json": "[1, 2",
        "composer.json": '"str"',
        "pnpm-lock.yaml": ": : :",
        "Dockerfile": "FROM $BASE_IMAGE\nFROM scratch\n",
    }
    result = detect_stack(MappingReader(files))
    assert all(t.ecosystem in ("lang", "tool") for t in result)


def test_pnpm_and_yarn_locks() -> None:
    pkg = '{"dependencies": {"vue": "^3.4.0", "@scope/lib": "^1.0.0"}}'
    pnpm = "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies:\n      vue:\n        specifier: ^3.4.0\n        version: 3.4.38(typescript@5.5.4)\n"
    found = by_key(detect_stack(MappingReader({"package.json": pkg, "pnpm-lock.yaml": pnpm})))
    assert found[("npm", "vue")].version_label == "3.4.38"
    yarn = '"vue@^3.4.0":\n  version "3.4.21"\n\n"@scope/lib@^1.0.0", "@scope/lib@^1.0.1":\n  version "1.2.3"\n'
    found = by_key(detect_stack(MappingReader({"package.json": pkg, "yarn.lock": yarn})))
    assert found[("npm", "vue")].version_label == "3.4.21" and found[("npm", "@scope/lib")].version_label == "1.2.3"


def test_summarize_is_serializable() -> None:
    files = {"requirements.txt": "Django==5.0.6\n", "manage.py": "import django\n"}
    out = summarize(detect_stack(MappingReader(files)))
    django = next(x for x in out if x["name"] == "django")
    assert django["version"] == "5.0.6" and django["evidence"][0]["path"] == "requirements.txt"
