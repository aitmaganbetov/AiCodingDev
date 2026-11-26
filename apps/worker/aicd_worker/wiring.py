"""Сборка оркестратора для воркеров: адаптер исполнения по `AICD_EXEC_ADAPTER`, порты approvals/verification."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters import limits as cli_limits
from aicd_cli_adapters import login
from aicd_cli_adapters import probe as cli_probe
from aicd_cli_adapters.container_launch import CredentialLauncher, LauncherError, credential_launcher
from aicd_cli_adapters.fake.adapter import FakeCLIExecutor
from aicd_cli_adapters.gateway import ExecutionGateway, worktree_resolver
from aicd_cli_adapters.models import ProbeResult
from aicd_cli_adapters.profiles import ProfileError
from aicd_cli_adapters.real.adapter import RealCLIExecutor, cli_container_layout
from aicd_cli_adapters.subprocess_adapter import SubprocessCLIAdapter
from aicd_orchestration.approvals import Approvals
from aicd_orchestration.cli_executor import AdapterFactory, CliTaskExecutor
from aicd_orchestration.engine import ApprovalsPort, Orchestrator, VerifierPort
from aicd_orchestration.recovery import Reconciler
from aicd_orchestration.worktrees import WorktreeManager
from aicd_platform.artifacts import ArtifactStore, build_artifact_store
from aicd_platform.clock import utcnow
from aicd_platform.ids import uuid7
from aicd_security.docker_runner import DockerSandboxRunner
from aicd_security.local_runner import LocalProcessRunner
from aicd_security.sandbox import SandboxRunner
from aicd_verification.engine import VerificationEngine
from aicd_verification.images import default_gate_images
from aicd_verification.security_gates import SECURITY_GATE_IMAGES
from aicd_worker.runtime import ProviderLoginPort
from aicd_worker.settings import WorkerSettings

#: Версия адаптера probe в `ProbeResult.adapter_version`: это probe реального CLI, а не адаптер исполнения
#: (реальных адаптеров исполнения ещё нет, ADR-0003).
PROBE_ADAPTER_VERSION = "probe-1.0.0"


class ConfigurationError(RuntimeError):
    pass


def check_exec_adapter(settings: WorkerSettings) -> None:
    """Fake CLI — только для разработки/CI: в production воркер исполнения не стартует (ADR-0003).

    Исполнение процессами хоста (`exec_runner=local`) в production тоже запрещено: официальный CLI
    получил бы права пользователя воркера вместо изоляции runner-контейнера (ADR-0010).
    """
    if settings.environment != "production":
        return
    if settings.exec_adapter == "fake":
        raise ConfigurationError("AICD_EXEC_ADAPTER=fake запрещён в production: нужен real (официальные CLI)")
    if settings.exec_runner == "local":
        raise ConfigurationError("AICD_EXEC_RUNNER=local запрещён в production: исполнение только в контейнере")


def fake_adapter_factory(settings: WorkerSettings) -> AdapterFactory:
    def factory(
        *,
        provider: str,
        installation_id: uuid.UUID,
        gateway: ExecutionGateway,
        worktree_root: Path,
        model: str | None = None,
    ) -> SubprocessCLIAdapter:
        del installation_id, model  # у эмулятора моделей нет
        return FakeCLIExecutor(
            provider="claude" if provider == "claude" else "codex",
            scenario=settings.fake_scenario,
            runner=LocalProcessRunner(),
            artifact_sink=gateway.store_artifact,
            worktree_path=worktree_resolver(worktree_root),
            env_source={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            agent_io=True,  # `.aicd/`, `output/` агента — в отдельный каталог вывода исполнения
        )

    return factory


def exec_env_source(settings: WorkerSettings, *, in_container: bool, home: str = "/home/agent") -> dict[str, str]:
    """Окружение процесса CLI. Ключей провайдеров тут нет и быть не может (`profiles.FORBIDDEN_ENV`)."""
    env = {"LANG": "C.UTF-8"}
    if in_container:
        return {**env, "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home}
    env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    host_home = settings.login_home or os.environ.get("HOME")
    if host_home:
        env["HOME"] = host_home
    return env


def build_exec_runner(settings: WorkerSettings) -> SandboxRunner:
    """Раннер исполнения задач. Корни bind-монтирований: worktree, каталоги промпта/вывода (tmp) и
    credential store CLI — других линтер контейнеров не пропустит."""
    if settings.exec_runner == "local":
        return LocalProcessRunner()
    roots = [str(settings.worktree_root), tempfile.gettempdir(), *settings.cli_credential_dirs.values()]
    return DockerSandboxRunner(
        settings.docker_url,
        allowed_bind_roots=tuple(roots),
        require_image_digest=settings.environment == "production",
    )


def real_adapter_factory(settings: WorkerSettings) -> AdapterFactory:
    """Официальные CLI провайдеров в runner-контейнере (ADR-0010, CLI_ADAPTER_CONTRACT §8)."""
    runner = build_exec_runner(settings)
    in_container = settings.exec_runner != "local"

    def factory(
        *,
        provider: str,
        installation_id: uuid.UUID,
        gateway: ExecutionGateway,
        worktree_root: Path,
        model: str | None = None,
    ) -> SubprocessCLIAdapter:
        del installation_id
        real = _real_provider(provider)
        if real is None:
            raise ConfigurationError(f"нет адаптера официального CLI для провайдера {provider}")
        binary = resolve_cli_binary(settings, provider)
        if binary is None:
            raise ConfigurationError(f"официальный CLI {provider} не найден: задайте путь в AICD_LOGIN_BINARIES")
        container = None
        if in_container:
            credential_volume = settings.cli_credential_volumes.get(provider)
            credential_dir = None if credential_volume else settings.cli_credential_dirs.get(provider)
            container = cli_container_layout(
                real,
                settings.cli_runner_image,
                credential_dir=Path(credential_dir) if credential_dir else None,
                credential_volume=credential_volume,
                network=settings.cli_runner_network,
                user=settings.cli_runner_user,
            )
        return RealCLIExecutor(
            provider=real,
            binary_path=binary,
            runner=runner,
            artifact_sink=gateway.store_artifact,
            worktree_path=worktree_resolver(worktree_root),
            env_source=exec_env_source(settings, in_container=in_container),
            agent_io=True,
            # Без слоя контейнера адаптер подставил бы в ContainerSpec образ-заглушку `local`, и Docker
            # отвечал бы 404 «no such image» ещё до старта CLI (исполнение — CRASHED/PROCESS_FAILED).
            container=container,
            # Модель из настроек провайдера в workspace (None — модель CLI по умолчанию).
            model=model,
        )

    return factory


def adapter_factory(settings: WorkerSettings) -> AdapterFactory:
    """Фабрика адаптеров исполнения по `AICD_EXEC_ADAPTER`."""
    return real_adapter_factory(settings) if settings.exec_adapter == "real" else fake_adapter_factory(settings)


def resolve_cli_binary(settings: WorkerSettings, provider: str) -> str | None:
    """Путь к официальному CLI провайдера: явное указание оператором, иначе поиск в PATH воркера.

    В production поиск по `PATH` запрещён (CLI_ADAPTER_CONTRACT §5.2 п. 1, защита от подмены бинаря):
    путь задаёт оператор в `AICD_LOGIN_BINARIES`, иначе установка считается отсутствующей.
    """
    explicit = settings.login_binaries.get(provider)
    if explicit:
        return explicit if Path(explicit).is_file() else None
    if settings.environment == "production":
        return None
    return shutil.which(provider, path=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))


def credential_launcher_for(
    settings: WorkerSettings, provider: str, *, purpose: str, interactive: bool
) -> CredentialLauncher | None:
    """Одноразовый контейнер с томом учётных данных провайдера (Docker-режим, cli-auth.md §4).

    None — том для провайдера не задан: вход и статус выполняются CLI хоста, как раньше.
    """
    volume = settings.cli_credential_volumes.get(provider)
    if not volume:
        return None
    docker = settings.cli_docker_binary or shutil.which("docker", path=os.environ.get("PATH", "/usr/bin:/bin"))
    if not docker:
        raise ConfigurationError("клиент docker не найден: задайте AICD_CLI_DOCKER_BINARY")
    try:
        return credential_launcher(
            docker=docker,
            image=settings.cli_runner_image,
            volume=volume,
            network=settings.cli_runner_network,
            user=settings.cli_runner_user or f"{os.getuid()}:{os.getgid()}",
            purpose=purpose,
            interactive=interactive,
        )
    except LauncherError as exc:
        raise ConfigurationError(str(exc)) from exc


async def _cleanup_container(launcher: CredentialLauncher) -> None:
    """Снять контейнер, переживший клиента `docker run` (отмена/таймаут). Ошибки не важны: `--rm`."""
    with contextlib.suppress(OSError, TimeoutError):
        proc = await asyncio.create_subprocess_exec(
            *launcher.cleanup_argv(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)


def build_provider_login(settings: WorkerSettings) -> ProviderLoginPort:
    """Интерактивный вход CLI на хосте exec: официальная команда входа в PTY (`login.run_login`).

    В stdin CLI уходит только код авторизации, введённый оператором в UI (`code_input`): он нужен тем
    CLI, которые завершают вход вводом кода из браузера. Платформа его не сохраняет (`login_input`).

    Платформа не передаёт CLI учётных данных и не читает его credential store: наружу идут только строки
    вывода, прошедшие `login.scrub()`, и публичная пара «ссылка + код» (ADR-0003, cli-auth.md §0).
    """

    async def run(
        provider: str,
        cancelled: Callable[[], Awaitable[bool]],
        code_input: Callable[[], Awaitable[str | None]] | None = None,
    ) -> AsyncIterator[login.LoginEvent]:
        if provider not in login.LOGIN_PROFILES:
            yield login.LoginFinished(state="FAILED", error_category="PROCESS_FAILED")
            return
        try:
            launcher = credential_launcher_for(settings, provider, purpose="login", interactive=True)
        except ConfigurationError as exc:
            yield login.LoginLine(text=str(exc))
            yield login.LoginFinished(state="FAILED", error_category="PROCESS_FAILED")
            return
        # Docker-режим: вход идёт в контейнере над томом `aicd-cred-<provider>` — там же его найдёт
        # CLI исполнения; путь CLI — внутри runner-образа. Иначе — CLI хоста.
        binary = f"/usr/local/bin/{provider}" if launcher is not None else resolve_cli_binary(settings, provider)
        if binary is None:
            yield login.LoginFinished(state="FAILED", error_category="CLI_NOT_INSTALLED")
            return
        try:
            stream = login.run_login(
                provider=provider,
                binary_path=binary,
                env_source=login_env_source(settings),
                cancelled=cancelled,
                code_input=code_input,
                launcher=launcher.argv if launcher is not None else (),
            )
            async for event in stream:
                yield event
        except ProfileError as exc:
            # Неподтверждённые флаги входа (codex, P2-01) или запрещённый вход по API-ключу.
            yield login.LoginLine(text=str(exc))
            yield login.LoginFinished(state="FAILED", error_category="PROCESS_FAILED")
        finally:
            if launcher is not None:
                await _cleanup_container(launcher)

    return run


def login_env_source(settings: WorkerSettings) -> dict[str, str]:
    """Окружение для команды входа: HOME хоста исполнения (там credential store CLI) и PATH."""
    source = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    home = settings.login_home or os.environ.get("HOME")
    if home:
        source["HOME"] = home
    return source


def build_provider_prober(
    settings: WorkerSettings, engine: AsyncEngine, store: ArtifactStore
) -> Callable[[str], Awaitable[ProbeResult]]:
    """Probe установки CLI на хосте exec (`AICD_PROBE_ADAPTER`).

    По умолчанию (`auto`) опрашивается **реальный** CLI, если он есть на хосте: версия, справка,
    матрица совместимости и статус входа (CLI_ADAPTER_CONTRACT §5.2). Иначе — Fake CLI, которым идёт
    исполнение: без него на стенде без установленных CLI не было бы ни одной установки в реестре.
    """
    factory = fake_adapter_factory(settings)

    async def probe(provider: str) -> ProbeResult:
        installation_id = uuid7()  # новая установка; существующая находится по (provider, host, binary) в record_probe
        real = _real_provider(provider)
        if real is not None and settings.probe_adapter != "fake":
            binary = resolve_cli_binary(settings, provider)
            if binary is not None or settings.probe_adapter == "real":
                launcher = credential_launcher_for(settings, provider, purpose="status", interactive=False)
                return await cli_probe.run_probe(
                    provider=real,
                    binary_path=binary,
                    installation_id=installation_id,
                    adapter_version=PROBE_ADAPTER_VERSION,
                    env_source=login_env_source(settings),
                    auth_launcher=launcher.argv if launcher is not None else (),
                    auth_binary=f"/usr/local/bin/{provider}" if launcher is not None else None,
                )
        adapter = factory(
            provider=provider,
            installation_id=installation_id,
            gateway=ExecutionGateway(engine=engine, store=store),
            worktree_root=settings.worktree_root,
        )
        return await adapter.probe(installation_id=installation_id)

    return probe


def build_limits_reader(settings: WorkerSettings) -> Callable[[str], Awaitable[dict[str, Any] | None]]:
    """Чтение лимитов подписки у самого CLI по запросу оператора (`aicd_cli_adapters.limits.read_limits`).

    Идёт тем же путём, что проверка статуса входа: в Docker-режиме — одноразовый контейнер над томом
    учётных данных, иначе CLI хоста. None — CLI этого провайдера лимитов не сообщает либо не ответил.
    """

    async def read(provider: str) -> dict[str, Any] | None:
        if provider not in cli_limits.READABLE:
            return None
        launcher = credential_launcher_for(settings, provider, purpose="limits", interactive=False)
        binary = f"/usr/local/bin/{provider}" if launcher is not None else resolve_cli_binary(settings, provider)
        if binary is None:
            return None
        # Codex отвечает о лимитах по stdio (`app-server`): контейнеру нужен открытый stdin, но не TTY.
        argv = launcher.argv if launcher is not None else ()
        if argv and provider == "codex":
            argv = (*argv[:2], "--interactive", *argv[2:])
        try:
            return await cli_limits.read_limits(
                provider=provider,
                binary_path=binary,
                env_source=login_env_source(settings),
                launcher=argv,
                now=utcnow(),
            )
        finally:
            if launcher is not None:
                await _cleanup_container(launcher)

    return read


def _real_provider(provider: str) -> Literal["codex", "claude"] | None:
    """Провайдер, у которого есть профиль probe реального CLI; иначе probe делает Fake CLI."""
    return "codex" if provider == "codex" else "claude" if provider == "claude" else None


def check_verification_runner(settings: WorkerSettings) -> None:
    if settings.verification_runner == "local" and settings.environment == "production":
        raise ConfigurationError("AICD_VERIFICATION_RUNNER=local запрещён в production: gates только в контейнере")
    prepare_on = settings.verification_prepare_root is not None and settings.verification_prepare_network is not None
    if prepare_on and settings.environment == "production" and not settings.verification_prepare_proxy_url:
        # Шаг prepare — единственный с egress; в production выход в сеть допустим только через
        # proxy с allowlist реестров пакетов (QUALITY_GATES §4.3, SSRF-правила security.ssrf).
        raise ConfigurationError(
            "AICD_VERIFICATION_PREPARE_PROXY_URL обязателен в production при включённом шаге prepare"
        )
    if settings.verification_prepare_network in ("host", "bridge", "default"):
        raise ConfigurationError(
            f"AICD_VERIFICATION_PREPARE_NETWORK={settings.verification_prepare_network} запрещён политикой контейнеров"
        )


def build_sandbox_runner(settings: WorkerSettings) -> SandboxRunner:
    if settings.verification_runner == "local":
        return LocalProcessRunner()
    # Bind-монтирования gates: worktree задачи (ro), staging входа SCOPE и кэш зависимостей шага
    # prepare (rw только в контейнере prepare) — других корней линтер не пропустит.
    optional = (settings.verification_prepare_root, settings.verification_vulndb_root)  # vulndb — только ro
    roots = (
        str(settings.worktree_root),
        str(settings.verification_staging_root),
        *(str(root) for root in optional if root is not None),
    )
    return DockerSandboxRunner(
        settings.docker_url,
        allowed_bind_roots=roots,
        require_image_digest=settings.environment == "production",  # профиль server: образы только по digest
    )


def build_executor(
    settings: WorkerSettings, engine: AsyncEngine, store: ArtifactStore, *, worker_host_id: uuid.UUID | None = None
) -> CliTaskExecutor:
    return CliTaskExecutor(
        engine=engine,
        gateway=ExecutionGateway(engine=engine, store=store),
        worktrees=WorktreeManager(settings.worktree_root),
        repository_roots=settings.repository_roots,
        adapter_factory=adapter_factory(settings),
        worker_host_id=worker_host_id,
        index_export_cache_root=settings.index_export_cache_root,
        ensure_index_timeout_seconds=settings.ensure_index_timeout_seconds,
    )


def default_ports(
    settings: WorkerSettings, engine: AsyncEngine, store: ArtifactStore
) -> tuple[ApprovalsPort, VerifierPort]:
    check_verification_runner(settings)
    approvals = Approvals()
    # Образы gates: качество/миграции (images.py) + безопасность/цепочка поставок (security_gates), все — по digest;
    # в production ссылка без digest — ошибка конфигурации (ImagePolicyError), воркер не стартует.
    images = default_gate_images(
        {**SECURITY_GATE_IMAGES, **settings.verification_images}, require_digest=settings.environment == "production"
    )
    verifier = VerificationEngine(
        engine=engine,
        store=store,
        runner=build_sandbox_runner(settings),
        image=settings.verification_image,
        images=images,
        staging_root=settings.verification_staging_root,
        prepare_root=settings.verification_prepare_root,
        prepare_network=settings.verification_prepare_network,
        prepare_proxy_url=settings.verification_prepare_proxy_url,
        prepare_user=settings.verification_prepare_user,
        prepare_timeout_seconds=settings.verification_prepare_timeout_seconds,
        vulndb_root=settings.verification_vulndb_root,
        local_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        approvals=approvals,  # DESTRUCTIVE_MIGRATION (P6-05): PASS только после APPROVED
    )
    return approvals, verifier


def build_orchestrator(
    settings: WorkerSettings,
    engine: AsyncEngine,
    *,
    approvals: ApprovalsPort | None = None,
    verifier: VerifierPort | None = None,
    store: ArtifactStore | None = None,
    worker_host_id: uuid.UUID | None = None,
) -> Orchestrator:
    store = store or build_artifact_store(settings)
    if approvals is None or verifier is None:
        approvals, verifier = default_ports(settings, engine, store)
    return Orchestrator(
        engine=engine,
        approvals=approvals,
        verifier=verifier,
        executor=build_executor(settings, engine, store, worker_host_id=worker_host_id),
        artifacts=store,
    )


def build_reconciler(
    settings: WorkerSettings, engine: AsyncEngine, worker_host_id: uuid.UUID, store: ArtifactStore | None = None
) -> Reconciler:
    """Recovery reconciler хоста исполнения: процессы CLI/контейнеры и worktrees принадлежат этому хосту.

    Probe внешних действий (GitHub и т. п.) появятся с интеграциями Phase 5; до тех пор прерванные действия
    остаются EXECUTING и попадают в отчёт (без слепого повтора, AC-09).
    """
    runners: tuple[SandboxRunner, ...] = (LocalProcessRunner(),)
    if settings.verification_runner == "docker":
        runners = (*runners, build_sandbox_runner(settings))
    return Reconciler(
        engine=engine,
        artifacts=store or build_artifact_store(settings),
        worktrees=WorktreeManager(settings.worktree_root),
        repository_roots=settings.repository_roots,
        worker_host_id=worker_host_id,
        runners=runners,
    )
