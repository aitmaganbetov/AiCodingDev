"""Протокол адаптера CLI (CLI_ADAPTER_CONTRACT §2, ТЗ §9.1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol
from uuid import UUID

from aicd_cli_adapters.models import (
    AuthCheckResult,
    ExecutionHandle,
    ExecutionRequest,
    ExecutionResult,
    NormalizedCliEvent,
    ProbeResult,
)

CancelReason = Literal["user", "run_cancel", "directive", "pause", "lease_lost", "timeout"]


class CLIExecutor(Protocol):
    provider: Literal["codex", "claude"]
    adapter_version: str

    async def probe(self, *, installation_id: UUID) -> ProbeResult: ...

    async def check_auth(self, *, installation_id: UUID) -> AuthCheckResult: ...

    async def execute(self, request: ExecutionRequest) -> ExecutionHandle:
        """Идемпотентно по execution_id: повтор не запускает второй процесс."""
        ...

    def stream_events(self, execution_id: UUID, *, after_seq: int = 0) -> AsyncIterator[NormalizedCliEvent]: ...

    async def cancel(self, execution_id: UUID, *, reason: CancelReason) -> None: ...

    async def collect_result(self, execution_id: UUID) -> ExecutionResult: ...
