"""P2-10: жизненный цикл процесса — группа процессов, SIGTERM→SIGKILL, чистое окружение."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from aicd_security.local_runner import LocalProcessRunner
from aicd_security.sandbox import ContainerSpec


def _spec(code: str, cwd: Path, env: dict[str, str] | None = None) -> ContainerSpec:
    return ContainerSpec(
        image="local",
        argv=(sys.executable, "-c", code),
        workdir="/",
        cwd=cwd,
        env=env if env is not None else {"PATH": os.environ["PATH"]},
    )


async def test_output_streams_and_exit_code(tmp_path: Path) -> None:
    proc = await LocalProcessRunner().start(
        _spec("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)", tmp_path)
    )
    chunks = [c async for c in proc.output()]
    assert await proc.wait() == 3
    assert b"out" in b"".join(c.data for c in chunks if c.stream == "stdout")
    assert b"err" in b"".join(c.data for c in chunks if c.stream == "stderr")


async def test_environment_is_not_inherited(tmp_path: Path) -> None:
    os.environ["AICD_HOST_SECRET_TEST"] = "leak"
    try:
        proc = await LocalProcessRunner().start(
            _spec("import os; print(sorted(os.environ))", tmp_path, {"PATH": os.environ["PATH"]})
        )
        out = b"".join([c.data async for c in proc.output()])
        await proc.wait()
    finally:
        del os.environ["AICD_HOST_SECRET_TEST"]
    assert b"AICD_HOST_SECRET_TEST" not in out


async def test_terminate_escalates_to_sigkill_and_kills_children(tmp_path: Path) -> None:
    code = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('child.pid','w').write(str(c.pid)); print('ready', flush=True); time.sleep(60)\n"
    )
    runner = LocalProcessRunner()
    proc = await runner.start(_spec(code, tmp_path))
    async for chunk in proc.output():
        if b"ready" in chunk.data:
            break
    child = int((tmp_path / "child.pid").read_text())
    started = time.monotonic()
    await proc.terminate(grace_seconds=0.5)
    assert await proc.wait() < 0
    assert time.monotonic() - started < 5
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("дочерний процесс пережил завершение группы")
    assert not await runner.is_alive(proc.ref)
