"""ST-AUD-03 (SC-AUD-03): каждая небезопасная операция контракта объявляет событие аудита (`x-audit-action`), и это
событие действительно записывается кодом (литерал действия в `audit.record(...)` или явно перечисленное
динамическое имя). Новая команда без аудита валит тест."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.support.contract import ROOT, openapi

pytestmark = pytest.mark.security

# Небезопасные операции без события аудита — причина обязательна.
NO_AUDIT: dict[str, str] = {
    "startGitHubInstallation": "выдаёт одноразовый state (HMAC) без изменения состояния; подключение аудируется",
}
# Действия, имя которых собирается в коде (f-строка) — файл и шаблон.
DYNAMIC: dict[str, tuple[str, str]] = {
    "run.pause_requested": ("packages/orchestration/aicd_orchestration/runs.py", 'f"run.{command}_requested"'),
    "run.resume_requested": ("packages/orchestration/aicd_orchestration/runs.py", 'f"run.{command}_requested"'),
    "run.cancel_requested": ("packages/orchestration/aicd_orchestration/runs.py", 'f"run.{command}_requested"'),
}


def _sources() -> str:
    parts = []
    for base in ("apps", "packages"):
        for path in (ROOT / base).rglob("*.py"):
            if "generated" not in path.parts and ".venv" not in path.parts:
                parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_every_unsafe_operation_is_audited() -> None:
    source = _sources()
    problems = []
    for path, item in openapi()["paths"].items():
        for method, op in item.items():
            if method not in ("post", "put", "patch", "delete"):
                continue
            op_id = op["operationId"]
            action = op.get("x-audit-action")
            if op_id in NO_AUDIT:
                assert action == "none", f"{op_id}: исключение NO_AUDIT, но объявлено {action}"
                continue
            if not action or action == "none":
                problems.append(f"{op_id} {method.upper()} {path}: нет x-audit-action")
                continue
            for name in re.split(r"[,\s]+", str(action).strip()):
                if not name:
                    continue
                if f'"{name}"' in source:
                    continue
                dynamic = DYNAMIC.get(name)
                if dynamic and dynamic[1] in (ROOT / dynamic[0]).read_text(encoding="utf-8"):
                    continue
                problems.append(f"{op_id}: действие {name} не записывается кодом")
    assert not problems, "\n".join(problems)


def test_dynamic_actions_are_real_commands() -> None:
    text = Path(ROOT / "packages/orchestration/aicd_orchestration/runs.py").read_text(encoding="utf-8")
    for command in ("pause", "resume", "cancel"):
        assert f'"{command}"' in text
