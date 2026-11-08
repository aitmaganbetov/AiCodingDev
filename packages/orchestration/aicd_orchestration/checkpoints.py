"""Checkpoint run (DATA_FLOW §7, P3-12).

Снимок — JSON-артефакт (`kind='checkpoint_state'`, sha256) + строка `checkpoints`: статусы и версии задач,
`plan_version`, HEAD каждого worktree, открытые approvals, ссылки на последние evidence. Идемпотентен по
`(run_id, task_id IS NULL, state_version)`: повтор для той же версии run возвращает существующий снимок.
Вызывается в транзакции владельца перехода (контекст workspace уже установлен).
"""

from __future__ import annotations

import io
import json
import uuid
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_platform.artifacts import ArtifactStore
from aicd_platform.clock import utcnow
from aicd_platform.ids import uuid7

Reason = Literal["plan_approved", "directive", "pause", "pre_external_action", "periodic", "recovery"]


def _jsonable(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, uuid.UUID):
            out[key] = str(value)
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


async def _rows(conn: AsyncConnection, sql: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return [_jsonable(r) for r in (await conn.execute(text(sql), {"r": run_id})).mappings().all()]


async def create(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    artifacts: ArtifactStore,
    reason: Reason,
    extra: dict[str, Any] | None = None,
) -> uuid.UUID:
    run = (
        (
            await conn.execute(
                text(
                    "SELECT id, workspace_id, project_id, status, version, plan_version, approved_plan_version,"
                    " resume_status FROM runs WHERE id = :r"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .one()
    )
    existing = (
        await conn.execute(
            text("SELECT id FROM checkpoints WHERE run_id = :r AND task_id IS NULL AND state_version = :v"),
            {"r": run_id, "v": run["version"]},
        )
    ).scalar_one_or_none()
    if existing is not None:
        return uuid.UUID(str(existing))
    state = {
        "schema": 1,
        "reason": reason,
        "created_at": utcnow().isoformat(),
        "run": _jsonable({k: v for k, v in run.items() if k not in ("workspace_id", "project_id")}),
        "tasks": await _rows(
            conn,
            "SELECT id, task_key, plan_version, status, version, agent_role, attempt_count,"
            " superseded_at IS NOT NULL AS superseded FROM tasks WHERE run_id = :r ORDER BY created_at, id",
            run_id,
        ),
        "worktrees": await _rows(
            conn,
            "SELECT id, task_id, branch_name, base_commit_sha, head_commit_sha, status FROM worktrees"
            " WHERE run_id = :r ORDER BY created_at, id",
            run_id,
        ),
        "open_approvals": await _rows(
            conn,
            "SELECT id, action_type FROM approvals WHERE run_id = :r AND status = 'PENDING' ORDER BY id",
            run_id,
        ),
        "latest_evidence": await _rows(
            conn,
            "SELECT DISTINCT ON (g.task_id, g.gate_id) g.task_id, g.gate_id, g.status, g.evidence_id, g.commit_sha"
            " FROM gate_executions g WHERE g.run_id = :r AND g.superseded_at IS NULL"
            " ORDER BY g.task_id, g.gate_id, g.created_at DESC",
            run_id,
        ),
        **(extra or {}),
    }
    data = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
    artifact_id = uuid7()
    stored = artifacts.put(run["workspace_id"], artifact_id, io.BytesIO(data))
    await conn.execute(
        text(
            "INSERT INTO artifacts (id, workspace_id, project_id, run_id, kind, storage_ref, sha256, size_bytes,"
            " mime_type, redaction_applied) VALUES (:id, :ws, :p, :r, 'checkpoint_state', :ref, :sha, :size,"
            " 'application/json', true)"
        ),
        {
            "id": artifact_id,
            "ws": run["workspace_id"],
            "p": run["project_id"],
            "r": run_id,
            "ref": stored.storage_ref,
            "sha": stored.sha256,
            "size": stored.size_bytes,
        },
    )
    checkpoint_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO checkpoints (id, workspace_id, run_id, state_version, plan_version, reason, state_ref)"
            " VALUES (:id, :ws, :r, :sv, :pv, :reason, :ref)"
        ),
        {
            "id": checkpoint_id,
            "ws": run["workspace_id"],
            "r": run_id,
            "sv": run["version"],
            "pv": run["plan_version"],
            "reason": reason,
            "ref": artifact_id,
        },
    )
    return checkpoint_id
