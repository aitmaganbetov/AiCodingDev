"""Общий адаптер «процесс CLI → нормализованные события → ExecutionResult» (CLI_ADAPTER_CONTRACT §2, §6, §9–§12).

Конкретные адаптеры (Fake сейчас; Codex/Claude после P2-01) задают профиль команды, парсер
машиночитаемого вывода и сигнатуры ошибок. Исключения не «пробиваются» наружу: любой сбой
отражается в `ExecutionResult.error` (правило «ошибка = данные»).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol
from uuid import UUID

from aicd_cli_adapters.errors import RETRYABLE, CliErrorCode, ErrorSignature, ExecutionStatus, classify_output
from aicd_cli_adapters.executor import CancelReason
from aicd_cli_adapters.models import (
    AuthCheckResult,
    ExecutionHandle,
    ExecutionRequest,
    ExecutionResult,
    NormalizedCliEvent,
    ProbeResult,
)
from aicd_cli_adapters.output import LineBuffer, RawLog, SanitizedTranscript, sanitize_chunks, sanitize_excerpt
from aicd_cli_adapters.profiles import CommandProfile, ProfileError, build_argv, build_env
from aicd_platform.clock import utcnow
from aicd_security.sandbox import ContainerSpec, Mount, ResourceLimits, SandboxProcess, SandboxRunner

STDERR_TAIL = 64 * 1024
PROMPT_TEXT_FILE = "prompt.md"
# Каталоги ввода-вывода агента (AGENT_ROLES §3.4): относительно cwd CLI, физически — вне worktree.
AGENT_IO_DIRS = (".aicd", "output")
# Скрытые рассуждения модели (reasoning/thinking) не показываются и не сохраняются (API_DESIGN §13 п.5, ТЗ §11):
# сообщение парсера с таким типом или флагом `hidden` отбрасывается до санитизации.
HIDDEN_MESSAGE_TYPES = frozenset({"reasoning", "thinking", "analysis", "chain_of_thought"})


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: UUID
    sha256: str
    size_bytes: int


class ArtifactSink(Protocol):
    async def __call__(
        self, request: ExecutionRequest, *, kind: str, data: BinaryIO, mime_type: str, sensitive: bool
    ) -> StoredArtifact: ...


@dataclass(frozen=True)
class ContainerLayout:
    """Исполнение в runner-контейнере (ADR-0010): пути плейсхолдеров — внутри образа."""

    image: str
    binary: str
    network: str = "none"
    extra_mounts: tuple[Mount, ...] = ()
    placeholder_overrides: dict[str, str] = field(default_factory=dict)
    prompt_mount: str = "/aicd/input"


@dataclass(frozen=True)
class ParsedLine:
    kind: Literal["message", "tool", "result", "unparsed"]
    data: dict[str, Any] = field(default_factory=dict)


class OutputParser(Protocol):
    def parse(self, line: str) -> ParsedLine: ...


@dataclass
class _Execution:
    request: ExecutionRequest
    events: list[NormalizedCliEvent] = field(default_factory=list)
    new_event: asyncio.Event = field(default_factory=asyncio.Event)
    process: SandboxProcess | None = None
    task: asyncio.Task[None] | None = None
    status: ExecutionStatus = "QUEUED"
    started_at: datetime | None = None
    result: ExecutionResult | None = None
    cancel_reason: CancelReason | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    started_or_done: asyncio.Event = field(default_factory=asyncio.Event)
    io_dir: Path | None = None
    io_mode: Literal["mount", "move"] = "move"
    io_links: list[Path] = field(default_factory=list)  # пути ввода-вывода агента, созданные в worktree


class SubprocessCLIAdapter:
    def __init__(
        self,
        *,
        provider: Literal["codex", "claude"],
        adapter_version: str,
        profile: CommandProfile,
        binary_path: str,
        runner: SandboxRunner,
        parser: OutputParser,
        signatures: tuple[ErrorSignature, ...],
        artifact_sink: ArtifactSink,
        worktree_path: Callable[[ExecutionRequest], Awaitable[Path]],
        extra_placeholders: dict[str, str] | None = None,
        env_source: dict[str, str] | None = None,
        cli_version: str = "unknown",
        image: str = "local",
        terminate_grace_seconds: float = 10.0,
        container: ContainerLayout | None = None,
        agent_io: bool = False,
        agent_io_root: Path | None = None,
    ) -> None:
        self.provider = provider
        self.adapter_version = adapter_version
        self._profile = profile
        self._binary = binary_path
        self._runner = runner
        self._parser = parser
        self._signatures = signatures
        self._sink = artifact_sink
        self._worktree_path = worktree_path
        self._extra = dict(extra_placeholders or {})
        self._env_source = dict(env_source or {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
        self._cli_version = cli_version
        self._image = image
        self._grace = terminate_grace_seconds
        self._container = container
        self._agent_io = agent_io
        self._agent_io_root = agent_io_root
        self._prompt_texts: dict[UUID, str] = {}
        self._executions: dict[UUID, _Execution] = {}
        self._lock = asyncio.Lock()

    # --- ввод-вывод агента (Phase 4: промпт роли и `.aicd/result.json`) -------------------------
    def stage_prompt(self, execution_id: UUID, text: str) -> None:
        """Текст собранного промпта роли: пишется рядом с task.json как `prompt.md` (`{prompt_text_file}`)."""
        self._prompt_texts[execution_id] = text

    def agent_io_dir(self, execution_id: UUID) -> Path | None:
        """Каталог вывода агента (содержит `.aicd/` и `output/`), если исполнение его создавало."""
        ex = self._executions.get(execution_id)
        return ex.io_dir if ex is not None else None

    def release_agent_io(self, execution_id: UUID) -> None:
        """Удалить каталог вывода агента после того, как оркестратор забрал результат."""
        ex = self._executions.get(execution_id)
        if ex is None or ex.io_dir is None:
            return
        shutil.rmtree(ex.io_dir, ignore_errors=True)
        ex.io_dir = None

    def _request_placeholders(self, request: ExecutionRequest) -> dict[str, str]:
        """Значения плейсхолдеров, зависящие от запроса (переопределяют адаптеры; по умолчанию — нет)."""
        del request
        return {}

    def _wire_agent_io(self, ex: _Execution, cwd: Path, *, read_only: bool) -> tuple[Mount, ...]:
        """Каталог вывода исполнения вне worktree: итог агента — `<io>/.aicd/result.json`, отчёты — `<io>/.aicd/output/`.

        Агент пишет по относительным путям `.aicd/…` и `output/…` (cwd CLI = worktree); `output` в worktree —
        относительная symlink на `.aicd/output`. Каталог `.aicd`:

        * worktree смонтирован read-only (контейнер, роль с `write_scope=none`) — единственное rw
          bind-монтирование `<io>/.aicd` поверх точки монтирования (политика контейнера допускает одно rw);
        * иначе (rw worktree, локальный запуск) — обычный каталог в worktree, после выхода процесса переносится
          в `<io>/.aicd` (`_unwire_agent_io`); в коммит задачи он не попадает в любом случае (worktrees).

        Каталог временный, как каталог промпта (системный tmp или `agent_io_root`); удаляет его
        `release_agent_io`. Пути `.aicd`/`output`, уже существующие в worktree (из репозитория), не подменяются.
        """
        if self._agent_io_root is not None:
            self._agent_io_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        io_dir = Path(tempfile.mkdtemp(prefix="aicd-io-", dir=self._agent_io_root))
        ex.io_dir = io_dir
        aicd, output = cwd / AGENT_IO_DIRS[0], cwd / AGENT_IO_DIRS[1]
        if os.path.lexists(aicd):
            return ()
        container = self._container is not None
        mode = 0o777 if container else 0o700  # в контейнере пишет uid runner, не владелец каталога
        mounts: tuple[Mount, ...] = ()
        if container and read_only:
            source = io_dir / AGENT_IO_DIRS[0]
            (source / "output").mkdir(parents=True)
            os.chmod(io_dir, 0o755)  # noqa: S103 — каталог исполнения; запись — только в смонтированный подкаталог
            for d in (source, source / "output"):
                os.chmod(d, mode)
            aicd.mkdir()  # точка монтирования внутри read-only worktree
            mounts = (Mount(source, f"{self._profile.cwd}/{AGENT_IO_DIRS[0]}", read_only=False),)
            ex.io_mode = "mount"
        else:
            (aicd / "output").mkdir(parents=True)
            for d in (aicd, aicd / "output"):
                os.chmod(d, mode)
            ex.io_mode = "move"
        if not os.path.lexists(output):
            output.symlink_to(f"{AGENT_IO_DIRS[0]}/output", target_is_directory=True)
            ex.io_links.append(output)
        ex.io_links.append(aicd)
        return mounts

    @staticmethod
    def _unwire_agent_io(ex: _Execution) -> None:
        """Убрать из worktree symlink `output` и `.aicd` (точку монтирования или перенести каталог в `<io>`)."""
        for link in ex.io_links:
            with contextlib.suppress(OSError):
                if link.is_symlink():
                    link.unlink()
                elif link.is_dir() and ex.io_mode == "mount":
                    link.rmdir()  # пустая точка монтирования
                elif link.is_dir() and ex.io_dir is not None:
                    shutil.move(link, ex.io_dir / link.name)
        ex.io_links.clear()

    # --- probe / auth: реализуют конкретные адаптеры --------------------------------------
    async def probe(self, *, installation_id: UUID) -> ProbeResult:
        raise NotImplementedError

    async def check_auth(self, *, installation_id: UUID) -> AuthCheckResult:
        raise NotImplementedError

    # --- исполнение --------------------------------------------------------------------------
    async def execute(self, request: ExecutionRequest) -> ExecutionHandle:
        async with self._lock:
            existing = self._executions.get(request.execution_id)
            if existing is None:
                existing = _Execution(request=request)
                self._executions[request.execution_id] = existing
                existing.task = asyncio.create_task(self._run(existing))
                await asyncio.sleep(0)  # дать задаче стартовать
        return ExecutionHandle(
            execution_id=request.execution_id,
            status=existing.status if existing.status in ("QUEUED", "STARTING", "RUNNING") else "RUNNING",
            runner_ref=existing.process.ref if existing.process else "pending",
            started_at=existing.started_at,
        )

    async def stream_events(self, execution_id: UUID, *, after_seq: int = 0) -> AsyncIterator[NormalizedCliEvent]:
        ex = self._executions[execution_id]
        index = after_seq
        while True:
            while index < len(ex.events):
                index += 1
                yield ex.events[index - 1]
            if ex.done.is_set() and index >= len(ex.events):
                return
            ex.new_event.clear()
            if index >= len(ex.events) and not ex.done.is_set():
                await ex.new_event.wait()

    async def cancel(self, execution_id: UUID, *, reason: CancelReason) -> None:
        ex = self._executions.get(execution_id)
        if ex is None or ex.done.is_set():
            return
        ex.cancel_reason = ex.cancel_reason or reason
        if ex.process is not None:
            await ex.process.terminate(self._grace)

    async def collect_result(self, execution_id: UUID) -> ExecutionResult:
        ex = self._executions[execution_id]
        await ex.done.wait()
        if ex.result is None:  # pragma: no cover — _run всегда выставляет результат
            raise RuntimeError("execution finished without result")
        return ex.result

    async def wait_started(self, execution_id: UUID) -> str | None:
        """Для gateway: дождаться старта процесса и вернуть его ref (или None при сбое старта)."""
        ex = self._executions[execution_id]
        await ex.started_or_done.wait()
        return ex.process.ref if ex.process else None

    # --- внутреннее ---------------------------------------------------------------------------
    @staticmethod
    def _cancel_requested(ex: _Execution) -> bool:
        # Отдельная функция: cancel() меняет поле конкурентно, сужение типов mypy здесь неверно.
        return ex.cancel_reason is not None

    def _emit(self, ex: _Execution, type_: str, payload: dict[str, Any]) -> None:
        ex.events.append(
            NormalizedCliEvent(
                contract_version="1.0",
                execution_id=ex.request.execution_id,
                seq=len(ex.events) + 1,
                type=type_,
                timestamp=utcnow(),
                payload=payload,
            )
        )
        ex.new_event.set()

    def _emit_text(self, ex: _Execution, transcript: SanitizedTranscript, stream: str, text: str, *, cut: bool) -> None:
        """Строка вывода → санитизированные чанки ≤ 4 KiB (`output`, по событию на чанк) + полный вывод."""
        transcript.write(stream, text)
        chunks, redactions = sanitize_chunks(text)
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            self._emit(
                ex,
                "output",
                {
                    "stream": stream,
                    "text": chunk,
                    # редакции считаются по строке целиком — относятся к её первому чанку
                    "redactions": redactions if i == 0 else 0,
                    "truncated": cut and last,
                },
            )

    async def _run(self, ex: _Execution) -> None:
        req = ex.request
        limits = req.limits
        raw = RawLog(max_bytes=limits.max_output_bytes or 32 * 1024 * 1024)
        transcript = SanitizedTranscript(max_bytes=limits.max_output_bytes or 32 * 1024 * 1024)
        stderr_lines = LineBuffer()
        stderr_tail = bytearray()
        stdout_buf = b""
        parse_errors = 0
        parsed_lines = 0
        result_line: dict[str, Any] | None = None
        timed_out = False
        exit_code: int | None = None
        error: tuple[CliErrorCode, str, int | None] | None = None
        started_mono = time.monotonic()
        prompt_dir = tempfile.mkdtemp(prefix="aicd-prompt-")
        try:
            if self._cancel_requested(ex):
                error = ("CANCELLED", "отменено до старта", None)
                return
            ex.status = "STARTING"
            try:
                cwd = await self._worktree_path(req)
                prompt_file = Path(prompt_dir) / "task.json"
                # Задача передаётся данными (файл), никогда — через argv (CLI_ADAPTER_CONTRACT §8.2).
                prompt_file.write_text(req.input.model_dump_json(), encoding="utf-8")
                os.chmod(prompt_file, 0o444 if self._container else 0o400)
                # Промпт роли (AGENT_ROLES §3.3) — тоже файл; без собранного промпта — только objective.
                text_file = Path(prompt_dir) / PROMPT_TEXT_FILE
                text_file.write_text(
                    self._prompt_texts.pop(req.execution_id, "") or req.input.objective, encoding="utf-8"
                )
                os.chmod(text_file, 0o444 if self._container else 0o400)
                # При расхождении профиля команды и запроса действует более строгое ограничение (SC-CLI-07).
                read_only = self._profile.write_scope == "none" or req.permissions.write_scope == "none"
                io_mounts = self._wire_agent_io(ex, cwd, read_only=read_only) if self._agent_io else ()
                mounts: tuple[Mount, ...] = ()
                request_values = self._request_placeholders(req)
                if self._container is None:
                    values = {
                        "binary": self._binary,
                        "prompt_file": str(prompt_file),
                        "prompt_text_file": str(text_file),
                        **self._extra,
                        **request_values,
                    }
                else:
                    os.chmod(prompt_dir, 0o755)  # noqa: S103 — каталог задачи читает uid runner; файл 0444
                    layout = self._container
                    values = {
                        **self._extra,
                        **request_values,
                        **layout.placeholder_overrides,
                        "binary": layout.binary,
                        "prompt_file": f"{layout.prompt_mount}/task.json",
                        "prompt_text_file": f"{layout.prompt_mount}/{PROMPT_TEXT_FILE}",
                    }
                    mounts = (
                        Mount(cwd, self._profile.cwd, read_only=read_only),
                        *io_mounts,
                        Mount(Path(prompt_dir), layout.prompt_mount, read_only=True),
                        *layout.extra_mounts,
                    )
                argv = build_argv(self._profile, values)
                env = build_env(self._profile, self._env_source)
            except ProfileError as exc:
                error = ("UNSUPPORTED_VERSION", str(exc), None)
                return
            spec = ContainerSpec(
                image=self._container.image if self._container else self._image,
                argv=argv,
                workdir=self._profile.cwd,
                env=env,
                mounts=mounts,
                network=self._container.network if self._container else "none",
                limits=ResourceLimits(
                    timeout_seconds=limits.timeout_seconds,
                    idle_timeout_seconds=limits.idle_timeout_seconds,
                    max_output_bytes=limits.max_output_bytes or 32 * 1024 * 1024,
                ),
                labels={"aicd.execution_id": str(req.execution_id)},
                cwd=cwd,
            )
            try:
                ex.process = await self._runner.start(spec)
            except (FileNotFoundError, PermissionError) as exc:
                error = ("CLI_NOT_INSTALLED", type(exc).__name__, None)
                return
            except ValueError as exc:
                error = ("SANDBOX_DENIED", str(exc)[:200], None)
                return
            ex.status = "RUNNING"
            ex.started_at = utcnow()
            ex.started_or_done.set()
            self._emit(
                ex,
                "started",
                {
                    "provider": self.provider,
                    "cli_version": self._cli_version,
                    "command_profile": self._profile.id,
                    "runner_ref": ex.process.ref,
                },
            )
            if self._cancel_requested(ex):
                await ex.process.terminate(self._grace)

            last_activity = time.monotonic()
            idle_limit = limits.idle_timeout_seconds
            truncated_warned = False
            output_iter = ex.process.output().__aiter__()
            deadline = started_mono + limits.timeout_seconds
            # Ожидание следующего фрагмента без отмены: отмена __anext__ закрыла бы генератор вывода.
            pending: asyncio.Future[Any] | None = None
            while True:
                remaining = deadline - time.monotonic()
                wait_for = (
                    remaining if idle_limit is None else min(remaining, idle_limit - (time.monotonic() - last_activity))
                )
                if wait_for <= 0 or self._cancel_requested(ex):
                    timed_out = not self._cancel_requested(ex)
                    break
                if pending is None:
                    pending = asyncio.ensure_future(output_iter.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=min(wait_for, 0.5))
                if not done:
                    continue
                fut, pending = pending, None
                try:
                    chunk = fut.result()
                except StopAsyncIteration:
                    break
                last_activity = time.monotonic()
                if chunk.stream == "stderr":
                    raw.write("stderr", chunk.data)
                if raw.truncated and not truncated_warned:
                    truncated_warned = True
                    self._emit(ex, "warning", {"code": "OUTPUT_TRUNCATED", "message": "raw log достиг лимита"})
                if chunk.stream == "stderr":
                    stderr_tail.extend(chunk.data)
                    del stderr_tail[:-STDERR_TAIL]
                    for line, cut in stderr_lines.feed(chunk.data):
                        if line.strip():
                            self._emit_text(ex, transcript, "stderr", line, cut=cut)
                    continue
                stdout_buf += chunk.data
                *lines, stdout_buf = stdout_buf.split(b"\n")
                for line_bytes in lines:
                    line = line_bytes.decode(errors="replace").strip()
                    if not line:
                        continue
                    parsed = self._parser.parse(line)
                    # stdout в raw log — построчно; скрытые рассуждения модели не сохраняются нигде.
                    if not (parsed.kind == "message" and _hidden(parsed.data)):
                        raw.write_line("stdout", line)
                    if parsed.kind == "unparsed":
                        parse_errors += 1
                        self._emit_text(ex, transcript, "stdout", line, cut=False)
                        continue
                    parsed_lines += 1
                    if parsed.kind == "message":
                        if _hidden(parsed.data):
                            continue
                        self._emit_text(ex, transcript, "agent_message", str(parsed.data.get("text", "")), cut=False)
                    elif parsed.kind == "tool":
                        tool = _tool_payload(parsed.data)
                        transcript.write("tool", f"{tool['tool_kind']} {tool['phase']} {tool['target'] or ''}".rstrip())
                        self._emit(ex, "tool_event", tool)
                    elif parsed.kind == "result":
                        result_line = parsed.data
            if timed_out or ex.cancel_reason:
                await ex.process.terminate(self._grace)
            exit_code = await ex.process.wait()
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
            for line, cut in stderr_lines.flush():
                self._emit_text(ex, transcript, "stderr", line, cut=cut)
            if stdout_buf.strip():
                tail = stdout_buf.decode(errors="replace").strip()
                parsed = self._parser.parse(tail)
                if not (parsed.kind == "message" and _hidden(parsed.data)):
                    raw.write_line("stdout", tail)
                if parsed.kind == "result":
                    result_line = parsed.data
                elif parsed.kind == "unparsed":
                    parse_errors += 1
                    self._emit_text(ex, transcript, "stdout", tail, cut=False)

            if self._cancel_requested(ex):
                error = ("CANCELLED", f"отменено: {ex.cancel_reason}", None)
            elif timed_out:
                error = ("TIMEOUT", "превышен лимит времени или бездействия", None)
            else:
                cls = classify_output(stderr_tail.decode(errors="replace"), self._signatures)
                if cls is not None:
                    error = (cls.code, cls.signal, cls.retry_after_seconds)
                elif exit_code != 0:
                    error = ("PROCESS_FAILED", f"код выхода {exit_code}", None)
                elif result_line is None or (parse_errors and parse_errors > 0.2 * max(parsed_lines + parse_errors, 1)):
                    error = ("OUTPUT_PARSE_ERROR", "машиночитаемый результат не разобран", None)
        except Exception as exc:  # noqa: BLE001 — «ошибка = данные»
            error = ("PROCESS_FAILED", f"сбой адаптера: {type(exc).__name__}", None)
            if ex.process is not None:
                with contextlib.suppress(Exception):
                    await ex.process.terminate(self._grace)
        finally:
            self._prompt_texts.pop(req.execution_id, "")
            self._unwire_agent_io(ex)
            with contextlib.suppress(OSError):
                for f in Path(prompt_dir).glob("*"):  # noqa: ASYNC240 — пара маленьких файлов
                    os.chmod(f, 0o600)
                    f.unlink()
                os.rmdir(prompt_dir)
            ex.result = await self._finish(ex, raw, exit_code, result_line, error, transcript)
            raw.close()
            transcript.close()
            ex.done.set()
            ex.started_or_done.set()
            ex.new_event.set()

    async def _finish(
        self,
        ex: _Execution,
        raw: RawLog,
        exit_code: int | None,
        result_line: dict[str, Any] | None,
        error: tuple[CliErrorCode, str, int | None] | None,
        transcript: SanitizedTranscript | None = None,
    ) -> ExecutionResult:
        req = ex.request
        artifacts: list[dict[str, Any]] = []
        raw.finish()
        if transcript is not None and transcript.written:
            # Полный санитизированный вывод (API_DESIGN §14 п.5): доступен по artifact_id события `finished`.
            try:
                out = await self._sink(
                    req, kind="cli_output", data=transcript.stream(), mime_type="text/plain", sensitive=False
                )
                ref = {"ref": f"artifact:{out.artifact_id}", "sha256": out.sha256, "size_bytes": out.size_bytes}
                artifacts.append({"type": "output_excerpt", **ref})
                self._emit(ex, "artifact", {"artifact_type": "output_excerpt", **ref})
            except Exception:  # noqa: BLE001 — потеря копии вывода не должна скрыть результат
                self._emit(ex, "warning", {"code": "OTHER", "message": "санитизированный вывод не сохранён"})
        if raw.written:
            try:
                stored = await self._sink(
                    req, kind="cli_raw_log", data=raw.stream(), mime_type="text/plain", sensitive=True
                )
                artifacts.append(
                    {
                        "type": "raw_log",
                        "ref": f"artifact:{stored.artifact_id}",
                        "sha256": stored.sha256,
                        "size_bytes": stored.size_bytes,
                        "protected": True,
                    }
                )
                self._emit(
                    ex,
                    "artifact",
                    {
                        "artifact_type": "raw_log",
                        "ref": f"artifact:{stored.artifact_id}",
                        "sha256": stored.sha256,
                        "size_bytes": stored.size_bytes,
                    },
                )
            except Exception:  # noqa: BLE001 — потеря raw log не должна скрыть результат
                self._emit(ex, "warning", {"code": "OTHER", "message": "raw log не сохранён"})

        code, message, retry_after = error if error else (None, None, None)
        status: ExecutionStatus
        if code == "CANCELLED":
            status = "CANCELLED"
        elif code == "TIMEOUT":
            status = "TIMED_OUT"
        elif code in ("AUTH_REQUIRED", "RATE_LIMITED"):
            status = code
        elif (
            code in ("CLI_NOT_INSTALLED", "UNSUPPORTED_VERSION")
            or (code == "SANDBOX_DENIED" and ex.process is None)
            or (code == "PROCESS_FAILED" and exit_code is None)
        ):
            status = "CRASHED"
        else:
            status = "COLLECTED"

        finished_status = "EXITED" if status == "COLLECTED" else status
        self._emit(ex, "finished", {"execution_status": finished_status, "exit_code": exit_code, "error_code": code})
        ex.status = status

        claims: list[dict[str, Any]] = []
        usage: dict[str, Any] = {"tokens": None, "source": "not_exposed_by_cli"}
        if result_line and code is None:
            for c in (result_line.get("claims") or [])[:50]:
                claim_type = c.get("type") if c.get("type") in _CLAIM_TYPES else "other"
                summary, _, _ = sanitize_excerpt(str(c.get("summary") or ""))
                # Ссылки на доказательства — только на артефакты, созданные платформой, не слова агента.
                claims.append({"type": claim_type, "summary": summary[:2000] or None, "evidence_refs": []})
            u = result_line.get("usage")
            if (
                isinstance(u, dict)
                and isinstance(u.get("input_tokens"), int)
                and isinstance(u.get("output_tokens"), int)
            ):
                usage = {
                    "tokens": u["input_tokens"] + u["output_tokens"],
                    "input_tokens": u["input_tokens"],
                    "output_tokens": u["output_tokens"],
                    "source": "cli_reported",
                }

        result: dict[str, Any] = {
            "contract_version": "1.0",
            "execution_id": str(req.execution_id),
            "provider": self.provider,
            "status": "cancelled" if status == "CANCELLED" else ("completed" if code is None else "failed"),
            "execution_status": status,
            "exit_code": exit_code if exit_code is not None and exit_code >= 0 else None,
            "signal": _signal_name(exit_code),
            "artifacts": artifacts,
            "claims": claims,
            "verification": {"status": "pending"},  # exit 0 ≠ verified (ТЗ §9.3)
            "usage": usage,
            "error": None
            if code is None
            else {
                "code": code,
                "message": (message or "")[:500],
                "retryable": RETRYABLE[code],
                "retry_after_seconds": retry_after,
                "detection_signal": (message or "")[:200],
            },
            "cli_version": self._cli_version,
            "adapter_version": self.adapter_version,
            "command_profile": self._profile.id,
            "started_at": ex.started_at.isoformat() if ex.started_at else None,
            "finished_at": utcnow().isoformat(),
            "events_count": len(ex.events),
            "truncated": raw.truncated,
        }
        # Необязательные поля контракта при отсутствии значения опускаются (схема не допускает null),
        # обязательные nullable (exit_code, started_at, usage.tokens) — остаются.
        for key in ("error", "signal", "cli_version", "adapter_version", "command_profile"):
            if result.get(key) is None:
                result.pop(key, None)
        if result.get("error") and result["error"].get("retry_after_seconds") is None:
            result["error"].pop("retry_after_seconds")
        return ExecutionResult.model_validate_json(json.dumps(result))


_CLAIM_TYPES = frozenset(
    {
        "implementation_completed",
        "tests_added",
        "tests_passed",
        "review_completed",
        "analysis_completed",
        "documentation_updated",
        "migration_prepared",
        "no_changes_needed",
        "other",
    }
)
_TOOL_KINDS = frozenset({"file_read", "file_write", "file_patch", "command", "search", "network", "other"})
_PHASES = frozenset({"requested", "started", "completed", "denied", "failed"})


def _hidden(data: dict[str, Any]) -> bool:
    kind = data.get("type") or data.get("kind")
    return bool(data.get("hidden")) or (isinstance(kind, str) and kind.lower() in HIDDEN_MESSAGE_TYPES)


def _tool_payload(data: dict[str, Any]) -> dict[str, Any]:
    target, _, _ = sanitize_excerpt(str(data.get("target") or ""))
    return {
        "tool_kind": data.get("tool_kind") if data.get("tool_kind") in _TOOL_KINDS else "other",
        "phase": data.get("phase") if data.get("phase") in _PHASES else "completed",
        "target": target[:1024] or None,
    }


def _signal_name(exit_code: int | None) -> Literal["SIGTERM", "SIGKILL"] | None:
    if exit_code == -signal.SIGTERM:
        return "SIGTERM"
    if exit_code == -signal.SIGKILL:
        return "SIGKILL"
    return None
