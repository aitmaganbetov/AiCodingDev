"""«Что разрешено» в форме привязки репозитория: кэш состава установки и обход разрешённых корней.

* `repo_cache` — проекция внешнего состояния GitHub в Redis (не источник истины): переживает перезапуск
  API, стареет, гасит частые обновления.
* `scan_repository_roots` — подсказка для выбора локального каталога. Проверяем ровно то, за что она
  отвечает: не выходит за корень, не обходит бесконечно и не выдаёт лишнего. Политику доступа держит
  `resolve_in_roots` при привязке, а не этот обход.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aicd_integrations_github.api import InstallationRepo, InstallationRepos, repo_cache
from aicd_projects.api import scan_repository_roots

pytestmark = pytest.mark.anyio


class FakeRedis:
    """Минимальный Redis для строковых ключей: set(nx, ex) / get / delete. TTL не истекает сам по себе."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int | None] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiries[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0


def repo(n: int) -> InstallationRepo:
    return InstallationRepo(
        id=n,
        owner="acme",
        name=f"repo-{n}",
        private=n % 2 == 0,
        archived=False,
        default_branch="main",
        html_url=f"https://github.com/acme/repo-{n}",
    )


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


async def test_cache_round_trip_preserves_every_field() -> None:
    redis = FakeRedis()
    saved = await repo_cache.save(
        redis,
        environment="test",
        installation_id="i1",
        repos=InstallationRepos(items=(repo(1), repo(2)), total=2, truncated=False),
        now=NOW,
    )
    loaded = await repo_cache.load(redis, environment="test", installation_id="i1")
    assert loaded == saved
    assert loaded is not None and loaded.items == (repo(1), repo(2))
    assert loaded.synced_at == NOW and loaded.total == 2 and not loaded.truncated


async def test_cache_is_scoped_by_environment_and_installation() -> None:
    redis = FakeRedis()
    await repo_cache.save(
        redis, environment="test", installation_id="i1",
        repos=InstallationRepos(items=(repo(1),), total=1, truncated=False), now=NOW,
    )  # fmt: skip
    assert await repo_cache.load(redis, environment="prod", installation_id="i1") is None
    assert await repo_cache.load(redis, environment="test", installation_id="i2") is None


async def test_cache_reports_staleness_but_keeps_the_list() -> None:
    """Устаревший список показывается (лучше вчерашний, чем пустой) — UI лишь предлагает обновить."""
    redis = FakeRedis()
    cached = await repo_cache.save(
        redis, environment="test", installation_id="i1",
        repos=InstallationRepos(items=(repo(1),), total=1, truncated=False), now=NOW,
    )  # fmt: skip
    assert cached.fresh(NOW + timedelta(seconds=repo_cache.FRESH_SECONDS - 1))
    assert not cached.fresh(NOW + timedelta(seconds=repo_cache.FRESH_SECONDS + 1))
    assert (await repo_cache.load(redis, environment="test", installation_id="i1")) is not None


async def test_cache_caps_the_number_of_items_and_says_so() -> None:
    redis = FakeRedis()
    many = tuple(repo(n) for n in range(repo_cache.MAX_ITEMS + 5))
    saved = await repo_cache.save(
        redis, environment="test", installation_id="i1",
        repos=InstallationRepos(items=many, total=len(many), truncated=False), now=NOW,
    )  # fmt: skip
    assert len(saved.items) == repo_cache.MAX_ITEMS and saved.truncated
    assert saved.total == len(many), "total остаётся настоящим: показано меньше, чем доступно"


async def test_cache_truncated_flag_survives_round_trip() -> None:
    redis = FakeRedis()
    await repo_cache.save(
        redis, environment="test", installation_id="i1",
        repos=InstallationRepos(items=(repo(1),), total=500, truncated=True), now=NOW,
    )  # fmt: skip
    loaded = await repo_cache.load(redis, environment="test", installation_id="i1")
    assert loaded is not None and loaded.truncated and loaded.total == 500


async def test_damaged_cache_reads_as_absent() -> None:
    """Мусор в Redis — не ошибка API: список просто считается непрочитанным."""
    redis = FakeRedis()
    redis.values[repo_cache.key("test", "i1")] = "{not json"
    assert await repo_cache.load(redis, environment="test", installation_id="i1") is None
    redis.values[repo_cache.key("test", "i1")] = '{"synced_at": "вчера", "items": []}'
    assert await repo_cache.load(redis, environment="test", installation_id="i1") is None


async def test_drop_forgets_the_list() -> None:
    redis = FakeRedis()
    await repo_cache.save(
        redis, environment="test", installation_id="i1",
        repos=InstallationRepos(items=(repo(1),), total=1, truncated=False), now=NOW,
    )  # fmt: skip
    await repo_cache.drop(redis, environment="test", installation_id="i1")
    assert await repo_cache.load(redis, environment="test", installation_id="i1") is None


