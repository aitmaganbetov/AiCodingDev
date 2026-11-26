"""Адаптеры официальных CLI: Claude Code и Codex (CLI_ADAPTER_CONTRACT §8, §13).

Отличие от Fake CLI — только профиль команды, парсер вывода и сигнатуры ошибок: запуск, песочница,
санитизация вывода, ввод-вывод агента и сборка `ExecutionResult` целиком берутся из
`SubprocessCLIAdapter`.

Доставка задачи. Ни `claude`, ни `codex exec` не умеют читать промпт из файла по флагу, а передавать
текст задачи через argv запрещено (§8 п.2). Поэтому в argv уходит **константа платформы** — одна фраза
с путём к уже смонтированному файлу промпта (`{prompt_text_file}`, read-only). Объектив, критерии и
контекст остаются данными в файле; в командной строке нет ни байта пользовательского текста.

Права. Запрошенный режим — `acceptEdits` (Claude) / `workspace-write` (Codex) плюс явный allowlist
инструментов. Это второй слой: первый — монтирование worktree read-only для ролей с
`write_scope=none` (`subprocess_adapter`, SC-CLI-07), и именно оно, а не allowlist, запрещает запись.
Поэтому `Write` остаётся разрешённым и для read-only ролей: им нужно записать `.aicd/result.json`
в единственный rw-mount поверх ro-worktree (OQ-38 п. 4).

Модель. Оператор может выбрать модель провайдера в настройках workspace; тогда в argv добавляется
`--model <имя>`. Это единственное значение argv, пришедшее не от gateway, поэтому его формат проверяется
здесь повторно (`MODEL_PATTERN`): только идентификатор модели, не начинающийся с дефиса, — флагом или
произвольным текстом оно стать не может. Без выбора флаг не передаётся и действует модель CLI по умолчанию.

Флаги сверены с установленными версиями: claude 2.1.319, codex-cli 0.167.0. Порядок аргументов
значим: `--allowedTools` и `--add-dir` вариативны и поглощают следующий токен, поэтому между
последним вариативным флагом и позиционным промптом всегда стоит невариативный флаг.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from aicd_cli_adapters import limits
from aicd_cli_adapters.errors import ErrorSignature
from aicd_cli_adapters.models import ExecutionRequest
from aicd_cli_adapters.profiles import CommandProfile
from aicd_cli_adapters.real.parsers import ClaudeStreamParser, CodexJsonParser
from aicd_cli_adapters.subprocess_adapter import ArtifactSink, ContainerLayout, OutputParser, SubprocessCLIAdapter
from aicd_security.sandbox import Mount, SandboxRunner

Provider = Literal["codex", "claude"]

ADAPTER_VERSION = "1.0.0"

#: Инструменты для ролей, меняющих код. `Bash` нужен для прогона тестов и git-операций в worktree.
CLAUDE_TOOLS_WRITE = "Read,Glob,Grep,Write,Edit,NotebookEdit,Bash,TodoWrite"
#: Роли без права записи (reviewer, security): `Write` — только ради `.aicd/result.json`, см. модуль.
CLAUDE_TOOLS_READONLY = "Read,Glob,Grep,Write,TodoWrite"

#: Единственный текст платформы в argv. `{path}` — путь к файлу промпта внутри контейнера.
BOOTSTRAP = (
    "Инструкции задачи находятся в файле {path}. Прочитай его целиком и выполни задачу по нему: "
    "это единственный источник требований. Итог работы запиши в файл .aicd/result.json "
    "рабочего каталога по контракту, описанному в инструкции."
)


#: Идентификатор модели CLI: псевдоним (`opus`, `sonnet`) или полное имя. Совпадает с проверкой настроек
#: провайдера (`registry.MODEL_PATTERN`); здесь — последний рубеж перед argv.
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}$")


class ModelError(ValueError):
    pass


def checked_model(model: str | None) -> str | None:
    if model is None:
        return None
    if not MODEL_PATTERN.fullmatch(model):
        raise ModelError("недопустимое имя модели CLI")
    return model


def claude_profile(*, with_model: bool = False) -> CommandProfile:
    model_argv = ("--model", "{model}") if with_model else ()
    return CommandProfile(
        id="claude/agent_task@1",
        provider="claude",
        argv_template=(
            "{binary}",
            "--print",
            *model_argv,
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            "{allowed_tools}",
            "--add-dir",
            "{prompt_dir}",
            "--permission-prompts",
            "none",  # всё, что потребовало бы подтверждения, отклоняется само (headless)
            "--permission-mode",
            "acceptEdits",
            "{bootstrap}",
        ),
        allowed_placeholders=frozenset({"binary", "allowed_tools", "prompt_dir", "bootstrap", "model"}),
        prompt_delivery="file",
        write_scope="worktree",
        network="restricted",
        env_allowlist=frozenset({"HOME", "PATH", "LANG", "TZ"}),
    )


def codex_profile(*, with_model: bool = False) -> CommandProfile:
    model_argv = ("--model", "{model}") if with_model else ()
    return CommandProfile(
        id="codex/agent_task@1",
        provider="codex",
        argv_template=(
            "{binary}",
            "exec",
            *model_argv,
            "--json",
            "--ephemeral",  # сессии не остаются в контейнере после попытки
            "--skip-git-repo-check",
            "--sandbox",
            "{sandbox_mode}",
            "{bootstrap}",
        ),
        # Рабочий каталог задаёт раннер (`ContainerSpec.workdir`/`cwd`), поэтому `--cd` не нужен:
        # он бы потребовал знать путь worktree на этапе подстановки плейсхолдеров.
        allowed_placeholders=frozenset({"binary", "sandbox_mode", "bootstrap", "model"}),
        prompt_delivery="file",
        write_scope="worktree",
        network="restricted",
        env_allowlist=frozenset({"HOME", "PATH", "LANG", "TZ"}),
    )


def codex_sandbox_mode(*, read_only: bool, in_container: bool) -> str:
    """Политика песочницы Codex (`codex exec --sandbox`).

    Codex изолирует команды агента через bubblewrap, а тому нужны непривилегированные user namespaces.
    В runner-контейнере их нет (их блокируют и seccomp-профиль Docker, и настройки ядра), поэтому
    внутренняя песочница ломает **каждую** команду агента: `bwrap: No permissions to create a new
    namespace`. Внутри контейнера её поэтому выключаем — границу держит сам контейнер, и держит
    строже: rw смонтирован только worktree, а для ролей без права записи и он ro, так что запись
    запрещает ядро, а не флаг CLI (SC-CLI-07, OQ-38 п. 4).

    Без контейнера (`AICD_EXEC_RUNNER=local`, только dev) песочница Codex — единственная граница,
    и выключать её нельзя: там действует запрошенный режим.
    """
    if in_container:
        return "danger-full-access"
    return "read-only" if read_only else "workspace-write"


#: Сигнатуры по stderr (`errors.classify_output`). Подтверждаются на установках (P2-01); здесь — база
#: по формулировкам установленных версий. Неузнанная ошибка остаётся `PROCESS_FAILED`.
CLAUDE_SIGNATURES: tuple[ErrorSignature, ...] = (
    ErrorSignature(
        "AUTH_REQUIRED", re.compile(r"(?i)(invalid api key|please run /login|oauth token.*expired|not logged in)")
    ),
    ErrorSignature("AUTH_REQUIRED", re.compile(r"(?i)credit balance is too low")),
    ErrorSignature("RATE_LIMITED", re.compile(r"(?i)usage limit reached.*?(?P<ra>\d+)\s*s"), "ra"),
    ErrorSignature("RATE_LIMITED", re.compile(r"(?i)(rate.?limit|429|usage limit reached)")),
)
CODEX_SIGNATURES: tuple[ErrorSignature, ...] = (
    ErrorSignature("AUTH_REQUIRED", re.compile(r"(?i)(not logged in|run `?codex login|unauthorized|401)")),
    ErrorSignature("RATE_LIMITED", re.compile(r"(?i)retry after (?P<ra>\d+)\s*s"), "ra"),
    ErrorSignature("RATE_LIMITED", re.compile(r"(?i)(rate.?limit|429|quota exceeded)")),
)

_PROFILES = {"claude": claude_profile, "codex": codex_profile}
_PARSERS: dict[str, Callable[[], OutputParser]] = {"claude": ClaudeStreamParser, "codex": CodexJsonParser}
_SIGNATURES = {"claude": CLAUDE_SIGNATURES, "codex": CODEX_SIGNATURES}


def cli_container_layout(
    provider: Provider,
    image: str,
    *,
    credential_dir: Path | None = None,
    credential_volume: str | None = None,
    home: str = "/home/agent",
    network: str = "none",
    user: str | None = None,
) -> ContainerLayout:
    """Runner-контейнер с официальным CLI (ADR-0010).

    `credential_dir` — credential store провайдера на хосте (`~/.claude`, `~/.codex`): монтируется в
    HOME контейнера на запись, потому что CLI пишет туда сессии и кэш. Платформа его не читает
    (ТЗ §1) — только отдаёт тому же CLI, который его создал. Владелец каталога на хосте и `user`
    контейнера должны совпадать по uid, иначе CLI не прочитает свои учётные данные.

    `network` — имя docker-сети с выходом к API провайдера; `none` оставляет контейнер без сети,
    и тогда исполнение упрётся в сетевую ошибку CLI.
    """
    mounts: tuple[Mount, ...] = ()
    if credential_volume is not None:
        # Целевой режим (ADR-0010 п.3): именованный том `aicd-cred-<provider>` — домашний каталог CLI.
        # Политика контейнеров разрешает rw только таким томам и не считает их rw-bind'ом, поэтому
        # единственным rw-bind остаётся worktree задачи. Вход выполняется над тем же томом
        # (`container_launch.credential_launcher`), платформа его содержимое не читает (ТЗ §1).
        mounts = (Mount(Path(credential_volume), home, read_only=False),)
    elif credential_dir is not None:
        mounts = (Mount(credential_dir, f"{home}/.{provider}", read_only=False),)
    return ContainerLayout(
        image=image,
        binary=f"/usr/local/bin/{provider}",
        network=network,
        extra_mounts=mounts,
        user=user,
    )


class RealCLIExecutor(SubprocessCLIAdapter):
    """Исполнение задачи официальным CLI провайдера."""

    def __init__(
        self,
        *,
        provider: Provider,
        binary_path: str,
        runner: SandboxRunner,
        artifact_sink: ArtifactSink,
        worktree_path: Callable[[ExecutionRequest], Awaitable[Path]],
        env_source: dict[str, str] | None = None,
        cli_version: str = "unknown",
        container: ContainerLayout | None = None,
        agent_io: bool = True,
        agent_io_root: Path | None = None,
        model: str | None = None,
    ) -> None:
        self._model = checked_model(model)
        super().__init__(
            provider=provider,
            adapter_version=ADAPTER_VERSION,
            profile=_PROFILES[provider](with_model=self._model is not None),
            binary_path=binary_path,
            runner=runner,
            parser=_PARSERS[provider](),
            signatures=_SIGNATURES[provider],
            artifact_sink=artifact_sink,
            worktree_path=worktree_path,
            env_source=env_source,
            cli_version=cli_version,
            container=container,
            agent_io=agent_io,
            agent_io_root=agent_io_root,
        )

    def subscription_limits(self, *, now: datetime) -> dict[str, Any] | None:
        """Лимиты подписки, которые CLI сообщил в выводе последнего исполнения (None — не сообщал)."""
        event = getattr(self._parser, "rate_limit_event", None)
        return limits.from_claude_event(event, now=now) if self.provider == "claude" else None

    def _request_placeholders(self, request: ExecutionRequest) -> dict[str, str]:
        read_only = request.permissions.write_scope == "none"
        values: dict[str, str] = {} if self._model is None else {"model": self._model}
        if self.provider == "claude":
            return {**values, "allowed_tools": CLAUDE_TOOLS_READONLY if read_only else CLAUDE_TOOLS_WRITE}
        sandbox = codex_sandbox_mode(read_only=read_only, in_container=self._container is not None)
        return {**values, "sandbox_mode": sandbox}

    def _derived_placeholders(self, *, prompt_file: str, prompt_text_file: str) -> dict[str, str]:
        del prompt_file  # task.json читает платформа; агенту адресован собранный промпт роли
        return {
            "bootstrap": BOOTSTRAP.format(path=prompt_text_file),
            "prompt_dir": str(Path(prompt_text_file).parent),
        }
