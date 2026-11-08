"""Корреляционный контекст запроса/задачи (FR-ORCH-12, NFR-OBS-01)."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_FIELDS = ("request_id", "trace_id", "workspace_id", "user_id", "run_id", "task_id", "execution_id")

_context: ContextVar[dict[str, str] | None] = ContextVar("aicd_correlation", default=None)


def bind(**values: Any) -> None:
    current = dict(_context.get() or {})
    for key, value in values.items():
        if key not in _FIELDS:
            raise KeyError(f"неизвестное поле корреляции: {key}")
        if value is None:
            current.pop(key, None)
        else:
            current[key] = str(value)
    _context.set(current)


def snapshot() -> dict[str, str]:
    return dict(_context.get() or {})


def clear() -> None:
    _context.set({})
