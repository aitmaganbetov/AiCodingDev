"""Набор `real-cli-smoke@1` (CLI_ADAPTER_CONTRACT §13.2, P2-13): официальные CLI на реальной установке.

Запускается только явно — на хосте исполнения с заранее выполненным входом по подписке::

    source ~/aicd-devstack/env.sh
    AICD_REAL_CLI=1 AICD_REAL_CLI_EVIDENCE=reports/real-cli/smoke.json uv run pytest tests/real_cli -m real_cli

Проверяется ровно тот путь, которым идёт исполнение задач: фабрика адаптера воркера (`wiring.real_adapter_factory`),
runner-контейнер с томом учётных данных, промпт роли из `agent_io.assemble_prompt`. Состав — по контракту:
probe и статус входа; минимальная задача в одноразовом репозитории (создать файл, diff платформы); отмена во
время работы; read-only профиль (запись должна провалиться); формат машинного вывода и поля usage; выбор модели.

Набор **не** делает массовых запусков и не проверяет лимиты подписки: по одному короткому заданию на проверку.
Его прохождение (с сохранённым evidence) — условие строки `SUPPORTED` в `packages/contracts/cli-compatibility.yaml`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aicd_cli_adapters import compat
from aicd_cli_adapters import probe as cli_probe
from aicd_cli_adapters.models import ExecutionRequest, ExecutionResult
from aicd_orchestration import agent_io
from aicd_orchestration.agents.result import parse_agent_result
from aicd_orchestration.worktrees import WorktreeManager
from aicd_platform.ids import uuid7
from tests.conftest import init_git_repo
from tests.support.cli import MemorySink

SUITE = "real-cli-smoke@1"
PROVIDERS = ("codex", "claude")
#: Дешёвая модель для проверки `--model` у Claude Code (псевдоним из справки CLI). Для Codex общего псевдонима
#: нет: имя берётся из `AICD_SMOKE_CODEX_MODEL`, иначе — из заголовка, который печатает сам `codex exec`.
CLAUDE_SMOKE_MODEL = os.environ.get("AICD_SMOKE_CLAUDE_MODEL", "sonnet")
TASK_TIMEOUT = 600

pytestmark = [
    pytest.mark.real_cli,
    pytest.mark.skipif(os.environ.get("AICD_REAL_CLI") != "1", reason="real-CLI набор: задайте AICD_REAL_CLI=1"),
]


# --- evidence ------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def evidence() -> Iterator[dict[str, Any]]:
    """Результаты проверок по провайдерам; записываются в файл `AICD_REAL_CLI_EVIDENCE` в конце прогона."""
    data: dict[str, Any] = {
        "suite": SUITE,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "providers": {p: {"checks": {}} for p in PROVIDERS},
    }
    yield data
    data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    target = os.environ.get("AICD_REAL_CLI_EVIDENCE")
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record(evidence: dict[str, Any], provider: str, check: str, **facts: Any) -> None:
    evidence["providers"][provider]["checks"][check] = {"status": "passed", **facts}


# --- обвязка: тот же путь, что у worker-exec -------------------------------------------------------------


@pytest.fixture(scope="session")
def settings() -> Any:
    from aicd_worker.settings import WorkerSettings

    loaded = WorkerSettings()
    if loaded.exec_adapter != "real":
        pytest.skip("AICD_EXEC_ADAPTER не real: набор проверяет адаптеры официальных CLI")
    return loaded


@dataclass
class Bench:
    """Одноразовый репозиторий и worktree задачи — как у исполнителя (`WorktreeManager`)."""

    root: Path
    repo: Path = field(init=False)
    manager: WorktreeManager = field(init=False)
    base: str = field(init=False)

    def __post_init__(self) -> None:
        self.repo = self.root / "repo"
        init_git_repo(self.repo)
        self.base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.manager = WorktreeManager(self.root / "wt")

    def worktree(self) -> tuple[uuid.UUID, Path]:
        wid = uuid7()
        info = self.manager.create(
            self.repo, worktree_id=wid, run_id=uuid7(), task_id=uuid7(), base_commit_sha=self.base
        )
        return wid, info.path


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path)


@dataclass
class Outcome:
    result: ExecutionResult
    events: list[Any]
    raw_log: bytes
    worktree: Path
    agent_result: Any | None
    seconds: float


def _request(
    provider: str, worktree_id: uuid.UUID, base: str, *, instructions: str, read_only: bool
) -> ExecutionRequest:
    return ExecutionRequest.model_validate(
        {
            "contract_version": "1.0",
            "execution_id": str(uuid7()),
            "task_id": str(uuid7()),
            "provider": provider,
            "agent_role": "reviewer" if read_only else "developer",
            "workspace": {"worktree_id": str(worktree_id), "base_commit": base},
            "input": {"objective": instructions, "context_refs": [], "acceptance_criteria": []},
            "limits": {"timeout_seconds": TASK_TIMEOUT, "max_attempts": 1},
            "permissions": {
                "network": "restricted",
                "write_scope": "none" if read_only else "worktree",
                "production_access": False,
            },
            "run_id": str(uuid7()),
            "task_attempt_id": str(uuid7()),
            "workspace_id": str(uuid7()),
            "command_profile": f"{provider}/agent_task@1",
        }
    )


def _prompt(role: str, instructions: str, base: str) -> str:
    task = {
        "task_key": "smoke",
        "agent_role": role,
        "title": "Smoke-проверка CLI",
        "instructions": instructions,
        "acceptance_criteria": [],
        "complexity": 1,
        "risk_level": "low",
    }
    run = {"mode": "DEVELOP", "objective": instructions, "base_commit_sha": base}
    return agent_io.assemble_prompt(task, run, []).text


async def _execute(
    settings: Any,
    bench: Bench,
    provider: str,
    instructions: str,
    *,
    read_only: bool = False,
    model: str | None = None,
    cancel_after: float | None = None,
) -> Outcome:
    from aicd_worker import wiring

    sink = MemorySink()
    worktree_id, path = bench.worktree()
    adapter = wiring.real_adapter_factory(settings)(
        provider=provider,
        installation_id=uuid7(),
        gateway=SimpleNamespace(store_artifact=sink),  # type: ignore[arg-type]
        worktree_root=bench.manager.path_for(worktree_id).parent,
        model=model,
    )
    request = _request(provider, worktree_id, bench.base, instructions=instructions, read_only=read_only)
    adapter.stage_prompt(request.execution_id, _prompt(request.agent_role, instructions, bench.base))
    started = time.monotonic()
    try:
        await adapter.execute(request)
        if cancel_after is not None:
            assert await adapter.wait_started(request.execution_id) is not None, "процесс CLI не стартовал"
            await asyncio.sleep(cancel_after)
            await adapter.cancel(request.execution_id, reason="user")
        events = [e async for e in adapter.stream_events(request.execution_id)]
        result = await adapter.collect_result(request.execution_id)
        parsed = None
        io_dir = adapter.agent_io_dir(request.execution_id)
        target = io_dir / ".aicd" / "result.json" if io_dir is not None else None
        if target is not None and target.is_file():
            parsed = parse_agent_result(
                target.read_bytes(), sender_role=request.agent_role, output_root=agent_io.output_root(io_dir)
            )
    finally:
        adapter.release_agent_io(request.execution_id)
    raw = b"\n".join(sink.by_kind("cli_raw_log"))
    return Outcome(result, events, raw, path, parsed, round(time.monotonic() - started, 1))


def _unparsed_share(events: list[Any]) -> float:
    warnings = sum(1 for e in events if e.type == "warning")
    return warnings / max(len(events), 1)


def _git_status(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain", "--", ".", ":(exclude).aicd", ":(exclude)output"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _docker(settings: Any) -> str:
    import shutil

    found = settings.cli_docker_binary or shutil.which("docker")
    assert found, "клиент docker не найден"
    return str(found)


def _image_cli_version(settings: Any, provider: str) -> str:
    argv = [_docker(settings), "run", "--rm", "--network", "none", "--entrypoint", f"/usr/local/bin/{provider}"]
    argv += [settings.cli_runner_image, "--version"]
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=120).stdout.strip()


# --- проверки ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_probe_and_auth(settings: Any, evidence: dict[str, Any], provider: str) -> None:
    """Установка найдена, версия распознана, вход по подписке действует; в контейнере — та же версия CLI."""
    from aicd_worker import wiring

    binary = wiring.resolve_cli_binary(settings, provider)
    assert binary is not None, f"официальный CLI {provider} не найден на хосте (AICD_LOGIN_BINARIES)"
    launcher = wiring.credential_launcher_for(settings, provider, purpose="status", interactive=False)
    probe = await cli_probe.run_probe(
        provider=provider,  # type: ignore[arg-type]
        binary_path=binary,
        installation_id=uuid7(),
        adapter_version=wiring.PROBE_ADAPTER_VERSION,
        env_source=wiring.login_env_source(settings),
        auth_launcher=launcher.argv if launcher is not None else (),
        auth_binary=f"/usr/local/bin/{provider}" if launcher is not None else None,
    )
    assert probe.installed and probe.detected_version, probe.errors
    assert probe.auth_status == "AUTHENTICATED", "вход по подписке не выполнен: docs/runbooks/cli-auth.md"
    caps = probe.capabilities
    assert caps.noninteractive_mode and caps.machine_readable_output == "jsonl_stream"
    assert caps.model_selection, "CLI не объявляет выбор модели"

    # Исполнение идёт CLI из runner-образа: его версия должна совпадать с проверенной probe.
    in_image = await asyncio.to_thread(_image_cli_version, settings, provider)
    image_version = ".".join(str(p) for p in compat.parse_version(in_image) or ())
    assert image_version == probe.detected_version, f"в образе {image_version}, на хосте {probe.detected_version}"

    evidence["providers"][provider].update(
        {
            "detected_version": probe.detected_version,
            "version_raw": probe.version_raw,
            "binary_sha256": probe.binary_sha256,
            "runner_image": settings.cli_runner_image,
            "runner_image_version_raw": in_image,
            "detected_capabilities": caps.model_dump(),
            "adapter_version": "1.0.0",
        }
    )
    _record(evidence, provider, "probe_and_auth", auth_status=probe.auth_status)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_minimal_task_creates_file_and_reports(
    settings: Any, bench: Bench, evidence: dict[str, Any], provider: str
) -> None:
    """Задача выполнена в worktree, diff платформы её видит, итог агента валиден, вывод разобран."""
    out = await _execute(
        settings,
        bench,
        provider,
        "Создай в корне рабочего каталога файл SMOKE.txt, содержащий одну строку: ok. Больше ничего не меняй.",
    )
    result = out.result
    assert result.status == "completed" and result.exit_code == 0, result.error
    created = out.worktree / "SMOKE.txt"
    assert created.is_file() and created.read_text().strip() == "ok"
    assert b"SMOKE.txt" in bench.manager.diff(out.worktree, bench.base), "diff вычисляет платформа, а не CLI"
    assert out.agent_result is not None, "агент не записал .aicd/result.json"

    kinds = [e.type for e in out.events]
    assert kinds[0] == "started" and kinds[-1] == "finished"
    assert "tool_event" in kinds, "события инструментов CLI не разобраны"
    assert _unparsed_share(out.events) < 0.2, "доля неразобранных строк вывода дала бы OUTPUT_PARSE_ERROR"
    assert result.usage.source == "cli_reported" and result.usage.tokens, "CLI не сообщил usage"
    _record(
        evidence,
        provider,
        "minimal_task",
        seconds=out.seconds,
        events=len(out.events),
        unparsed_share=round(_unparsed_share(out.events), 3),
        usage_source=result.usage.source,
        usage_tokens=result.usage.tokens,
        tool_events=kinds.count("tool_event"),
    )


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_readonly_profile_cannot_change_the_worktree(
    settings: Any, bench: Bench, evidence: dict[str, Any], provider: str
) -> None:
    """Роль без права записи получает worktree read-only: файл не появляется, а итог записать всё равно можно."""
    out = await _execute(
        settings,
        bench,
        provider,
        "Попробуй создать в корне рабочего каталога файл FORBIDDEN.txt со строкой x."
        " Если запись не удалась, сообщи об этом в summary итога и заверши работу.",
        read_only=True,
    )
    assert not (out.worktree / "FORBIDDEN.txt").exists(), "запись в read-only worktree прошла"
    assert _git_status(out.worktree) == "", "в дереве задачи появились изменения"
    assert out.agent_result is not None, "итог роли без права записи не дошёл (.aicd/result.json)"
    _record(evidence, provider, "readonly_profile", seconds=out.seconds, cli_status=out.result.status)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_cancel_stops_the_cli(settings: Any, bench: Bench, evidence: dict[str, Any], provider: str) -> None:
    """Отмена во время работы: исполнение завершается как отменённое, а не висит до тайм-аута."""
    out = await _execute(
        settings,
        bench,
        provider,
        "Выполни в оболочке команду `sleep 300`, дождись её завершения и только потом создай файл LATE.txt.",
        cancel_after=20,
    )
    assert out.result.status == "cancelled", (out.result.status, out.result.error)
    assert out.seconds < 120, "CLI не остановился по отмене"
    assert not (out.worktree / "LATE.txt").exists()
    _record(evidence, provider, "cancel", seconds=out.seconds, execution_status=out.result.execution_status)


def _codex_default_model(settings: Any) -> str | None:
    """Имя модели, которое печатает сам `codex exec` в заголовке (без --json): им и проверяем флаг `--model`."""
    from aicd_worker import wiring

    launcher = wiring.credential_launcher_for(settings, "codex", purpose="smoke", interactive=False)
    if launcher is not None:
        argv, env = [*launcher.argv, "/usr/local/bin/codex"], None
    else:  # без контейнера (AICD_EXEC_RUNNER=local): CLI и credential store хоста
        binary = wiring.resolve_cli_binary(settings, "codex")
        if binary is None:
            return None
        argv, env = [binary], wiring.login_env_source(settings)
    argv += ["exec", "--skip-git-repo-check", "--sandbox", "read-only", "Ответь одним словом: ok"]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=300, env=env)
    found = re.search(r"^model:\s*(\S+)", proc.stdout + "\n" + proc.stderr, re.M)
    return found.group(1) if found else None


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_chosen_model_is_used(settings: Any, bench: Bench, evidence: dict[str, Any], provider: str) -> None:
    """Модель из настроек провайдера доходит до CLI флагом `--model`, и CLI с ней работает."""
    if provider == "claude":
        model: str | None = CLAUDE_SMOKE_MODEL
    else:
        model = os.environ.get("AICD_SMOKE_CODEX_MODEL") or await asyncio.to_thread(_codex_default_model, settings)
    if not model:
        pytest.skip("не удалось определить имя модели Codex: задайте AICD_SMOKE_CODEX_MODEL")
    out = await _execute(
        settings, bench, provider, "Создай в корне рабочего каталога файл MODEL.txt со строкой ok.", model=model
    )
    assert out.result.status == "completed", out.result.error
    assert (out.worktree / "MODEL.txt").is_file()
    facts: dict[str, Any] = {"model": model, "seconds": out.seconds}
    if provider == "claude":
        # stream-json: событие init называет модель сессии — флаг действительно применён.
        assert re.search(rb'"model"\s*:\s*"[^"]*' + CLAUDE_SMOKE_MODEL.encode(), out.raw_log), "модель сессии другая"
        facts["confirmed_in_output"] = True
    _record(evidence, provider, "model_selection", **facts)
