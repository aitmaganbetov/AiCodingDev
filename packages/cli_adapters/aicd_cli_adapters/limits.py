"""Лимиты подписки CLI: то, что сам CLI сообщает о расходе окна подписки (сессия, неделя).

Источник — только официальный CLI. Платформа не обращается к API провайдера и не читает credential store
(ТЗ §1); спрашивает она у самого CLI:

* Claude Code печатает событие `rate_limit_event` в машинном выводе (`--output-format stream-json`): доля
  израсходованного окна и время сброса. Оно приходит попутно с каждым исполнением задачи
  (`RealCLIExecutor.subscription_limits`), а по запросу оператора — одним минимальным запросом, после
  которого процесс снимается (запрос к модели уже сделан, поэтому чтение немного расходует подписку);
* Codex CLI в `exec --json` лимитов не сообщает, но отвечает на них своему клиенту по протоколу
  `codex app-server` (метод `account/rateLimits/read`): окна лимита и баланс кредитов. Запроса к модели
  при этом нет — чтение ничего не расходует, поэтому выполняется и при обычной проверке установки.

Хранение — Redis, а не БД: это проекция внешнего состояния на момент наблюдения (как список репозиториев
установки GitHub), она ничего не решает в маршрутизации и восстанавливается следующим наблюдением.
В записи только числа и моменты времени: ни идентификаторов аккаунта, ни организации.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import signal
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from aicd_cli_adapters.profiles import FORBIDDEN_ENV, ProfileError

#: Сколько наблюдение хранится: недельное окно остаётся осмысленным до своего сброса.
TTL_SECONDS: Final = 8 * 24 * 60 * 60
MAX_WINDOWS: Final = 8
READ_TIMEOUT_SECONDS: Final = 90.0
_KIND = re.compile(r"^[a-z0-9_]{1,40}$")
_STATUS = re.compile(r"^[a-z_]{1,40}$")
#: Провайдеры, CLI которых сообщает лимиты подписки.
READABLE: Final = frozenset({"claude", "codex"})
#: Чтение лимитов этих провайдеров делает запрос к модели (расходует подписку) — только по явной просьбе.
COSTLY: Final = frozenset({"claude"})
#: `codex app-server`: JSON-RPC по stdio (сверено по `codex app-server generate-json-schema` версии 0.167.0).
CODEX_LIMITS_ARGV: Final = ("app-server",)
CODEX_READ_TIMEOUT_SECONDS: Final = 30.0
_BALANCE = re.compile(r"^\d{1,12}(\.\d{1,4})?$")
#: Команда чтения лимитов Claude Code: тот же машинный формат, что у исполнения задач; запрос — константа
#: платформы. Событие с лимитами печатается до ответа модели, после него процесс снимается.
CLAUDE_LIMITS_ARGV: Final = ("--print", "--output-format", "stream-json", "--verbose", "Ответь одним словом: ok")
_ENV_ALLOWLIST: Final = frozenset({"HOME", "PATH", "LANG", "TZ", "TERM"})


#: Клиент Redis (`redis.asyncio.Redis`); типизирован свободно — как в `integrations_github.repo_cache`.
RedisLike = Any


def key(environment: str, installation_id: uuid.UUID | str) -> str:
    return f"aicd:{environment}:cli_limits:{installation_id}"


def _iso(epoch: object) -> str | None:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(epoch) or epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _window(kind: object, raw: object) -> dict[str, Any] | None:
    if not isinstance(kind, str) or not _KIND.fullmatch(kind) or not isinstance(raw, dict):
        return None
    used = raw.get("utilization")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
        return None
    status = raw.get("status")
    return {
        "kind": kind,
        "used": round(min(max(float(used), 0.0), 1.0), 4),
        "resets_at": _iso(raw.get("resetsAt")),
        "status": status if isinstance(status, str) and _STATUS.fullmatch(status) else None,
    }


def from_claude_event(event: object, *, now: datetime) -> dict[str, Any] | None:
    """Наблюдение лимитов из события `rate_limit_event` Claude Code; None — в событии нет окон с расходом.

    Берутся только окна (`unifiedWindows`): тип, доля расхода 0…1, время сброса, статус. Прочие поля события
    (настройки перерасхода организации и т. п.) в наблюдение не попадают.
    """
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        info = event.get("rate_limit")
    if not isinstance(info, dict):
        return None
    raw_windows = info.get("unifiedWindows")
    windows: list[dict[str, Any]] = []
    if isinstance(raw_windows, dict):
        for kind, raw in raw_windows.items():
            window = _window(kind, raw)
            if window is not None:
                windows.append(window)
    if not windows:
        return None
    return {"provider": "claude", "observed_at": now.isoformat(), "windows": windows[:MAX_WINDOWS], "credits": None}


def _codex_window(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used, minutes = raw.get("usedPercent"), raw.get("windowDurationMins")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
        return None
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 < minutes <= 525_600:
        return None
    kind = {300: "five_hour", 10080: "seven_day"}.get(minutes, f"window_{minutes}m")
    return {
        "kind": kind,
        "used": round(min(max(float(used) / 100, 0.0), 1.0), 4),
        "resets_at": _iso(raw.get("resetsAt")),
        "status": None,
    }


def from_codex_rate_limits(result: object, *, now: datetime) -> dict[str, Any] | None:
    """Наблюдение из ответа `account/rateLimits/read` Codex: окна лимита и баланс кредитов.

    Идентификатор аккаунта, тип плана и купоны сброса из ответа в наблюдение не попадают.
    """
    if not isinstance(result, dict):
        return None
    snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        return None
    windows = [w for w in (_codex_window(snapshot.get(k)) for k in ("primary", "secondary")) if w is not None]
    credit_info: dict[str, Any] | None = None
    raw_credits = snapshot.get("credits")
    if isinstance(raw_credits, dict):
        balance = raw_credits.get("balance")
        credit_info = {
            "balance": balance if isinstance(balance, str) and _BALANCE.fullmatch(balance) else None,
            "unlimited": raw_credits.get("unlimited") is True,
        }
    if not windows and credit_info is None:
        return None
    return {"provider": "codex", "observed_at": now.isoformat(), "windows": windows, "credits": credit_info}


async def save(redis: RedisLike, *, environment: str, installation_id: uuid.UUID, limits: dict[str, Any]) -> None:
    await redis.set(key(environment, installation_id), json.dumps(limits), ex=TTL_SECONDS)


async def load(redis: RedisLike, *, environment: str, installation_id: uuid.UUID | str) -> dict[str, Any] | None:
    """Последнее наблюдение установки; повреждённая или чужая запись читается как отсутствующая."""
    raw = await redis.get(key(environment, installation_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("windows"), list) or not data.get("observed_at"):
        return None
    raw_credits = data.get("credits")
    credit_info: dict[str, Any] | None = None
    if isinstance(raw_credits, dict):
        balance = raw_credits.get("balance")
        credit_info = {
            "balance": balance if isinstance(balance, str) and _BALANCE.fullmatch(balance) else None,
            "unlimited": raw_credits.get("unlimited") is True,
        }
    windows = [
        {"kind": w["kind"], "used": w["used"], "resets_at": w.get("resets_at"), "status": w.get("status")}
        for w in data["windows"]
        if isinstance(w, dict) and isinstance(w.get("kind"), str) and isinstance(w.get("used"), (int, float))
    ]
    if not windows and credit_info is None:
        return None
    return {
        "provider": str(data.get("provider") or ""),
        "observed_at": str(data["observed_at"]),
        "windows": windows,
        "credits": credit_info,
    }


def build_env(source: dict[str, str]) -> dict[str, str]:
    """Окружение команды чтения лимитов: с нуля из allowlist; ключи AI-провайдеров в источнике — отказ."""
    forbidden = sorted(k for k in source if FORBIDDEN_ENV.search(k))
    if forbidden:
        raise ProfileError(f"в окружении запрещённые переменные: {', '.join(forbidden)}")
    return {k: v for k, v in source.items() if k in _ENV_ALLOWLIST}


async def read_limits(
    *,
    provider: str,
    binary_path: str,
    env_source: dict[str, str],
    launcher: tuple[str, ...] = (),
    now: datetime,
    wait_seconds: float = READ_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Спросить лимиты у самого CLI одним минимальным запросом. None — CLI их не сообщил.

    `launcher` — команда одноразового контейнера над томом учётных данных (Docker-режим), как у проверки
    статуса входа. Вывод читается построчно до события с лимитами, после чего процесс снимается: ответ
    модели платформе не нужен. Из вывода в результат попадают только числа окон (`from_claude_event`).
    """
    if provider not in READABLE:
        return None
    if provider == "codex":
        return await _read_codex(binary_path=binary_path, env_source=env_source, launcher=launcher, now=now)
    process = await asyncio.create_subprocess_exec(
        *launcher,
        binary_path,
        *CLAUDE_LIMITS_ARGV,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=build_env(env_source),
        start_new_session=True,
        limit=4 * 1024 * 1024,
    )
    found: dict[str, Any] | None = None

    async def scan() -> None:
        nonlocal found
        if process.stdout is None:  # pragma: no cover — PIPE задан выше
            return
        async for raw in process.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            found = from_claude_event(event, now=now)
            if found is not None:
                return

    try:
        with contextlib.suppress(TimeoutError, ValueError):
            await asyncio.wait_for(scan(), timeout=wait_seconds)
    finally:
        await _stop(process)
    return found


