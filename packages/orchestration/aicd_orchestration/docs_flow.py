"""Запрос документации агентом: BLOCKED(DOCS_REQUIRED) → политика OFF/SMART/STRICT (ТЗ §12, ACP §9.4, AC-10).

Агент заявляет потребность сообщением `BLOCKED` (`reason=DOCS_REQUIRED`) в итоге попытки. Оркестратор
(`engine.Orchestrator`) оценивает `DocumentationGate` (code_intelligence.docs.policy) по каждому включённому
сетевому источнику документации проекта — источнику workspace без технологии или с технологией из
`project_technologies` проекта. Режим — `runs.policy_snapshot.documentation_mode` (снимок на момент создания run).

Итог оценки (`DocsEvaluation.outcome`):

* `blocked`  — нужен approval `DOCS_DOWNLOAD` (запрошен/ожидает) или загрузка уже утверждена и идёт: задача
  `BLOCKED(DOCS_REQUIRED)`. Утверждённая загрузка (`DOWNLOAD`) планируется в ledger `external_actions`
  (`docs_download`, request = предмет approval ⇒ `request_hash = subject_hash`) и событием outbox
  `documentation_source.refresh_requested` уходит импортёру (воркер, `aicd_worker.docs`);
* `manual_review` — STRICT: approval отклонён/истёк или утверждённая загрузка не удалась: задача
  `BLOCKED(MANUAL_REVIEW)` + approval `MANUAL_REVIEW` (предмет помечен `reason=documentation_required`);
* `proceed`  — OFF, источников нет (SMART), снимок уже в кэше, SMART-approval отклонён/истёк или загрузка
  не удалась: ничего не загружается; задача получает новую попытку, а оркестратор отправляет ей ACP
  `DECISION_RECORDED` (`chosen_option`: `local_index` — работать по локальному индексу кода; `docs_cache` —
  документация в кэше, раздел `docs` Context Pack).

Защита от цикла: задача, уже получившая такое решение, повторно заявившая DOCS_REQUIRED, завершает попытку
с `POLICY_DENIED` (не повторяемая категория ⇒ задача FAILED): агент оспаривает решение политики, которое
оркестратор уже принял и сообщил; повтор попытки ничего не изменит. `OUTPUT_PARSE_ERROR` была бы нечестной
(итог разобран успешно) и повторяемой — цикл продолжился бы до исчерпания бюджета.

SMART никогда не загружает без APPROVED `DOCS_DOWNLOAD`; OFF не запрашивает approvals и не загружает (AC-10).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_code_intelligence.docs.policy import (
    DEFAULT_MODE,
    ApprovalState,
    DocAction,
    DocNeed,
    DocumentationGate,
    DocumentationMode,
    docs_download_subject,
)
from aicd_collaboration.acp import Accepted, AcpBroker, Sender
from aicd_orchestration import external_actions
from aicd_orchestration.approvals import subject_hash
from aicd_orchestration.docs_ports import DocsApprovals, DocsLedger
from aicd_platform import outbox
from aicd_platform.ids import uuid7

REFRESH_EVENT: Final = "documentation_source.refresh_requested"
MANUAL_REVIEW_REASON: Final = "documentation_required"  # subject.reason approval MANUAL_REVIEW этого потока
LOCAL_INDEX: Final = "local_index"
DOCS_CACHE: Final = "docs_cache"
DECISION_OPTIONS: Final = frozenset({LOCAL_INDEX, DOCS_CACHE})
MAX_SOURCES: Final = 20  # источников на одну оценку (остальные — в следующей попытке, после загрузки первых)

Outcome = Literal["proceed", "blocked", "manual_review"]


@dataclass(frozen=True)
class PlannedDownload:
    source_id: uuid.UUID
    uri: str
    approval_id: uuid.UUID | None


@dataclass(frozen=True)
class DocsEvaluation:
    mode: str
    outcome: Outcome
    reason: str
    cached: bool = False  # все потребности закрыты READY-снимками (документация в разделе docs)
    downloads: tuple[PlannedDownload, ...] = ()


def mode_of(run: dict[str, Any]) -> str:
    snapshot = run.get("policy_snapshot") or {}
    raw = snapshot.get("documentation_mode") if isinstance(snapshot, dict) else None
    raw = raw or run.get("documentation_mode") or DEFAULT_MODE.value
    try:
        return DocumentationMode(str(raw)).value
    except ValueError:
        return DEFAULT_MODE.value


async def docs_required(conn: AsyncConnection, message_ids: Sequence[uuid.UUID]) -> bool:
    """Среди принятых брокером сообщений попытки есть BLOCKED(DOCS_REQUIRED)."""
    if not message_ids:
        return False
    found = (
        await conn.execute(
            text(
                "SELECT 1 FROM agent_messages WHERE id = ANY(:ids) AND message_type = 'BLOCKED'"
                " AND payload ->> 'reason' = 'DOCS_REQUIRED' LIMIT 1"
            ),
            {"ids": list(message_ids)},
        )
    ).first()
    return found is not None


async def decision_sent(conn: AsyncConnection, run_id: uuid.UUID, task_id: uuid.UUID) -> bool:
    """Задаче уже отправлено решение оркестратора о документации (защита от цикла DOCS_REQUIRED)."""
    found = (
        await conn.execute(
            text(
                "SELECT 1 FROM agent_messages WHERE run_id = :r AND recipient_task_id = :t"
                " AND message_type = 'DECISION_RECORDED' AND sender_role = 'orchestrator'"
                " AND payload ->> 'chosen_option' = ANY(:opts) LIMIT 1"
            ),
            {"r": run_id, "t": task_id, "opts": sorted(DECISION_OPTIONS)},
        )
    ).first()
    return found is not None


async def _approval_for(conn: AsyncConnection, task_id: uuid.UUID, subject: dict[str, Any]) -> uuid.UUID | None:
    """Approval DOCS_DOWNLOAD на предмет: последний этой задачи; иначе действующий (PENDING/APPROVED) другой
    задачи workspace — PENDING по предмету в workspace один (uq approvals), его решение общее."""
    value = (
        await conn.execute(
            text(
                "SELECT id FROM approvals WHERE action_type = 'DOCS_DOWNLOAD' AND subject_hash = :h"
                " AND (task_id IS NOT DISTINCT FROM :t"
                "      OR (status IN ('PENDING','APPROVED') AND (expires_at IS NULL OR expires_at > now())))"
                " ORDER BY (task_id IS NOT DISTINCT FROM :t) DESC, requested_at DESC, id DESC LIMIT 1"
            ),
            {"h": subject_hash(subject), "t": task_id},
        )
    ).scalar_one_or_none()
    return uuid.UUID(str(value)) if value is not None else None


async def project_needs(
    conn: AsyncConnection, approvals: DocsApprovals, *, project_id: uuid.UUID, task_id: uuid.UUID, required: bool
) -> list[tuple[DocNeed, uuid.UUID | None]]:
    """Потребности по включённым сетевым источникам проекта (источники-артефакты не загружаются из сети)."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT s.id, s.uri_or_file_ref,"
                    " EXISTS (SELECT 1 FROM documentation_snapshots d WHERE d.source_id = s.id"
                    "   AND d.status = 'READY') AS ready,"
                    " EXISTS (SELECT 1 FROM documentation_snapshots d JOIN external_actions e"
                    "   ON e.id = d.external_action_id WHERE d.source_id = s.id AND d.status = 'READY'"
                    "   AND e.run_id IS NULL) AS human_refreshed"
                    " FROM documentation_sources s WHERE s.enabled AND s.uri_or_file_ref LIKE 'https://%'"
                    " AND (s.technology_version_id IS NULL OR s.technology_version_id IN"
                    "   (SELECT pt.technology_version_id FROM project_technologies pt WHERE pt.project_id = :p))"
                    " ORDER BY s.created_at, s.id LIMIT :n"
                ),
                {"p": project_id, "n": MAX_SOURCES},
            )
        )
        .mappings()
        .all()
    )
    needs: list[tuple[DocNeed, uuid.UUID | None]] = []
    for r in rows:
        uri = str(r["uri_or_file_ref"])
        approval_id = await _approval_for(conn, task_id, docs_download_subject(r["id"], uri))
        state = await approvals.approval_state(conn, approval_id) if approval_id else ApprovalState.NONE
        needs.append(
            (
                DocNeed(
                    key=uri,
                    source_id=r["id"],
                    source_uri=uri,
                    has_ready_snapshot=bool(r["ready"]),
                    human_refreshed=bool(r["human_refreshed"]),
                    approval=state,
                    required=required,
                ),
                approval_id,
            )
        )
    return needs


