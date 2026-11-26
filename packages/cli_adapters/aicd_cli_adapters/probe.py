"""Probe реальной установки официального CLI на хосте исполнения (CLI_ADAPTER_CONTRACT §5.2, P2-01).

Отвечает на вопрос «что за CLI стоит на этом хосте и вошёл ли в него оператор», не касаясь учётных
данных: читаются только версия, справка (`--help`) и официальная status-команда входа. Учётные данные
CLI остаются в его credential store (ТЗ §7.1, ADR-0003) — платформа их не читает, не копирует и не
логирует, а из вывода status-команды берёт только статус и неидентифицирующий тип плана.

Ограничения, которые обеспечивает модуль:

* argv — из allowlist `PROBE_PROFILES`, запрос не может передать флаги; профиль с несверёнными по
  `--help` флагами не исполняется (как `login.LOGIN_PROFILES` и `profiles.CommandProfile`);
* окружение собирается с нуля из allowlist; `*_API_KEY`/`*_TOKEN`/`*_SECRET` в источнике — отказ
  (`profiles.FORBIDDEN_ENV`): иначе CLI отчитался бы о входе по API-ключу, запрещённом ТЗ §1;
* вывод команд проходит `login.scrub()` прежде чем попасть в сообщение об ошибке, лог или БД;
* маркер в `--help` **не** подтверждает capability (§5.2 п. 4): пока версии нет в матрице
  совместимости, capabilities записываются как обнаруженные, а статус остаётся `UNTESTED`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from aicd_cli_adapters import compat
from aicd_cli_adapters.login import Provider, scrub
from aicd_cli_adapters.models import AuthCheckResult, ProbeResult
from aicd_cli_adapters.profiles import FORBIDDEN_ENV, ProfileError
from aicd_platform.clock import utcnow

#: Таймаут одной команды probe (§5.2 п. 3). `--help` и status-команда укладываются в тот же предел.
PROBE_TIMEOUT_SECONDS: Final = 10.0
#: Сколько ждать завершения после SIGTERM, прежде чем SIGKILL.
TERMINATE_GRACE_SECONDS: Final = 2.0
#: Предел на объём вывода одной команды: справка CLI большая, но не безразмерная.
MAX_OUTPUT_BYTES: Final = 256 * 1024
#: Размер блока при вычислении `binary_sha256` (§5.2 п. 2): нативный бинарь CLI — сотни мегабайт.
_HASH_CHUNK: Final = 1024 * 1024


@dataclass(frozen=True)
class ProbeProfile:
    """Allowlist команд probe провайдера: argv фиксирован кодом, подстановок нет."""

    provider: Provider
    #: Официальный флаг версии (§5.2 п. 3).
    version_argv: tuple[str, ...]
    #: Справки, по которым идёт help-сканирование: ключ → argv (`root` — справка самого CLI).
    help_argv: tuple[tuple[str, tuple[str, ...]], ...]
    #: Официальная status-команда входа (§7). Пустой кортеж — у CLI её нет.
    auth_argv: tuple[str, ...]
    #: Официальная команда CLI, печатающая каталог моделей (JSON). Пустой кортеж — такой команды нет,
    #: и модели берутся из псевдонимов, которые CLI называет в справке флага `--model`.
    models_argv: tuple[str, ...] = ()
    env_allowlist: frozenset[str] = frozenset({"HOME", "PATH", "LANG", "TZ", "TERM", "XDG_CONFIG_HOME"})
    #: Сверены ли команды по `--help` установленной версии CLI (cli-auth.md §2, §3).
    confirmed: bool = True


PROBE_PROFILES: Final[dict[Provider, ProbeProfile]] = {
    # Проверено по `codex --help`, `codex exec --help`, `codex login --help` версии 0.167.0 (P2-01).
    "codex": ProbeProfile(
        provider="codex",
        version_argv=("--version",),
        help_argv=(("root", ("--help",)), ("exec", ("exec", "--help"))),
        auth_argv=("login", "status"),
        models_argv=("debug", "models"),  # `codex debug --help`: «Render the raw model catalog as JSON»
    ),
    # Проверено по `claude --help` и `claude auth status --json` версии 2.1.319 (Claude Code).
    "claude": ProbeProfile(
        provider="claude",
        version_argv=("--version",),
        help_argv=(("root", ("--help",)),),
        auth_argv=("auth", "status", "--json"),
    ),
}

#: Capabilities неизвестной установки: ничего не обнаружено — значит ничего и не заявляем.
UNKNOWN_CAPABILITIES: Final[dict[str, Any]] = {
    "noninteractive_mode": False,
    "machine_readable_output": "none",
    "sandbox_modes": ["cli_managed_none"],
    "resume_session": False,
    "token_usage_exposed": False,
    "auth_status_command": False,
    "cancel_signal_safe": False,
}

# Вход по API-ключу вместо подписки (ТЗ §1): такой CLI платформа авторизованным не считает.
_API_KEY_AUTH = re.compile(r"(?i)api[ _-]?key")
_CODEX_LOGGED_IN = re.compile(r"(?i)\blogged in\b")
_CODEX_NOT_LOGGED_IN = re.compile(r"(?i)\bnot logged in\b|\bplease (?:run )?login\b")
#: Режимы sandbox самого CLI в терминах контракта (ADR-0010: это слой поверх sandbox раннера).
_SANDBOX_MODES: Final = {
    "read-only": "read_only",
    "workspace-write": "workspace_write",
    "danger-full-access": "cli_managed_none",
}


def build_probe_argv(profile: ProbeProfile, binary_path: str, argv: tuple[str, ...]) -> tuple[str, ...]:
    """argv команды probe. Профиль с несверёнными флагами не исполняется (§5.2, как и вход)."""
    if not profile.confirmed:
        raise ProfileError(
            f"команды probe {profile.provider} не сверены с установленным CLI (cli-auth.md §3, P2-01):"
            " оператор должен подтвердить их по `--help`"
        )
    return (binary_path, *argv)


def build_probe_env(profile: ProbeProfile, source: dict[str, str]) -> dict[str, str]:
    """Окружение probe с нуля из allowlist; ключи AI-провайдеров в источнике — отказ в старте."""
    forbidden = sorted(k for k in source if FORBIDDEN_ENV.search(k))
    if forbidden:
        raise ProfileError(f"в окружении probe запрещённые переменные: {', '.join(forbidden)}")
    env = {k: v for k, v in source.items() if k in profile.env_allowlist}
    env.setdefault("TERM", "dumb")  # вывод справки парсится как текст, без управляющих последовательностей
    env.setdefault("NO_COLOR", "1")
    return env


async def _run(argv: tuple[str, ...], env: dict[str, str], *, limit: int = MAX_OUTPUT_BYTES) -> tuple[int | None, str]:
    """Выполнить команду probe без shell и PTY. `None` в коде выхода — таймаут (процесс снят)."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        start_new_session=True,  # probe снимается вместе с группой, не оставляя процесс на хосте
    )
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        await _terminate(process)
        return None, ""
    return process.returncode, raw[:limit].decode("utf-8", errors="replace")


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _codex_capabilities(help_texts: dict[str, str]) -> dict[str, Any]:
    """Маркеры `codex --help` / `codex exec --help`. Подтверждение — только строка матрицы (§5.2 п. 4)."""
    root, exec_help = help_texts.get("root", ""), help_texts.get("exec", "")
    jsonl = bool(re.search(r"^\s+--json\b", exec_help, re.M))
    values = _sandbox_values(exec_help)
    modes = [contract_mode for flag, contract_mode in _SANDBOX_MODES.items() if flag in values]
    return {
        "noninteractive_mode": bool(re.search(r"^\s+exec\b.*", root, re.M)) and "non-interactively" in root,
        "machine_readable_output": "jsonl_stream" if jsonl else "none",
        "sandbox_modes": modes or ["cli_managed_none"],
        "resume_session": bool(re.search(r"^\s+resume\b", exec_help, re.M)),
        # Не выводится из справки: подтверждается только прогоном smoke-набора (§13.2).
        "token_usage_exposed": False,
        "auth_status_command": bool(re.search(r"^\s+login\b", root, re.M)),
        "cancel_signal_safe": False,
        "model_selection": bool(re.search(r"^\s+-m, --model\b", exec_help, re.M)),
    }


