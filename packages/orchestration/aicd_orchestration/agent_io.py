"""Ввод-вывод агента в исполнении задачи (Phase 4; AGENT_ROLES §3.3–§3.4, ACP §1, §5, §7, §9.2).

**Промпт → CLI.** Перед запуском CLI оркестратор собирает промпт роли (`agents.prompt.build_prompt`): профиль
роли `tasks.agent_role`, шаблон инструкций `<role>@<version>`, поля задачи и раздел `inbox`. Промпт
сохраняется artifact-ом `kind='context_pack'` (`is_sensitive=true`: может содержать код клиента; отдельного
вида `prompt` в CHECK `artifacts.kind` нет), его sha256 и манифест — в `agent_activity_events`
(`agent.prompt_assembled`), ссылка — в `ExecutionRequest.input.context_pack` (`context_pack_id` = id
artifact-а: таблицы `context_packs` ещё нет). Адаптер пишет текст рядом с `task.json` как `prompt.md`
(плейсхолдер `{prompt_text_file}`).

**Правило inbox (упрощение ACP §7).** В inbox попадают все *принятые брокером* сообщения этого run, адресованные
задаче (`recipient_task_id` = задача) или роли задачи без конкретной задачи (`recipient_role` = роль,
`recipient_task_id IS NULL`), кроме отправленных самой задачей и сохранённых с `loop_limit_exceeded` (у них
нет доставки `orchestrator.acp_handler`); не более `INBOX_LIMIT` последних. Подтверждение (`acked_at`)
обработчиком не требуется, «уже включённые в прошлый Context Pack» не исключаются (учёт включений — вместе с
таблицей `context_packs`). Всё содержимое inbox — **недоверенные данные**: только внутри ограждений UNTRUSTED.

**Результат ← CLI.** Итог читается из отдельного каталога вывода исполнения (не из worktree): адаптер
направляет `.aicd/` и `output/` в cwd CLI в каталог `<корень worktree>/.aicd-io/<execution_id>/` (контейнер —
rw bind-монтирования поверх, в т.ч. read-only worktree ролей с `write_scope=none`; локально — symlink).
Так ролям только для чтения есть куда писать итог, а итог пишущих ролей не смешивается с изменениями кода;
`.aicd/` и `output/` дополнительно исключаются из коммита/diff задачи (`worktrees`). Итог разбирается
`agents.result.load_agent_result`; `declared_outputs` сохраняются artifact-ами проекта, ссылки `output/<путь>`
в payload заменяются на `artifact:<uuid>`. Факт платформы `commit_sha` (REVIEW_REQUEST — всегда; TASK_COMPLETED
и элементы EVIDENCE_SUBMITTED — если указан) заменяется снимком worktree после исполнения: агент не знает SHA
коммита, который сделает платформа. Каждый кандидат отправляется `AcpBroker.submit` с отправителем
(роль, провайдер, задача, попытка) в одной транзакции воркера (savepoint на кандидата); отказы — данные,
исполнение не прерывают. Отказы предварительной проверки (`agents.result`) пишутся тем же событием
`acp.rejected`, что и отказы брокера, и учитываются в правиле «≥ 3 отказа ACL/POLICY в попытке».
Claims — заявления (`verified=false`), сохраняются в `AgentIoSummary` и событии `agent.result_processed`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aicd_collaboration.acp import CONSUMER_ACP_HANDLER, Accepted, AcpBroker, Rejected, Sender
from aicd_collaboration.acp.broker import ACTIVITY_REJECTED, insert_activity
from aicd_collaboration.acp.sql import SqlFacts
from aicd_orchestration.agents.profiles import Permissions, profile
from aicd_orchestration.agents.prompt import (
    RESULT_PATH,
    AssembledPrompt,
    TaskInput,
    UntrustedFragment,
    build_prompt,
)
from aicd_orchestration.agents.result import (
    Claim,
    ParsedAgentResult,
    ResultParseError,
    load_agent_result,
)
from aicd_platform.artifacts import ArtifactStore
from aicd_platform.db import DbContext, transaction
from aicd_platform.ids import uuid7

INBOX_LIMIT: Final = 50
MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024
ACTIVITY_PROMPT: Final = "agent.prompt_assembled"
ACTIVITY_RESULT: Final = "agent.result_processed"
_ACTIVITY_BUDGET: Final = 30_000  # ck_activity_payload: ≤ 32 KiB
# declared_outputs.kind → artifacts.kind (CHECK миграции 0001).
_OUTPUT_KIND: Final[Mapping[str, str]] = {
    "documentation": "documentation",
    "requirements_doc": "documentation",
    "adr": "documentation",
    "test_report": "test_report",
}
_MIME: Final[Mapping[str, str]] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".diff": "text/x-diff",
    ".patch": "text/x-diff",
    ".json": "application/json",
}
_COMMIT_SHA_TYPES: Final = frozenset({"REVIEW_REQUEST", "TASK_COMPLETED"})

ResultStatus = Literal["parsed", "missing", "invalid", "unavailable", "skipped", "error"]


# --- Inbox ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InboxMessage:
    id: uuid.UUID
    type: str
    sender_role: str
    sender_provider: str | None
    task_id: uuid.UUID | None
    correlation_id: uuid.UUID
    reply_to_id: uuid.UUID | None
    payload: dict[str, Any]
    created_at: datetime

    def fragment(self) -> UntrustedFragment:
        """Фрагмент раздела inbox: одна строка JSON (метаданные платформы + payload агента/пользователя)."""
        sender: dict[str, Any] = {"role": self.sender_role}
        if self.sender_provider:
            sender["provider"] = self.sender_provider
        body = {
            "message_id": str(self.id),
            "type": self.type,
            "from": sender,
            "correlation_id": str(self.correlation_id),
            "reply_to_id": str(self.reply_to_id) if self.reply_to_id else None,
            "created_at": self.created_at.isoformat(),
            "payload": self.payload,
        }
        return UntrustedFragment(
            kind="inbox",
            content=json.dumps(body, ensure_ascii=False),
            ref=f"message:{self.id}",
            rank=int(self.created_at.timestamp()),
        )


async def load_inbox(
    conn: AsyncConnection, *, run_id: uuid.UUID, task_id: uuid.UUID, role: str, limit: int = INBOX_LIMIT
) -> list[InboxMessage]:
    rows = (
        await conn.execute(
            text(
                "SELECT m.id, m.message_type, m.sender_role, m.sender_provider, m.task_id, m.correlation_id,"
                " m.reply_to_id, m.payload, m.created_at FROM agent_messages m"
                " WHERE m.run_id = :run"
                " AND (m.recipient_task_id = :task OR (m.recipient_task_id IS NULL AND m.recipient_role = :role))"
                " AND m.task_id IS DISTINCT FROM :task"
                " AND EXISTS (SELECT 1 FROM message_deliveries d WHERE d.message_id = m.id AND d.consumer_id = :c)"
                " ORDER BY m.created_at DESC, m.id DESC LIMIT :lim"
            ),
            {"run": run_id, "task": task_id, "role": role, "c": CONSUMER_ACP_HANDLER, "lim": limit},
        )
    ).all()
    return [
        InboxMessage(
            id=r.id,
            type=r.message_type,
            sender_role=r.sender_role,
            sender_provider=r.sender_provider,
            task_id=r.task_id,
            correlation_id=r.correlation_id,
            reply_to_id=r.reply_to_id,
            payload=dict(r.payload),
            created_at=r.created_at,
        )
        for r in reversed(rows)
    ]


# --- Промпт -------------------------------------------------------------------------------------------


def role_permissions(role: str) -> Permissions:
    return profile(role).permissions


def task_input(task: Mapping[str, Any], run: Mapping[str, Any]) -> TaskInput:
    instructions = str(task.get("instructions") or "")
    return TaskInput(
        task_key=str(task["task_key"]),
        mode=str(run.get("mode") or "DEVELOP"),
        complexity=int(task.get("complexity") or 1),
        risk_level=str(task.get("risk_level") or "low"),
        title=str(task.get("title") or task["task_key"]),
        objective=str(run.get("objective") or instructions),
        instructions=instructions,
        acceptance_criteria=tuple(str(c) for c in task.get("acceptance_criteria") or ()),
        base_commit=run.get("base_commit_sha"),
    )


def assemble_prompt(task: Mapping[str, Any], run: Mapping[str, Any], inbox: Sequence[InboxMessage]) -> AssembledPrompt:
    return build_prompt(profile(str(task["agent_role"])), task_input(task, run), [m.fragment() for m in inbox])


@dataclass(frozen=True)
class StoredFile:
    artifact_id: uuid.UUID
    sha256: str
    size_bytes: int


async def store_artifact(
    conn: AsyncConnection,
    store: ArtifactStore,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    kind: str,
    data: bytes,
    mime_type: str,
    sensitive: bool,
) -> StoredFile:
    """Artifact проекта/run (project_id обязателен: брокер проверяет ссылки `artifact:` в пределах проекта)."""
    artifact_id = uuid7()
    stored = await asyncio.to_thread(store.put, workspace_id, artifact_id, io.BytesIO(data))
    await conn.execute(
        text(
            "INSERT INTO artifacts (id, workspace_id, project_id, run_id, kind, storage_ref, sha256, size_bytes,"
            " mime_type, is_sensitive) VALUES (:id, :ws, :p, :r, :kind, :ref, :sha, :size, :mime, :sens)"
        ),
        {
            "id": artifact_id,
            "ws": workspace_id,
            "p": project_id,
            "r": run_id,
            "kind": kind,
            "ref": stored.storage_ref,
            "sha": stored.sha256,
            "size": stored.size_bytes,
            "mime": mime_type,
            "sens": sensitive,
        },
    )
    return StoredFile(artifact_id, stored.sha256, stored.size_bytes)


# --- Итог ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectedCandidate:
    stage: Literal["result", "broker"]  # предварительная проверка `agents.result` или конвейер брокера
    index: int  # позиция в `acp_messages` итога
    type: str | None
    code: str  # acp_rejection_code (или INTERNAL_ERROR — сбой обработки одного кандидата)
    reason: str


@dataclass(frozen=True)
class AgentIoSummary:
    """Итог ввода-вывода агента для одной попытки (возвращается оркестратору вместе с TaskExecution)."""

    instruction_template_version: str
    prompt_sha256: str
    prompt_artifact_id: uuid.UUID | None
    inbox_message_ids: tuple[uuid.UUID, ...] = ()
    # parsed — итог разобран; missing — файла нет; invalid — итог отклонён целиком (OUTPUT_PARSE_ERROR);
    # unavailable — каталога вывода нет (повтор доставки исполнения); skipped — CLI не завершился успешно;
    # error — сбой обработки на стороне платформы.
    result_status: ResultStatus = "unavailable"
    result_error: str | None = None
    result_sha256: str | None = None
    summary: str | None = None
    claims: tuple[Claim, ...] = ()  # заявления агента: verified=False, подтверждают только гейты
    output_artifacts: Mapping[str, uuid.UUID] = field(default_factory=dict)  # `output/<путь>` → artifact
    accepted_message_ids: tuple[uuid.UUID, ...] = ()
    rejected: tuple[RejectedCandidate, ...] = ()
    review_verdict: str | None = None  # из принятого TASK_COMPLETED (reviewer/security)
    attempt_should_fail: bool = False  # ≥ 3 отказов ACL/POLICY в попытке ⇒ failure_category POLICY_DENIED (§5)
    warnings: tuple[str, ...] = ()

    @property
    def rejected_codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.rejected)

    def as_json(self) -> dict[str, Any]:
        return {
            "instruction_template_version": self.instruction_template_version,
            "prompt_sha256": self.prompt_sha256,
            "prompt_artifact_id": str(self.prompt_artifact_id) if self.prompt_artifact_id else None,
            "inbox_message_ids": [str(i) for i in self.inbox_message_ids],
            "result_status": self.result_status,
            "result_error": self.result_error,
            "result_sha256": self.result_sha256,
            "summary": self.summary,
            "claims": [
                {"type": c.type, "summary": c.summary, "files": list(c.files), "verified": False} for c in self.claims
            ],
            "output_artifacts": {k: str(v) for k, v in self.output_artifacts.items()},
            "accepted_message_ids": [str(i) for i in self.accepted_message_ids],
            "rejected": [
                {"stage": r.stage, "index": r.index, "type": r.type, "code": r.code, "reason": r.reason}
                for r in self.rejected
            ],
            "review_verdict": self.review_verdict,
            "attempt_should_fail": self.attempt_should_fail,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class AgentContext:
    """Контекст отправителя: задаётся оркестратором, а не итогом агента (ACP §1)."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID
    attempt_id: uuid.UUID
    role: str
    provider: str
    execution_id: uuid.UUID

    def sender(self) -> Sender:
        return Sender(
            role=self.role,
            provider=self.provider,
            task_id=self.task_id,
            task_attempt_id=self.attempt_id,
            source_execution_id=self.execution_id,
        )


