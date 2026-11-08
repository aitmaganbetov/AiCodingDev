# ruff: noqa: S608 — SQL собирается только из констант модуля (фильтры/тела представлений); значения — bind-параметры.
"""Сводка Analytics workspace за окно времени (P8-07, FR-OBS-05, FR-CI-08).

Источник данных — только read-only представления `v_*` модулей-источников (DATABASE_DESIGN §17.2,
MODULE_BOUNDARIES §2 п.6; миграция 0013, `security_invoker`): таблиц других модулей модуль не читает.
Все запросы — в RLS-контексте workspace (`aicd_app`), фильтр `workspace_id = app_current_workspace()`
дублирует RLS явно (defence in depth и использование индексов).

Определения метрик (ТЗ §16):
* run success rate — доля `COMPLETED` среди завершённых run окна (создание run в окне);
* verified completion rate — доля успешных попыток среди попыток, характеризующих качество агента
  (ROUTING_POLICY §7: неудачи только из `FAILURES`, исключения `EXCLUDED` не учитываются), без сглаживания;
* retry count — попытки с `attempt_no > 1`; task latency — от создания задачи до терминального статуса;
* gate failures — `FAIL`/`ERROR` актуальных (не заменённых повтором) исполнений gates;
* provider usage/limits — исполнения CLI по провайдеру и статусу Execution FSM (`RATE_LIMITED`,
  `AUTH_REQUIRED` — ограничения), токены — только `cli_reported` (иначе `null`, UI показывает «unknown»);
* review loops — ACP `REVIEW_REQUEST`/`FIX_REQUEST` по цепочкам `correlation_id`, эскалации
  `REVIEW_LOOP_EXHAUSTED`; agent message count — принятые ACP-сообщения по типу;
* approval wait time — от `requested_at` до `decided_at` решений человека (APPROVED/REJECTED);
* context pack size, доля stale-фрагментов (index freshness) и полезность контекста (FR-CI-08, ADR-0005):
  доля уникальных путей фрагментов Context Pack, на которые сослались заявления (`claims[].files`) итога
  попытки, подтверждённой платформой (`task_attempts.status = SUCCEEDED` — только после PASS обязательных
  gates, AC-11). Заявления агента сами по себе не считаются подтверждением; знаменатель — пакеты успешных
  попыток. Оценка без эмбеддингов, без обещаний экономии.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# ROUTING_POLICY §7 (= aicd_orchestration.quality.FAILURES; модуль того же слоя не импортируется — сверка в тесте).
QUALITY_FAILURES = (
    "GATE_FAILED",
    "REVIEW_REJECTED",
    "PROCESS_FAILED",
    "OUTPUT_PARSE_ERROR",
    "TIMEOUT",
    "SANDBOX_DENIED",
    "POLICY_DENIED",
)
RUN_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")
USEFULNESS_PACK_LIMIT = 2000  # верхняя граница пакетов для оценки полезности за запрос
MAX_DAILY_POINTS = 400


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    project_id: uuid.UUID | None = None

    def params(self) -> dict[str, Any]:
        return {"from_ts": self.start, "to_ts": self.end, "project": self.project_id}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _num(value: Any, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _stats(row: Mapping[str, Any] | None) -> dict[str, float | None]:
    row = row or {}
    return {"p50": _num(row.get("p50")), "p90": _num(row.get("p90")), "avg": _num(row.get("avg"))}


# Фильтр workspace и проекта (у каждого представления есть workspace_id и project_id). `:project` NULL ⇒ все проекты.
_SCOPE = "v.workspace_id = app_current_workspace() AND (CAST(:project AS uuid) IS NULL OR v.project_id = :project)"


async def _all(conn: AsyncConnection, sql: str, params: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    return [dict(r) for r in (await conn.execute(text(sql), dict(params))).mappings().all()]


async def _one(conn: AsyncConnection, sql: str, params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    row = (await conn.execute(text(sql), dict(params))).mappings().first()
    return dict(row) if row is not None else None


# --- v_run_outcomes (orchestration: runs) ------------------------------------------------------------------


async def runs_section(conn: AsyncConnection, w: Window) -> dict[str, Any]:
    rows = await _all(
        conn,
        "SELECT v.mode, v.status, count(*) AS n, avg(v.duration_seconds) AS avg_duration"
        f" FROM v_run_outcomes v WHERE {_SCOPE} AND v.created_at >= :from_ts AND v.created_at < :to_ts"
        " GROUP BY v.mode, v.status",
        w.params(),
    )
    by_status: dict[str, int] = {}
    modes: dict[str, dict[str, Any]] = {}
    for row in rows:
        status, mode, n = str(row["status"]), str(row["mode"]), int(row["n"])
        by_status[status] = by_status.get(status, 0) + n
        m = modes.setdefault(mode, {"mode": mode, "total": 0, "finished": 0, "completed": 0, "_dur": 0.0, "_dn": 0})
        m["total"] += n
        if status in RUN_TERMINAL:
            m["finished"] += n
            if row["avg_duration"] is not None:
                m["_dur"] += float(row["avg_duration"]) * n
                m["_dn"] += n
        if status == "COMPLETED":
            m["completed"] += n
    durations = await _one(
        conn,
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS p50,"
        " percentile_cont(0.9) WITHIN GROUP (ORDER BY d) AS p90, avg(d) AS avg FROM ("
        " SELECT v.duration_seconds AS d FROM v_run_outcomes v"
        f" WHERE {_SCOPE} AND v.created_at >= :from_ts AND v.created_at < :to_ts"
        " AND v.finished_at IS NOT NULL) s",
        w.params(),
    )
    finished = sum(by_status.get(s, 0) for s in RUN_TERMINAL)
    completed = by_status.get("COMPLETED", 0)
    return {
        "total": sum(by_status.values()),
        "finished": finished,
        "completed": completed,
        "success_rate": _rate(completed, finished),
        "by_status": dict(sorted(by_status.items())),
        "duration_seconds": _stats(durations),
        "by_mode": [
            {
                "mode": m["mode"],
                "total": m["total"],
                "finished": m["finished"],
                "completed": m["completed"],
                "success_rate": _rate(m["completed"], m["finished"]),
                "avg_duration_seconds": _num(m["_dur"] / m["_dn"]) if m["_dn"] else None,
            }
            for m in sorted(modes.values(), key=lambda x: str(x["mode"]))
        ],
    }


async def daily_section(conn: AsyncConnection, w: Window) -> list[dict[str, Any]]:
    rows = await _all(
        conn,
        "SELECT to_char(date_trunc('day', v.created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD') AS day, count(*) AS created,"
        " count(*) FILTER (WHERE v.status = 'COMPLETED') AS completed,"
        " count(*) FILTER (WHERE v.status = 'FAILED') AS failed,"
        " count(*) FILTER (WHERE v.status = 'CANCELLED') AS cancelled"
        f" FROM v_run_outcomes v WHERE {_SCOPE} AND v.created_at >= :from_ts AND v.created_at < :to_ts"
        " GROUP BY 1 ORDER BY 1 LIMIT :n",
        {**w.params(), "n": MAX_DAILY_POINTS},
    )
    return [
        {
            "date": str(r["day"]),
            "runs_created": int(r["created"]),
            "runs_completed": int(r["completed"]),
            "runs_failed": int(r["failed"]),
            "runs_cancelled": int(r["cancelled"]),
        }
        for r in rows
    ]


# --- v_task_latency / v_attempt_outcomes (orchestration: tasks, task_attempts) -----------------------------


async def tasks_section(conn: AsyncConnection, w: Window) -> dict[str, Any]:
    params = {**w.params(), "failures": list(QUALITY_FAILURES)}
    roles = await _all(
        conn,
        "SELECT v.agent_role AS role, count(*) AS attempts,"
        " count(*) FILTER (WHERE v.status = 'SUCCEEDED') AS succeeded,"
        " count(*) FILTER (WHERE v.status = 'FAILED' AND v.failure_category = ANY(:failures)) AS quality_failed,"
        " count(*) FILTER (WHERE v.status = 'FAILED') AS failed,"
        " count(*) FILTER (WHERE v.attempt_no > 1) AS retries"
        f" FROM v_attempt_outcomes v WHERE {_SCOPE} AND v.started_at >= :from_ts AND v.started_at < :to_ts"
        " GROUP BY v.agent_role ORDER BY v.agent_role",
        params,
    )
    by_role = [
        {
            "role": str(r["role"]),
            "attempts": int(r["attempts"]),
            "succeeded": int(r["succeeded"]),
            "failed": int(r["failed"]),
            "retries": int(r["retries"]),
            "verified_completion_rate": _rate(int(r["succeeded"]), int(r["succeeded"]) + int(r["quality_failed"])),
        }
        for r in roles
    ]
    succeeded = sum(int(r["succeeded"]) for r in roles)
    quality_failed = sum(int(r["quality_failed"]) for r in roles)
    latency = await _one(
        conn,
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS p50,"
        " percentile_cont(0.9) WITHIN GROUP (ORDER BY d) AS p90, avg(d) AS avg, count(*) AS n FROM ("
        " SELECT v.latency_seconds AS d FROM v_task_latency v"
        f" WHERE {_SCOPE} AND v.finished_at IS NOT NULL AND v.finished_at >= :from_ts AND v.finished_at < :to_ts) s",
        w.params(),
    )
    return {
        "attempts": sum(int(r["attempts"]) for r in roles),
        "succeeded": succeeded,
        "failed": sum(int(r["failed"]) for r in roles),
        "retries": sum(int(r["retries"]) for r in roles),
        "verified_completion_rate": _rate(succeeded, succeeded + quality_failed),
        "tasks_finished": int((latency or {}).get("n") or 0),
        "latency_seconds": _stats(latency),
        "by_role": by_role,
    }


# --- v_gate_outcomes (verification: gate_executions) ------------------------------------------------------


async def gates_section(conn: AsyncConnection, w: Window) -> list[dict[str, Any]]:
    rows = await _all(
        conn,
        "SELECT v.gate_type, count(*) AS total,"
        " count(*) FILTER (WHERE v.status = 'PASS') AS passed,"
        " count(*) FILTER (WHERE v.status = 'FAIL') AS failed,"
        " count(*) FILTER (WHERE v.status = 'ERROR') AS errored,"
        " count(*) FILTER (WHERE v.status IN ('SKIPPED','NOT_APPLICABLE')) AS skipped,"
        " avg(v.duration_seconds) AS avg_d"
        f" FROM v_gate_outcomes v WHERE {_SCOPE} AND v.finished_at IS NOT NULL"
        " AND v.finished_at >= :from_ts AND v.finished_at < :to_ts"
        " GROUP BY v.gate_type ORDER BY v.gate_type",
        w.params(),
    )
    out = []
    for r in rows:
        passed, failed, errored = int(r["passed"]), int(r["failed"]), int(r["errored"])
        out.append(
            {
                "gate_type": str(r["gate_type"]),
                "total": int(r["total"]),
                "passed": passed,
                "failed": failed,
                "errored": errored,
                "skipped": int(r["skipped"]),
                # SKIPPED ≠ PASS (ТЗ §14): знаменатель — только исполненные проверки.
                "pass_rate": _rate(passed, passed + failed + errored),
                "avg_duration_seconds": _num(r["avg_d"]),
            }
        )
    return out


# --- v_cli_usage (cli_adapters: cli_executions, usage_metrics, cli_installations, cli_providers) ------------


async def providers_section(conn: AsyncConnection, w: Window) -> list[dict[str, Any]]:
    scope = f" FROM v_cli_usage v WHERE {_SCOPE} AND v.created_at >= :from_ts AND v.created_at < :to_ts"
    rows = await _all(
        conn,
        "SELECT v.provider, count(*) AS executions,"
        " count(*) FILTER (WHERE v.status = 'COLLECTED') AS collected,"
        " count(*) FILTER (WHERE v.status IN ('TIMED_OUT','CRASHED')) AS errors,"
        " count(*) FILTER (WHERE v.status = 'RATE_LIMITED') AS rate_limited,"
        " count(*) FILTER (WHERE v.status = 'AUTH_REQUIRED') AS auth_required,"
        " count(*) FILTER (WHERE v.status = 'CANCELLED') AS cancelled,"
        " count(*) FILTER (WHERE v.finished_at IS NULL) AS in_progress,"
        " avg(v.duration_seconds) AS avg_d,"
        " COALESCE(sum(v.usage_reported), 0) AS tokens_reported,"
        " COALESCE(sum(v.usage_not_exposed), 0) AS tokens_not_exposed,"
        " sum(v.usage_total) AS tokens_total"
        f"{scope} GROUP BY v.provider ORDER BY v.provider",
        w.params(),
    )
    failures = await _all(
        conn,
        f"SELECT v.provider, v.failure_category AS category, count(*) AS n{scope}"
        " AND v.failure_category IS NOT NULL GROUP BY v.provider, v.failure_category",
        w.params(),
    )
    by_failure: dict[str, dict[str, int]] = {}
    for f in failures:
        by_failure.setdefault(str(f["provider"]), {})[str(f["category"])] = int(f["n"])
    out = []
    for r in rows:
        provider = str(r["provider"])
        reported = int(r["tokens_reported"])
        not_exposed = int(r["tokens_not_exposed"])
        source = "cli_reported" if reported and not not_exposed else "mixed" if reported else "not_exposed_by_cli"
        out.append(
            {
                "provider": provider,
                "executions": int(r["executions"]),
                "collected": int(r["collected"]),
                "errors": int(r["errors"]),
                "rate_limited": int(r["rate_limited"]),
                "auth_required": int(r["auth_required"]),
                "cancelled": int(r["cancelled"]),
                "in_progress": int(r["in_progress"]),
                "success_rate": _rate(int(r["collected"]), int(r["executions"]) - int(r["in_progress"])),
                "avg_duration_seconds": _num(r["avg_d"]),
                "failures_by_category": dict(sorted(by_failure.get(provider, {}).items())),
                # Токены — только достоверно сообщённые CLI (ТЗ §9, §16): иначе null («unknown» в UI).
                "tokens": {
                    "total": int(r["tokens_total"]) if r["tokens_total"] is not None else None,
                    "source": source,
                    "executions_reported": reported,
                },
            }
        )
    return out


# --- v_review_loops / v_agent_messages (collaboration: agent_messages; orchestration: tasks) -----------------


async def collaboration_section(conn: AsyncConnection, w: Window) -> tuple[dict[str, Any], dict[str, Any]]:
    scope = f" FROM v_review_loops v WHERE {_SCOPE} AND v.created_at >= :from_ts AND v.created_at < :to_ts"
    by_type_rows = await _all(
        conn, f"SELECT v.message_type AS t, count(*) AS n{scope} GROUP BY v.message_type", w.params()
    )
    by_type = {str(r["t"]): int(r["n"]) for r in by_type_rows}
    loops = await _one(
        conn,
        "SELECT count(*) AS chains, COALESCE(max(fixes), 0) AS max_fixes, avg(fixes) AS avg_fixes,"
        " count(*) FILTER (WHERE fixes > 0) AS with_fixes FROM ("
        " SELECT v.correlation_id, count(*) FILTER (WHERE v.message_type = 'FIX_REQUEST') AS fixes"
        f"{scope} GROUP BY v.correlation_id HAVING bool_or(v.message_type = 'REVIEW_REQUEST')) s",
        w.params(),
    )
    exhausted = await _one(
        conn,
        "SELECT count(*) AS n FROM v_task_latency v"
        f" WHERE {_SCOPE} AND v.blocked_reason = 'REVIEW_LOOP_EXHAUSTED'"
        " AND v.updated_at >= :from_ts AND v.updated_at < :to_ts",
        w.params(),
    )
    loops = loops or {}
    review = {
        "review_requests": by_type.get("REVIEW_REQUEST", 0),
        "fix_requests": by_type.get("FIX_REQUEST", 0),
        "review_chains": int(loops.get("chains") or 0),
        "chains_with_fixes": int(loops.get("with_fixes") or 0),
        "avg_fix_rounds": _num(loops.get("avg_fixes"), 2),
        "max_fix_rounds": int(loops.get("max_fixes") or 0),
        "loops_exhausted": int((exhausted or {}).get("n") or 0),
    }
    messages = {"total": sum(by_type.values()), "by_type": dict(sorted(by_type.items()))}
    return review, messages


# --- v_approval_wait (orchestration: approvals) ---------------------------------------------------------------


async def approvals_section(conn: AsyncConnection, w: Window) -> dict[str, Any]:
    scope = f" FROM v_approval_wait v WHERE {_SCOPE} AND v.requested_at >= :from_ts AND v.requested_at < :to_ts"
    counts = await _all(conn, f"SELECT v.status, count(*) AS n{scope} GROUP BY v.status", w.params())
    wait = await _one(
        conn,
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS p50,"
        " percentile_cont(0.9) WITHIN GROUP (ORDER BY d) AS p90, avg(d) AS avg FROM ("
        f" SELECT v.wait_seconds AS d{scope} AND v.wait_seconds IS NOT NULL) s",
        w.params(),
    )
    by_status = {str(r["status"]): int(r["n"]) for r in counts}
    return {
        "requested": sum(by_status.values()),
        "pending": by_status.get("PENDING", 0),
        "by_status": dict(sorted(by_status.items())),
        "wait_seconds": _stats(wait),
    }


# --- v_context_usefulness (code_intelligence: context_packs; orchestration: task_attempts, activity) ---------


async def context_section(conn: AsyncConnection, w: Window) -> dict[str, Any]:
    scope = f" FROM v_context_usefulness v WHERE {_SCOPE} AND v.created_at >= :from_ts AND v.created_at < :to_ts"
    sizes = await _one(
        conn,
        "SELECT count(*) AS packs, avg(v.size_bytes) AS avg_size, max(v.size_bytes) AS max_size,"
        " COALESCE(sum(v.fragments), 0) AS fragments, COALESCE(sum(v.stale_fragments), 0) AS stale"
        f"{scope}",
        w.params(),
    )
    rows = await _all(
        conn,
        "SELECT v.fragment_paths AS paths, v.claimed_files AS claimed"
        f"{scope} AND v.attempt_status = 'SUCCEEDED' ORDER BY v.created_at DESC LIMIT :lim",
        {**w.params(), "lim": USEFULNESS_PACK_LIMIT},
    )
    referenced = total = evaluated = 0
    for row in rows:
        paths = {str(p) for p in row["paths"] or []}
        if not paths:
            continue
        claimed = {_normalize(str(f)) for f in row["claimed"] or []}
        evaluated += 1
        total += len(paths)
        referenced += sum(1 for p in paths if _normalize(p) in claimed)
    sizes = sizes or {}
    fragments = int(sizes.get("fragments") or 0)
    return {
        "packs": int(sizes.get("packs") or 0),
        "avg_size_bytes": _num(sizes.get("avg_size"), 1),
        "max_size_bytes": int(sizes["max_size"]) if sizes.get("max_size") is not None else None,
        "fragments": fragments,
        "stale_fragment_ratio": _rate(int(sizes.get("stale") or 0), fragments),
        "usefulness": {
            "rate": _rate(referenced, total),
            "packs_evaluated": evaluated,
            "fragments_referenced": referenced,
            "fragments_total": total,
            "truncated": len(rows) >= USEFULNESS_PACK_LIMIT,
        },
    }


def _normalize(path: str) -> str:
    return path.strip().removeprefix("./").lstrip("/")


async def summary(conn: AsyncConnection, w: Window) -> dict[str, Any]:
    """Полная сводка окна; соединение — в RLS-контексте workspace пользователя."""
    review, messages = await collaboration_section(conn, w)
    return {
        "window": {"from": w.start, "to": w.end},
        "project_id": w.project_id,
        "runs": await runs_section(conn, w),
        "daily": await daily_section(conn, w),
        "tasks": await tasks_section(conn, w),
        "gates": await gates_section(conn, w),
        "providers": await providers_section(conn, w),
        "review_loops": review,
        "agent_messages": messages,
        "approvals": await approvals_section(conn, w),
        "context": await context_section(conn, w),
    }
