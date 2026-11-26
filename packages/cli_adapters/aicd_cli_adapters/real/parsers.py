"""Парсеры машиночитаемого вывода официальных CLI (CLI_ADAPTER_CONTRACT §11).

Каждая строка stdout отображается в `ParsedLine`:

* `message` — текст агента оператору; `hidden=True` — рассуждения модели и служебные события,
  они не попадают ни в ленту, ни в raw log (см. `subprocess_adapter._hidden`);
* `tool` — событие инструмента (`tool_kind`/`phase`/`target` из allowlist платформы);
* `result` — итоговая строка исполнения: из неё берётся `usage`. Claims здесь **не** берутся:
  итог агента платформа читает из `.aicd/result.json` (ACP §1, RISK-04), вывод CLI вторичен.

Неизвестный тип события — `unparsed`: такие строки считаются ошибками разбора, и при доле > 20 %
исполнение завершается `OUTPUT_PARSE_ERROR`. Поэтому служебные события обоих CLI перечислены явно.

Схемы сняты с установленных версий (claude 2.1.319 `--output-format stream-json`,
codex-cli 0.167.0 `exec --json`), а не из документации.
"""

from __future__ import annotations

import json
from typing import Any, Final

from aicd_cli_adapters.subprocess_adapter import ParsedLine

_HIDDEN: Final = ParsedLine("message", {"hidden": True})

# --- Claude Code ------------------------------------------------------------------------------

#: Имя инструмента Claude → `tool_kind` платформы (`subprocess_adapter._TOOL_KINDS`).
CLAUDE_TOOL_KINDS: Final[dict[str, str]] = {
    "Read": "file_read",
    "NotebookRead": "file_read",
    "Write": "file_write",
    "Edit": "file_write",
    "NotebookEdit": "file_write",
    "MultiEdit": "file_patch",
    "ApplyPatch": "file_patch",
    "Bash": "command",
    "BashOutput": "command",
    "KillShell": "command",
    "Glob": "search",
    "Grep": "search",
    "WebFetch": "network",
    "WebSearch": "network",
}
#: Поля `input`, пригодные как `target` события инструмента (порядок — приоритет).
_TARGET_KEYS: Final = ("file_path", "notebook_path", "path", "command", "pattern", "url", "query")
#: Служебные события потока: показывать нечего, но и ошибкой разбора они не являются.
CLAUDE_SILENT_TYPES: Final = frozenset({"system", "rate_limit_event", "stream_event", "control_response"})


def _target(payload: dict[str, Any]) -> str:
    for key in _TARGET_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def claude_usage(raw: object) -> dict[str, int] | None:
    """`usage` итогового события → токены контракта. Кэш оплачивается, поэтому входит во входные."""
    if not isinstance(raw, dict):
        return None
    inp, out = raw.get("input_tokens"), raw.get("output_tokens")
    if not isinstance(inp, int) or not isinstance(out, int):
        return None
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        extra = raw.get(key)
        if isinstance(extra, int) and extra > 0:
            inp += extra
    return {"input_tokens": inp, "output_tokens": out}


