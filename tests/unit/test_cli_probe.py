"""Probe реальной установки CLI (P2-01): версия, help-сканирование, матрица, статус входа.

Проверяется главное свойство: платформа узнаёт о CLI только то, что он печатает сам, и ничего из
его учётных данных. Вход по API-ключу авторизованной установкой не считается (ТЗ §1), а из вывода
status-команды в результат не попадает ничего, кроме статуса и типа плана (ТЗ §7.1).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from aicd_cli_adapters import compat, probe
from aicd_cli_adapters.models import ProbeResult
from aicd_cli_adapters.profiles import ProfileError

CODEX_HELP = """Codex CLI

Usage: codex [OPTIONS] [PROMPT]

Commands:
  exec              Run Codex non-interactively [aliases: e]
  login             Manage login
  logout            Remove stored authentication credentials
"""

CODEX_EXEC_HELP = """Run Codex non-interactively

Commands:
  resume  Resume a previous session by id

Options:
  -m, --model <MODEL>
          Model the agent should use
  -s, --sandbox <SANDBOX_MODE>
          Select the sandbox policy

          [possible values: read-only, workspace-write, danger-full-access]
      --json
          Print events to stdout as JSONL
"""

# `claude auth status --json` печатает e-mail и организацию: в результат probe они попасть не должны.
CLAUDE_AUTH_JSON = json.dumps(
    {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "subscriptionType": "max",
        "email": "operator@example.com",
        "orgName": "Example Org",
    }
)
CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --model <model>                       Model for the session
  --output-format <format>              Output format (only works with --print):
                                        "text", "json", "stream-json"
  -p, --print                           Print response and exit
  -r, --resume [value]                  Resume a conversation
Commands:
  auth                                  Manage authentication
"""


def _stub_cli(tmp_path: Path, name: str, script: str) -> str:
    """Исполняемый скрипт, играющий роль официального CLI (настоящего CLI в CI нет)."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _codex_stub(tmp_path: Path, *, login_status: str = "Logged in using ChatGPT", login_code: int = 0) -> str:
    return _stub_cli(
        tmp_path,
        "codex",
        f"""
case "$1 $2" in
  "--version ") echo "codex-cli 0.167.0" ;;
  "--help ") cat <<'EOF'
{CODEX_HELP}
EOF
  ;;
  "exec --help") cat <<'EOF'
{CODEX_EXEC_HELP}
EOF
  ;;
  "login status") echo "{login_status}"; exit {login_code} ;;
  *) echo "unexpected: $*"; exit 2 ;;
esac
""",
    )


def _claude_stub(tmp_path: Path, auth_json: str = CLAUDE_AUTH_JSON) -> str:
    return _stub_cli(
        tmp_path,
        "claude",
        f"""
case "$1 $2" in
  "--version ") echo "2.1.319 (Claude Code)" ;;
  "--help ") cat <<'EOF'
{CLAUDE_HELP}
EOF
  ;;
  "auth status") echo '{auth_json}' ;;
  *) echo "unexpected: $*"; exit 2 ;;
esac
""",
    )


ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/runner", "LANG": "C.UTF-8"}


@pytest.fixture(autouse=True)
def _empty_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Скрипты-заглушки называют себя версиями настоящих CLI; строки матрицы репозитория к ним не относятся.

    Поведение probe проверяется на пустой матрице, а сама матрица — отдельно (`test_cli_core`).
    """
    monkeypatch.setattr(compat, "load_matrix", lambda *a, **k: ())


async def _probe(provider: str, binary: str | None) -> ProbeResult:
    return await probe.run_probe(
        provider=provider,  # type: ignore[arg-type]
        binary_path=binary,
        installation_id=uuid4(),
        adapter_version="probe-1.0.0",
        env_source=dict(ENV),
    )


# --- allowlist команд probe --------------------------------------------------------------------


def test_probe_commands_are_fixed_by_the_profile() -> None:
    argv = probe.build_probe_argv(probe.PROBE_PROFILES["codex"], "/usr/bin/codex", ("login", "status"))
    assert argv == ("/usr/bin/codex", "login", "status")