def _claude_capabilities(help_texts: dict[str, str]) -> dict[str, Any]:
    """Маркеры `claude --help`. Собственного sandbox у CLI нет — изоляцию даёт раннер (ADR-0010)."""
    root = help_texts.get("root", "")
    stream = "stream-json" in root
    return {
        "noninteractive_mode": bool(re.search(r"^\s+-p, --print\b", root, re.M)),
        "machine_readable_output": "jsonl_stream" if stream else ("json" if "--output-format" in root else "none"),
        "sandbox_modes": ["cli_managed_none"],
        "resume_session": bool(re.search(r"^\s+-r, --resume\b", root, re.M)),
        "token_usage_exposed": False,
        "auth_status_command": bool(re.search(r"^\s+auth\b", root, re.M)),
        "cancel_signal_safe": False,
        "model_selection": bool(re.search(r"^\s+--model\b", root, re.M)),
    }


def _sandbox_values(exec_help: str) -> str:
    """Перечисление значений `--sandbox` из справки (в ней несколько блоков `possible values`)."""
    block = re.search(r"-s, --sandbox.*?possible values:\s*([^\]]*)\]", exec_help, re.S)
    return block.group(1) if block else ""


_CAPABILITY_DETECTORS: Final = {"codex": _codex_capabilities, "claude": _claude_capabilities}


