"""Маршрутизация задачи по данным БД: кандидаты, история качества, авторы изменения → Router → `routing_decisions`.

Выполняется в транзакции engine (контекст workspace установлен), до захвата аренды задачи. Чистая логика —
в `router`; здесь только сбор входов (ROUTING_POLICY §4, §5, §7, §9) и запись решения (§4.3).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_cli_adapters import registry
from aicd_orchestration import quality, router
from aicd_orchestration.review_flow import WRITING_ROLES
from aicd_platform.ids import uuid7

ALL_PROVIDERS = ("codex", "claude")
_PROVIDER_FAILURES = ("PROCESS_FAILED", "TIMEOUT")


async def _workspace_sessions(conn: AsyncConnection) -> dict[str, int]:
    """Сессии провайдеров в workspace текущего RLS-контекста: попытки задач в статусе RUNNING (попытка создаётся
    в той же транзакции, что и решение Router, поэтому учитывает и ещё не запущенный процесс CLI)."""
    rows = (
        await conn.execute(
            text(
                "SELECT a.provider, count(*) FROM task_attempts a WHERE a.status = 'RUNNING'"
                " AND a.workspace_id = app_current_workspace() GROUP BY a.provider"
            )
        )
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}


async def _candidates(conn: AsyncConnection, now: datetime) -> list[router.Candidate]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT i.id, p.code, i.enabled AND p.enabled AS enabled, i.compatibility, i.auth_status,"
                    " i.capabilities, i.max_concurrency, sys_active_executions(i.id) AS active,"
                    " (SELECT h.availability FROM provider_health h WHERE h.installation_id = i.id"
                    "   ORDER BY h.observed_at DESC LIMIT 1) AS availability,"
                    " EXISTS (SELECT 1 FROM cli_executions e WHERE e.installation_id = i.id AND e.status = 'COLLECTED'"
                    "   AND e.exited_at > :now - interval '1 hour' AND e.exit_code = 0) AS recent_success,"
                    " ARRAY(SELECT EXTRACT(EPOCH FROM :now - e.finished_at) / 3600 FROM cli_executions e"
                    "   WHERE e.installation_id = i.id AND e.finished_at > :now - interval '24 hours'"
                    "   AND e.failure_category IN ('PROCESS_FAILED','TIMEOUT','OUTPUT_PARSE_ERROR')) AS failure_ages"
                    " FROM cli_installations i JOIN cli_providers p ON p.id = i.provider_id ORDER BY p.code, i.id"
                ),
                {"now": now},
            )
        )
        .mappings()
        .all()
    )
    # Настройки провайдера в workspace (policies provider_settings; без строки — значения по умолчанию).
    settings = await registry.workspace_settings_by_code(conn)
    sessions = await _workspace_sessions(conn)

    def ws_setting(code: str) -> dict[str, Any]:
        return settings.get(code) or registry.default_workspace_settings()

    return [
        router.Candidate(
            workspace_enabled=bool(ws_setting(r["code"])["enabled"]),
            allowed_roles=frozenset(ws_setting(r["code"])["allowed_roles"]),
            workspace_active=sessions.get(r["code"], 0),
            workspace_max_sessions=int(ws_setting(r["code"])["max_parallel_sessions"]),
            installation_id=r["id"],
            provider=r["code"],
            enabled=bool(r["enabled"]),
            installed=True,  # строка установки создаётся только по результату probe (registry)
            compatibility=r["compatibility"],
            auth_status=r["auth_status"],
            # Нет записи health — установка не наблюдалась неработоспособной; probe при старте воркера её создаёт.
            availability=r["availability"] or "AVAILABLE",
            active=int(r["active"]),
            max_concurrent=int(r["max_concurrency"]),
            capabilities=r["capabilities"] or {},
            recent_success_within_hour=bool(r["recent_success"]),
            recent_failure_ages_h=tuple(float(a) for a in r["failure_ages"] or ()),
        )
        for r in rows
    ]


async def _authors(conn: AsyncConnection, task_id: uuid.UUID) -> tuple[frozenset[str], int]:
    """Провайдеры успешных попыток предков задачи, создавших коммиты (проверяемое изменение, §5), и
    максимальная сложность этих задач (порог независимости — по уровню проверяемого изменения)."""
    rows = (
        await conn.execute(
            text(
                "WITH RECURSIVE anc(id) AS (SELECT depends_on_task_id FROM task_dependencies WHERE task_id = :t"
                " UNION SELECT d.depends_on_task_id FROM task_dependencies d JOIN anc ON d.task_id = anc.id)"
                " SELECT a.provider, t.complexity FROM task_attempts a JOIN anc ON anc.id = a.task_id"
                " JOIN tasks t ON t.id = a.task_id"
                " WHERE a.status = 'SUCCEEDED' AND t.agent_role = ANY(:writers)"
                " AND EXISTS (SELECT 1 FROM worktrees w WHERE w.task_id = a.task_id"
                "   AND w.head_commit_sha IS NOT NULL AND w.head_commit_sha <> w.base_commit_sha)"
            ),
            {"t": task_id, "writers": sorted(WRITING_ROLES)},
        )
    ).all()
    return frozenset(str(r[0]) for r in rows), max((int(r[1]) for r in rows), default=0)


async def _previous(conn: AsyncConnection, task_id: uuid.UUID) -> tuple[str | None, bool]:
    rows = (
        await conn.execute(
            text(
                "SELECT provider, failure_category FROM task_attempts WHERE task_id = :t AND status <> 'RUNNING'"
                " ORDER BY attempt_no DESC LIMIT 2"
            ),
            {"t": task_id},
        )
    ).all()
    if not rows:
        # FIX-итерация (ROUTING_POLICY §4.2): у новой fix-задачи своих попыток нет — бонус stickiness получает
        # провайдер исходной реализации (контекст сессии; независимость повторного ревью сохраняется).
        origin = (
            await conn.execute(
                text(
                    "SELECT a.provider FROM tasks t JOIN task_attempts a ON a.task_id = t.origin_task_id"
                    " WHERE t.id = :t AND t.origin_task_id IS NOT NULL AND a.status = 'SUCCEEDED'"
                    " ORDER BY a.attempt_no DESC LIMIT 1"
                ),
                {"t": task_id},
            )
        ).scalar_one_or_none()
        return (str(origin) if origin else None), False
    last = str(rows[0][0])
    twice = len(rows) == 2 and all(r[0] == last and r[1] in _PROVIDER_FAILURES for r in rows)
    return last, twice


async def select(
    conn: AsyncConnection, *, task: dict[str, Any], run: dict[str, Any], now: datetime
) -> router.RouteDecision:
    snapshot: dict[str, Any] = run["policy_snapshot"] or {}
    routing_policy: dict[str, Any] = snapshot.get("routing", {})
    review_policy: dict[str, Any] = snapshot.get("review", {})
    profile = str(run["quality_profile"])
    allowed = frozenset(routing_policy.get("allowed_providers", ALL_PROVIDERS))
    authors, subject_level = await _authors(conn, task["id"])
    previous, failed_twice = await _previous(conn, task["id"])
    assignments: dict[str, int] = dict(
        (
            await conn.execute(
                text(
                    "SELECT a.provider, count(*) FROM task_attempts a JOIN tasks t ON t.id = a.task_id"
                    " WHERE t.run_id = :r GROUP BY a.provider"
                ),
                {"r": run["id"]},
            )
        ).all()
    )
    request = router.RouteRequest(
        role=task["agent_role"],
        level=max(int(task["complexity"]), subject_level),
        now=now,
        allowed_providers=allowed,
        profile=profile if profile in ("basic", "strict", "critical") else "strict",  # type: ignore[arg-type]
        is_review_task=bool(task.get("is_review_task", False)),
        authors=authors,
        independence_required_from_level=int(
            review_policy.get("independence_required_from_level", 2 if profile == "critical" else 3)
        ),
        independence_fallback=review_policy.get("independence_fallback"),
        allow_untested=bool(routing_policy.get("allow_untested_cli_versions", False)),
        previous_provider=previous,
        previous_failed_twice_by_provider=failed_twice,
        implementer_provider=next(iter(authors)) if len(authors) == 1 else None,
        run_assignments={str(k): int(v) for k, v in assignments.items()},
        hints=routing_policy.get("provider_hints", {}).get(task["agent_role"], {}),
        quality=await quality.load(
            conn, workspace_id=run["workspace_id"], role=task["agent_role"], providers=ALL_PROVIDERS, now=now
        ),
        weights=routing_policy.get("weights") or dict(router.DEFAULT_WEIGHTS),
    )
    # Лимит сессий провайдера в workspace (max_parallel_sessions) считается по всем run workspace: решения разных run
    # сериализуются блокировкой транзакции на workspace (run заблокирован вызывающим только свой).
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('aicd:provider_sessions:' || :ws, 0))"),
        {"ws": str(run["workspace_id"])},
    )
    decision = router.route(request, await _candidates(conn, now))
    if decision.outcome != "WAIT":  # ожидание слота — не решение; повторится при освобождении
        await record(conn, task=task, run=run, decision=decision)
    return decision


async def record(
    conn: AsyncConnection, *, task: dict[str, Any], run: dict[str, Any], decision: router.RouteDecision
) -> uuid.UUID:
    decision_id = uuid7()
    snapshot: dict[str, Any] = run["policy_snapshot"] or {}
    await conn.execute(
        text(
            "INSERT INTO routing_decisions (id, workspace_id, task_id, outcome, selected_provider, complexity_level,"
            " score_breakdown, reason, policy_version)"
            " VALUES (:id, :ws, :t, :o, :p, :lvl, CAST(:sb AS jsonb), :reason, :pv)"
        ),
        {
            "id": decision_id,
            "ws": run["workspace_id"],
            "t": task["id"],
            "o": decision.outcome,
            "p": decision.selected.provider if decision.selected else None,
            "lvl": int(task["complexity"]),
            "sb": json.dumps(decision.breakdown, ensure_ascii=False, default=str),
            "reason": decision.reason[:2000],
            "pv": int(snapshot.get("schema", 1)),
        },
    )
    return decision_id
