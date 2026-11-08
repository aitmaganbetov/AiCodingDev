"""Конечные автоматы Run / Task / Execution / Directive / Approval / ExternalAction (ADR-0007, P3-02).

Нормативный список переходов — DATABASE_DESIGN.md §18.3 (справочник `allowed_transitions`).
Этот модуль — его зеркало в коде: тест сверяет множества с БД, поэтому расхождение ломает CI.
Переход выполняется условным UPDATE (`WHERE status = :from AND version = :expected`) в одной
транзакции с outbox-событием; 0 строк ⇒ VersionConflict / InvalidTransition.
"""

from __future__ import annotations

from typing import Final, Literal

Entity = Literal["run", "task", "execution", "directive", "approval", "external_action"]
NEW: Final = "__new__"

_RUN: dict[str, set[str]] = {
    NEW: {"CREATED"},
    "CREATED": {"DISCOVERY", "CANCELLED", "FAILED"},
    "DISCOVERY": {"PLANNED", "PAUSED", "BLOCKED", "FAILED", "CANCELLED"},
    "PLANNED": {"AWAITING_APPROVAL", "RUNNING", "DISCOVERY", "PAUSED", "BLOCKED", "FAILED", "CANCELLED"},
    "AWAITING_APPROVAL": {"RUNNING", "PLANNED", "DISCOVERY", "PAUSED", "BLOCKED", "FAILED", "CANCELLED"},
    "RUNNING": {"VERIFYING", "PLANNED", "PAUSED", "BLOCKED", "FAILED", "CANCELLED"},
    "VERIFYING": {"COMPLETED", "RUNNING", "PAUSED", "BLOCKED", "FAILED", "CANCELLED"},
    "PAUSED": {"DISCOVERY", "PLANNED", "AWAITING_APPROVAL", "RUNNING", "VERIFYING", "FAILED", "CANCELLED"},
    "BLOCKED": {"DISCOVERY", "PLANNED", "AWAITING_APPROVAL", "RUNNING", "VERIFYING", "PAUSED", "FAILED", "CANCELLED"},
}
_TASK: dict[str, set[str]] = {
    NEW: {"PENDING"},
    "PENDING": {"READY", "BLOCKED", "CANCELLED"},
    "READY": {"LEASED", "PENDING", "BLOCKED", "CANCELLED"},
    "LEASED": {"RUNNING", "READY", "RETRY_WAIT", "BLOCKED", "FAILED", "CANCELLED"},
    "RUNNING": {"VERIFYING", "RETRY_WAIT", "BLOCKED", "FAILED", "CANCELLED"},
    "VERIFYING": {"SUCCEEDED", "RETRY_WAIT", "BLOCKED", "FAILED", "CANCELLED"},
    "RETRY_WAIT": {"READY", "BLOCKED", "FAILED", "CANCELLED"},
    "BLOCKED": {"PENDING", "READY", "SUCCEEDED", "FAILED", "CANCELLED"},
}
_EXECUTION: dict[str, set[str]] = {
    NEW: {"QUEUED"},
    "QUEUED": {"STARTING", "CANCELLED", "CRASHED"},
    "STARTING": {"RUNNING", "TIMED_OUT", "CANCELLED", "CRASHED", "AUTH_REQUIRED", "RATE_LIMITED"},
    "RUNNING": {"EXITED", "TIMED_OUT", "CANCELLED", "CRASHED", "AUTH_REQUIRED", "RATE_LIMITED"},
    "EXITED": {"COLLECTED", "CRASHED", "AUTH_REQUIRED", "RATE_LIMITED"},
}
_DIRECTIVE: dict[str, set[str]] = {
    NEW: {"RECEIVED"},
    "RECEIVED": {"IMPACT_ANALYSIS_PENDING", "DISPATCHED", "REJECTED", "CANCELLED", "FAILED"},
    "IMPACT_ANALYSIS_PENDING": {"IMPACT_READY", "CANCELLED", "FAILED"},
    "IMPACT_READY": {"AWAITING_CONFIRMATION", "CONFIRMED", "CHECKPOINTING", "REJECTED", "CANCELLED"},
    "AWAITING_CONFIRMATION": {"CONFIRMED", "REJECTED", "CANCELLED"},
    "CONFIRMED": {"CHECKPOINTING", "REPLANNING", "DISPATCHED", "CANCELLED", "FAILED"},
    "CHECKPOINTING": {"REPLANNING", "DISPATCHED", "FAILED"},
    "REPLANNING": {"DISPATCHED", "FAILED"},
    "DISPATCHED": {"ACKNOWLEDGED", "FAILED"},
    "ACKNOWLEDGED": {"COMPLETED", "FAILED"},
}
_APPROVAL: dict[str, set[str]] = {
    NEW: {"PENDING"},
    "PENDING": {"APPROVED", "REJECTED", "EXPIRED", "CANCELLED"},
}
_EXTERNAL_ACTION: dict[str, set[str]] = {
    NEW: {"PLANNED"},
    "PLANNED": {"EXECUTING", "FAILED"},
    "EXECUTING": {"CONFIRMED", "FAILED"},
    "FAILED": {"EXECUTING"},
    "CONFIRMED": {"COMPENSATED"},
}

TRANSITIONS: Final[dict[Entity, dict[str, frozenset[str]]]] = {
    "run": {k: frozenset(v) for k, v in _RUN.items()},
    "task": {k: frozenset(v) for k, v in _TASK.items()},
    "execution": {k: frozenset(v) for k, v in _EXECUTION.items()},
    "directive": {k: frozenset(v) for k, v in _DIRECTIVE.items()},
    "approval": {k: frozenset(v) for k, v in _APPROVAL.items()},
    "external_action": {k: frozenset(v) for k, v in _EXTERNAL_ACTION.items()},
}

TERMINAL: Final[dict[Entity, frozenset[str]]] = {
    "run": frozenset({"COMPLETED", "FAILED", "CANCELLED"}),
    "task": frozenset({"SUCCEEDED", "FAILED", "CANCELLED"}),
    "execution": frozenset({"COLLECTED", "TIMED_OUT", "CANCELLED", "CRASHED", "AUTH_REQUIRED", "RATE_LIMITED"}),
    "directive": frozenset({"COMPLETED", "REJECTED", "CANCELLED", "FAILED"}),
    "approval": frozenset({"APPROVED", "REJECTED", "EXPIRED", "CANCELLED"}),
    "external_action": frozenset({"COMPENSATED"}),
}

# Состояния run, из которых можно уйти в PAUSED/BLOCKED и вернуться (resume_status, §18.3).
RESUMABLE_RUN_STATES: Final = frozenset({"DISCOVERY", "PLANNED", "AWAITING_APPROVAL", "RUNNING", "VERIFYING"})


class InvalidTransitionError(Exception):
    def __init__(self, entity: Entity, current: str, target: str) -> None:
        super().__init__(f"{entity}: {current} → {target} не разрешён")
        self.entity = entity
        self.current = current
        self.target = target


def can_transition(entity: Entity, current: str, target: str) -> bool:
    return target in TRANSITIONS[entity].get(current, frozenset())


def require_transition(entity: Entity, current: str, target: str) -> None:
    if not can_transition(entity, current, target):
        raise InvalidTransitionError(entity, current, target)


def as_rows() -> set[tuple[str, str, str]]:
    """Плоское представление для сверки со справочником `allowed_transitions` в БД."""
    return {(e, f, t) for e, table in TRANSITIONS.items() for f, targets in table.items() for t in targets}
