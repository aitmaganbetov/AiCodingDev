"""Политика документации OFF/SMART/STRICT (ТЗ §12, ADR-0005, SC-DOC-08; AC-10)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from aicd_code_intelligence.docs.policy import (
    ApprovalState,
    DocAction,
    DocNeed,
    DocumentationGate,
    DocumentationMode,
    decide,
    docs_download_subject,
)

SRC = uuid.UUID("01890000-0000-7000-8000-000000000001")
URI = "https://docs.example.com/guide"


def need(**kw: Any) -> DocNeed:
    base: dict[str, Any] = {"key": "python:fastapi", "source_id": SRC, "source_uri": URI}
    base.update(kw)
    return DocNeed(**base)


# --- AC-10: OFF — только локальный индекс, никогда не загружает -----------------------------------


@pytest.mark.parametrize("approval", list(ApprovalState))
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("ready", [False, True])
def test_off_never_downloads_nor_asks(approval: ApprovalState, required: bool, ready: bool) -> None:
    d = decide("OFF", need(approval=approval, required=required, has_ready_snapshot=ready))
    assert d.action is DocAction.SKIP and not d.downloads and not d.blocking and d.approval_action_type is None


def test_off_without_any_source_proceeds() -> None:
    d = decide(DocumentationMode.OFF, DocNeed(key="x", required=True))
    assert d.action is DocAction.SKIP and not d.blocking


# --- AC-10: SMART не скачивает без разрешения ------------------------------------------------------


def test_smart_uses_cache_first() -> None:
    assert decide("SMART", need(has_ready_snapshot=True)).action is DocAction.USE_CACHE


def test_smart_requests_approval_when_missing_and_does_not_block() -> None:
    d = decide("SMART", need())
    assert d.action is DocAction.REQUEST_APPROVAL and d.approval_action_type == "DOCS_DOWNLOAD"
    assert not d.downloads and not d.blocking


@pytest.mark.parametrize("approval", [s for s in ApprovalState if s is not ApprovalState.APPROVED])
def test_smart_never_downloads_without_approved(approval: ApprovalState) -> None:
    assert not decide("SMART", need(approval=approval)).downloads


def test_smart_downloads_only_after_approved() -> None:
    d = decide("SMART", need(approval=ApprovalState.APPROVED))
    assert d.action is DocAction.DOWNLOAD and not d.blocking


def test_smart_pending_and_rejected() -> None:
    assert decide("SMART", need(approval=ApprovalState.PENDING)).action is DocAction.WAIT_APPROVAL
    for st in (ApprovalState.REJECTED, ApprovalState.EXPIRED, ApprovalState.CANCELLED):
        assert decide("SMART", need(approval=st)).action is DocAction.SKIP


def test_smart_unregistered_or_disabled_source_skips() -> None:
    assert decide("SMART", DocNeed(key="x")).action is DocAction.SKIP
    assert decide("SMART", need(source_enabled=False, approval=ApprovalState.APPROVED)).action is DocAction.SKIP


def test_strict_not_required_behaves_like_smart() -> None:
    for kw in ({}, {"approval": ApprovalState.APPROVED}, {"has_ready_snapshot": True}):
        assert decide("STRICT", need(**kw)) == decide("SMART", need(**kw))


# --- STRICT: обязательные утверждённые источники -----------------------------------------------


def test_strict_required_without_source_blocks() -> None:
    d = decide("STRICT", DocNeed(key="x", required=True))
    assert d.action is DocAction.BLOCK and d.outcome == "BLOCKED" and d.blocking


def test_strict_required_disabled_source_blocks() -> None:
    d = decide("STRICT", need(required=True, source_enabled=False, has_ready_snapshot=True, human_refreshed=True))
    assert d.action is DocAction.BLOCK and d.outcome == "BLOCKED"


def test_strict_required_unapproved_requests_blocking_approval() -> None:
    d = decide("STRICT", need(required=True))
    assert d.action is DocAction.REQUEST_APPROVAL and d.blocking and not d.downloads
    assert decide("STRICT", need(required=True, approval=ApprovalState.PENDING)).blocking


def test_strict_required_unapproved_cache_not_used() -> None:
    """READY-снимок без утверждения (не human refresh, нет APPROVED) в STRICT не используется."""
    d = decide("STRICT", need(required=True, has_ready_snapshot=True))
    assert d.action is DocAction.REQUEST_APPROVAL


@pytest.mark.parametrize("approval", [ApprovalState.REJECTED, ApprovalState.EXPIRED, ApprovalState.CANCELLED])
def test_strict_required_rejected_goes_to_manual_review(approval: ApprovalState) -> None:
    d = decide("STRICT", need(required=True, approval=approval))
    assert d.action is DocAction.BLOCK and d.outcome == "MANUAL_REVIEW"


def test_strict_required_approved_downloads_or_uses_cache() -> None:
    d = decide("STRICT", need(required=True, approval=ApprovalState.APPROVED))
    assert d.action is DocAction.DOWNLOAD and d.blocking
    d = decide("STRICT", need(required=True, has_ready_snapshot=True, human_refreshed=True))
    assert d.action is DocAction.USE_CACHE


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ValueError, match="AUTO"):
        decide("AUTO", need())


def test_subject_binds_source_and_url() -> None:
    assert docs_download_subject(SRC, URI) == {"source_id": str(SRC), "url": URI}


# --- Gate + порт approvals (реализует оркестрация) --------------------------------------------


@dataclass
class FakeApprovals:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def request_docs_download(self, conn: Any, **kw: Any) -> uuid.UUID:
        self.calls.append(kw)
        return uuid.UUID(int=len(self.calls))

    async def approval_state(self, conn: Any, approval_id: uuid.UUID) -> ApprovalState:
        return ApprovalState.PENDING


async def _evaluate(gate: DocumentationGate, mode: str, n: DocNeed) -> Any:
    ws, project = uuid.uuid4(), uuid.uuid4()
    return await gate.evaluate(
        None,  # type: ignore[arg-type]
        mode=mode,
        need=n,
        workspace_id=ws,
        project_id=project,
        run_id=None,
        task_id=None,
        requested_by_role="developer",
    )


async def test_gate_requests_approval_only_when_decided() -> None:
    port = FakeApprovals()
    gate = DocumentationGate(port)
    off = await _evaluate(gate, "OFF", need())
    assert off.approval_id is None and port.calls == [], "OFF: approval не запрашивается (AC-10)"
    cached = await _evaluate(gate, "SMART", need(has_ready_snapshot=True))
    assert cached.approval_id is None and port.calls == []
    smart = await _evaluate(gate, "SMART", need())
    assert smart.approval_id is not None and len(port.calls) == 1
    assert port.calls[0]["subject"] == docs_download_subject(SRC, URI)
