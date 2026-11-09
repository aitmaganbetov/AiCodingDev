"""Сервис runs (P3-08): создание run со snapshot политик, чтение, команды pause/resume/cancel.

Команды не ждут исполнения: состояние + outbox фиксируются транзакционно (202 Accepted),
фактическую остановку активных исполнений выполняет orchestrator по событию (safe point).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_orchestration import fsm, store
from aicd_platform import audit, outbox
from aicd_platform.errors import FieldError, ProblemError
from aicd_platform.ids import uuid7

ALL_PROVIDERS = ("codex", "claude")


def allow_untested_cli_versions(quality_profile: str) -> bool:
    """Значение политики по умолчанию (CLI_ADAPTER_CONTRACT §5.3, ROUTING_POLICY H2): `false` только для critical."""
    return quality_profile != "critical"


async def create_run(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    requested_by: uuid.UUID,
    mode: str,
    objective: str,
    quality_profile: str | None,
    approval_policy: str,
    allowed_providers: list[str] | None,
    routing_strategy: str = "adaptive",
) -> dict[str, Any]:
    settings = (
        (
            await conn.execute(
                text(
                    "SELECT s.documentation_mode, s.quality_profile, s.max_parallel_tasks, s.max_review_loops,"
                    " s.security_profile, p.status FROM project_settings s JOIN projects p ON p.id = s.project_id"
                    " WHERE s.project_id = :p"
                ),
                {"p": project_id},
            )
        )
        .mappings()
        .first()
    )
    if settings is None:
        raise ProblemError("not_found")
    if settings["status"] != "ACTIVE":
        raise ProblemError("invalid_state_transition", detail="Проект не активен")
    repos = (
        (
            await conn.execute(
                text("SELECT id, last_synced_commit_sha FROM repositories WHERE project_id = :p AND status = 'ACTIVE'"),
                {"p": project_id},
            )
        )
        .mappings()
        .all()
    )
    if len(repos) != 1:
        # OQ-22: в v1 run требует ровно один активный репозиторий проекта.
        raise ProblemError(
            "validation_error",
            errors=[
                FieldError(
                    "/project_id", "repository_required", "у проекта должен быть ровно один активный репозиторий"
                )
            ],
        )
    profile_code = quality_profile or settings["quality_profile"]
    profile_id = (
        await conn.execute(
            text(
                "SELECT id FROM quality_profiles WHERE code = :c AND active"
                " AND (workspace_id = :ws OR workspace_id IS NULL) ORDER BY workspace_id NULLS LAST LIMIT 1"
            ),
            {"c": profile_code, "ws": workspace_id},
        )
    ).scalar_one()
    providers = sorted(set(allowed_providers or ALL_PROVIDERS))
    snapshot = {
        "schema": 1,
        "routing": {
            "strategy": routing_strategy,
            "allowed_providers": providers,
            # CLI_ADAPTER_CONTRACT §5.3: версия CLI вне матрицы совместимости (UNTESTED) допускается в basic/strict
            # (с предупреждением UNTESTED_CLI_VERSION в routing_decisions), в critical — нет.
            "allow_untested_cli_versions": allow_untested_cli_versions(profile_code),
        },
        "approval_policy": approval_policy,
        "quality_profile": {"code": profile_code, "id": str(profile_id)},
        "documentation_mode": settings["documentation_mode"],
        "limits": {
            "max_parallel_tasks": settings["max_parallel_tasks"],
            "max_review_loops": settings["max_review_loops"],
        },
        "security_profile": settings["security_profile"],
    }
    run_id = uuid7()
    import json

    await conn.execute(
        text(
            "INSERT INTO runs (id, workspace_id, project_id, repository_id, requested_by, mode, objective,"
            " base_commit_sha, quality_profile, quality_profile_id, approval_policy, documentation_mode, policy_snapshot)"
            " VALUES (:id, :ws, :p, :repo, :by, :mode, :obj, :sha, :qp, :qpid, :ap, :dm, CAST(:snap AS jsonb))"
        ),
        {
            "id": run_id,
            "ws": workspace_id,
            "p": project_id,
            "repo": repos[0]["id"],
            "by": requested_by,
            "mode": mode,
            "obj": objective,
            "sha": repos[0]["last_synced_commit_sha"],
            "qp": profile_code,
            "qpid": profile_id,
            "ap": approval_policy,
            "dm": settings["documentation_mode"],
            "snap": json.dumps(snapshot),
        },
    )
    await outbox.emit(
        conn,
        event_type="run.state_changed",
        aggregate_type="run",
        aggregate_id=run_id,
        workspace_id=workspace_id,
        run_id=run_id,
        payload={"from": None, "to": "CREATED", "version": 0},
    )
    await outbox.emit(
        conn,
        event_type="run.created",
        aggregate_type="run",
        aggregate_id=run_id,
        workspace_id=workspace_id,
        run_id=run_id,
        payload={"mode": mode},
    )
    await audit.record(
        conn,
        action="run.create",
        resource_type="run",
        resource_id=run_id,
        actor_type="user",
        actor_id=requested_by,
        workspace_id=workspace_id,
        metadata={"mode": mode, "project_id": str(project_id)},
    )
    return {"run_id": run_id, "status": "CREATED"}


async def command(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    command: Literal["pause", "resume", "cancel"],
    expected_version: int | None,
    reason: str | None,
) -> store.RunState:
    """Команда управления run. Уже достигнутое состояние — no-op (202); недопустимое — 409."""
    current = await store.load_run(conn, run_id, lock=True)
    if current is None:
        raise ProblemError("not_found")
    if expected_version is not None and current.version != expected_version:
        raise ProblemError("precondition_failed", headers={"ETag": f'"v{current.version}"'})
    target = {"pause": "PAUSED", "cancel": "CANCELLED"}.get(command)
    if command == "resume":
        if current.status != "PAUSED":
            if current.status in fsm.TERMINAL["run"]:
                raise ProblemError("invalid_state_transition", extensions={"current_status": current.status})
            return current  # уже не на паузе — no-op
        target = current.resume_status or "RUNNING"
    if target is None:  # pragma: no cover — все команды сопоставлены выше
        raise ProblemError("malformed_request")
    if current.status == target:
        return current
    if not fsm.can_transition("run", current.status, target):
        raise ProblemError("invalid_state_transition", extensions={"current_status": current.status})
    extra: dict[str, Any] = {}
    if command == "cancel":
        extra["cancel_requested_at"] = store._NOW
    new = await store.transition_run(conn, run_id, target, reason=reason or f"user_{command}", extra=extra)
    if command == "cancel":
        await conn.execute(
            text(
                "UPDATE tasks SET status = 'CANCELLED', blocked_reason = NULL, next_attempt_at = NULL,"
                " finished_at = now(), version = version + 1"
                " WHERE run_id = :r AND status IN ('PENDING','READY','RETRY_WAIT','BLOCKED')"
            ),
            {"r": run_id},
        )
    await audit.record(
        conn,
        action=f"run.{command}_requested",
        resource_type="run",
        resource_id=run_id,
        actor_type="user",
        actor_id=actor_id,
        workspace_id=workspace_id,
        metadata={"from": current.status, "to": new.status},
    )
    return new
