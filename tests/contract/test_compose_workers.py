"""Сервисы воркеров в compose (OQ-46 (3), OQ-51 (d)): команды, роль БД, метрики, изоляция worker-exec."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from aicd_worker.settings import WorkerSettings
from aicd_worker.wiring import ConfigurationError, check_exec_adapter

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infrastructure" / "docker"
WORKERS = {
    "worker-outbox": "outbox",
    "worker-orchestrator": "orchestrator",
    "worker-exec": "exec",
    "worker-index": "index",
}


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((DOCKER / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    return dict(_load("compose.yaml")["services"])


def _mounts(svc: dict[str, Any]) -> list[str]:
    return [str(v) for v in svc.get("volumes", [])]


def test_worker_services_run_backend_image_commands(services: dict[str, Any]) -> None:
    for name, kind in WORKERS.items():
        svc = services[name]
        assert svc["image"] == services["api"]["image"], name
        assert svc["command"] == ["aicd-worker", kind], name
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert "ports" not in svc, f"{name}: порт не публикуется"
        assert svc["networks"] == ["backend"], name
        assert svc["depends_on"]["migrate"]["condition"] == "service_completed_successfully", name
        # HEALTHCHECK образа проверяет API :8000 — у воркера своя проверка /metrics с токеном.
        test = " ".join(svc["healthcheck"]["test"])
        assert "9464/metrics" in test and "/run/secrets/metrics_token" in test, name


def test_workers_use_worker_role_and_no_api_or_maintenance_secrets(services: dict[str, Any]) -> None:
    for name in WORKERS:
        svc = services[name]
        env = svc["environment"]
        assert env["AICD_DATABASE_URL_FILE"] == "/run/secrets/worker_database_url", name
        assert set(svc["secrets"]) == {"worker_database_url", "app_redis_url", "metrics_token"}, name
        assert not any("MAINTENANCE" in k or "SECRET_KEY" in k for k in env), name
    init = (DOCKER / "init-secrets.sh").read_text(encoding="utf-8")
    assert re.search(r"worker_database_url.*postgresql://aicd_worker:", init)
    assert _load("compose.yaml")["secrets"]["worker_database_url"] == {"file": "./secrets/worker_database_url"}


def test_fake_cli_cannot_start_in_compose_production(services: dict[str, Any]) -> None:
    for name in WORKERS:
        env = services[name]["environment"]
        assert env["AICD_ENVIRONMENT"] == "production", name
        assert "AICD_EXEC_ADAPTER" not in env and "AICD_FAKE_SCENARIO" not in env, name
    exec_env = services["worker-exec"]["environment"]
    assert exec_env["AICD_VERIFICATION_RUNNER"] == "docker"  # local (процессы хоста) в production запрещён
    settings = WorkerSettings(environment=exec_env["AICD_ENVIRONMENT"])
    assert settings.exec_adapter == "fake"  # единственный адаптер до P2-01
    with pytest.raises(ConfigurationError, match="production"):
        check_exec_adapter(settings)


def test_prometheus_discovers_worker_service_names(services: dict[str, Any]) -> None:
    prometheus = yaml.safe_load((ROOT / "infrastructure" / "monitoring" / "prometheus" / "prometheus.yml").read_text())
    job = next(j for j in prometheus["scrape_configs"] if j["job_name"] == "aicd-worker")
    (sd,) = job["dns_sd_configs"]
    assert set(sd["names"]) == set(WORKERS)
    for name in WORKERS:
        env = services[name]["environment"]
        assert env["AICD_METRICS_PORT"] == str(sd["port"]), name
        assert env["AICD_METRICS_TOKEN_FILE"] == "/run/secrets/metrics_token", name
        assert "backend" in services["prometheus"]["networks"]


def test_outbox_reads_backup_status_read_only(services: dict[str, Any]) -> None:
    svc = services["worker-outbox"]
    status_file = Path(svc["environment"]["AICD_BACKUP_STATUS_FILE"])
    (mount,) = _mounts(svc)
    source, target, mode = mount.rsplit(":", 2)
    assert source.startswith("${AICD_BACKUP_STATUS_DIR:-") and mode == "ro"
    assert status_file.parent == Path(target)


def test_filesystem_access_least_privilege(services: dict[str, Any]) -> None:
    # Репозитории: запись только у worker-exec (git worktree add), остальным — только чтение.
    for name in ("api", "worker-orchestrator", "worker-index"):
        repos = [m for m in _mounts(services[name]) if m.endswith(":/srv/repos:ro")]
        assert len(repos) == 1, name
    assert "${AICD_REPOS_DIR:-./repos}:/srv/repos" in _mounts(services["worker-exec"])
    # Worktrees и staging gates — по тому же пути, что на хосте (их монтирует dockerd в контейнеры gates).
    exec_env = services["worker-exec"]["environment"]
    for var, setting in (
        ("AICD_WORKTREES_DIR", "AICD_WORKTREE_ROOT"),
        ("AICD_VERIFICATION_STAGING_DIR", "AICD_VERIFICATION_STAGING_ROOT"),
    ):
        (bind,) = [m for m in _mounts(services["worker-exec"]) if m.startswith("${" + var)]
        source, target = bind.split("}:", 1)
        assert source + "}" == target == exec_env[setting], var
    # Сокет Docker базовый профиль не монтирует никому.
    for name, svc in services.items():
        assert not any("docker.sock" in m for m in _mounts(svc)), name


def test_only_worker_exec_reaches_docker_socket_proxy(services: dict[str, Any]) -> None:
    sandbox = _load("compose.sandbox.yaml")
    attached = {
        name
        for name, svc in sandbox["services"].items()
        if "docker-ctl" in (svc.get("networks") or {}) and name != "docker-socket-proxy"
    }
    assert attached == {"worker-exec"}
    overlay = sandbox["services"]["worker-exec"]
    assert set(overlay) == {"networks", "depends_on"}, "оверлей добавляет worker-exec только сеть docker-ctl"
    assert "volumes" not in overlay
    assert services["worker-exec"]["environment"]["AICD_DOCKER_URL"] == "http://docker-socket-proxy:2375"
    for name, svc in services.items():
        assert "docker-ctl" not in svc.get("networks", []), name
