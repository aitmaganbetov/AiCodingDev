"""Правила продвижения отправленного поручения (directives.progress) и флаг остановки попытки."""

from __future__ import annotations

from aicd_orchestration import directives


def test_progress_rules_are_ordered() -> None:
    """Чистое правило: отказ агента важнее прочего; ack до выполнения; run завершён — FAILED."""
    acked = [("developer", "ACKNOWLEDGED")]
    assert directives.progress(
        "DISPATCHED", "RUNNING", acked, [("main", "RUNNING")], recipients_bound=True
    ).targets == ("ACKNOWLEDGED",)
    done = directives.progress("DISPATCHED", "COMPLETED", acked, [("main", "SUCCEEDED")], recipients_bound=True)
    assert done.targets == ("ACKNOWLEDGED", "COMPLETED") and done.failure is None
    rejected = [("developer", "REJECTED"), ("reviewer", "ACKNOWLEDGED")]
    assert directives.progress("DISPATCHED", "RUNNING", rejected, [], recipients_bound=True).failure
    waiting = directives.progress("DISPATCHED", "RUNNING", [("developer", "DELIVERED")], [("main", "RUNNING")],
                                  recipients_bound=True)  # fmt: skip
    assert waiting == directives.Progress()
    ended = directives.progress("DISPATCHED", "FAILED", [("developer", "PENDING")], [], recipients_bound=False)
    assert ended.failure and "FAILED" in ended.failure
    assert directives.execution_cancel_reason("RUNNING", "RUNNING", True) == "directive"
    assert directives.execution_cancel_reason("PAUSED", "RUNNING", True) == "pause"
    assert directives.execution_cancel_reason("RUNNING", "RUNNING", False) is None
    assert directives.agent_ack_status(None) == "ACKNOWLEDGED", "нет записи в принятом итоге — неявное accepted"
    assert directives.agent_ack_status("not_applicable") == "ACKNOWLEDGED"
    assert directives.agent_ack_status("cannot_comply") == "REJECTED"
