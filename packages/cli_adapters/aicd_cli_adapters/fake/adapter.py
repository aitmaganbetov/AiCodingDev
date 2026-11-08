"""FakeCLIExecutor: реальный процесс-эмулятор + общий адаптер (CLI_ADAPTER_CONTRACT §13.1).

Используется в обычном CI и локальной разработке без подписок. Не регистрируется
для маршрутизации реальных задач: provider отражает эмулируемый CLI только для контракта.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from aicd_cli_adapters.errors import ErrorSignature
from aicd_cli_adapters.models import AuthCheckResult, ExecutionRequest, ProbeResult
from aicd_cli_adapters.profiles import CommandProfile
from aicd_cli_adapters.subprocess_adapter import ArtifactSink, ContainerLayout, ParsedLine, SubprocessCLIAdapter
from aicd_platform.clock import utcnow
from aicd_security.sandbox import SandboxRunner

FAKE_SCRIPT = Path(__file__).with_name("fake_cli.py")


def fake_container_layout(image: str, network: str = "none") -> ContainerLayout:
    """Fake CLI в runner-контейнере: скрипт монтируется только для чтения."""
    from aicd_security.sandbox import Mount

    return ContainerLayout(
        image=image,
        binary="python3",
        network=network,
        extra_mounts=(Mount(FAKE_SCRIPT.parent, "/aicd/fake", read_only=True),),
        placeholder_overrides={"fake_script": "/aicd/fake/fake_cli.py"},
    )


FAKE_VERSION = "0.0.0-fake"

FAKE_SIGNATURES: tuple[ErrorSignature, ...] = (
    ErrorSignature("AUTH_REQUIRED", re.compile(r"FAKE-AUTH: login required")),
    ErrorSignature("RATE_LIMITED", re.compile(r"FAKE-LIMIT: usage limit.*retry after (?P<ra>\d+)s"), "ra"),
)


class FakeOutputParser:
    def parse(self, line: str) -> ParsedLine:
        try:
            obj = json.loads(line)
        except ValueError:
            return ParsedLine("unparsed")
        if not isinstance(obj, dict):
            return ParsedLine("unparsed")
        kind = obj.get("event")
        if kind in ("message", "tool", "result"):
            return ParsedLine(kind, obj)
        return ParsedLine("unparsed")


def fake_profile(provider: Literal["codex", "claude"]) -> CommandProfile:
    return CommandProfile(
        id=f"fake-{provider}-agent-task-v1",
        provider=provider,
        argv_template=("{binary}", "{fake_script}", "--scenario", "{scenario}", "--prompt-file", "{prompt_file}"),
        allowed_placeholders=frozenset({"binary", "fake_script", "scenario", "prompt_file"}),
        env_allowlist=frozenset({"PATH", "LANG", "HOME"}),
    )


class FakeCLIExecutor(SubprocessCLIAdapter):
    def __init__(
        self,
        *,
        provider: Literal["codex", "claude"] = "codex",
        scenario: str = "success_diff",
        runner: SandboxRunner,
        artifact_sink: ArtifactSink,
        worktree_path: Callable[[ExecutionRequest], Awaitable[Path]],
        auth_status: str = "AUTHENTICATED",
        env_source: dict[str, str] | None = None,
        container: ContainerLayout | None = None,
    ) -> None:
        super().__init__(
            provider=provider,
            adapter_version="1.0.0",
            profile=fake_profile(provider),
            binary_path=sys.executable,
            runner=runner,
            parser=FakeOutputParser(),
            signatures=FAKE_SIGNATURES,
            artifact_sink=artifact_sink,
            worktree_path=worktree_path,
            extra_placeholders={"fake_script": str(FAKE_SCRIPT), "scenario": scenario},
            env_source=env_source,
            cli_version=FAKE_VERSION,
            container=container,
        )
        self._auth_status = auth_status

    async def probe(self, *, installation_id: UUID) -> ProbeResult:
        data: dict[str, Any] = {
            "contract_version": "1.0",
            "provider": self.provider,
            "installation_id": str(installation_id),
            "installed": True,
            "binary_path": sys.executable,
            "detected_version": FAKE_VERSION,
            "compatibility": "UNTESTED",
            "capabilities": {
                "noninteractive_mode": True,
                "machine_readable_output": "jsonl_stream",
                "sandbox_modes": ["cli_managed_none"],
                "resume_session": False,
                "token_usage_exposed": True,
                "auth_status_command": True,
                "cancel_signal_safe": True,
            },
            "auth_status": self._auth_status,
            "adapter_version": self.adapter_version,
            "checked_at": utcnow().isoformat(),
        }
        return ProbeResult.model_validate(data)

    async def check_auth(self, *, installation_id: UUID) -> AuthCheckResult:
        return AuthCheckResult.model_validate(
            {
                "contract_version": "1.0",
                "provider": self.provider,
                "installation_id": str(installation_id),
                "auth_status": self._auth_status,
                "method": "cli_status_command",
                "checked_at": utcnow().isoformat(),
            }
        )