async def _download_status(conn: AsyncConnection, run_id: uuid.UUID, task_id: uuid.UUID, uri: str) -> str | None:
    key = external_actions.idempotency_key(run_id, task_id, "docs_download", uri, None)
    value = (
        await conn.execute(text("SELECT status FROM external_actions WHERE idempotency_key = :k"), {"k": key})
    ).scalar_one_or_none()
    return str(value) if value is not None else None


async def evaluate(
    conn: AsyncConnection,
    gate: DocumentationGate,
    approvals: DocsApprovals,
    *,
    run: dict[str, Any],
    task: dict[str, Any],
) -> DocsEvaluation:
    mode = mode_of(run)
    if mode == DocumentationMode.OFF:
        # OFF: ни approvals, ни загрузок, ни чтения источников — только локальный индекс (AC-10, NFR-DOC-01).
        return DocsEvaluation(mode, "proceed", "documentation_mode=OFF: только локальный индекс кода")
    strict = mode == DocumentationMode.STRICT
    needs = await project_needs(conn, approvals, project_id=run["project_id"], task_id=task["id"], required=strict)
    if not needs:
        needs = [(DocNeed(key="project", required=strict), None)]
    blocked = manual = False
    cached = True
    reasons: list[str] = []
    downloads: list[PlannedDownload] = []
    for need, approval_id in needs:
        result = await gate.evaluate(
            conn,
            mode=mode,
            need=need,
            workspace_id=run["workspace_id"],
            project_id=run["project_id"],
            run_id=run["id"],
            task_id=task["id"],
            requested_by_role=str(task["agent_role"]),
        )
        decision = result.decision
        reasons.append(f"{need.key}: {decision.reason}")
        cached = cached and decision.action is DocAction.USE_CACHE
        if decision.action is DocAction.DOWNLOAD and need.source_id is not None and need.source_uri is not None:
            status = await _download_status(conn, run["id"], task["id"], need.source_uri)
            if status in ("FAILED", "CONFIRMED", "COMPENSATED"):
                # Утверждённая загрузка уже выполнялась и READY-снимка не дала: повтор ничего не изменит.
                reasons[-1] += "; загрузка не дала снимка"
                manual = manual or need.required
            else:
                blocked = True
                if status is None:
                    downloads.append(PlannedDownload(need.source_id, need.source_uri, approval_id))
        elif decision.action in (DocAction.REQUEST_APPROVAL, DocAction.WAIT_APPROVAL):
            blocked = True
        elif decision.action is DocAction.BLOCK:
            if decision.outcome == "MANUAL_REVIEW":
                manual = True
            else:
                blocked = True
    outcome: Outcome = "manual_review" if manual else "blocked" if blocked else "proceed"
    return DocsEvaluation(mode, outcome, "; ".join(reasons)[:4000], cached=cached, downloads=tuple(downloads))


