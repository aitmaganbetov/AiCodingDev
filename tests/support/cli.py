"""Фабрики для тестов CLI Gateway."""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from aicd_cli_adapters.models import ExecutionRequest
from aicd_cli_adapters.subprocess_adapter import StoredArtifact
from aicd_contracts.generated.cli_v1_0.execution_request import Limits
from aicd_platform.ids import uuid7


def make_request(
    *,
    worktree_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
    provider: str = "codex",
    objective: str = "Добавить документ",
    timeout_seconds: int = 60,
    small_limits: dict[str, Any] | None = None,
    task_attempt_id: uuid.UUID | None = None,
) -> ExecutionRequest:
    data = {
        "contract_version": "1.0",
        "execution_id": str(uuid7()),
        "task_id": str(uuid7()),
        "provider": provider,
        "agent_role": "developer",
        "workspace": {"worktree_id": str(worktree_id), "base_commit": "a" * 40},
        "input": {"objective": objective, "context_refs": [], "acceptance_criteria": ["Файл создан"]},
        "limits": {"timeout_seconds": timeout_seconds, "max_attempts": 2},
        "permissions": {"network": "none", "write_scope": "worktree", "production_access": False},
        "run_id": str(uuid7()),
        "task_attempt_id": str(task_attempt_id or uuid7()),
        "workspace_id": str(workspace_id or uuid7()),
        "command_profile": f"{provider}/agent_task@1",
    }
    request = ExecutionRequest.model_validate(data)
    if small_limits:
        # Только для тестов логики таймаутов: схема требует ≥ 30 c, здесь — доли секунды.
        limits = Limits.model_construct(**{**request.limits.model_dump(), **small_limits})
        request = request.model_copy(update={"limits": limits})
    return request


@dataclass
class MemorySink:
    items: dict[uuid.UUID, tuple[str, bytes, bool]] = field(default_factory=dict)

    async def __call__(
        self, request: ExecutionRequest, *, kind: str, data: BinaryIO, mime_type: str, sensitive: bool
    ) -> StoredArtifact:
        content = data.read()
        aid = uuid7()
        self.items[aid] = (kind, content, sensitive)
        return StoredArtifact(aid, hashlib.sha256(content).hexdigest(), len(content))

    def by_kind(self, kind: str) -> list[bytes]:
        return [c for k, c, _ in self.items.values() if k == kind]


def as_stream(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