async def test_refresh_cooldown_lets_one_request_through() -> None:
    """Частые нажатия «обновить» не превращаются в поток запросов к GitHub."""
    redis = FakeRedis()
    assert await repo_cache.claim_refresh(redis, environment="test", installation_id="i1") is True
    assert await repo_cache.claim_refresh(redis, environment="test", installation_id="i1") is False
    assert await repo_cache.claim_refresh(redis, environment="test", installation_id="i2") is True


# --- обход разрешённых корней -------------------------------------------------------------------


def make_repo_dir(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def test_scan_finds_repositories_and_builds_refs_attach_accepts(tmp_path: Path) -> None:
    make_repo_dir(tmp_path / "alpha")
    make_repo_dir(tmp_path / "team" / "beta")
    (tmp_path / "plain").mkdir()
    [root] = scan_repository_roots({"src": tmp_path})
    assert root["name"] == "src" and root["path"] == str(tmp_path) and not root["truncated"]
    assert [r["ref"] for r in root["repositories"]] == ["src/alpha", "src/team/beta"]
    assert [r["name"] for r in root["repositories"]] == ["alpha", "team/beta"]
    assert root["repositories"][0]["path"] == str(tmp_path / "alpha")


def test_scan_marks_directories_that_are_already_attached(tmp_path: Path) -> None:
    """Занятый каталог остаётся в списке, но помечен: форма его покажет и не даст выбрать."""
    make_repo_dir(tmp_path / "alpha")
    make_repo_dir(tmp_path / "beta")
    [root] = scan_repository_roots({"src": tmp_path}, attached={"src/alpha"})
    assert {r["ref"]: r["attached"] for r in root["repositories"]} == {"src/alpha": True, "src/beta": False}
    assert all(r["attached"] is False for r in scan_repository_roots({"src": tmp_path})[0]["repositories"]), (
        "без списка привязанных ничего не помечено"
    )


def test_scan_treats_the_root_itself_as_a_repository(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    [root] = scan_repository_roots({"src": tmp_path})
    assert [r["ref"] for r in root["repositories"]] == ["src"], "ссылка на сам корень — просто его имя"


def test_scan_does_not_descend_into_a_repository(tmp_path: Path) -> None:
    """Вложенные репозитории (submodules) не предлагаются отдельно: привязывается корень."""
    make_repo_dir(tmp_path / "alpha")
    make_repo_dir(tmp_path / "alpha" / "vendor")
    [root] = scan_repository_roots({"src": tmp_path})
    assert [r["ref"] for r in root["repositories"]] == ["src/alpha"]


def test_scan_respects_depth_and_says_nothing_about_deeper_repositories(tmp_path: Path) -> None:
    make_repo_dir(tmp_path / "a" / "b" / "c" / "deep")
    [shallow] = scan_repository_roots({"src": tmp_path}, max_depth=2)
    assert shallow["repositories"] == []
    [deep] = scan_repository_roots({"src": tmp_path}, max_depth=4)
    assert [r["ref"] for r in deep["repositories"]] == ["src/a/b/c/deep"]


def test_scan_stops_at_the_limit_and_marks_the_result(tmp_path: Path) -> None:
    for n in range(5):
        make_repo_dir(tmp_path / f"repo{n}")
    [root] = scan_repository_roots({"src": tmp_path}, limit=3)
    assert len(root["repositories"]) == 3 and root["truncated"]


def test_scan_skips_hidden_directories(tmp_path: Path) -> None:
    make_repo_dir(tmp_path / ".cache" / "hidden")
    make_repo_dir(tmp_path / "visible")
    [root] = scan_repository_roots({"src": tmp_path})
    assert [r["ref"] for r in root["repositories"]] == ["src/visible"]


def test_scan_does_not_follow_symlinks_out_of_the_root(tmp_path: Path) -> None:
    """Symlink наружу не должен приводить к показу чужих каталогов как «разрешённых»."""
    outside = tmp_path / "outside"
    make_repo_dir(outside / "secret")
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "link").symlink_to(outside, target_is_directory=True)
    make_repo_dir(root_dir / "own")
    [root] = scan_repository_roots({"src": root_dir})
    assert [r["ref"] for r in root["repositories"]] == ["src/own"]


def test_scan_skips_unreadable_directories(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root читает любой каталог")
    make_repo_dir(tmp_path / "visible")
    closed = tmp_path / "closed"
    closed.mkdir()
    closed.chmod(0o000)
    try:
        [root] = scan_repository_roots({"src": tmp_path})
        assert [r["ref"] for r in root["repositories"]] == ["src/visible"]
    finally:
        closed.chmod(0o700)


def test_scan_of_a_missing_root_is_empty_not_an_error(tmp_path: Path) -> None:
    """Корень может быть ещё не смонтирован — форма покажет его в списке разрешённых и предложит ручной ввод."""
    [root] = scan_repository_roots({"src": tmp_path / "nowhere"})
    assert root["repositories"] == [] and not root["truncated"]


def test_scan_returns_roots_sorted_by_name(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    roots: dict[str, Any] = {"zeta": tmp_path / "one", "alpha": tmp_path / "two"}
    assert [r["name"] for r in scan_repository_roots(roots)] == ["alpha", "zeta"]