async def _read_codex(
    *, binary_path: str, env_source: dict[str, str], launcher: tuple[str, ...], now: datetime
) -> dict[str, Any] | None:
    """Спросить лимиты у `codex app-server`: initialize → account/rateLimits/read. Запросов к модели нет."""
    process = await asyncio.create_subprocess_exec(
        *launcher,
        binary_path,
        *CODEX_LIMITS_ARGV,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=build_env(env_source),
        start_new_session=True,
        limit=4 * 1024 * 1024,
    )
    found: dict[str, Any] | None = None
    requests = (
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "aicd", "title": "AiCodingDev", "version": "1"}},
        },
        {"method": "initialized"},
        {"id": 2, "method": "account/rateLimits/read"},
    )

    async def talk() -> None:
        nonlocal found
        if process.stdin is None or process.stdout is None:  # pragma: no cover — PIPE задан выше
            return
        for message in requests:
            process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()
        async for raw in process.stdout:
            try:
                reply = json.loads(raw)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == 2:
                found = from_codex_rate_limits(reply.get("result"), now=now)
                return

    try:
        with contextlib.suppress(TimeoutError, ValueError, ConnectionError):
            await asyncio.wait_for(talk(), timeout=CODEX_READ_TIMEOUT_SECONDS)
    finally:
        await _stop(process)
    return found


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
