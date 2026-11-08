"""CLI Gateway (P2-11): исполнение ExecutionRequest с учётом в `cli_executions` по Execution FSM.

* идемпотентность по `execution_id`: повтор для существующей записи не запускает второй процесс;
* лимит параллелизма на установку (`cli_installations.max_concurrency`) — под блокировкой строки установки;
* каждая запись состояния условна по fencing token (CLI_ADAPTER_CONTRACT §2); полноценные leases — P3;
* артефакты (raw log, нормализованный результат, git diff) сохраняются с sha256; diff вычисляет платформа;
* usage: токены только если CLI их сообщил, иначе `not_exposed_by_cli` (ТЗ §9.3).
Очередь (Dramatiq) и выдача заданий воркерам появятся в Phase 3; здесь — исполнитель задания.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_cli_adapters.models import ExecutionRequest, ExecutionResult, NormalizedCliEvent, result_json
from aicd_cli_adapters.subprocess_adapter import StoredArtifact, SubprocessCLIAdapter
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7

EventSink = Callable[[NormalizedCliEvent], Awaitable[None]]


class ConcurrencyLimitReachedError(RuntimeError):
    """Нет свободного слота установки CLI — задание нужно отложить (не ошибка исполнения)."""


class StaleFencingTokenError(RuntimeError):
    """Запись отклонена: исполнением владеет другой воркер (fencing)."""


@dataclass(frozen=True)
class GatewayOutcome:
    result: ExecutionResult
    reused: bool  # True — исполнение уже существовало (повтор доставки)


def _request_hash(request: ExecutionRequest) -> str:
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


class ExecutionGateway:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        store: ArtifactStore,
        diff_provider: Callable[[ExecutionRequest], Awaitable[bytes | None]] | None = None,
    ) -> None:
        self._engine = engine
        self._store = store
        self._diff_provider = diff_provider

    def _ctx(self, request: ExecutionRequest) -> DbContext:
        return DbContext(workspace_id=request.workspace_id, actor_type="system")

    # --- артефакты ---------------------------------------------------------------------------
    async def store_artifact(
        self, request: ExecutionRequest, *, kind: str, data: BinaryIO, mime_type: str, sensitive: bool
    ) -> StoredArtifact:
        artifact_id = uuid7()
        stored = self._store.put(request.workspace_id, artifact_id, data)
        async with transaction(self._engine, self._ctx(request)) as conn:
            await conn.execute(
                text(
                    "INSERT INTO artifacts (id, workspace_id, kind, storage_ref, sha256, size_bytes, mime_type,"
                    " is_sensitive, redaction_applied) VALUES (:id, :ws, :kind, :ref, :sha, :size, :mime, :sens, true)"
                ),
                {
                    "id": artifact_id,
                    "ws": request.workspace_id,
                    "kind": kind,
                    "ref": stored.storage_ref,
                    "sha": stored.sha256,
                    "size": stored.size_bytes,
                    "mime": mime_type,
                    "sens": sensitive,
                },
            )
        return StoredArtifact(artifact_id, stored.sha256, stored.size_bytes)

    # --- исполнение --------------------------------------------------------------------------
    async def run(
        self,
        request: ExecutionRequest,
        adapter: SubprocessCLIAdapter,
        *,
        installation_id: uuid.UUID,
        on_event: EventSink | None = None,
    ) -> GatewayOutcome:
        existing = await self._existing(request)
        if existing is not None:
            return existing
        token = await self._admit(request, installation_id)
        handle = await adapter.execute(request)
        ref = await adapter.wait_started(request.execution_id)
        if ref is not None:
            await self._transition(request, token, "STARTING", extra="pid_ref = :ref", params={"ref": ref})
            await self._transition(request, token, "RUNNING", extra="started_at = now()")
        async for event in adapter.stream_events(handle.execution_id):
            if on_event is not None:
                await on_event(event)
        result = await adapter.collect_result(request.execution_id)
        result = await self._attach_diff(request, result)
        await self._complete(request, token, result, started=ref is not None)
        return GatewayOutcome(result=result, reused=False)

    async def _existing(self, request: ExecutionRequest) -> GatewayOutcome | None:
        async with transaction(self._engine, self._ctx(request)) as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT status, request_hash, output_artifact_id FROM cli_executions WHERE id = :id"),
                        {"id": request.execution_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["request_hash"] != _request_hash(request):
                raise ValueError("execution_id уже использован с другим запросом")
            if row["output_artifact_id"] is None:
                # Исполнение не завершено: повтор доставки не запускает второй процесс (recovery — P3).
                raise ConcurrencyLimitReachedError("исполнение уже выполняется")
            ref = (
                (
                    await conn.execute(
                        text("SELECT storage_ref, sha256 FROM artifacts WHERE id = :id"),
                        {"id": row["output_artifact_id"]},
                    )
                )
                .mappings()
                .one()
            )
        data = b"".join(self._store.open(ref["storage_ref"], expected_sha256=ref["sha256"]))
        return GatewayOutcome(result=ExecutionResult.model_validate_json(data), reused=True)

    async def _admit(self, request: ExecutionRequest, installation_id: uuid.UUID) -> int:
        """QUEUED + проверка лимита параллелизма в одной транзакции под блокировкой установки."""
        async with transaction(self._engine, self._ctx(request)) as conn:
            limit = (
                await conn.execute(
                    text("SELECT max_concurrency FROM cli_installations WHERE id = :id AND enabled FOR UPDATE"),
                    {"id": installation_id},
                )
            ).scalar_one_or_none()
            if limit is None:
                raise ConcurrencyLimitReachedError("установка CLI недоступна")
            active = await self._active_count(conn, installation_id)
            if active >= limit:
                raise ConcurrencyLimitReachedError(f"занято {active} из {limit} слотов установки")
            token = int((await conn.execute(text("SELECT nextval('worker_lease_fencing_seq')"))).scalar_one())
            await conn.execute(
                text(
                    "INSERT INTO cli_executions (id, workspace_id, task_attempt_id, installation_id, worktree_id,"
                    " command_profile, request_hash, worktree_ref, timeout_seconds, fencing_token)"
                    " VALUES (:id, :ws, :attempt, :inst, NULL, :profile, :hash, :wt_ref, :timeout, :token)"
                ),
                {
                    "id": request.execution_id,
                    "ws": request.workspace_id,
                    "attempt": request.task_attempt_id,
                    "inst": installation_id,
                    "profile": _profile_slug(request.command_profile),
                    "hash": _request_hash(request),
                    "wt_ref": str(request.workspace.worktree_id),
                    "timeout": max(30, request.limits.timeout_seconds),
                    "token": token,
                },
            )
        return token

    @staticmethod
    async def _active_count(conn: AsyncConnection, installation_id: uuid.UUID) -> int:
        # Подсчёт по всем workspace (лимит — свойство установки): SECURITY DEFINER не нужен,
        # т.к. воркер видит только свой workspace — поэтому считаем через агрегирующую функцию БД.
        value = (await conn.execute(text("SELECT sys_active_executions(:id)"), {"id": installation_id})).scalar_one()
        return int(value)

    async def _transition(
        self,
        request: ExecutionRequest,
        token: int,
        status: str,
        *,
        extra: str = "",
        params: dict[str, object] | None = None,
    ) -> None:
        sets = "status = :status" + (", " + extra if extra else "")
        async with transaction(self._engine, self._ctx(request)) as conn:
            res = await conn.execute(
                text(f"UPDATE cli_executions SET {sets} WHERE id = :id AND fencing_token = :token"),  # noqa: S608
                {"status": status, "id": request.execution_id, "token": token, **(params or {})},
            )
            if res.rowcount != 1:
                raise StaleFencingTokenError(str(request.execution_id))

    async def _attach_diff(self, request: ExecutionRequest, result: ExecutionResult) -> ExecutionResult:
        if self._diff_provider is None or result.execution_status != "COLLECTED":
            return result
        diff = await self._diff_provider(request)
        if not diff or not diff.strip():
            return result
        stored = await self.store_artifact(
            request, kind="git_diff", data=io.BytesIO(diff), mime_type="text/x-diff", sensitive=False
        )
        data = result.model_dump(mode="json", exclude_unset=True)
        data["artifacts"].append(
            {
                "type": "git_diff",
                "ref": f"artifact:{stored.artifact_id}",
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
            }
        )
        return ExecutionResult.model_validate(data)

    async def _complete(self, request: ExecutionRequest, token: int, result: ExecutionResult, *, started: bool) -> None:
        payload = result_json(result).encode()
        output = await self.store_artifact(
            request, kind="cli_output", data=io.BytesIO(payload), mime_type="application/json", sensitive=False
        )
        raw_id = next((a.ref.removeprefix("artifact:") for a in result.artifacts if a.type == "raw_log"), None)
        failure = result.error.code if result.error else None
        status = result.execution_status
        if status == "COLLECTED":
            # RUNNING → EXITED → COLLECTED: процесс завершился, результат собран.
            await self._transition(
                request,
                token,
                "EXITED",
                extra="exit_code = :code, exited_at = now()",
                params={"code": result.exit_code if result.exit_code is not None else -1},
            )
        elif not started and status in ("TIMED_OUT", "AUTH_REQUIRED", "RATE_LIMITED"):
            await self._transition(request, token, "STARTING")
        await self._transition(
            request,
            token,
            status,
            extra="failure_category = :fail, output_artifact_id = :out, raw_log_artifact_id = :raw,"
            " finished_at = now(), exit_code = COALESCE(exit_code, :code)",
            params={
                "fail": failure,
                "out": output.artifact_id,
                "raw": uuid.UUID(raw_id) if raw_id else None,
                "code": result.exit_code,
            },
        )
        async with transaction(self._engine, self._ctx(request)) as conn:
            await self._record_usage(conn, request, result)

    @staticmethod
    async def _record_usage(conn: AsyncConnection, request: ExecutionRequest, result: ExecutionResult) -> None:
        rows: list[tuple[str, float | None, str, str]] = []
        if result.usage.source == "cli_reported" and result.usage.tokens is not None:
            rows.append(("tokens.total", float(result.usage.tokens), "tokens", "cli_reported"))
        else:
            rows.append(("tokens.total", None, "tokens", "not_exposed_by_cli"))
        if result.started_at is not None:
            duration = (result.finished_at - result.started_at).total_seconds()
            rows.append(("duration.wall", max(duration, 0.0), "seconds", "platform_measured"))
        for name, value, unit, source in rows:
            await conn.execute(
                text(
                    "INSERT INTO usage_metrics (id, workspace_id, execution_id, provider, metric_name, metric_value,"
                    " unit, source) VALUES (:id, :ws, :ex, :prov, :name, :val, :unit, :src)"
                ),
                {
                    "id": uuid7(),
                    "ws": request.workspace_id,
                    "ex": request.execution_id,
                    "prov": request.provider,
                    "name": name,
                    "val": value,
                    "unit": unit,
                    "src": source,
                },
            )


def _profile_slug(profile: str) -> str:
    """`codex/agent_task@1` → `codex.agent_task.1` (формат CHECK колонки command_profile)."""
    slug = profile.lower().replace("/", ".").replace("@", ".")
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in slug)[:64]


def worktree_resolver(root: Path) -> Callable[[ExecutionRequest], Awaitable[Path]]:
    async def resolve(request: ExecutionRequest) -> Path:
        path = root / str(request.workspace.worktree_id)
        if not path.is_dir():
            raise FileNotFoundError("worktree не найден")
        return path

    return resolve
