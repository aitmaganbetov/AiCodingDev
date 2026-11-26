"""Адаптеры официальных CLI (Claude Code, Codex): профили команд и разбор машиночитаемого вывода.

Строки событий в фикстурах сняты с установленных версий (claude 2.1.319 `--output-format stream-json`,
codex-cli 0.167.0 `exec --json`), а не сочинены: парсер проверяется против того формата, который CLI
действительно печатает.

Главные свойства, которые здесь закрепляются:

* в argv нет ни байта пользовательского текста — задача доставляется файлом (CLI_ADAPTER_CONTRACT §8.2);
* вариативные флаги (`--allowedTools`, `--add-dir`) не поглощают позиционный аргумент: между последним
  из них и промптом стоит невариативный флаг, иначе CLI теряет задачу;
* служебные события обоих CLI разбираются, а не считаются мусором: доля `unparsed` > 20 % дала бы
  `OUTPUT_PARSE_ERROR` на каждом исполнении;
* рассуждения модели (`thinking`) скрыты и не попадают ни в ленту, ни в raw log.
"""

from __future__ import annotations

import json

import pytest

from aicd_cli_adapters.profiles import build_argv
from aicd_cli_adapters.real.adapter import (
    BOOTSTRAP,
    CLAUDE_TOOLS_READONLY,
    CLAUDE_TOOLS_WRITE,
    ModelError,
    checked_model,
    claude_profile,
    codex_profile,
    codex_sandbox_mode,
)
from aicd_cli_adapters.real.parsers import ClaudeStreamParser, CodexJsonParser

# --- фикстуры живого вывода --------------------------------------------------------------------

CLAUDE_LINES = [
    '{"type":"system","subtype":"init","session_id":"s1"}',
    '{"type":"rate_limit_event","rate_limit":{"status":"allowed"}}',
    '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"секрет рассуждений","signature":"x"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Write",'
    '"input":{"file_path":"/workspace/SMOKE.txt","content":"ok"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"File created"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"готово"}]}}',
    '{"type":"result","subtype":"success","is_error":false,"result":"готово",'
    '"usage":{"input_tokens":4,"cache_creation_input_tokens":30737,"cache_read_input_tokens":76815,'
    '"output_tokens":174}}',
]

CODEX_LINES = [
    '{"type":"thread.started","thread_id":"01a1"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"Создам файл."}}',
    '{"type":"item.started","item":{"id":"i1","type":"command_execution",'
    '"command":"/bin/bash -lc \'printf ok > hello.txt\'","exit_code":null,"status":"in_progress"}}',
    '{"type":"item.completed","item":{"id":"i1","type":"command_execution",'
    '"command":"/bin/bash -lc \'printf ok > hello.txt\'","exit_code":0,"status":"completed"}}',
    '{"type":"turn.completed","usage":{"input_tokens":30115,"cached_input_tokens":24576,"output_tokens":54}}',
]


def kinds(parser: object, lines: list[str]) -> list[str]:
    return [parser.parse(line).kind for line in lines]  # type: ignore[attr-defined]


# --- разбор вывода -----------------------------------------------------------------------------


def test_claude_output_fully_parsed() -> None:
    """Ни одна строка живого потока не считается мусором, и итог ровно один."""
    parsed = kinds(ClaudeStreamParser(), CLAUDE_LINES)
    assert "unparsed" not in parsed
    assert parsed.count("result") == 1


def test_codex_output_fully_parsed() -> None:
    parsed = kinds(CodexJsonParser(), CODEX_LINES)
    assert "unparsed" not in parsed
    assert parsed.count("result") == 1


def test_claude_thinking_is_hidden() -> None:
    """Рассуждения модели не показываются оператору и не пишутся в raw log."""
    line = ClaudeStreamParser().parse(CLAUDE_LINES[2])
    assert line.kind == "message"
    assert line.data.get("hidden") is True
    assert "секрет рассуждений" not in json.dumps(line.data, ensure_ascii=False)


def test_claude_tool_use_mapped_to_platform_kinds() -> None:
    line = ClaudeStreamParser().parse(CLAUDE_LINES[3])
    assert line.kind == "tool"
    assert line.data == {"tool_kind": "file_write", "phase": "completed", "target": "/workspace/SMOKE.txt"}


def test_claude_usage_counts_cache_as_input() -> None:
    """Кэш оплачивается, поэтому входит во входные токены, иначе расход занижен в разы."""
    usage = ClaudeStreamParser().parse(CLAUDE_LINES[-1]).data["usage"]
    assert usage == {"input_tokens": 4 + 30737 + 76815, "output_tokens": 174}


def test_claude_failed_tool_result_is_visible() -> None:
    line = ClaudeStreamParser().parse(
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1",'
        '"is_error":true,"content":"EACCES"}]}}'
    )
    assert line.kind == "tool"
    assert line.data["phase"] == "failed"


def test_codex_command_phases() -> None:
    parser = CodexJsonParser()
    started = parser.parse(CODEX_LINES[3])
    done = parser.parse(CODEX_LINES[4])
    assert (started.kind, started.data["phase"]) == ("tool", "started")
    assert (done.kind, done.data["phase"], done.data["tool_kind"]) == ("tool", "completed", "command")


def test_codex_failed_command_marked_failed() -> None:
    line = CodexJsonParser().parse(
        '{"type":"item.completed","item":{"id":"i2","type":"command_execution","command":"false","exit_code":1}}'
    )
    assert line.data["phase"] == "failed"


