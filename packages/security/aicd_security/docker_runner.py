"""`SandboxRunner` поверх Docker Engine API (ADR-0010, P2-07).

Транспорт — httpx: unix-сокет (`unix:///var/run/docker.sock`, только dev/тесты) или TCP к
`docker-socket-proxy` (`http://docker-socket-proxy:2375`, профиль compose). Используются только
endpoint'ы класса CONTAINERS (create/start/logs/wait/stop/kill/json/delete) — их пропускает
docker-socket-proxy с `CONTAINERS=1, POST=1`.

Перед каждым `create` тело запроса проверяется `container_policy.lint_container_config`.
Жизненный цикл: create → start → (logs follow ‖ wait) → remove. Wall-clock тайм-аут
(`limits.timeout_seconds`) соблюдается сторожем, который запускается вместе с контейнером:
по истечении — `stop` (SIGTERM → grace → SIGKILL), `process.timed_out = True`.
Вывод сверх `limits.max_output_bytes` отбрасывается (`process.truncated = True`), но поток
дочитывается до конца, чтобы процесс не блокировался и учёт байтов оставался полным.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import secrets
import struct
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx

from aicd_security.container_policy import (
    DEFAULT_ALLOWED_BIND_ROOTS,
    DEFAULT_ALLOWED_VOLUME_PREFIXES,
    ContainerPolicyError,
    build_host_config,
    lint_container_config,
)
from aicd_security.sandbox import ContainerSpec, OutputChunk, StreamName

DEFAULT_API_VERSION = "1.44"
REF_PREFIX = "docker:"
_FRAME_HEADER = struct.Struct(">BxxxL")
_STREAMS: dict[int, StreamName] = {1: "stdout", 2: "stderr"}


class DockerError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


def _client_for(base_url: str, timeout: float) -> httpx.AsyncClient:
    parts = urlsplit(base_url)
    # Явный transport: переменные HTTP(S)_PROXY окружения к Docker API не применяются.
    if parts.scheme == "unix":
        transport = httpx.AsyncHTTPTransport(uds=parts.path)
        return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=timeout, trust_env=False)
    if parts.scheme == "tcp":
        base_url = f"http://{parts.netloc}"
    elif parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported docker base_url scheme: {parts.scheme!r}")
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(), base_url=base_url, timeout=timeout, trust_env=False)


class DockerFrameParser:
    """Разбор мультиплексированного потока `logs`/`attach` (Tty=false): 8-байтовый заголовок
    `[stream, 0, 0, 0, size_be32]`, затем `size` байт данных. Фреймы могут резаться по чанкам."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[OutputChunk]:
        self._buf.extend(data)
        out: list[OutputChunk] = []
        while len(self._buf) >= _FRAME_HEADER.size:
            stream_id, size = _FRAME_HEADER.unpack_from(self._buf, 0)
            end = _FRAME_HEADER.size + size
            if len(self._buf) < end:
                break
            payload = bytes(self._buf[_FRAME_HEADER.size : end])
            del self._buf[:end]
            stream = _STREAMS.get(stream_id)
            if stream is not None and payload:
                out.append(OutputChunk(stream=stream, data=payload))
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)