_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}$")
#: Каталог моделей Codex — сотни килобайт (в него входят системные инструкции моделей).
MODELS_OUTPUT_BYTES: Final = 4 * 1024 * 1024
MAX_MODELS: Final = 50


def _model_priority(model: dict[str, Any]) -> int:
    priority = model.get("priority")
    return priority if isinstance(priority, int) else 10**6


def parse_model_catalog(output: str) -> list[str]:
    """Имена моделей из JSON-каталога CLI (`codex debug models`): только те, что CLI показывает в списке.

    Каталог — данные CLI, а не платформы: берутся лишь имена (`slug`) видимых моделей в порядке приоритета,
    каждое проверяется тем же шаблоном, что и настройка `model`. Непонятный вывод — пустой список.
    """
    try:
        data = json.loads(output)
    except ValueError:
        return []
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        return []
    visible = [m for m in models if isinstance(m, dict) and m.get("visibility", "list") == "list"]
    visible.sort(key=_model_priority)
    names: list[str] = []
    for m in visible:
        slug = m.get("slug")
        if isinstance(slug, str) and _MODEL_NAME.fullmatch(slug) and slug not in names:
            names.append(slug)
    return names[:MAX_MODELS]


def parse_model_aliases(help_text: str) -> list[str]:
    """Псевдонимы моделей из описания флага `--model` в справке CLI (Claude Code перечисляет их в кавычках)."""
    found = re.search(r"^\s+--model <model>(.*?)(?=^\s+-{1,2}[A-Za-z]|\Z)", help_text, re.M | re.S)
    if not found:
        return []
    names: list[str] = []
    for alias in re.findall(r"'([^'\s]+)'", found.group(1)):
        if _MODEL_NAME.fullmatch(alias) and alias not in names:
            names.append(alias)
    return names[:MAX_MODELS]


def detect_capabilities(provider: Provider, help_texts: dict[str, str]) -> dict[str, Any]:
    """Capabilities по маркерам справки; пустая справка — ничего не заявляем."""
    if not any(help_texts.values()):
        return dict(UNKNOWN_CAPABILITIES)
    return {**UNKNOWN_CAPABILITIES, **_CAPABILITY_DETECTORS[provider](help_texts)}


