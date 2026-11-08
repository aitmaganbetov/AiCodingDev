"""Модели контракта CLI v1.0: сгенерированы из JSON Schema (packages/contracts) + ExecutionHandle."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aicd_cli_adapters.errors import ExecutionStatus
from aicd_contracts.generated.cli_v1_0.auth_check_result import AuthCheckResult
from aicd_contracts.generated.cli_v1_0.execution_request import ExecutionRequest
from aicd_contracts.generated.cli_v1_0.execution_result import ExecutionResult
from aicd_contracts.generated.cli_v1_0.normalized_event import NormalizedCliEvent
from aicd_contracts.generated.cli_v1_0.probe_result import ProbeResult

__all__ = [
    "AuthCheckResult",
    "ExecutionHandle",
    "ExecutionRequest",
    "ExecutionResult",
    "NormalizedCliEvent",
    "ProbeResult",
    "result_json",
]


def result_json(result: ExecutionResult) -> str:
    """Сериализация строго по схеме: неустановленные необязательные поля не выводятся как null."""
    return result.model_dump_json(exclude_unset=True)


class ExecutionHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: UUID
    status: ExecutionStatus
    runner_ref: str
    started_at: datetime | None
