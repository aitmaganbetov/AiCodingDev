"""Нормализация ошибок CLI (CLI_ADAPTER_CONTRACT §6)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

CliErrorCode = Literal[
    "CLI_NOT_INSTALLED",
    "AUTH_REQUIRED",
    "RATE_LIMITED",
    "TIMEOUT",
    "SANDBOX_DENIED",
    "PROCESS_FAILED",
    "OUTPUT_PARSE_ERROR",
    "UNSUPPORTED_VERSION",
    "CANCELLED",
]
ExecutionStatus = Literal[
    "QUEUED",
    "STARTING",
    "RUNNING",
    "EXITED",
    "COLLECTED",
    "TIMED_OUT",
    "CANCELLED",
    "CRASHED",
    "AUTH_REQUIRED",
    "RATE_LIMITED",
]
TERMINAL_STATUSES: Final = frozenset(
    {"COLLECTED", "TIMED_OUT", "CANCELLED", "CRASHED", "AUTH_REQUIRED", "RATE_LIMITED"}
)

RETRYABLE: Final[dict[str, bool]] = {
    "CANCELLED": False,
    "CLI_NOT_INSTALLED": False,
    "UNSUPPORTED_VERSION": False,
    "AUTH_REQUIRED": False,
    "RATE_LIMITED": True,
    "TIMEOUT": True,
    "SANDBOX_DENIED": False,
    "OUTPUT_PARSE_ERROR": True,
    "PROCESS_FAILED": True,
}


@dataclass(frozen=True)
class ErrorSignature:
    """Сигнатура ошибки по санитизированному выводу. Источник — версионируемый файл адаптера.

    Для реальных CLI сигнатуры подтверждаются на установках (P2-01); здесь — механизм.
    """

    code: CliErrorCode
    pattern: re.Pattern[str]
    retry_after_group: str | None = None


@dataclass(frozen=True)
class Classification:
    code: CliErrorCode
    retry_after_seconds: int | None
    signal: str


# Порядок проверки — сверху вниз (первое совпадение), как в таблице §6.
_PRIORITY: Final = ("AUTH_REQUIRED", "RATE_LIMITED", "SANDBOX_DENIED", "UNSUPPORTED_VERSION")


def classify_output(text: str, signatures: tuple[ErrorSignature, ...]) -> Classification | None:
    ordered = sorted(signatures, key=lambda s: _PRIORITY.index(s.code) if s.code in _PRIORITY else len(_PRIORITY))
    for sig in ordered:
        match = sig.pattern.search(text)
        if match:
            retry_after = None
            if sig.retry_after_group and match.group(sig.retry_after_group):
                retry_after = int(match.group(sig.retry_after_group))
            return Classification(sig.code, retry_after, f"signature:{sig.code.lower()}")
    return None
