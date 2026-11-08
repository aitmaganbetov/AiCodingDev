"""Локальное исполнение процесса на хосте (P2-10).

Используется только: (1) политикой LOCAL_EXECUTION_FALLBACK с approval (ADR-0010), (2) Fake-адаптером
и тестами. Гарантии:
* argv-массив, без shell; окружение формируется заново из `spec.env` (переменные хоста не наследуются);
* процесс — лидер новой сессии/группы; terminate() отправляет SIGTERM всей группе, затем SIGKILL;
* wall-clock timeout и idle-timeout контролирует вызывающий (ExecutionGateway) через terminate().
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from collections.abc import AsyncIterator
from pathlib import Path

from aicd_security.sandbox import ContainerSpec, OutputChunk, SandboxProcess, StreamName

_CHUNK = 64 * 1024


class LocalProcess:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._pgid = proc.pid  # start_new_session=True ⇒ pgid == pid
        self._queue: asyncio.Queue[OutputChunk | None] = asyncio.Queue(maxsize=256)
        self._readers = [
            asyncio.create_task(self._pump("stdout", proc.stdout)),
            asyncio.create_task(self._pump("stderr", proc.stderr)),
        ]
        self._open_streams = 2

    @property
    def ref(self) -> str:
        return f"{socket.gethostname()}:{self._proc.pid}:{self._pgid}"

    async def _pump(self, name: StreamName, stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while chunk := await stream.read(_CHUNK):
                await self._queue.put(OutputChunk(name, chunk))
        await self._queue.put(None)

    async def output(self) -> AsyncIterator[OutputChunk]:
        while self._open_streams:
            item = await self._queue.get()
            if item is None:
                self._open_streams -= 1
                continue
            yield item

    async def wait(self) -> int:
        code = await self._proc.wait()
        await asyncio.gather(*self._readers, return_exceptions=True)
        return code

    def _signal_group(self, sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._pgid, sig)

    async def terminate(self, grace_seconds: float = 15.0) -> None:
        if self._proc.returncode is not None:
            # Лидер группы завершился, но потомки могли остаться — добиваем группу.
            self._signal_group(signal.SIGKILL)
            return
        self._signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace_seconds)
        except TimeoutError:
            self._signal_group(signal.SIGKILL)
            await self._proc.wait()
        self._signal_group(signal.SIGKILL)  # потомки, проигнорировавшие SIGTERM


class LocalProcessRunner:
    """Реализация `SandboxRunner` для процессов хоста."""

    async def start(self, spec: ContainerSpec) -> SandboxProcess:
        if not spec.argv:
            raise ValueError("пустой argv")
        if spec.cwd is None or not Path(spec.cwd).is_dir():  # noqa: ASYNC240 — проверка одного пути
            raise ValueError("для локального исполнения нужен существующий cwd")
        proc = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=str(spec.cwd),
            env=dict(spec.env),  # чистое окружение: только явно переданные переменные
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return LocalProcess(proc)

    def owns(self, ref: str) -> bool:
        parts = ref.split(":")
        return len(parts) == 3 and parts[0] == socket.gethostname() and parts[1].isdigit() and parts[2].isdigit()

    async def kill(self, ref: str) -> bool:
        if not self.owns(ref):
            return False
        _host, _pid, pgid = ref.split(":")
        try:
            os.killpg(int(pgid), signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        for _ in range(50):
            if not await self.is_alive(ref):
                return True
            await asyncio.sleep(0.05)
        return False

    async def is_alive(self, ref: str) -> bool:
        try:
            host, pid, _pgid = ref.split(":")
        except ValueError:
            return False
        if host != socket.gethostname():
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