def test_unconfirmed_profile_is_not_executed() -> None:
    """Несверённые по `--help` команды probe не исполняются — как и несверённая команда входа."""
    profile = probe.ProbeProfile(
        provider="codex", version_argv=("--version",), help_argv=(), auth_argv=(), confirmed=False
    )
    with pytest.raises(ProfileError, match="не сверены"):
        probe.build_probe_argv(profile, "/usr/bin/codex", ("--version",))


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SOME_TOKEN"])
def test_provider_keys_in_environment_block_probe(key: str) -> None:
    """Ключ в окружении — CLI отчитался бы о входе по API-ключу, запрещённом ТЗ §1."""
    with pytest.raises(ProfileError, match="запрещённые переменные"):
        probe.build_probe_env(probe.PROBE_PROFILES["codex"], {key: "v", "PATH": "/usr/bin"})


def test_probe_env_is_allowlisted() -> None:
    env = probe.build_probe_env(probe.PROBE_PROFILES["codex"], {"PATH": "/usr/bin", "AWS_REGION": "eu"})
    assert env == {"PATH": "/usr/bin", "TERM": "dumb", "NO_COLOR": "1"}


# --- алгоритм probe (§5.2) ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_binary_is_reported_as_not_installed(tmp_path: Path) -> None:
    result = await _probe("codex", str(tmp_path / "absent"))
    assert result.installed is False
    assert [e.code for e in result.errors or []] == ["CLI_NOT_INSTALLED"]


@pytest.mark.asyncio
async def test_codex_probe_reads_version_capabilities_and_auth(tmp_path: Path) -> None:
    result = await _probe("codex", _codex_stub(tmp_path))
    assert (result.installed, result.detected_version, result.version_raw) == (True, "0.167.0", "codex-cli 0.167.0")
    assert result.binary_sha256 is not None
    assert result.auth_status == "AUTHENTICATED"
    caps = result.capabilities
    assert caps.noninteractive_mode and caps.machine_readable_output == "jsonl_stream"
    assert caps.sandbox_modes == ["read_only", "workspace_write", "cli_managed_none"]
    assert caps.resume_session and caps.auth_status_command and caps.model_selection
    # По справке это не определяется: подтверждает только smoke-набор (§5.2 п. 4, §13.2).
    assert caps.cancel_signal_safe is False and caps.token_usage_exposed is False
    assert result.errors is None


@pytest.mark.asyncio
async def test_claude_probe_keeps_identifying_data_out_of_the_result(tmp_path: Path) -> None:
    """В выводе `claude auth status --json` есть e-mail и организация — в БД им не место (ТЗ §7.1)."""
    result = await _probe("claude", _claude_stub(tmp_path))
    assert result.auth_status == "AUTHENTICATED"
    assert result.capabilities.machine_readable_output == "jsonl_stream"
    dumped = result.model_dump_json()
    assert "operator@example.com" not in dumped and "Example Org" not in dumped


@pytest.mark.asyncio
async def test_unknown_version_stays_untested(tmp_path: Path) -> None:
    """Версии нет в матрице — UNTESTED, исполнение решает policy (§5.3)."""
    result = await _probe("codex", _codex_stub(tmp_path))
    assert (result.compatibility, result.compatibility_matrix_entry) == ("UNTESTED", None)


def test_matrix_entry_wins_over_detected_capabilities() -> None:
    """Capabilities подтверждённой версии берутся из строки матрицы, а не из маркеров справки (§5.3)."""
    entries = (
        compat.MatrixEntry(
            id="codex-0.167-test",
            provider="codex",
            version_range=">=0.167.0,<0.168.0",
            status="SUPPORTED",
            capabilities={"cancel_signal_safe": True},
            command_profiles=(),
        ),
    )
    status, entry = compat.evaluate("codex", "codex-cli 0.167.0", entries)
    assert (status, entry.id if entry else None) == ("SUPPORTED", "codex-0.167-test")


# --- статус входа (§7) -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_without_login_requires_auth(tmp_path: Path) -> None:
    binary = _codex_stub(tmp_path, login_status="Not logged in", login_code=1)
    result = await _probe("codex", binary)
    assert result.auth_status == "AUTH_REQUIRED"
    assert any(e.code == "PROCESS_FAILED" for e in result.errors or [])


