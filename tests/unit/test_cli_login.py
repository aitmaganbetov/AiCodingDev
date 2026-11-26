"""Интерактивный вход CLI из UI (P2-01): allowlist команды, вычистка вывода, разбор device code, PTY-сессия.

Проверяется главное свойство: платформа не получает учётных данных. Вход по API-ключу невозможен ни флагом,
ни переменной окружения; всё токеноподобное в выводе CLI вырезается до того, как попадёт в БД или браузер.
"""

from __future__ import annotations

import asyncio
import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from aicd_cli_adapters import login
from aicd_cli_adapters.profiles import ProfileError
from aicd_platform import login_input


def _stub_cli(tmp_path: Path, script: str, name: str = "claude") -> str:
    """Исполняемый скрипт, играющий роль официального CLI (настоящего CLI в CI нет)."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# --- allowlist команды входа -------------------------------------------------------------------


def test_claude_profile_is_subscription_login() -> None:
    argv = login.build_login_argv(login.LOGIN_PROFILES["claude"], "/usr/bin/claude")
    assert argv == ("/usr/bin/claude", "auth", "login", "--claudeai")


def test_codex_profile_is_device_code_subscription_login() -> None:
    """`--device-auth`: ссылку и код оператор открывает у себя, а не на хосте исполнения (cli-auth.md §3)."""
    argv = login.build_login_argv(login.LOGIN_PROFILES["codex"], "/usr/bin/codex")
    assert argv == ("/usr/bin/codex", "login", "--device-auth")


def test_unconfirmed_profile_is_not_executed() -> None:
    """Профиль с несверёнными по `--help` флагами не исполняется, пока оператор их не подтвердит (P2-01)."""
    profile = login.LoginProfile(provider="codex", argv=("login",), confirmed=False)
    with pytest.raises(ProfileError, match="не сверена"):
        login.build_login_argv(profile, "/usr/bin/codex")


@pytest.mark.parametrize("flag", ["--with-api-key", "--console", "--api-key", "--with-access-token"])
def test_api_key_login_flags_are_rejected(flag: str) -> None:
    """Вход по ключу/токену запрещён ТЗ §1: профиль с таким флагом не собирается ни при каких условиях."""
    profile = login.LoginProfile(provider="claude", argv=("auth", "login", flag))
    with pytest.raises(ProfileError, match="API-ключу запрещён"):
        login.build_login_argv(profile, "/usr/bin/claude")


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SOME_TOKEN", "X_SECRET"])
def test_provider_keys_in_environment_block_login(key: str) -> None:
    with pytest.raises(ProfileError, match="запрещённые переменные"):
        login.build_login_env(login.LOGIN_PROFILES["claude"], {key: "v", "PATH": "/usr/bin"})


def test_login_env_is_allowlisted() -> None:
    env = login.build_login_env(
        login.LOGIN_PROFILES["claude"], {"PATH": "/usr/bin", "HOME": "/home/runner", "AWS_REGION": "eu"}
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/home/runner", "TERM": "dumb"}


# --- вычистка вывода --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "token: sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZ",
        "key sk-proj-abcdefghijklmnopqrstuvwxyz",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        "Authorization: Bearer abcdefghijklmnop",
    ],
)
def test_scrub_removes_token_like_fragments(raw: str) -> None:
    out = login.scrub(raw)
    assert "«скрыто»" in out or "[redacted]" in out.lower()
    for secret in ("sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZ", "abcdefghijklmnopqrstuvwxyz", "dBjftJeZ4CVPmB92K27uhbUJU1p1r"):
        assert secret not in out


def test_scrub_keeps_verification_url_but_drops_code_parameter() -> None:
    out = login.scrub("Open https://claude.ai/oauth/authorize?state=abc123secret&scope=user")
    assert "https://claude.ai/oauth/authorize" in out
    assert "abc123secret" not in out


def test_scrub_keeps_operator_instructions() -> None:
    assert login.scrub("Enter code WDJB-MJHT at https://claude.ai/device") == (
        "Enter code WDJB-MJHT at https://claude.ai/device"
    )


# --- разбор device code -----------------------------------------------------------------------


def test_parse_challenge_finds_url_and_code() -> None:
    challenge = login.parse_challenge(
        "Visit https://claude.ai/device to continue.\nEnter the code: WDJB-MJHT\nWaiting...\n"
    )
    assert challenge == login.DeviceChallenge(verification_url="https://claude.ai/device", user_code="WDJB-MJHT")


# Так Codex CLI 0.167.0 печатает device code: подсказка и сам код — на разных строках.
CODEX_DEVICE_AUTH_OUTPUT = (
    "Follow these steps to sign in with ChatGPT using device code authorization:\r\n"
    "\r\n"
    "1. Open this link in your browser and sign in to your account\r\n"
    "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\r\n"
    "\r\n"
    "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\r\n"
    "   \x1b[94m8LYC-TYTU1\x1b[0m\r\n"
)


def test_parse_challenge_reads_code_printed_under_the_hint() -> None:
    """Codex печатает код строкой ниже подсказки — без него оператору нечего ввести в браузере."""
    challenge = login.parse_challenge(CODEX_DEVICE_AUTH_OUTPUT)
    assert challenge == login.DeviceChallenge(
        verification_url="https://auth.openai.com/codex/device", user_code="8LYC-TYTU1"
    )


def test_parse_challenge_without_code() -> None:
    challenge = login.parse_challenge("Opening https://auth.openai.com/activate in your browser\n")
    assert challenge is not None
    assert challenge.verification_url == "https://auth.openai.com/activate"
    assert challenge.user_code is None


# Так Claude Code 2.1.319 печатает ссылку входа: адрес лежит внутри гиперссылки OSC-8.
OSC8_LOGIN_LINE = (
    "If the browser didn't open, visit: "
    "\x1b]8;;https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&state=LYqBZuzDg8\x07"
    "Sign in\x1b]8;;\x07\x1b[0m"
)


def test_parse_challenge_reads_url_from_osc8_hyperlink() -> None:
    """Адрес внутри OSC-8 нельзя просто вырезать вместе с управляющей последовательностью."""
    challenge = login.parse_challenge(OSC8_LOGIN_LINE)
    assert challenge is not None
    assert challenge.verification_url == (
        "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&state=LYqBZuzDg8"
    )


def test_verification_url_keeps_its_query_intact() -> None:
    """Ссылку входа нельзя «вычищать»: оператор должен открыть ровно её, иначе вход не состоится.

    `code_challenge` публичен по построению PKCE, `state` защищает от CSRF, авторизационный код приходит
    самому CLI, а не в эту ссылку — секрета в query нет.
    """
    url = "https://claude.com/cai/oauth/authorize?code=true&code_challenge=7r2Avt&state=LYqBZu"
    assert login.sanitize_verification_url(url) == url


@pytest.mark.parametrize("bad", ["http://claude.com/device", "ftp://x/y", "not a url", "https://x/" + "a" * 2100])
def test_sanitize_verification_url_rejects_unusable(bad: str) -> None:
    assert login.sanitize_verification_url(bad) is None


def test_scrub_removes_terminal_control_sequences() -> None:
    out = login.scrub(OSC8_LOGIN_LINE)
    assert "\x1b" not in out
    assert "]8;;" not in out
    assert "If the browser didn't open" in out


def test_parse_challenge_without_url_is_none() -> None:
    assert login.parse_challenge("Logging in...\nPlease wait\n") is None


def test_parse_challenge_does_not_take_url_fragments_as_code() -> None:
    challenge = login.parse_challenge("code: https://claude.ai/DEVICE/ABCDEF\n")
    assert challenge is not None
    assert challenge.user_code is None


# --- сессия входа в PTY -----------------------------------------------------------------------


async def _collect(**kwargs: object) -> list[login.LoginEvent]:
    return [event async for event in login.run_login(**kwargs)]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_missing_cli_reports_not_installed(tmp_path: Path) -> None:
    events = await _collect(provider="claude", binary_path=str(tmp_path / "absent"))
    assert events == [login.LoginFinished(state="FAILED", error_category="CLI_NOT_INSTALLED")]


@pytest.mark.asyncio
async def test_successful_login_yields_challenge_then_authenticated(tmp_path: Path) -> None:
    binary = _stub_cli(
        tmp_path,
        'echo "Visit https://claude.ai/device and enter code: WDJB-MJHT"\necho "Logged in as owner"\nexit 0\n',
    )
    events = await _collect(provider="claude", binary_path=binary)
    challenges = [e for e in events if isinstance(e, login.DeviceChallenge)]
    assert challenges == [login.DeviceChallenge(verification_url="https://claude.ai/device", user_code="WDJB-MJHT")]
    assert events[-1] == login.LoginFinished(state="AUTHENTICATED")


@pytest.mark.asyncio
async def test_code_printed_after_the_link_still_reaches_the_operator(tmp_path: Path) -> None:
    """Ссылка и код могут прийти разными чтениями PTY: без второй пары оператору нечего ввести."""
    binary = _stub_cli(
        tmp_path,
        'echo "Open https://auth.openai.com/codex/device"\nsleep 0.4\n'
        'echo "Enter this one-time code"\necho "8LYC-TYTU1"\n',
        name="codex",
    )
    events = await _collect(provider="codex", binary_path=binary)
    challenges = [e for e in events if isinstance(e, login.DeviceChallenge)]
    assert [c.user_code for c in challenges] == [None, "8LYC-TYTU1"]


@pytest.mark.asyncio
async def test_failed_login_is_reported_as_auth_required(tmp_path: Path) -> None:
    binary = _stub_cli(tmp_path, 'echo "login failed: device code expired"\nexit 1\n')
    events = await _collect(provider="claude", binary_path=binary)
    assert events[-1] == login.LoginFinished(state="FAILED", error_category="AUTH_REQUIRED")


@pytest.mark.asyncio
async def test_token_in_cli_output_never_leaves_the_module(tmp_path: Path) -> None:
    """Если CLI напечатает токен, оператору и в БД уходит уже вычищенная строка."""
    binary = _stub_cli(tmp_path, 'echo "saved credential sk-ant-api03-SUPERSECRETVALUE0001"\nexit 0\n')
    events = await _collect(provider="claude", binary_path=binary)
    text = "\n".join(e.text for e in events if isinstance(e, login.LoginLine))
    assert "SUPERSECRETVALUE0001" not in text
    assert "«скрыто»" in text


@pytest.mark.asyncio
async def test_cancel_stops_the_session_and_kills_the_cli(tmp_path: Path) -> None:
    binary = _stub_cli(tmp_path, 'echo "Visit https://claude.ai/device"\nsleep 60\n')
    stop = False

    async def cancelled() -> bool:
        return stop

    events: list[login.LoginEvent] = []
    stream = login.run_login(provider="claude", binary_path=binary, cancelled=cancelled)
    async for event in stream:
        events.append(event)
        if isinstance(event, login.DeviceChallenge):
            stop = True
    assert events[-1] == login.LoginFinished(state="CANCELLED", error_category="CANCELLED")


@pytest.mark.asyncio
async def test_timeout_expires_the_session(tmp_path: Path) -> None:
    binary = _stub_cli(tmp_path, "sleep 60\n")
    events = await _collect(
        provider="claude", binary_path=binary, timeout=timedelta(milliseconds=300), poll_seconds=0.05
    )
    assert events[-1] == login.LoginFinished(state="EXPIRED", error_category="TIMEOUT")


def _processes_matching(needle: str) -> list[str]:
    """pid процессов, в чьей командной строке есть `needle` (без pathlib: вызывается из async-теста)."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read()
        except OSError:
            continue
        if needle.encode() in cmdline:
            found.append(pid)
    return found


