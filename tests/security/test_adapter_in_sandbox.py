"""P2-14: адаптер исполняет CLI в runner-контейнере — изменения только внутри worktree, без сети."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from aicd_cli_adapters.fake.adapter import FAKE_SCRIPT, FakeCLIExecutor, fake_container_layout
from aicd_cli_adapters.gateway import worktree_resolver
from aicd_platform.ids import uuid7
from aicd_security.docker_runner import DockerSandboxRunner
from tests.support.cli import MemorySink, make_request

pytestmark = pytest.mark.docker
IMAGE = "python:3.12-slim-bookworm"


@pytest.fixture
def docker_runner(tmp_path: Path) -> DockerSandboxRunner:
    if not Path("/var/run/docker.sock").exists():
        pytest.skip("Docker недоступен")
    return DockerSandboxRunner(
        allowed_bind_roots=[str(tmp_path), str(FAKE_SCRIPT.parent.parent), tempfile.gettempdir()],
        timeout_grace_seconds=2,
    )


async def test_fake_cli_runs_in_container_and_writes_only_worktree(
    tmp_path: Path, docker_runner: DockerSandboxRunner
) -> None:
    wid = uuid7()
    worktree = tmp_path / str(wid)
    worktree.mkdir()
    os.chown(worktree, 10001, 10001)  # владелец worktree — непривилегированный uid runner
    sink = MemorySink()
    adapter = FakeCLIExecutor(
        scenario="success_diff",
        runner=docker_runner,
        artifact_sink=sink,
        worktree_path=worktree_resolver(tmp_path),
        env_source={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        container=fake_container_layout(IMAGE),
    )
    request = make_request(worktree_id=wid)
    handle = await adapter.execute(request)
    events = [e async for e in adapter.stream_events(handle.execution_id)]
    result = await adapter.collect_result(handle.execution_id)
    assert result.status == "completed", result.error
    assert (worktree / "FAKE_CHANGE.md").stat().st_uid == 10001
    started = next(e for e in events if e.type == "started")
    assert str(started.payload["runner_ref"]).startswith("docker:")


async def test_network_is_unavailable_in_runner(tmp_path: Path, docker_runner: DockerSandboxRunner) -> None:
    """Сценарий env_dump запускается без сети; проверяем, что выход наружу невозможен."""
    from aicd_security.sandbox import ContainerSpec

    spec = ContainerSpec(
        image=IMAGE,
        workdir="/",
        labels={"aicd.execution_id": str(uuid7())},
        argv=("python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"),
    )
    proc = await docker_runner.start(spec)
    async for _ in proc.output():
        pass
    assert await proc.wait() != 0
