"""Настройки воркеров (`AICD_*`, поверх общих настроек платформы)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from aicd_platform.config import Settings


class WorkerSettings(Settings):
    # Адаптер исполнения задач: `real` — официальные CLI провайдеров (`real.adapter.RealCLIExecutor`),
    # `fake` (эмулятор CLI_ADAPTER_CONTRACT §13.1) запрещён в production.
    exec_adapter: Literal["fake", "real"] = "fake"
    fake_scenario: str = "success_diff"
    # Где исполняется CLI задачи: `docker` — эфемерный runner-контейнер (ADR-0010); `local` (процессы
    # хоста) оставляет агенту права пользователя воркера и запрещён в production.
    exec_runner: Literal["docker", "local"] = "docker"
    # Образ runner-контейнера с официальными CLI (infrastructure/docker/cli-runner/Dockerfile).
    cli_runner_image: str = "aicd/cli-runner:latest"
    # Docker-сеть контейнера исполнения: CLI нужен выход к API провайдера. `none` — без сети.
    cli_runner_network: str = "none"
    # uid:gid процесса в контейнере. Должен совпадать с владельцем credential store на хосте,
    # иначе официальный CLI не прочитает свои учётные данные (`id -u`:`id -g` пользователя воркера).
    cli_runner_user: str | None = None
    # Credential store официальных CLI на хосте: {"claude": "/home/u/.claude", "codex": "/home/u/.codex"}.
    # Монтируется в HOME контейнера. Платформа его не читает и не хранит (ТЗ §1): им пользуется только
    # сам CLI, который его и создал командой входа.
    cli_credential_dirs: dict[str, str] = Field(default_factory=dict)
    # Целевой Docker-режим (ADR-0010 п.3, cli-auth.md §4): именованные тома учётных данных
    # {"claude": "aicd-cred-claude", "codex": "aicd-cred-codex"}. Том — домашний каталог CLI в runner'е;
    # вход из UI и проверка статуса входа выполняются в одноразовом контейнере над тем же томом.
    # Имеет приоритет над `cli_credential_dirs` (bind каталога хоста политика контейнеров отвергает:
    # допускается единственный rw-bind — worktree задачи).
    cli_credential_volumes: dict[str, str] = Field(default_factory=dict)
    # Клиент docker для команды входа/статуса в контейнере (нужны TTY и stdin, которых у
    # DockerSandboxRunner нет по политике). По умолчанию ищется в PATH воркера.
    cli_docker_binary: str | None = None
    # Интерактивный вход CLI из UI (POST /providers/{id}/login). Пути к официальным CLI, если их нет в PATH
    # воркера: {"claude": "/opt/claude/bin/claude", "codex": "/opt/codex/bin/codex"}. Учётные данные здесь
    # не задаются и не могут быть заданы (ТЗ §1): вход выполняет сам CLI в свой credential store.
    login_binaries: dict[str, str] = Field(default_factory=dict)
    # Чем выполнять probe установки CLI (CLI_ADAPTER_CONTRACT §5.2): `auto` — реальным CLI, если он найден
    # на хосте, иначе Fake CLI; `real` — только реальным (нет CLI ⇒ `CLI_NOT_INSTALLED`); `fake` — эмулятором
    # (как исполнение при `AICD_EXEC_ADAPTER=fake`).
    probe_adapter: Literal["auto", "real", "fake"] = "auto"
    # HOME процесса входа — там официальный CLI держит свой credential store (том `aicd-cred-<provider>`).
    login_home: str | None = None
    worktree_root: Path = Path("/var/lib/aicd/worktrees")
    # Verification Engine (P3-14): gates в изолированном контейнере; `local` (процессы хоста) — только dev/test.
    verification_runner: Literal["docker", "local"] = "docker"
    verification_image: str = "python:3.12-slim-bookworm"
    # Переопределение образов gates поверх aicd_verification.images (JSON {ключ: ссылка@sha256:…}), напр. образы,
    # собранные платформой: {"migrations-python": "aicd/migrations-python:17-3.12@sha256:…", "php": "aicd/phpstan:…"}.
    verification_images: dict[str, str] = Field(default_factory=dict)
    verification_staging_root: Path = Path("/var/lib/aicd/verification")
    # Шаг `prepare` (QUALITY_GATES §4.3): зависимости проверяемого проекта для gates pytest/mypy/
    # coverage/build. Выключен, пока не заданы каталог кэша и имя docker-сети с egress: без него gate
    # без инструмента даёт `ERROR(TOOL_UNAVAILABLE)`, а не `PASS`.
    # Каталог кэша на хосте (`aicd-prep-<sha256>` внутри); владелец — пользователь воркера.
    verification_prepare_root: Path | None = None
    # Имя docker-сети для prepare — единственного шага верификации с исходящим доступом к реестрам
    # пакетов. `bridge`/`host`/`default` запрещены политикой контейнеров; gates остаются `network none`.
    verification_prepare_network: str | None = None
    # Egress-proxy с allowlist реестров (SSRF-правила `security.ssrf`); в production обязателен.
    verification_prepare_proxy_url: str | None = None
    # uid:gid процесса prepare. По умолчанию — пользователь воркера (владелец каталога кэша).
    verification_prepare_user: str | None = None
    verification_prepare_timeout_seconds: int = Field(default=900, ge=60, le=7200)
    # Корень офлайн-БД уязвимостей для gates SCA (`<корень>/osv`, `<корень>/trivy`), монтируется в gate только
    # для чтения. Наполняется утверждённым шагом обновления (runbook vulndb-update.md); не задан или БД старше
    # 7 суток — gates SCA дают `ERROR(TOOL_UNAVAILABLE)`, а не `PASS`.
    verification_vulndb_root: Path | None = None
    docker_url: str = "unix:///var/run/docker.sock"

    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_poll_seconds: float = Field(default=0.5, gt=0, le=10)
    tick_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    stale_ready_seconds: int = Field(default=30, ge=1)
    worker_threads: int = Field(default=4, ge=1, le=64)
    exec_time_limit_seconds: int = Field(default=5 * 3600, ge=60)

    # Индексация кода (P5-02/P5-06): `aicd-worker index` и индекс перед исполнением задачи.
    index_poll_seconds: float = Field(default=5.0, gt=0, le=300)
    index_workspace_batch: int = Field(default=10, ge=1, le=1000)
    index_jobs_per_workspace: int = Field(default=5, ge=1, le=100)
    index_stale_seconds: int = Field(default=1800, ge=60)  # RUNNING дольше — аренда потеряна, задание повторяется
    index_export_cache_root: Path = Path("/var/lib/aicd/index-export")
    index_export_keep: int = Field(default=8, ge=1, le=1000)
    index_prune_interval_seconds: float = Field(default=3600.0, gt=0)
    ensure_index_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)

    # Загрузка документации (P5-10, AC-10): только через security.ssrf; egress-proxy — второй слой защиты
    # (`ProxyTransport`), без него — прямое соединение с проверенным pinned IP (`PinnedTransport`).
    egress_proxy_url: str | None = None
    docs_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    docs_fetch_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