@pytest.mark.asyncio
async def test_session_does_not_leave_the_cli_process_running(tmp_path: Path) -> None:
    """После выхода из потока событий процесс CLI снят: иначе он держал бы подписку до device-code timeout."""
    marker = tmp_path / "alive"
    binary = _stub_cli(tmp_path, f'echo "https://claude.ai/device"\ntouch {marker}\nsleep 60\n')
    await _collect(provider="claude", binary_path=binary, timeout=timedelta(milliseconds=300), poll_seconds=0.05)
    await asyncio.sleep(0.2)
    assert await asyncio.to_thread(marker.exists)  # скрипт успел стартовать
    assert not await asyncio.to_thread(_processes_matching, binary), "процесс входа остался жив"


# --- payload события --------------------------------------------------------------------------


# --- код авторизации из UI в stdin CLI (login_input) --------------------------------------------


def test_cli_prompt_for_code_is_recognized() -> None:
    """По этой строке UI понимает, что нужно показать поле ввода (Claude Code 2.1.319)."""
    assert login.prompts_for_code("Paste code here if prompted > ") is True
    assert login.prompts_for_code("Enter the code: WDJB-MJHT") is False


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("", "пустым"),
        ("   ", "пустым"),
        ("a" * (login_input.MAX_CODE_LENGTH + 1), "длиннее"),
        ("code\nrm -rf /", "управляющие"),
        ("code\x00", "управляющие"),
    ],
)
def test_unusable_codes_are_rejected(bad: str, reason: str) -> None:
    """В stdin CLI уходит ровно одна строка: перевод строки и управляющие символы недопустимы."""
    with pytest.raises(ValueError, match=reason):
        login_input.normalize(bad)