@pytest.mark.parametrize(
    ("provider", "output"),
    [
        ("codex", "Logged in using an API key"),
        ("claude", json.dumps({"loggedIn": True, "authMethod": "apiKey"})),
    ],
)
def test_api_key_login_is_not_an_authenticated_subscription(provider: str, output: str) -> None:
    """Вход по ключу — биллинг API, а не подписка: такую установку платформа не использует (ТЗ §1)."""
    status, hint, action = probe.parse_auth(provider, 0, output)  # type: ignore[arg-type]
    assert status == "AUTH_REQUIRED"
    assert hint is None and action is not None and "API-ключу" in action


def test_timeout_of_status_command_is_not_an_authenticated_state() -> None:
    assert probe.parse_auth("codex", None, "")[0] == "UNKNOWN"


@pytest.mark.asyncio
async def test_check_auth_reports_plan_type_without_identity(tmp_path: Path) -> None:
    result = await probe.check_auth(
        provider="claude", binary_path=_claude_stub(tmp_path), installation_id=uuid4(), env_source=dict(ENV)
    )
    assert (result.auth_status, result.account_hint, result.method) == ("AUTHENTICATED", "max", "cli_status_command")


@pytest.mark.asyncio
async def test_hanging_cli_is_reported_as_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "PROBE_TIMEOUT_SECONDS", 0.3)
    binary = _stub_cli(tmp_path, "codex", 'sleep 30\necho "never"\n')
    result = await _probe("codex", binary)
    assert result.installed is True
    assert any(e.code == "TIMEOUT" for e in result.errors or [])
    assert result.auth_status in ("UNKNOWN", "AUTH_REQUIRED")


# --- модели, которые называет сам CLI (выбор модели в настройках провайдера) ---------------------

CATALOG = json.dumps(
    {
        "models": [
            {"slug": "gpt-b", "visibility": "list", "priority": 2, "base_instructions": "x" * 1000},
            {"slug": "gpt-hidden", "visibility": "hide", "priority": 1},
            {"slug": "gpt-a", "visibility": "list", "priority": 1},
            {"slug": "--not-a-model", "visibility": "list", "priority": 0},
            {"slug": "gpt-a", "visibility": "list", "priority": 9},
        ]
    }
)


def test_model_catalog_keeps_listed_models_in_cli_order_and_drops_everything_else() -> None:
    """Из каталога берутся только имена видимых моделей; значение, не похожее на имя модели, отбрасывается."""
    assert probe.parse_model_catalog(CATALOG) == ["gpt-a", "gpt-b"]
    for garbage in ("", "not json", "[1, 2]", '{"models": "x"}', "{}"):
        assert probe.parse_model_catalog(garbage) == []


def test_model_aliases_come_from_the_model_flag_help_only() -> None:
    help_text = (
        "Options:\n"
        "  --fallback-model <model>              Enable 'other' fallback\n"
        "  --model <model>                       Model for the current session. Provide\n"
        "                                        an alias for the latest model (e.g.\n"
        "                                        'fable', 'opus', or 'sonnet') or a\n"
        "                                        model's full name.\n"
        "  -n, --name <name>                     Set a 'display' name\n"
    )
    assert probe.parse_model_aliases(help_text) == ["fable", "opus", "sonnet"]
    assert probe.parse_model_aliases(CLAUDE_HELP) == [], "справка без псевдонимов — список пуст, а не выдуман"


@pytest.mark.asyncio
async def test_probe_reports_models_the_cli_names(tmp_path: Path) -> None:
    """Codex печатает каталог командой `debug models`; CLI без неё и без псевдонимов моделей не сообщает."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(CATALOG)
    binary = _stub_cli(
        tmp_path,
        "codex",
        f"""
case "$1 $2" in
  "--version ") echo "codex-cli 0.167.0" ;;
  "--help ") cat <<'EOF'
{CODEX_HELP}
EOF
  ;;
  "exec --help") cat <<'EOF'
{CODEX_EXEC_HELP}
EOF
  ;;
  "login status") echo "Logged in using ChatGPT" ;;
  "debug models") cat {catalog} ;;
  *) echo "unexpected: $*"; exit 2 ;;
esac
""",
    )
    result = await _probe("codex", binary)
    assert [m.root for m in result.capabilities.available_models or []] == ["gpt-a", "gpt-b"]
    assert result.errors is None
    # Старый CLI без `debug models`: probe не падает и моделей не выдумывает.
    older = await _probe("codex", _codex_stub(tmp_path))
    assert older.capabilities.available_models is None and older.errors is None
