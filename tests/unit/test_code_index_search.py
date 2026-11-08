"""P5-04: ripgrep с безопасным argv по экспорту снимка; тест command injection через запрос.

Запрос передаётся одним элементом argv после `--` без shell: `--pre=sh`, `-e`, `$(…)`, обратные кавычки,
`;`, `|` ищутся как литеральный текст и не исполняются; перевод строки/NUL/управляющие символы отклоняются.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from aicd_code_intelligence.index.gitsource import GitSourceError
from aicd_code_intelligence.index.search import (
    SearchLimits,
    SearchQueryError,
    build_rg_argv,
    export_dir_name,
    materialize_export,
    prune_export_cache,
    ripgrep_search,
    validate_query,
)

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": os.environ["PATH"],
}

INJECTIONS = [
    "--pre=sh",
    "--pre=/bin/sh",
    "-e",
    "-e touch",
    "--files",
    "$(touch PWNED)",
    "`touch PWNED`",
    "; touch PWNED",
    "| touch PWNED",
    "&& rm -rf /",
    "-",
    "--",
    "'quoted' \"double\"",
    "*.py",
]

LINES = "\n".join(f"line {i}: {q}" for i, q in enumerate(INJECTIONS)) + "\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        check=True,
        env=GIT_ENV,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def exported(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=GIT_ENV)
    (repo / "src").mkdir()
    (repo / "src" / "payloads.txt").write_text(LINES)
    (repo / "src" / "code.py").write_text("def getUserName():\n    return 'Alice'\n")
    (repo / ".hidden.cfg").write_text("needle in hidden\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c")
    sha = git(repo, "rev-parse", "HEAD")
    files = [(line.split("\t")[1], line.split()[2]) for line in git(repo, "ls-tree", "-r", sha).splitlines()]
    return materialize_export(repo, sha, files, tmp_path / "cache")


@pytest.mark.parametrize("query", INJECTIONS)
def test_injection_is_literal(exported: Path, query: str, tmp_path: Path) -> None:
    before = sorted(p.name for p in exported.rglob("*"))
    result = ripgrep_search(exported, query)
    expected = [i for i, q in enumerate(INJECTIONS) if query in q]
    assert sorted(int(m.text.split(":")[0].removeprefix("line ")) for m in result.matches) == expected
    assert all(m.path == "src/payloads.txt" for m in result.matches)
    assert sorted(p.name for p in exported.rglob("*")) == before  # ничего не создано
    assert not (tmp_path / "PWNED").exists() and not Path("PWNED").exists()


@pytest.mark.parametrize("query", ["a\nb", "a\x00b", "a\rb", "\x1b[31m", "", "x" * 1001])
def test_control_characters_rejected(exported: Path, query: str) -> None:
    with pytest.raises(SearchQueryError):
        ripgrep_search(exported, query)


def test_argv_shape_is_fixed() -> None:
    argv = build_rg_argv("/usr/bin/rg", "--pre=sh")
    assert argv[-3:] == ["--", "--pre=sh", "."]
    assert "--no-config" in argv and "--fixed-strings" in argv and "--no-pre" in argv
    assert "--no-search-zip" in argv and "--engine=default" in argv
    assert not any(a.startswith("--pre=") for a in argv[:-3])
    regex = build_rg_argv("/usr/bin/rg", r"get\w+Name", regex=True, case_sensitive=False)
    assert "--fixed-strings" not in regex and "--ignore-case" in regex


def test_regex_mode_and_errors(exported: Path) -> None:
    result = ripgrep_search(exported, r"get[A-Z]\w+Name", regex=True)
    assert [(m.path, m.line) for m in result.matches] == [("src/code.py", 1)]
    assert result.matches[0].submatches == ((4, 15),)
    with pytest.raises(SearchQueryError):
        ripgrep_search(exported, "(unclosed", regex=True)
    # fixed-strings по умолчанию: метасимволы regex — литералы
    assert ripgrep_search(exported, "*.py").matches


def test_hidden_files_and_case(exported: Path) -> None:
    assert [m.path for m in ripgrep_search(exported, "needle").matches] == [".hidden.cfg"]
    assert not ripgrep_search(exported, "alice").matches
    assert ripgrep_search(exported, "alice", case_sensitive=False).matches


def test_output_caps(exported: Path) -> None:
    result = ripgrep_search(exported, "line", limits=SearchLimits(max_matches=3))
    assert len(result.matches) == 3 and result.truncated


def test_export_cache_reuse_and_prune(exported: Path) -> None:
    cache = exported.parent
    assert exported.name.endswith("-v1")
    assert (exported / ".aicd-export-complete").is_file()
    old = cache / export_dir_name("c" * 40)
    old.mkdir()
    stale = time.time() - 30 * 24 * 3600
    os.utime(old, (stale, stale))
    tmp = cache / ".tmp-abandoned"
    tmp.mkdir()
    os.utime(tmp, (stale, stale))
    removed = prune_export_cache(cache)
    assert sorted(removed) == sorted([old.name, tmp.name])
    assert exported.is_dir()


def test_export_rejects_unsafe_paths(tmp_path: Path) -> None:
    with pytest.raises(GitSourceError):
        materialize_export(tmp_path, "a" * 40, [("../evil", "b" * 40)], tmp_path / "cache")
    with pytest.raises(GitSourceError):
        materialize_export(tmp_path, "a" * 40, [("/etc/passwd", "b" * 40)], tmp_path / "cache")


def test_validate_query_accepts_unicode() -> None:
    assert validate_query("пользователь") == "пользователь"