class ClaudeStreamParser:
    """`claude -p --output-format stream-json --verbose`: по JSON-объекту на строку."""

    def __init__(self) -> None:
        #: Последнее событие `rate_limit_event` потока: лимиты подписки, как их сообщил сам CLI
        #: (`limits.from_claude_event`). В ленту и raw log оно не идёт — это не вывод агента.
        self.rate_limit_event: dict[str, Any] | None = None

    def parse(self, line: str) -> ParsedLine:
        obj = _loads(line)
        if obj is None:
            return ParsedLine("unparsed")
        kind = obj.get("type")
        if kind == "rate_limit_event":
            self.rate_limit_event = obj
            return _HIDDEN
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "user":
            return self._user(obj)
        if kind == "result":
            return ParsedLine("result", {"claims": [], "usage": claude_usage(obj.get("usage"))})
        if kind in CLAUDE_SILENT_TYPES:
            return _HIDDEN
        return ParsedLine("unparsed")

    @staticmethod
    def _assistant(obj: dict[str, Any]) -> ParsedLine:
        blocks = _blocks(obj)
        # Вызов инструмента важнее текста: он единственный несёт имя и цель.
        for block in blocks:
            if block.get("type") == "tool_use":
                raw = block.get("input")
                payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
                return ParsedLine(
                    "tool",
                    {
                        "tool_kind": CLAUDE_TOOL_KINDS.get(str(block.get("name") or ""), "other"),
                        "phase": "completed",
                        "target": _target(payload),
                    },
                )
        texts = [b.get("text") for b in blocks if b.get("type") == "text"]
        joined = "\n".join(t for t in texts if isinstance(t, str) and t)
        if joined:
            return ParsedLine("message", {"text": joined})
        return _HIDDEN  # thinking и прочие скрытые блоки

    @staticmethod
    def _user(obj: dict[str, Any]) -> ParsedLine:
        # Результаты инструментов дублируют уже показанный вызов; видимым делаем только отказ.
        for block in _blocks(obj):
            if block.get("type") == "tool_result" and block.get("is_error"):
                return ParsedLine("tool", {"tool_kind": "other", "phase": "failed", "target": ""})
        return _HIDDEN


# --- Codex ------------------------------------------------------------------------------------

#: Тип элемента Codex (`item.type`) → `tool_kind` платформы.
CODEX_ITEM_KINDS: Final[dict[str, str]] = {
    "command_execution": "command",
    "file_change": "file_write",
    "patch_apply": "file_patch",
    "web_search": "network",
    "mcp_tool_call": "other",
    "todo_list": "other",
}
CODEX_SILENT_TYPES: Final = frozenset({"thread.started", "turn.started", "item.updated"})


def codex_usage(raw: object) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    inp, out = raw.get("input_tokens"), raw.get("output_tokens")
    if not isinstance(inp, int) or not isinstance(out, int):
        return None
    return {"input_tokens": inp, "output_tokens": out}


class CodexJsonParser:
    """`codex exec --json`: по JSON-объекту на строку, события thread/turn/item."""

    def parse(self, line: str) -> ParsedLine:
        obj = _loads(line)
        if obj is None:
            return ParsedLine("unparsed")
        kind = obj.get("type")
        if kind in ("item.started", "item.completed"):
            return self._item(kind, obj.get("item"))
        if kind == "turn.completed":
            return ParsedLine("result", {"claims": [], "usage": codex_usage(obj.get("usage"))})
        if kind in ("turn.failed", "error"):
            # Итог есть (иначе OUTPUT_PARSE_ERROR), но usage недоступен; причина — в stderr/сигнатурах.
            return ParsedLine("result", {"claims": [], "usage": None})
        if kind in CODEX_SILENT_TYPES:
            return _HIDDEN
        return ParsedLine("unparsed")

    @staticmethod
    def _item(event: str, raw: object) -> ParsedLine:
        item = raw if isinstance(raw, dict) else {}
        item_type = str(item.get("type") or "")
        if item_type == "agent_message":
            if event != "item.completed":
                return _HIDDEN  # текст приходит целиком в item.completed
            text = item.get("text")
            return ParsedLine("message", {"text": text}) if isinstance(text, str) and text else _HIDDEN
        if item_type in ("reasoning", "todo_list"):
            return _HIDDEN
        exit_code = item.get("exit_code")
        if event == "item.started":
            phase = "started"
        elif isinstance(exit_code, int) and exit_code != 0:
            phase = "failed"
        else:
            phase = "completed"
        return ParsedLine(
            "tool",
            {
                "tool_kind": CODEX_ITEM_KINDS.get(item_type, "other"),
                "phase": phase,
                "target": _target(item) or str(item.get("command") or ""),
            },
        )


def _loads(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    content = (obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]