def test_codex_turn_failed_still_yields_result() -> None:
    """Без итоговой строки исполнение было бы `OUTPUT_PARSE_ERROR` вместо настоящей причины отказа."""
    line = CodexJsonParser().parse('{"type":"turn.failed","error":{"message":"rate limit"}}')
    assert line.kind == "result"
    assert line.data["usage"] is None


@pytest.mark.parametrize("parser", [ClaudeStreamParser(), CodexJsonParser()])
def test_garbage_line_is_unparsed(parser: object) -> None:
    assert parser.parse("не json").kind == "unparsed"  # type: ignore[attr-defined]
    assert parser.parse('{"type":"невиданное"}').kind == "unparsed"  # type: ignore[attr-defined]


# --- профили команд ----------------------------------------------------------------------------


def test_profiles_are_confirmed() -> None:
    """Неподтверждённый флаг (`<candidate-…>`) запретил бы исполнение в `build_argv`."""
    assert claude_profile().confirmed
    assert codex_profile().confirmed


def test_claude_argv_keeps_prompt_out_of_command_line() -> None:
    prompt_path = "/aicd/input/prompt.md"
    argv = build_argv(
        claude_profile(),
        {
            "binary": "/usr/local/bin/claude",
            "allowed_tools": CLAUDE_TOOLS_WRITE,
            "prompt_dir": "/aicd/input",
            "bootstrap": BOOTSTRAP.format(path=prompt_path),
        },
    )
    assert argv[0] == "/usr/local/bin/claude"
    assert "--print" in argv
    # В argv — только путь к файлу: ни объектива, ни критериев, ни контекста.
    assert argv[-1].endswith("контракту, описанному в инструкции.")
    assert prompt_path in argv[-1]


def test_claude_variadic_flags_do_not_swallow_prompt() -> None:
    """Между последним вариативным флагом и позиционным промптом обязателен невариативный флаг."""
    argv = build_argv(
        claude_profile(),
        {
            "binary": "claude",
            "allowed_tools": CLAUDE_TOOLS_WRITE,
            "prompt_dir": "/aicd/input",
            "bootstrap": "задача",
        },
    )
    for variadic in ("--allowedTools", "--add-dir"):
        following = argv[argv.index(variadic) + 2]
        assert following.startswith("--"), f"{variadic} поглотит следующий аргумент"
    assert argv[-2] == "acceptEdits"  # невариативное значение перед промптом


def test_readonly_role_keeps_write_for_result_file() -> None:
    """Запись запрещает ro-mount worktree, а не allowlist: `.aicd/result.json` писать всё равно нужно."""
    assert "Write" in CLAUDE_TOOLS_READONLY
    assert "Bash" not in CLAUDE_TOOLS_READONLY
    assert "Bash" in CLAUDE_TOOLS_WRITE


def test_codex_sandbox_mode_is_a_placeholder() -> None:
    argv = build_argv(codex_profile(), {"binary": "codex", "sandbox_mode": "read-only", "bootstrap": "задача"})
    assert argv[:3] == ("codex", "exec", "--json")
    assert "read-only" in argv
    assert argv[-1] == "задача"


def test_codex_inner_sandbox_disabled_only_inside_container() -> None:
    """В контейнере bubblewrap Codex не работает (нет user namespaces), и границу держит контейнер.

    Без контейнера песочница Codex — единственная граница, поэтому выключать её нельзя.
    """
    assert codex_sandbox_mode(read_only=False, in_container=True) == "danger-full-access"
    assert codex_sandbox_mode(read_only=True, in_container=True) == "danger-full-access"
    assert codex_sandbox_mode(read_only=False, in_container=False) == "workspace-write"
    assert codex_sandbox_mode(read_only=True, in_container=False) == "read-only"


# --- выбор модели -------------------------------------------------------------------------------


def test_model_is_passed_to_both_clis_only_when_chosen() -> None:
    """Без выбора флага нет вовсе — действует модель CLI по умолчанию, а не пустое значение."""
    assert "--model" not in claude_profile().argv_template
    assert "--model" not in codex_profile().argv_template
    claude = build_argv(
        claude_profile(with_model=True),
        {
            "binary": "claude",
            "allowed_tools": CLAUDE_TOOLS_WRITE,
            "prompt_dir": "/aicd/input",
            "bootstrap": "задача",
            "model": "opus",
        },
    )
    assert claude[claude.index("--model") + 1] == "opus"
    assert claude[-1] == "задача" and claude[-2] == "acceptEdits", "порядок вокруг промпта не изменился"
    codex = build_argv(
        codex_profile(with_model=True),
        {"binary": "codex", "sandbox_mode": "read-only", "bootstrap": "задача", "model": "gpt-5-codex"},
    )
    assert codex[:4] == ("codex", "exec", "--model", "gpt-5-codex")
    assert codex[-1] == "задача"


@pytest.mark.parametrize("model", ["opus", "sonnet", "claude-opus-4-5", "gpt-5.1-codex", "opus[1m]", "a:b_c.d"])
def test_model_names_cli_understands_are_accepted(model: str) -> None:
    assert checked_model(model) == model


@pytest.mark.parametrize(
    "model",
    ["--dangerously-skip-permissions", "-m", "opus --verbose", "opus\nx", "", "a/b", "$(id)", "x" * 101, "модель"],
)
def test_model_cannot_become_a_flag_or_free_text(model: str) -> None:
    """Имя модели — единственное значение argv не от gateway: флагом или текстом оно стать не должно."""
    with pytest.raises(ModelError):
        checked_model(model)


def test_no_model_means_none() -> None:
    assert checked_model(None) is None