async def plan_download(
    conn: AsyncConnection,
    ledger: DocsLedger,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID,
    download: PlannedDownload,
) -> uuid.UUID:
    """PLANNED `docs_download` (request = предмет approval) + событие для импортёра в той же транзакции."""
    action_id = await ledger.plan(
        conn,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=task_id,
        target=download.uri,
        request=docs_download_subject(download.source_id, download.uri),
        approval_id=download.approval_id,
    )
    await outbox.emit(
        conn,
        event_type=REFRESH_EVENT,
        aggregate_type="documentation_source",
        aggregate_id=download.source_id,
        workspace_id=workspace_id,
        run_id=None,
        payload={"source_id": str(download.source_id), "external_action_id": str(action_id)},
    )
    return action_id


async def record_decision(
    conn: AsyncConnection,
    broker: AcpBroker,
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    evaluation: DocsEvaluation,
    decided_by_user: uuid.UUID | None = None,
    approval_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """`agent_decisions` + ACP `DECISION_RECORDED` (orchestrator → задача): как продолжать без загрузки."""
    option = DOCS_CACHE if evaluation.cached else LOCAL_INDEX
    if option == DOCS_CACHE:
        rationale = (
            "Документация загружена и доступна в разделе docs Context Pack. Продолжайте задачу по ней и по "
            "локальному индексу кода; повторный запрос документации не требуется."
        )
    else:
        rationale = (
            f"Внешняя документация недоступна (documentation_mode={evaluation.mode}). Продолжайте задачу по "
            "локальному индексу кода (разделы code/symbols_graph/tests Context Pack); повторный запрос "
            "документации в этой задаче отклоняется."
        )
    rationale = f"{rationale}\nОснование: {evaluation.reason}"[:20000]
    decision_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO agent_decisions (id, workspace_id, run_id, task_id, decision_type, rationale,"
            " decided_by_type, decided_by_user_id) VALUES (:id, :ws, :r, :t, 'other', :why, :by, :uid)"
        ),
        {
            "id": decision_id,
            "ws": run["workspace_id"],
            "r": run["id"],
            "t": task["id"],
            "why": rationale,
            "by": "user" if decided_by_user else "system",
            "uid": decided_by_user,
        },
    )
    decided_by: dict[str, Any] = (
        {"type": "user", "user_id": str(decided_by_user)}
        if decided_by_user
        else {"type": "policy", "policy_version": f"documentation_mode={evaluation.mode}"}
    )
    payload: dict[str, Any] = {
        "decision_id": str(decision_id),
        "decision_type": "other",
        "outcome": "accepted",
        "chosen_option": option,
        "decided_by": decided_by,
        "rationale": rationale[:4000],
    }
    if approval_id is not None:
        payload["approval_id"] = str(approval_id)
    # task_id конверта не задаётся: сообщение адресовано задаче (to.task_id) и попадает в её inbox.
    outcome = await broker.submit(
        conn,
        {
            "to": {"role": str(task["agent_role"]), "task_id": str(task["id"])},
            "type": "DECISION_RECORDED",
            "payload": payload,
        },
        sender=Sender(role="orchestrator"),
        run_id=run["id"],
    )
    return outcome.message_id if isinstance(outcome, Accepted) else None