class DockerSandboxProcess:
    """Запущенный runner-контейнер. `output()` читается одним потребителем."""

    def __init__(self, runner: DockerSandboxRunner, container_id: str, spec: ContainerSpec) -> None:
        self._runner = runner
        self.container_id = container_id
        self.spec = spec
        self.started_at = time.monotonic()
        self.timed_out = False
        self.terminated = False
        self.oom_killed = False
        self.truncated = False
        self.output_bytes_total = 0
        self.output_bytes_emitted = 0
        self._exit_code: int | None = None
        self._exited = asyncio.Event()
        self._output_started = False
        self._output_done = asyncio.Event()
        self._wait_lock = asyncio.Lock()
        self._removed = False
        self._watchdog = asyncio.create_task(self._enforce_timeout(), name=f"aicd-timeout-{container_id[:12]}")

    @property
    def ref(self) -> str:
        return f"{REF_PREFIX}{self.container_id}"

    async def _enforce_timeout(self) -> None:
        timeout = self.spec.limits.timeout_seconds
        try:
            await asyncio.wait_for(self._exited.wait(), timeout=timeout)
        except TimeoutError:
            self.timed_out = True
            with contextlib.suppress(DockerError, httpx.HTTPError):
                await self.terminate(grace_seconds=self._runner.timeout_grace_seconds)

    async def output(self) -> AsyncIterator[OutputChunk]:
        if self._output_started:
            raise RuntimeError("output() can only be consumed once")
        self._output_started = True
        limit = self.spec.limits.max_output_bytes
        parser = DockerFrameParser()
        try:
            async with self._runner.client.stream(
                "GET",
                self._runner.path(f"/containers/{self.container_id}/logs"),
                params={"follow": "1", "stdout": "1", "stderr": "1"},
                timeout=httpx.Timeout(None, connect=10.0),
            ) as resp:
                if resp.status_code == 404:
                    return
                if resp.status_code >= 400:
                    await resp.aread()
                    raise DockerError(f"logs failed: {resp.status_code} {resp.text}", resp.status_code)
                async for raw in resp.aiter_bytes():
                    for chunk in parser.feed(raw):
                        self.output_bytes_total += len(chunk.data)
                        remaining = limit - self.output_bytes_emitted
                        if remaining <= 0:
                            self.truncated = True
                            continue
                        data = chunk.data
                        if len(data) > remaining:
                            data = data[:remaining]
                            self.truncated = True
                        self.output_bytes_emitted += len(data)
                        yield OutputChunk(stream=chunk.stream, data=data)
        finally:
            self._output_done.set()

    async def wait(self) -> int:
        """Код завершения. Для убитых нами (тайм-аут/terminate) или OOM — `-signal`.

        После завершения контейнер удаляется (если runner создан с `remove_on_exit=True`),
        предварительно дождавшись, пока `output()` дочитает логи.
        """
        async with self._wait_lock:
            if self._exit_code is None:
                self._exit_code = await self._wait_exit()
            if self._runner.remove_on_exit and not self._removed:
                if self._output_started:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._output_done.wait(), timeout=self._runner.output_drain_seconds)
                await self.remove()
            return self._exit_code

    async def _wait_exit(self) -> int:
        resp = await self._runner.client.post(
            self._runner.path(f"/containers/{self.container_id}/wait"),
            timeout=httpx.Timeout(None, connect=10.0),
        )
        self._exited.set()
        if resp.status_code == 404:
            raise DockerError(f"container {self.container_id} disappeared", 404)
        self._runner.raise_for(resp, "wait")
        body = resp.json()
        code = int(body.get("StatusCode", -1))
        with contextlib.suppress(DockerError, httpx.HTTPError):
            info = await self._runner.inspect(self.ref)
            if info is not None:
                self.oom_killed = bool(info.get("State", {}).get("OOMKilled"))
        if (self.terminated or self.oom_killed) and 128 < code <= 128 + 64:
            code = -(code - 128)
        return code

    async def terminate(self, grace_seconds: float = 15.0) -> None:
        """`docker stop -t grace` (SIGTERM → grace → SIGKILL всему PID namespace), затем kill."""
        if self._exited.is_set() or self._removed:
            return
        self.terminated = True
        t = max(0, math.ceil(grace_seconds))
        resp = await self._runner.client.post(
            self._runner.path(f"/containers/{self.container_id}/stop"),
            params={"t": str(t), "signal": "SIGTERM"},
            timeout=httpx.Timeout(t + 30.0, connect=10.0),
        )
        if resp.status_code not in (204, 304, 404):
            self._runner.raise_for(resp, "stop")
        kill = await self._runner.client.post(
            self._runner.path(f"/containers/{self.container_id}/kill"), params={"signal": "SIGKILL"}
        )
        if kill.status_code not in (204, 404, 409):  # 409 — уже не запущен
            self._runner.raise_for(kill, "kill")

    async def remove(self) -> None:
        if self._removed:
            return
        resp = await self._runner.client.delete(
            self._runner.path(f"/containers/{self.container_id}"), params={"force": "1", "v": "1"}
        )
        if resp.status_code not in (204, 404):
            self._runner.raise_for(resp, "remove")
        self._removed = True
        self._exited.set()
        if not self._watchdog.done():
            self._watchdog.cancel()