def test_code_is_trimmed_but_not_interpreted() -> None:
    assert login_input.normalize("  ABCD-1234  ") == "ABCD-1234"


@pytest.mark.asyncio
async def test_operator_code_reaches_the_cli_and_never_returns_in_output(tmp_path: Path) -> None:
    """Код оператора уходит в stdin CLI, а из вывода вырезается: в БД и браузер он попасть не должен."""
    binary = _stub_cli(
        tmp_path,
        'printf "Paste code here if prompted > "\nread value\necho "received $value"\necho "Logged in"\nexit 0\n',
    )
    codes = ["ABCD-1234-EFGH"]

    async def code_input() -> str | None:
        return codes.pop() if codes else None

    events = await _collect(provider="claude", binary_path=binary, code_input=code_input)
    lines = [e.text for e in events if isinstance(e, login.LoginLine)]
    assert login.CODE_SUBMITTED_NOTICE in lines  # оператору видно, что код ушёл
    assert any("received" in line for line in lines)  # CLI его прочитал
    assert not any("ABCD-1234-EFGH" in line for line in lines)  # но сам код в выводе не показывается
    assert events[-1] == login.LoginFinished(state="AUTHENTICATED")


@pytest.mark.asyncio
async def test_login_without_code_input_works_as_before(tmp_path: Path) -> None:
    binary = _stub_cli(tmp_path, 'echo "Visit https://claude.ai/device and enter code: WDJB-MJHT"\nexit 0\n')
    events = await _collect(provider="claude", binary_path=binary)
    assert events[-1] == login.LoginFinished(state="AUTHENTICATED")


def test_payload_key_survives_redaction() -> None:
    """Ключ идентификатора в payload не должен содержать `session`.

    `aicd_platform.redaction` затирает любой ключ с подстрокой `session` (правило для сессий
    пользователей), и worker-exec получил бы `[REDACTED]` вместо идентификатора: сессия входа навсегда
    осталась бы `PENDING`. Эта ошибка была допущена и исправлена — тест её фиксирует.
    """
    from aicd_cli_adapters.login_registry import PAYLOAD_ID_KEY
    from aicd_platform.redaction import redact_value

    payload = {PAYLOAD_ID_KEY: "01a1c06f-017f-753f-b9ba-bca36ffadd40", "provider": "claude"}
    assert redact_value(payload) == payload
    assert "session" not in PAYLOAD_ID_KEY
