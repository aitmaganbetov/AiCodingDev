"""Allowlist профилей команд и сборка argv/env (CLI_ADAPTER_CONTRACT §8).

Профиль поставляется с кодом адаптера; запрос не может передать флаги. Значения плейсхолдеров —
только пути, сгенерированные gateway. Токены `<candidate-…>` — неподтверждённые флаги реальных CLI:
такой профиль не исполняется, пока probe (P2-01) не заменит их подтверждёнными значениями.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

_PLACEHOLDER = re.compile(r"^\{([a-z_]+)\}$")
# Ключи AI API не должны попадать в окружение CLI (ТЗ §1, CLI_ADAPTER_CONTRACT §7).
FORBIDDEN_ENV = re.compile(r"(?i)(^|_)(API_?KEY|TOKEN|SECRET)$|^(OPENAI|ANTHROPIC|AZURE_OPENAI|GOOGLE_API)_")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class CommandProfile:
    id: str
    provider: Literal["codex", "claude"]
    argv_template: tuple[str, ...]
    allowed_placeholders: frozenset[str]
    requires_capabilities: frozenset[str] = frozenset()
    prompt_delivery: Literal["stdin", "file"] = "file"
    write_scope: Literal["worktree", "none"] = "worktree"
    network: Literal["none", "restricted"] = "restricted"
    env_allowlist: frozenset[str] = field(default_factory=lambda: frozenset({"HOME", "PATH", "LANG", "TZ"}))
    cwd: str = "/workspace"

    @property
    def confirmed(self) -> bool:
        return not any(token.startswith("<candidate-") for token in self.argv_template)


def build_argv(profile: CommandProfile, values: dict[str, str]) -> tuple[str, ...]:
    if not profile.confirmed:
        raise ProfileError(f"профиль {profile.id} содержит неподтверждённые флаги (нужен probe P2-01)")
    argv: list[str] = []
    for token in profile.argv_template:
        m = _PLACEHOLDER.match(token)
        if m:
            name = m.group(1)
            if name not in profile.allowed_placeholders:
                raise ProfileError(f"плейсхолдер {name} не разрешён профилем")
            if name not in values:
                raise ProfileError(f"не задано значение плейсхолдера {name}")
            value = values[name]
            if "\x00" in value or "\n" in value:
                raise ProfileError("недопустимые символы в значении плейсхолдера")
            argv.append(value)
        elif "{" in token or "}" in token:
            raise ProfileError("частичная подстановка в токен argv запрещена")
        else:
            argv.append(token)
    return tuple(argv)


def build_env(profile: CommandProfile, source: dict[str, str]) -> dict[str, str]:
    """Окружение с нуля из allowlist профиля; запрещённые переменные — отказ в старте."""
    forbidden = sorted(k for k in source if FORBIDDEN_ENV.search(k))
    if forbidden:
        raise ProfileError(f"в конфигурации исполнения запрещённые переменные окружения: {', '.join(forbidden)}")
    return {k: v for k, v in source.items() if k in profile.env_allowlist}