def message_id_for(execution_id: uuid.UUID, index: int) -> uuid.UUID:
    """Детерминированный UUIDv7 кандидата (идемпотентная повторная обработка того же исполнения)."""
    digest = hashlib.sha256(execution_id.bytes + index.to_bytes(4, "big")).digest()
    raw = bytearray(execution_id.bytes[:6] + digest[:10])  # 48 бит времени исполнения + хеш
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return uuid.UUID(bytes=bytes(raw))


def _read_output(root: Path, rel: str) -> bytes:
    target = root.joinpath(*PurePosixPath(rel).parts)
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("не обычный файл")
        if st.st_size > MAX_OUTPUT_BYTES:
            raise ValueError("превышен лимит размера вывода")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _substitute(value: Any, mapping: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, dict):
        return {k: _substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, mapping) for v in value]
    return value


def _platform_facts(message_type: str, payload: dict[str, Any], commit_sha: str) -> bool:
    """`commit_sha` — факт платформы (снимок worktree), а не заявление агента. True — значение изменено."""
    changed = False
    if message_type in _COMMIT_SHA_TYPES and (message_type == "REVIEW_REQUEST" or "commit_sha" in payload):
        changed = payload.get("commit_sha") != commit_sha
        payload["commit_sha"] = commit_sha
    if message_type == "EVIDENCE_SUBMITTED":
        for item in payload.get("items") or []:
            if isinstance(item, dict) and "commit_sha" in item and item["commit_sha"] != commit_sha:
                item["commit_sha"] = commit_sha
                changed = True
    return changed


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Payload события активности в пределах ck_activity_payload (обрезаются списки, затем тексты)."""
    data = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    for key in ("fragments", "claims", "rejected", "inbox_message_ids", "accepted_message_ids", "warnings"):
        if len(json.dumps(data, ensure_ascii=False).encode()) <= _ACTIVITY_BUDGET:
            break
        if isinstance(data.get(key), list) and data[key]:
            data[f"{key}_total"] = len(data[key])
            data[key] = data[key][:10]
    if len(json.dumps(data, ensure_ascii=False).encode()) > _ACTIVITY_BUDGET:
        data = {k: v for k, v in data.items() if not isinstance(v, list | dict | str) or len(str(v)) < 500}
        data["truncated"] = True
    return dict(data)


async def record_prompt(
    conn: AsyncConnection,
    ctx: AgentContext,
    prompt: AssembledPrompt,
    *,
    artifact_id: uuid.UUID | None,
    inbox: Sequence[InboxMessage],
) -> None:
    manifest = dict(prompt.manifest)
    payload = {
        "task_attempt_id": str(ctx.attempt_id),
        "artifact_id": str(artifact_id) if artifact_id else None,
        **manifest,
        "inbox_message_ids": [str(m.id) for m in inbox],
    }
    await insert_activity(
        conn,
        workspace_id=ctx.workspace_id,
        run_id=ctx.run_id,
        task_id=ctx.task_id,
        execution_id=ctx.execution_id,
        event_type=ACTIVITY_PROMPT,
        payload=_fit(payload),
        correlation_id=None,
    )


async def _record_result_rejection(conn: AsyncConnection, ctx: AgentContext, rej: RejectedCandidate) -> None:
    # Та же форма, что у отказов брокера (broker._reject): учитывается в denials_in_attempt.
    await insert_activity(
        conn,
        workspace_id=ctx.workspace_id,
        run_id=ctx.run_id,
        task_id=ctx.task_id,
        execution_id=ctx.execution_id,
        event_type=ACTIVITY_REJECTED,
        payload={
            "rejection_code": rej.code,
            "reason": rej.reason[:1000],
            "message_type": rej.type,
            "from_role": ctx.role,
            "task_attempt_id": str(ctx.attempt_id),
            "stage": "result",
            "index": rej.index,
        },
        correlation_id=None,
    )


class ResultProcessor:
    """Разбор итога агента и отправка ACP-кандидатов брокеру (одна транзакция воркера)."""

    def __init__(self, *, engine: AsyncEngine, store: ArtifactStore, broker: AcpBroker) -> None:
        self.engine = engine
        self.store = store
        self.broker = broker

    def read(self, io_dir: Path | None, role: str) -> tuple[ParsedAgentResult | None, ResultStatus, str | None]:
        if io_dir is None:
            return None, "unavailable", "каталог вывода агента недоступен"
        if not os.path.lexists(io_dir.joinpath(*PurePosixPath(RESULT_PATH).parts)):
            return None, "missing", "файл результата отсутствует"
        try:
            return load_agent_result(io_dir, sender_role=role), "parsed", None
        except ResultParseError as exc:
            return None, "invalid", f"{exc.code}: {exc.reason}"

    async def process(
        self,
        summary: AgentIoSummary,
        ctx: AgentContext,
        *,
        io_dir: Path | None,
        commit_sha: str,
    ) -> AgentIoSummary:
        parsed, status, error = await asyncio.to_thread(self.read, io_dir, ctx.role)
        summary = replace(summary, result_status=status, result_error=error)
        async with transaction(self.engine, DbContext(workspace_id=ctx.workspace_id, actor_type="agent")) as conn:
            if parsed is not None and io_dir is not None:
                summary = await self._submit(conn, summary, ctx, parsed, io_dir, commit_sha)
            await insert_activity(
                conn,
                workspace_id=ctx.workspace_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
                execution_id=ctx.execution_id,
                event_type=ACTIVITY_RESULT,
                payload=_fit({"task_attempt_id": str(ctx.attempt_id), **summary.as_json()}),
                correlation_id=None,
            )
        return summary

    async def _store_outputs(
        self, conn: AsyncConnection, ctx: AgentContext, parsed: ParsedAgentResult, io_dir: Path
    ) -> tuple[dict[str, StoredFile], list[str]]:
        stored: dict[str, StoredFile] = {}
        warnings: list[str] = []
        for out in parsed.declared_outputs:
            try:
                data = await asyncio.to_thread(_read_output, io_dir, out.path)
            except (OSError, ValueError):
                warnings.append(f"output_not_stored:{out.path}"[:300])
                continue
            stored[out.path] = await store_artifact(
                conn,
                self.store,
                workspace_id=ctx.workspace_id,
                project_id=ctx.project_id,
                run_id=ctx.run_id,
                kind=_OUTPUT_KIND.get(out.kind, "report"),
                data=data,
                mime_type=_MIME.get(PurePosixPath(out.path).suffix.lower(), "application/octet-stream"),
                sensitive=False,
            )
        return stored, warnings

    async def _submit(
        self,
        conn: AsyncConnection,
        summary: AgentIoSummary,
        ctx: AgentContext,
        parsed: ParsedAgentResult,
        io_dir: Path,
        commit_sha: str,
    ) -> AgentIoSummary:
        stored, warnings = await self._store_outputs(conn, ctx, parsed, io_dir)
        refs = {path: f"artifact:{f.artifact_id}" for path, f in stored.items()}
        rejected = [RejectedCandidate("result", r.index, r.type, r.code, r.reason) for r in parsed.rejected_messages]
        for rej in rejected:
            await _record_result_rejection(conn, ctx, rej)
        # Индексы принятых предварительной проверкой кандидатов в исходном списке acp_messages.
        rejected_idx = {r.index for r in parsed.rejected_messages}
        indexes = [i for i in range(len(parsed.acp_messages) + len(rejected_idx)) if i not in rejected_idx]
        accepted: list[uuid.UUID] = []
        verdict: str | None = None
        sender = ctx.sender()
        for index, cand in zip(indexes, parsed.acp_messages, strict=True):
            unresolved = [r for r in cand.output_refs if r not in refs]
            if unresolved:
                rej = RejectedCandidate("result", index, cand.type, "SCHEMA_INVALID", "ссылка на несохранённый вывод")
                rejected.append(rej)
                await _record_result_rejection(conn, ctx, rej)
                continue
            payload = _substitute(cand.payload, refs)
            if _platform_facts(cand.type, payload, commit_sha):
                warnings.append(f"commit_sha_set_by_platform:{index}")
            candidate: dict[str, Any] = {"to": {"role": cand.to_role}, "type": cand.type, "payload": payload}
            if cand.reply_to_id:
                candidate["reply_to_id"] = cand.reply_to_id
            try:
                async with conn.begin_nested():
                    outcome = await self.broker.submit(
                        conn,
                        candidate,
                        sender=sender,
                        run_id=ctx.run_id,
                        message_id=message_id_for(ctx.execution_id, index),
                    )
            except Exception as exc:  # noqa: BLE001 — сбой одного кандидата не прерывает исполнение
                rejected.append(RejectedCandidate("broker", index, cand.type, "INTERNAL_ERROR", type(exc).__name__))
                continue
            if isinstance(outcome, Accepted):
                accepted.append(outcome.message_id)
                if cand.type == "TASK_COMPLETED" and isinstance(payload.get("review_verdict"), str):
                    verdict = payload["review_verdict"]
            elif isinstance(outcome, Rejected):
                rejected.append(RejectedCandidate("broker", index, cand.type, str(outcome.code), outcome.reason))
        denials = await SqlFacts(conn).denials_in_attempt(ctx.attempt_id)
        return replace(
            summary,
            result_sha256=parsed.sha256,
            summary=parsed.summary,
            claims=parsed.claims,
            output_artifacts={p: f.artifact_id for p, f in stored.items()},
            accepted_message_ids=tuple(accepted),
            rejected=tuple(sorted(rejected, key=lambda r: r.index)),
            review_verdict=verdict,
            attempt_should_fail=denials >= 3,
            warnings=tuple(sorted({*parsed.warnings, *warnings})),
        )
