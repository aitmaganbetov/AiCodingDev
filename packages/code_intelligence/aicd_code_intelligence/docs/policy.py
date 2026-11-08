"""Политика документации OFF/SMART/STRICT (ТЗ §12, ADR-0005 п.5, SC-DOC-08, AC-10; P5-09).

Решение принимается чистой функцией `decide()` по режиму проекта и потребности задачи в документации
(`DocNeed`). Итог:

* `SKIP`             — документация не используется, задача продолжает работу по локальному индексу кода;
* `USE_CACHE`        — использовать уже загруженный снимок (`documentation_snapshots.status = READY`);
* `DOWNLOAD`         — загрузка разрешена (через ledger `external_actions` + `security.ssrf`);
* `REQUEST_APPROVAL` — запросить approval `DOCS_DOWNLOAD` (`blocking` — ждать ли решения задаче);
* `WAIT_APPROVAL`    — approval уже запрошен и ожидает решения;
* `BLOCK`            — задача не может продолжаться (`outcome`: `BLOCKED` или `MANUAL_REVIEW`).

Правила (точно по ТЗ §12 / ADR-0005):

* **OFF** — внешняя документация не загружается и не используется, approval не запрашивается
  никогда; задача всегда продолжает работу по локальному индексу (AC-10 «работа полностью офлайн»).
  Даже явно одобренный approval не включает загрузку.
* **SMART** (default) — сначала локальный код и кэш: есть READY-снимок ⇒ `USE_CACHE`; иначе загрузка
  только после `APPROVED`, не истёкшего approval `DOCS_DOWNLOAD` на этот источник; нет approval ⇒
  `REQUEST_APPROVAL` (неблокирующий: задача продолжает работу по локальному коду); `PENDING` ⇒
  `WAIT_APPROVAL` (неблокирующий); отклонён/истёк/источника нет ⇒ `SKIP`. SMART никогда не скачивает
  без предусмотренного разрешения (AC-10).
* **STRICT** — для потребностей, помеченных `required` (документация нужна для заданных проверок),
  нужны утверждённые источники: READY-снимок утверждённого источника ⇒ `USE_CACHE`; утверждён, но не
  загружен ⇒ `DOWNLOAD` (блокирующе); источник зарегистрирован, но не утверждён ⇒ `REQUEST_APPROVAL`
  (блокирующий) / `WAIT_APPROVAL` (блокирующий); источник не зарегистрирован ⇒ `BLOCK(BLOCKED)` (нужен
  admin для регистрации); approval отклонён или истёк ⇒ `BLOCK(MANUAL_REVIEW)`. Не-`required`
  потребности в STRICT обрабатываются как в SMART.

«Утверждённый источник»: включённый источник с APPROVED, не истёкшим approval `DOCS_DOWNLOAD`
(`documentation_sources.approval_id`) либо со снимком, загруженным по явному запросу человека
(refresh — согласие пользователя с правом, API_DESIGN §19 п.7). Отключённый источник не утверждён.

Порты (`DocsApprovalPort`, `DocsDownloadPort`) реализует оркестрация (approvals / external_actions);
этот модуль её не импортирует (import-linter: домены не зависят от orchestration).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

DOCS_DOWNLOAD: Final = "DOCS_DOWNLOAD"
EXTERNAL_ACTION_TYPE: Final = "docs_download"


class DocumentationMode(StrEnum):
    OFF = "OFF"
    SMART = "SMART"
    STRICT = "STRICT"


DEFAULT_MODE: Final = DocumentationMode.SMART


class DocAction(StrEnum):
    SKIP = "SKIP"
    USE_CACHE = "USE_CACHE"
    DOWNLOAD = "DOWNLOAD"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    WAIT_APPROVAL = "WAIT_APPROVAL"
    BLOCK = "BLOCK"


class ApprovalState(StrEnum):
    NONE = "NONE"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class DocNeed:
    """Потребность задачи в документации конкретного источника/технологии."""

    key: str  # например, 'python:fastapi@0.115' — для журналов
    source_id: uuid.UUID | None = None  # None — источник не зарегистрирован
    source_uri: str | None = None
    source_enabled: bool = True
    has_ready_snapshot: bool = False
    human_refreshed: bool = False  # READY-снимок получен по явному refresh человека
    approval: ApprovalState = ApprovalState.NONE
    required: bool = False  # STRICT: документация обязательна для заданных проверок


@dataclass(frozen=True)
class DocDecision:
    action: DocAction
    reason: str
    blocking: bool = False
    outcome: str | None = None  # BLOCKED | MANUAL_REVIEW для BLOCK
    approval_action_type: str | None = None

    @property
    def downloads(self) -> bool:
        return self.action is DocAction.DOWNLOAD


def _source_approved(need: DocNeed) -> bool:
    if need.source_id is None or not need.source_enabled:
        return False
    return need.approval is ApprovalState.APPROVED or (need.has_ready_snapshot and need.human_refreshed)


def _smart(need: DocNeed, *, blocking: bool) -> DocDecision:
    if need.source_id is None or not need.source_enabled:
        if need.has_ready_snapshot and need.source_id is not None:
            return DocDecision(DocAction.SKIP, "источник отключён")
        return DocDecision(DocAction.SKIP, "источник не зарегистрирован — только локальный код")
    if need.has_ready_snapshot:
        return DocDecision(DocAction.USE_CACHE, "есть загруженный снимок")
    if need.approval is ApprovalState.APPROVED:
        return DocDecision(DocAction.DOWNLOAD, "approval DOCS_DOWNLOAD утверждён", blocking=blocking)
    if need.approval is ApprovalState.PENDING:
        return DocDecision(
            DocAction.WAIT_APPROVAL,
            "approval DOCS_DOWNLOAD ожидает решения",
            blocking=blocking,
            approval_action_type=DOCS_DOWNLOAD,
        )
    if need.approval in (ApprovalState.REJECTED, ApprovalState.EXPIRED, ApprovalState.CANCELLED):
        return DocDecision(DocAction.SKIP, f"approval DOCS_DOWNLOAD: {need.approval.value}")
    return DocDecision(
        DocAction.REQUEST_APPROVAL,
        "нет снимка — требуется approval DOCS_DOWNLOAD",
        blocking=blocking,
        approval_action_type=DOCS_DOWNLOAD,
    )


def decide(mode: DocumentationMode | str, need: DocNeed) -> DocDecision:
    mode = DocumentationMode(mode)
    if mode is DocumentationMode.OFF:
        return DocDecision(DocAction.SKIP, "documentation_mode=OFF: только локальный индекс кода")
    if mode is DocumentationMode.SMART or not need.required:
        return _smart(need, blocking=False)
    # STRICT + required
    if need.source_id is None:
        return DocDecision(DocAction.BLOCK, "STRICT: нет утверждённого источника", blocking=True, outcome="BLOCKED")
    if not need.source_enabled:
        return DocDecision(DocAction.BLOCK, "STRICT: источник отключён", blocking=True, outcome="BLOCKED")
    if _source_approved(need):
        if need.has_ready_snapshot:
            return DocDecision(DocAction.USE_CACHE, "STRICT: утверждённый источник загружен", blocking=True)
        return DocDecision(DocAction.DOWNLOAD, "STRICT: утверждённый источник не загружен", blocking=True)
    if need.approval is ApprovalState.PENDING:
        return DocDecision(
            DocAction.WAIT_APPROVAL,
            "STRICT: approval DOCS_DOWNLOAD ожидает решения",
            blocking=True,
            approval_action_type=DOCS_DOWNLOAD,
        )
    if need.approval in (ApprovalState.REJECTED, ApprovalState.EXPIRED, ApprovalState.CANCELLED):
        return DocDecision(
            DocAction.BLOCK,
            f"STRICT: approval DOCS_DOWNLOAD {need.approval.value}",
            blocking=True,
            outcome="MANUAL_REVIEW",
        )
    return DocDecision(
        DocAction.REQUEST_APPROVAL,
        "STRICT: источник не утверждён — требуется approval DOCS_DOWNLOAD",
        blocking=True,
        approval_action_type=DOCS_DOWNLOAD,
    )


def docs_download_subject(source_id: uuid.UUID, uri: str) -> dict[str, Any]:
    """Предмет approval `DOCS_DOWNLOAD` и request внешнего действия `docs_download` — один и тот же
    словарь, поэтому `approvals.subject_hash == external_actions.request_hash` (SECURITY_ARCHITECTURE §11:
    привязка к `source_id`, URL)."""
    return {"source_id": str(source_id), "url": uri}


# --- Порты (реализует оркестрация) -------------------------------------------------------------


class DocsApprovalPort(Protocol):
    async def request_docs_download(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        run_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        subject: dict[str, Any],
        requested_by_role: str | None,
    ) -> uuid.UUID:
        """Создаёт (или возвращает существующий PENDING) approval DOCS_DOWNLOAD; TTL 7 дней (§11)."""
        ...

    async def approval_state(self, conn: AsyncConnection, approval_id: uuid.UUID) -> ApprovalState: ...


class DocsDownloadPort(Protocol):
    """Ledger `external_actions` для загрузки (ADR-0012): PLANNED → EXECUTING → CONFIRMED/FAILED."""

    async def plan(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        target: str,
        request: dict[str, Any],
        approval_id: uuid.UUID | None,
    ) -> uuid.UUID: ...

    async def begin(self, conn: AsyncConnection, action_id: uuid.UUID, fencing_token: int) -> None: ...

    async def confirm(
        self,
        conn: AsyncConnection,
        action_id: uuid.UUID,
        *,
        fencing_token: int,
        external_ref: str,
        result: dict[str, Any],
    ) -> None: ...

    async def fail(
        self, conn: AsyncConnection, action_id: uuid.UUID, *, error: str, fencing_token: int | None
    ) -> None: ...


@dataclass(frozen=True)
class GateResult:
    decision: DocDecision
    approval_id: uuid.UUID | None = None


class DocumentationGate:
    """Применяет решение: при `REQUEST_APPROVAL` запрашивает approval через порт.

    Загрузку (`DOWNLOAD`) исполняет импортёр (`docs.importer`) в очереди `docs` — gate её не запускает.
    """

    def __init__(self, approvals: DocsApprovalPort) -> None:
        self._approvals = approvals

    async def evaluate(
        self,
        conn: AsyncConnection,
        *,
        mode: DocumentationMode | str,
        need: DocNeed,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        run_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        requested_by_role: str | None = None,
    ) -> GateResult:
        decision = decide(mode, need)
        if decision.action is not DocAction.REQUEST_APPROVAL:
            return GateResult(decision)
        if need.source_id is None or need.source_uri is None:  # pragma: no cover — decide() не допускает
            raise ValueError("approval без источника")
        approval_id = await self._approvals.request_docs_download(
            conn,
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            subject=docs_download_subject(need.source_id, need.source_uri),
            requested_by_role=requested_by_role,
        )
        return GateResult(decision, approval_id)