def parse_auth(provider: Provider, code: int | None, output: str) -> tuple[str, str | None, str | None]:
    """Статус входа, неидентифицирующий тип плана и инструкция оператору по выводу status-команды.

    Из вывода не берётся ничего, кроме статуса и типа плана: в нём бывают e-mail и название
    организации (`claude auth status --json`), которым не место ни в БД, ни в логах (ТЗ §7.1).
    """
    relogin = "выполните официальный вход CLI по подписке на хосте исполнения (cli-auth.md)"
    if code is None:
        return "UNKNOWN", None, "status-команда CLI не ответила за отведённое время"
    if provider == "claude":
        try:
            data = json.loads(output[output.index("{") : output.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            return "ERROR", None, relogin
        if not data.get("loggedIn"):
            return "AUTH_REQUIRED", None, relogin
        if _API_KEY_AUTH.search(str(data.get("authMethod", ""))):
            # Вход по ключу — биллинг API, а не подписка: платформа такую установку не использует (ТЗ §1).
            return "AUTH_REQUIRED", None, f"вход выполнен по API-ключу, это запрещено (ТЗ §1): {relogin}"
        plan = str(data.get("subscriptionType") or "")[:100] or None
        return "AUTHENTICATED", plan, None
    if code != 0 or _CODEX_NOT_LOGGED_IN.search(output):
        return "AUTH_REQUIRED", None, relogin
    if not _CODEX_LOGGED_IN.search(output):
        return "UNKNOWN", None, relogin
    if _API_KEY_AUTH.search(output):
        return "AUTH_REQUIRED", None, f"вход выполнен по API-ключу, это запрещено (ТЗ §1): {relogin}"
    hint = "ChatGPT" if re.search(r"(?i)chatgpt", output) else None
    return "AUTHENTICATED", hint, None


async def check_auth(
    *,
    provider: Provider,
    binary_path: str,
    installation_id: UUID,
    env_source: dict[str, str] | None = None,
    launcher: tuple[str, ...] = (),
) -> AuthCheckResult:
    """Статус входа CLI официальной status-командой (§7). Учётные данные не читаются."""
    profile = PROBE_PROFILES[provider]
    if not profile.auth_argv:
        return AuthCheckResult.model_validate(
            {
                "contract_version": "1.0",
                "provider": provider,
                "installation_id": str(installation_id),
                "auth_status": "UNKNOWN",
                "method": "not_determinable",
                "checked_at": utcnow().isoformat(),
            }
        )
    env = build_probe_env(profile, dict(env_source or {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}))
    # `launcher` — префикс одноразового контейнера с томом учётных данных: статус входа читается
    # там же, где его найдёт CLI исполнения (`binary_path` — путь CLI внутри образа).
    argv = (*launcher, *build_probe_argv(profile, binary_path, profile.auth_argv))
    try:
        code, output = await _run(argv, env)
    except OSError:
        code, output = None, ""
    status, hint, action = parse_auth(provider, code, output)
    return AuthCheckResult.model_validate(
        {
            "contract_version": "1.0",
            "provider": provider,
            "installation_id": str(installation_id),
            "auth_status": status,
            "method": "cli_status_command",
            "account_hint": hint,
            "operator_action": action,
            "checked_at": utcnow().isoformat(),
        }
    )


async def run_probe(
    *,
    provider: Provider,
    binary_path: str | None,
    installation_id: UUID,
    adapter_version: str,
    env_source: dict[str, str] | None = None,
    host_id: UUID | None = None,
    auth_launcher: tuple[str, ...] = (),
    auth_binary: str | None = None,
) -> ProbeResult:
    """Probe установки по алгоритму §5.2: бинарь → хеш → версия → справка → матрица → check_auth."""
    started = utcnow()
    profile = PROBE_PROFILES[provider]
    base: dict[str, Any] = {
        "contract_version": "1.0",
        "provider": provider,
        "installation_id": str(installation_id),
        "host_id": str(host_id) if host_id else None,
        "binary_path": binary_path,
        "adapter_version": adapter_version,
        "checked_at": started.isoformat(),
    }
    if binary_path is None or not await asyncio.to_thread(Path(binary_path).is_file):
        return ProbeResult.model_validate(
            {
                **base,
                "installed": False,
                "compatibility": "UNTESTED",
                "capabilities": dict(UNKNOWN_CAPABILITIES),
                "auth_status": "UNKNOWN",
                "errors": [{"code": "CLI_NOT_INSTALLED", "message": "официальный CLI не найден на хосте исполнения"}],
                "duration_ms": _elapsed_ms(started),
            }
        )

    env = build_probe_env(profile, dict(env_source or {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}))
    errors: list[dict[str, str]] = []
    sha256 = await asyncio.to_thread(_sha256, Path(binary_path))

    version_raw: str | None = None
    code, output = await _probe_command(profile, binary_path, profile.version_argv, env, errors, "версия")
    if code == 0:
        version_raw = scrub(output).strip().splitlines()[0][:500] if output.strip() else None
    parsed = compat.parse_version(version_raw)
    if version_raw is not None and parsed is None:
        errors.append({"code": "OUTPUT_PARSE_ERROR", "message": "версия CLI не распознана как SemVer"})

    help_texts: dict[str, str] = {}
    for key, argv in profile.help_argv:
        code, output = await _probe_command(profile, binary_path, argv, env, errors, f"справка ({key})")
        help_texts[key] = output if code == 0 else ""

    compatibility, entry = compat.evaluate(provider, version_raw)
    # §5.3: capabilities подтверждённой версии берутся из матрицы; иначе — только обнаруженные маркеры.
    capabilities = dict(entry.capabilities) if entry is not None else detect_capabilities(provider, help_texts)
    # Модели для выбора в настройках провайдера называет сам CLI: каталог (если у CLI есть такая команда)
    # либо псевдонимы из справки. Это не свойство версии из матрицы, поэтому определяется при каждом probe.
    models: list[str] = []
    if profile.models_argv:
        try:
            code, catalog = await _run(
                build_probe_argv(profile, binary_path, profile.models_argv), env, limit=MODELS_OUTPUT_BYTES
            )
        except OSError:
            code, catalog = None, ""
        models = parse_model_catalog(catalog) if code == 0 else []
    if not models:
        models = parse_model_aliases(help_texts.get("root", ""))
    if models:
        capabilities["available_models"] = models
    # Docker-режим: статус входа — из тома учётных данных (тот же том монтируется в runner), а не из
    # credential store хоста; версия и справка по-прежнему берутся у CLI хоста той же версии.
    auth = await check_auth(
        provider=provider,
        binary_path=auth_binary or binary_path,
        installation_id=installation_id,
        env_source=env_source,
        launcher=auth_launcher,
    )
    if auth.operator_action:
        errors.append({"code": "PROCESS_FAILED", "message": auth.operator_action[:1000]})

    return ProbeResult.model_validate(
        {
            **base,
            "installed": True,
            "binary_sha256": sha256,
            "detected_version": ".".join(str(p) for p in parsed) if parsed else None,
            "version_raw": version_raw,
            "compatibility": compatibility,
            "compatibility_matrix_entry": entry.id if entry is not None else None,
            "capabilities": capabilities,
            "auth_status": auth.auth_status,
            "errors": errors or None,
            "duration_ms": _elapsed_ms(started),
        }
    )


async def _probe_command(
    profile: ProbeProfile,
    binary_path: str,
    argv: tuple[str, ...],
    env: dict[str, str],
    errors: list[dict[str, str]],
    what: str,
) -> tuple[int | None, str]:
    """Одна команда probe с записью отказа в `errors`; вывод в сообщение попадает только через `scrub`."""
    try:
        code, output = await _run(build_probe_argv(profile, binary_path, argv), env)
    except OSError as exc:
        errors.append({"code": "PROCESS_FAILED", "message": f"{what}: {scrub(str(exc))[:200]}"})
        return None, ""
    if code is None:
        errors.append({"code": "TIMEOUT", "message": f"{what}: CLI не ответил за {PROBE_TIMEOUT_SECONDS:.0f} с"})
    elif code != 0:
        errors.append({"code": "PROCESS_FAILED", "message": f"{what}: код выхода {code}; {scrub(output)[:200]}"})
    return code, output


def _elapsed_ms(started: datetime) -> int:
    return max(0, int((utcnow() - started).total_seconds() * 1000))
