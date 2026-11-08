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
from aicd_cli_adapters.output import RawLog, sanitize_excerpt
from aicd_cli_adapters.profiles import CommandProfile, ProfileError, build_argv, build_env
from aicd_platform.clock import utcnow
from aicd_security.sandbox import ContainerSpec, Mount, ResourceLimits, SandboxProcess, SandboxRunner

STDERR_TAIL = 64 * 1024


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
        self._executions: dict[UUID, _Execution] = {}
        self._lock = asyncio.Lock()

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

    async def _run(self, ex: _Execution) -> None:
        req = ex.request
        limits = req.limits
        raw = RawLog(max_bytes=limits.max_output_bytes or 32 * 1024 * 1024)
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
                mounts: tuple[Mount, ...] = ()
                if self._container is None:
                    values = {"binary": self._binary, "prompt_file": str(prompt_file), **self._extra}
                else:
                    os.chmod(prompt_dir, 0o755)  # noqa: S103 — каталог задачи читает uid runner; файл 0444
                    layout = self._container
                    values = {
                        **self._extra,
                        **layout.placeholder_overrides,
                        "binary": layout.binary,
                        "prompt_file": f"{layout.prompt_mount}/task.json",
                    }
                    mounts = (
                        Mount(cwd, self._profile.cwd, read_only=self._profile.write_scope == "none"),
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
                raw.write(chunk.stream, chunk.data)
                if raw.truncated and not truncated_warned:
                    truncated_warned = True
                    self._emit(ex, "warning", {"code": "OUTPUT_TRUNCATED", "message": "raw log достиг лимита"})
                if chunk.stream == "stderr":
                    stderr_tail.extend(chunk.data)
                    del stderr_tail[:-STDERR_TAIL]
                    text, redactions, truncated = sanitize_excerpt(chunk.data.decode(errors="replace"))
                    self._emit(
                        ex,
                        "output",
                        {"stream": "stderr", "text": text, "redactions": redactions, "truncated": truncated},
                    )
                    continue
                stdout_buf += chunk.data
                *lines, stdout_buf = stdout_buf.split(b"\n")
                for line_bytes in lines:
                    line = line_bytes.decode(errors="replace").strip()
                    if not line:
                        continue
                    parsed = self._parser.parse(line)
                    if parsed.kind == "unparsed":
                        parse_errors += 1
                        continue
                    parsed_lines += 1
                    if parsed.kind == "message":
                        text, redactions, truncated = sanitize_excerpt(str(parsed.data.get("text", "")))
                        self._emit(
                            ex,
                            "output",
                            {"stream": "agent_message", "text": text, "redactions": redactions, "truncated": truncated},
                        )
                    elif parsed.kind == "tool":
                        self._emit(ex, "tool_event", _tool_payload(parsed.data))
                    elif parsed.kind == "result":
                        result_line = parsed.data
            if timed_out or ex.cancel_reason:
                await ex.process.terminate(self._grace)
            exit_code = await ex.process.wait()
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
            if stdout_buf.strip():
                parsed = self._parser.parse(stdout_buf.decode(errors="replace").strip())
                if parsed.kind == "result":
                    result_line = parsed.data
                elif parsed.kind == "unparsed":
                    parse_errors += 1

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
            with contextlib.suppress(OSError):
                for f in Path(prompt_dir).glob("*"):  # noqa: ASYNC240 — один маленький файл
                    os.chmod(f, 0o600)
                    f.unlink()
                os.rmdir(prompt_dir)
            ex.result = await self._finish(ex, raw, exit_code, result_line, error)
            raw.close()
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
    ) -> ExecutionResult:
        req = ex.request
        artifacts: list[dict[str, Any]] = []
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