class DockerSandboxRunner:
    """Реализация `SandboxRunner` для Docker Engine API.

    `allowed_bind_roots`/`allowed_volume_prefixes`/`allowed_images` передаются линтеру;
    `require_image_digest=True` — для профиля server (образы только по digest).
    """

    def __init__(
        self,
        base_url: str = "unix:///var/run/docker.sock",
        *,
        api_version: str = DEFAULT_API_VERSION,
        allowed_bind_roots: Iterable[str] = DEFAULT_ALLOWED_BIND_ROOTS,
        allowed_volume_prefixes: Iterable[str] = DEFAULT_ALLOWED_VOLUME_PREFIXES,
        allowed_images: Iterable[str] | None = None,
        require_image_digest: bool = False,
        remove_on_exit: bool = True,
        timeout_grace_seconds: float = 10.0,
        output_drain_seconds: float = 30.0,
        request_timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url
        self.api_version = api_version
        self.allowed_bind_roots = tuple(allowed_bind_roots)
        self.allowed_volume_prefixes = tuple(allowed_volume_prefixes)
        self.allowed_images = None if allowed_images is None else frozenset(allowed_images)
        self.require_image_digest = require_image_digest
        self.remove_on_exit = remove_on_exit
        self.timeout_grace_seconds = timeout_grace_seconds
        self.output_drain_seconds = output_drain_seconds
        self.client = _client_for(base_url, request_timeout)

    async def __aenter__(self) -> DockerSandboxRunner:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    def path(self, endpoint: str) -> str:
        return f"/v{self.api_version}{endpoint}"

    @staticmethod
    def raise_for(resp: httpx.Response, op: str) -> None:
        if resp.status_code >= 400:
            try:
                message = str(resp.json().get("message", resp.text))
            except ValueError:
                message = resp.text
            raise DockerError(f"docker {op} failed ({resp.status_code}): {message}", resp.status_code)

    async def ping(self) -> bool:
        try:
            resp = await self.client.get("/_ping", timeout=5.0)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    def create_payload(self, spec: ContainerSpec) -> dict[str, Any]:
        """Тело create, прошедшее линтер (иначе `ContainerPolicyError`)."""
        payload = build_host_config(spec)
        violations = lint_container_config(
            payload,
            allowed_bind_roots=self.allowed_bind_roots,
            allowed_volume_prefixes=self.allowed_volume_prefixes,
            allowed_images=self.allowed_images,
            require_image_digest=self.require_image_digest,
        )
        if violations:
            raise ContainerPolicyError(violations)
        return payload

    async def start(self, spec: ContainerSpec) -> DockerSandboxProcess:
        payload = self.create_payload(spec)
        execution_id = spec.labels.get("aicd.execution_id", "x")
        safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in execution_id)[:48]
        name = f"aicd-{safe}-{secrets.token_hex(4)}"
        resp = await self.client.post(self.path("/containers/create"), params={"name": name}, json=payload)
        self.raise_for(resp, "create")
        container_id = str(resp.json()["Id"])
        started = await self.client.post(self.path(f"/containers/{container_id}/start"))
        if started.status_code not in (204, 304):
            with contextlib.suppress(httpx.HTTPError):
                await self.client.delete(self.path(f"/containers/{container_id}"), params={"force": "1", "v": "1"})
            self.raise_for(started, "start")
        return DockerSandboxProcess(self, container_id, spec)

    @staticmethod
    def _container_id(ref: str) -> str:
        if not ref.startswith(REF_PREFIX) or len(ref) <= len(REF_PREFIX):
            raise ValueError(f"not a docker sandbox ref: {ref!r}")
        cid = ref[len(REF_PREFIX) :]
        if not all(c.isalnum() or c in "_.-" for c in cid):
            raise ValueError(f"invalid container id in ref: {ref!r}")
        return cid

    async def inspect(self, ref: str) -> dict[str, Any] | None:
        resp = await self.client.get(self.path(f"/containers/{self._container_id(ref)}/json"))
        if resp.status_code == 404:
            return None
        self.raise_for(resp, "inspect")
        data: dict[str, Any] = resp.json()
        return data

    async def is_alive(self, ref: str) -> bool:
        info = await self.inspect(ref)
        if info is None:
            return False
        state = info.get("State") or {}
        return bool(state.get("Running") or state.get("Paused") or state.get("Restarting"))
