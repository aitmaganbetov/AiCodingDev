"""P4-10: 4-й review/fix цикл → BLOCKED(REVIEW_LOOP_EXHAUSTED) + approval MANUAL_REVIEW → решение человека.

Сквозь настоящие брокер ACP (aicd_collaboration) и эскалацию оркестратора (`ReviewLoopEscalator`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_collaboration.acp import Accepted, AcpBroker
from aicd_orchestration.approvals import Approvals
from aicd_orchestration.engine import Orchestrator
from aicd_orchestration.review_loops import ReviewLoopEscalator
from aicd_platform.db import DbContext, transaction
from aicd_worker import handlers
from aicd_worker.runtime import Runtime
from tests.integration.test_acp_broker import _drain_outbox, _review_request, _rows, _scene, _submit
from tests.support import db as testdb
from tests.support.acp import valid_payload

pytestmark = pytest.mark.db


@pytest.mark.parametrize("decision", ["APPROVED", "REJECTED"])
async def test_loop_exhaustion_escalates_to_human_and_decision_resolves_task(
    database: testdb.TestDatabase, worker_engine: AsyncEngine, app_engine: AsyncEngine, decision: str
) -> None:
    scene = _scene(database)
    broker = AcpBroker(escalation=ReviewLoopEscalator())
    rr = await _submit(worker_engine, scene, "developer", _review_request(scene), broker)
    assert isinstance(rr, Accepted)
    fix: dict[str, Any] = {"to": {"role": "developer"}, "type": "FIX_REQUEST", "reply_to_id": str(rr.message_id)}
    for i in range(1, 5):
        result = await _submit(
            worker_engine, scene, "reviewer", {**fix, "payload": valid_payload("FIX_REQUEST", loop_iteration=i)}, broker
        )
        assert isinstance(result, Accepted)
    assert isinstance(result, Accepted) and result.loop_exhausted is not None, "4-й цикл исчерпывает лимит 3"
    developer = scene.tasks["developer"]
    assert _rows(database, "SELECT status, blocked_reason FROM tasks WHERE id = %s", developer) == [
        ("BLOCKED", "REVIEW_LOOP_EXHAUSTED")
    ]
    approvals = _rows(
        database,
        "SELECT id, version, subject ->> 'reason', (subject ->> 'max_review_loops')::int FROM approvals"
        " WHERE task_id = %s AND action_type = 'MANUAL_REVIEW' AND status = 'PENDING'",
        developer,
    )
    assert len(approvals) == 1 and approvals[0][2:] == ("REVIEW_LOOP_EXHAUSTED", 3)
    approval_id, version = approvals[0][0], approvals[0][1]

    owner = scene.tenant.owner_id
    ws = scene.tenant.workspace_id
    async with transaction(app_engine, DbContext(user_id=owner, workspace_id=ws)) as conn:
        await Approvals().decide(
            conn,
            approval_id,
            actor_id=owner,
            actor_role="owner",
            decision=decision,
            reason="лимит циклов исчерпан — решение по результату ревью",
            expected_version=version,
        )
    # Решение доходит до оркестратора событием approval.decided; здесь — прямой вызов обработчика.
    orchestrator = Orchestrator(engine=worker_engine, approvals=Approvals(), verifier=None, executor=None)  # type: ignore[arg-type]
    await orchestrator.on_task_approval_decided(uuid.UUID(str(approval_id)), ws)
    expected = "SUCCEEDED" if decision == "APPROVED" else "FAILED"
    assert _rows(database, "SELECT status FROM tasks WHERE id = %s", developer) == [(expected,)]


async def test_worker_projects_accepted_acp_message_into_chat_and_activity(
    database: testdb.TestDatabase, worker_engine: AsyncEngine
) -> None:
    """P4-05: событие outbox `agent.message` → воркер → проекции (чат run + лента активности), повтор без эффекта."""
    scene = _scene(database)
    accepted = await _submit(worker_engine, scene, "developer", _review_request(scene))
    assert isinstance(accepted, Accepted)
    rec = await _drain_outbox(worker_engine)
    event = next(e for e in rec.published if e["aggregate_id"] == str(accepted.message_id))
    rt = Runtime(engine=worker_engine, orchestrator=None, loop=None, dispatch_execution=None)  # type: ignore[arg-type]
    assert await handlers._route(rt, event, uuid.uuid4(), scene.run, scene.tenant.workspace_id) == "acp_delivered"
    await handlers._route(rt, event, uuid.uuid4(), scene.run, scene.tenant.workspace_id)  # at-least-once повтор
    assert _rows(database, "SELECT count(*) FROM chat_messages WHERE acp_message_id = %s", accepted.message_id) == [
        (1,)
    ]
    assert _rows(
        database,
        "SELECT count(*) FROM agent_activity_events WHERE event_type = 'acp.message_accepted' AND payload ->> 'message_id' = %s",
        str(accepted.message_id),
    ) == [(1,)]
