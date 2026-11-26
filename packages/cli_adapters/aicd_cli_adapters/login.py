"""Интерактивный вход официального CLI по подписке, инициированный из веб-интерфейса (P2-01, §6.2).

Платформа **не** получает и не хранит учётные данные: она запускает официальную команду входа CLI в PTY на
хосте исполнения, вытаскивает из её вывода только пару «ссылка + код устройства» (device code) и отдаёт их
оператору в браузер. Вход оператор завершает сам на сайте провайдера; токен пишет сам CLI в свой credential
store. Поэтому в БД попадают только `verification_url` и `user_code` — они бесполезны без входа владельца
подписки и живут до конца сессии входа.

Ограничения, которые обеспечивает модуль:

* argv — из allowlist `LOGIN_PROFILES`, запрос не может передать флаги (как `profiles.CommandProfile`);
  запрещены `--with-api-key` и `--console` (биллинг API вместо подписки, ТЗ §1, cli-auth.md §0);
* окружение собирается с нуля из allowlist; переменные вида `*_API_KEY`/`*_TOKEN`/`*_SECRET` —
  отказ в старте (`profiles.FORBIDDEN_ENV`);
* каждая строка вывода проходит `scrub()` до того, как уйти в БД, лог или браузер: вывод CLI может
  содержать фрагменты токена (`sk-ant-…`, `oauth`-URL с `code=`), их видеть платформе нельзя;
* сессия ограничена по времени (`LOGIN_TIMEOUT`) и отменяема; процесс снимается SIGTERM, затем SIGKILL;
* в stdin CLI уходит только то, что оператор ввёл в UI как код авторизации (`code_input`,
  `aicd_platform.login_input`): платформа его нигде не сохраняет и вырезает из вывода, если CLI его
  отобразил. Это одноразовый код OAuth/PKCE — обменять его может только CLI (ТЗ §1, ADR-0003).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import termios
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Literal

from aicd_cli_adapters.profiles import FORBIDDEN_ENV, ProfileError
from aicd_platform.redaction import redact_text

Provider = Literal["codex", "claude"]

#: Сессия входа не может длиться дольше: device code у обоих провайдеров живёт минутами.
LOGIN_TIMEOUT: Final = timedelta(minutes=10)
#: Сколько ждать завершения процесса после SIGTERM, прежде чем SIGKILL.
TERMINATE_GRACE_SECONDS: Final = 5.0
#: Жёсткий предел на объём прочитанного вывода (защита от «болтливого» CLI).
MAX_OUTPUT_BYTES: Final = 256 * 1024
#: Сколько строк вывода (уже вычищенных) отдавать оператору.
MAX_LINES: Final = 200


@dataclass(frozen=True)
class LoginProfile:
    """Allowlist команды входа провайдера: argv фиксирован кодом, подстановок нет."""

    provider: Provider
    argv: tuple[str, ...]
    env_allowlist: frozenset[str] = frozenset({"HOME", "PATH", "LANG", "TZ", "TERM", "XDG_CONFIG_HOME"})
    #: Подтверждены ли флаги по `--help` установленной версии CLI (cli-auth.md §2, §3).
    confirmed: bool = True


LOGIN_PROFILES: Final[dict[Provider, LoginProfile]] = {
    # `claude auth login --claudeai` — вход по подписке Claude; проверено по `claude --help` 2.1.319.
    "claude": LoginProfile(provider="claude", argv=("auth", "login", "--claudeai")),
    # `codex login --device-auth` — вход учётной записью ChatGPT (подписка) по device code: CLI печатает
    # ссылку и одноразовый код, оператор вводит код в браузере своей машины. Без `--device-auth` CLI
    # поднимает приёмник OAuth на самом хосте исполнения (`localhost:1455`) и печатает ссылку на него —
    # из браузера оператора такая ссылка не открывается. Проверено по `codex login --help` 0.167.0 (P2-01).
    "codex": LoginProfile(provider="codex", argv=("login", "--device-auth")),
}

#: Флаги и переменные, означающие вход по API-ключу вместо подписки: запрещены безусловно (ТЗ §1).
FORBIDDEN_LOGIN_TOKENS: Final = frozenset({"--with-api-key", "--console", "--api-key", "--with-access-token"})

_URL = re.compile(r"https://[\w.-]+(?:/[\w./~%+-]*)?(?:\?[\w=&.%+-]*)?")
# Код устройства: 4-10 символов групп через дефис (WDJB-MJHT) либо один блок 6-10 символов в верхнем регистре.
_CODE = re.compile(r"\b(?:[A-Z0-9]{4,10}(?:-[A-Z0-9]{4,10})+|[A-Z0-9]{6,10})\b")
# Строки, рядом с которыми стоит код: не принимаем за код случайные слова вывода.
_CODE_HINT = re.compile(r"(?i)\b(code|код)\b")
# Приглашение CLI ввести код авторизации: Claude Code печатает «Paste code here if prompted >».
# По нему UI понимает, что нужно показать оператору поле ввода (`ProviderLoginStatus.awaiting_code`).
_CODE_PROMPT = re.compile(r"(?i)\bpaste\b[^\n]{0,40}\bcode\b|вставьте код|введите код авторизации")
#: Строка в выводе вместо самого кода: что именно ввёл оператор, платформа не показывает и не хранит.
CODE_SUBMITTED_NOTICE: Final = "— код авторизации отправлен CLI —"
#: Фрагменты, по которым узнаём успешный вход (оба CLI печатают подтверждение).
_SUCCESS = re.compile(r"(?i)\b(logged in|login successful|successfully logged|вход выполнен|authenticated)\b")
_FAILURE = re.compile(r"(?i)\b(login failed|authentication failed|expired|denied|отказано)\b")

# Управляющие последовательности терминала. `TERM=dumb` их не отключает: Claude Code 2.1.319 печатает
# ссылку входа гиперссылкой OSC-8 (`ESC ]8;;<url> BEL <подпись> ESC ]8;; BEL`), то есть сам адрес лежит
# внутри управляющей последовательности. Поэтому OSC-8 сначала разворачивается в текст (адрес остаётся —
# иначе ссылку входа было бы не найти), и только потом вырезаются остальные последовательности.
_OSC8 = re.compile(r"\x1b\]8;[^;]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


def _plain(text: str) -> str:
    """Текст без управляющих последовательностей; адрес из гиперссылки OSC-8 сохраняется как слово."""
    return _ANSI.sub("", _OSC8.sub(lambda m: f" {m.group(1)} ", text))


#: Токеноподобные фрагменты, которые нельзя показывать и хранить даже частично.
_SECRETS = (
    re.compile(r"(?i)\b(sk|pk)-[a-z0-9]{2,}-[\w-]{8,}"),
    re.compile(r"(?i)\bsk-[\w-]{12,}"),
    re.compile(r"(?i)\b(ey[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,})\b"),  # JWT
    re.compile(r"(?i)([?&](?:code|token|access_token|id_token|refresh_token|state)=)[^\s&]+"),
    re.compile(r"(?i)\b(bearer\s+)[\w.\-+/=]{8,}"),
)


def scrub(text: str) -> str:
    """Убрать из строки вывода управляющие последовательности и всё токеноподобное.

    Применяется к каждой строке вывода CLI до записи в БД, лог и поток оператору. К самой ссылке входа
    (`DeviceChallenge.verification_url`) **не** применяется: см. `sanitize_verification_url`.
    """
    out = _plain(text)
    for pattern in _SECRETS:
        out = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "«скрыто»", out)
    return redact_text(out)


def sanitize_verification_url(url: str) -> str | None:
    """Проверить ссылку входа, не меняя её. `None` — ссылка не годится и оператору не показывается.

    Query-параметры сохраняются целиком намеренно: оператор должен открыть ровно ту ссылку, которую
    напечатал CLI, иначе вход провайдера не состоится. Секрета в ней нет — это публичная часть OAuth/PKCE
    (`code_challenge` по построению публичен, `state` защищает от CSRF, авторизационный код приходит не
    сюда, а самому CLI). Поэтому `scrub()` к ней не применяется: он вырезал бы `state=`/`code=` и сломал
    бы вход. Требования: только https и разумная длина (ограничение столбца — 2048).
    """
    cleaned = _plain(url).strip().rstrip(".,);]")
    if not cleaned.startswith("https://") or len(cleaned) > 2048 or any(c.isspace() for c in cleaned):
        return None
    return cleaned


@dataclass(frozen=True)
class DeviceChallenge:
    """Пара «открыть ссылку — ввести код», которую печатает CLI и показывает UI."""

    verification_url: str
    user_code: str | None


def prompts_for_code(output: str) -> bool:
    """Ждёт ли CLI ввода кода авторизации (по его же выводу)."""
    return bool(_CODE_PROMPT.search(_plain(output)))


def parse_challenge(raw: str) -> DeviceChallenge | None:
    """Найти в накопленном выводе ссылку входа и код устройства. Код необязателен (CLI может его не печатать)."""
    text = _plain(raw)
    found = _URL.search(text)
    if found is None:
        return None
    url = sanitize_verification_url(found.group(0))
    if url is None:
        return None
    code: str | None = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _CODE_HINT.search(line):
            continue
        # Код ищем в строке без URL: иначе в него попадут фрагменты пути. Кроме строки с подсказкой
        # смотрим следующую за ней: Codex CLI 0.167.0 печатает код отдельной строкой под подсказкой
        # («Enter this one-time code» / «8LYC-TYTU1»).
        for candidate_line in lines[index : index + 2]:
            candidate = _CODE.search(_URL.sub(" ", candidate_line))
            if candidate is not None:
                code = candidate.group(0)
                break
        if code is not None:
            break
    return DeviceChallenge(verification_url=url, user_code=code)


LoginState = Literal["PENDING", "CHALLENGE", "AUTHENTICATED", "FAILED", "CANCELLED", "EXPIRED"]
#: Категории отказа — подмножество `errors.CLI_ERROR_CODES`, пригодное для входа.
LoginErrorCategory = Literal["CLI_NOT_INSTALLED", "AUTH_REQUIRED", "TIMEOUT", "PROCESS_FAILED", "CANCELLED"]


@dataclass(frozen=True)
class LoginLine:
    """Строка вывода CLI, уже прошедшая `scrub()`."""

    text: str


@dataclass(frozen=True)
class LoginFinished:
    state: LoginState
    error_category: LoginErrorCategory | None = None


LoginEvent = LoginLine | DeviceChallenge | LoginFinished


@dataclass
class LoginSession:
    """Состояние запущенной сессии входа: строки вывода, найденный challenge и итог."""

    provider: Provider
    state: LoginState = "PENDING"
    challenge: DeviceChallenge | None = None
    error_category: LoginErrorCategory | None = None
    lines: list[str] = field(default_factory=list)


def build_login_argv(profile: LoginProfile, binary_path: str) -> tuple[str, ...]:
    """argv команды входа. Профиль с неподтверждёнными флагами и запрещённые флаги — отказ."""
    if not profile.confirmed:
        raise ProfileError(
            f"команда входа {profile.provider} не сверена с установленным CLI (cli-auth.md §3, P2-01):"
            " оператор должен подтвердить флаги по `--help`"
        )
    argv = (binary_path, *profile.argv)
    forbidden = sorted(set(argv) & FORBIDDEN_LOGIN_TOKENS)
    if forbidden:
        raise ProfileError(f"вход по API-ключу запрещён (ТЗ §1): {', '.join(forbidden)}")
    return argv


def build_login_env(profile: LoginProfile, source: dict[str, str]) -> dict[str, str]:
    """Окружение входа с нуля из allowlist; ключи AI-провайдеров в источнике — отказ в старте."""
    forbidden = sorted(k for k in source if FORBIDDEN_ENV.search(k))
    if forbidden:
        raise ProfileError(f"в окружении входа запрещённые переменные: {', '.join(forbidden)}")
    env = {k: v for k, v in source.items() if k in profile.env_allowlist}
    env.setdefault("TERM", "dumb")  # PTY без управляющих последовательностей: вывод парсится как текст
    return env


async def run_login(
    *,
    provider: Provider,
    binary_path: str,
    env_source: dict[str, str] | None = None,
    timeout: timedelta = LOGIN_TIMEOUT,  # noqa: ASYNC109 — срок жизни device code, не таймаут вызова
    cancelled: Callable[[], Awaitable[bool]] | None = None,
    code_input: Callable[[], Awaitable[str | None]] | None = None,
    poll_seconds: float = 0.2,
    launcher: tuple[str, ...] = (),
) -> AsyncIterator[LoginEvent]:
    """Выполнить вход официального CLI в PTY, отдавая события по мере вывода.

    PTY нужен потому, что оба CLI печатают device code только в интерактивном режиме. `cancelled`
    опрашивается между чтениями вывода — им воркер проверяет отмену оператором в БД.
    Последнее событие — всегда `LoginFinished`.

    В stdin CLI уходит единственное: код авторизации, который оператор ввёл в UI (`code_input` отдаёт
    его воркеру, см. `aicd_platform.login_input`). Он нужен тем CLI, которые завершают вход вводом кода
    из браузера (Claude Code). Платформа его не хранит и вырезает из вывода: CLI может его отобразить.
    """
    profile = LOGIN_PROFILES[provider]
    # `launcher` — argv-префикс одноразового контейнера с томом учётных данных
    # (`container_launch.credential_launcher`); тогда `binary_path` — путь CLI внутри образа.
    argv = (*launcher, *build_login_argv(profile, binary_path))
    env = build_login_env(profile, dict(env_source or {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}))

    master, slave = os.openpty()
    _disable_echo(slave)  # иначе введённый код вернулся бы в выводе CLI и попал в БД и в браузер
    submitted: list[str] = []
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
        )
    except FileNotFoundError:
        os.close(master)
        os.close(slave)
        yield LoginFinished(state="FAILED", error_category="CLI_NOT_INSTALLED")
        return
    os.close(slave)  # владеет только дочерний процесс: иначе чтение master никогда не увидит EOF

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(master, "rb", 0)
    )
    deadline = loop.time() + timeout.total_seconds()
    buffer = ""
    read_bytes = 0
    challenge: DeviceChallenge | None = None
    outcome: LoginFinished | None = None
    try:
        while True:
            if cancelled is not None and await cancelled():
                outcome = LoginFinished(state="CANCELLED", error_category="CANCELLED")
                break
            if code_input is not None:
                submission = await code_input()
                if submission is not None:
                    submitted.append(submission)
                    # `\r` — то же, что видит CLI от клавиши Enter: в каноническом режиме PTY его переводит
                    # в перевод строки, в «сыром» readline CLI сам понимает его как конец ввода.
                    os.write(master, submission.encode() + b"\r")
                    yield LoginLine(text=CODE_SUBMITTED_NOTICE)
            if loop.time() >= deadline:
                outcome = LoginFinished(state="EXPIRED", error_category="TIMEOUT")
                break
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=poll_seconds)
            except TimeoutError:
                continue
            except OSError:  # PTY закрыт вместе с процессом
                chunk = b""
            if not chunk:
                break
            read_bytes += len(chunk)
            if read_bytes > MAX_OUTPUT_BYTES:
                outcome = LoginFinished(state="FAILED", error_category="PROCESS_FAILED")
                break
            text = chunk.decode("utf-8", errors="replace")
            buffer += text
            for raw in text.splitlines():
                line = scrub(_hide(raw, submitted)).strip()
                if line:
                    yield LoginLine(text=line)
            # Пока код не найден — перечитываем накопленный вывод: ссылка и код могут прийти разными
            # чтениями PTY, и оператору нужна вся пара, а не одна ссылка.
            if challenge is None or challenge.user_code is None:
                found = parse_challenge(_hide(buffer, submitted))
                if found is not None and found != challenge:
                    challenge = found
                    yield challenge
        if outcome is None:
            code = await _wait_for_exit(process)
            scrubbed = scrub(_hide(buffer, submitted))
            if code == 0 or _SUCCESS.search(scrubbed):
                outcome = LoginFinished(state="AUTHENTICATED")
            elif _FAILURE.search(scrubbed):
                outcome = LoginFinished(state="FAILED", error_category="AUTH_REQUIRED")
            else:
                outcome = LoginFinished(state="FAILED", error_category="PROCESS_FAILED")
    finally:
        transport.close()
        await _terminate(process)
    yield outcome


def _disable_echo(fd: int) -> None:
    """Выключить эхо PTY: код, записанный в stdin, не должен вернуться в выводе CLI."""
    with contextlib.suppress(termios.error, OSError):
        attributes = termios.tcgetattr(fd)
        attributes[3] &= ~termios.ECHO  # lflag
        termios.tcsetattr(fd, termios.TCSANOW, attributes)


def _hide(text: str, submitted: list[str]) -> str:
    """Вырезать из вывода коды, введённые оператором: CLI мог отобразить их сам, несмотря на эхо."""
    for code in submitted:
        text = text.replace(code, "«скрыто»")
    return text


async def _wait_for_exit(process: asyncio.subprocess.Process) -> int | None:
    """Код выхода, если процесс завершился за grace-период после EOF на PTY."""
    with contextlib.suppress(TimeoutError):
        return await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
    return None


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Снять процесс входа: SIGTERM всей группе, затем SIGKILL. Осиротевший CLI ждал бы до device-code timeout."""
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
