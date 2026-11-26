"""«Передать ИИ на исправление»: run-исправление по итогам неуспешного run.

Новый run режима DEVELOP в том же проекте получает цель, составленную платформой из фактов исходного run:
исходная цель, проваленные обязательные gates (последние исполнения, `FAIL`) и открытые находки. Сбои
инфраструктуры проверок (`ERROR`: нет инструмента, БД уязвимостей, зависимостей) агент изменением кода не
устранит — они перечисляются отдельно как справка и основанием для исправления не считаются.

Изменения исходного run в базу нового не входят: run-исправление стартует от текущего HEAD рабочей копии
(как любой run), поэтому исходная цель повторяется целиком. Текст цели — данные пользователя: в промпте он
передаётся в ограждении UNTRUSTED (`agents.prompt`), сообщения сканеров в нём инструкциями не становятся.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_orchestration import runs as runs_service
from aicd_platform.errors import ProblemError

OBJECTIVE_MAX_CHARS: Final = 20000  # RunCreateRequest.objective
MAX_GATES: Final = 30
MAX_FINDINGS: Final = 40
_LINE_MAX: Final = 400
_SOURCE_OBJECTIVE_MAX: Final = 6000
_NOTE_MAX: Final = 2000
#: Статусы исходного run, из которых исправление имеет смысл: работа остановилась без принятого результата.
FIXABLE_STATUSES: Final = frozenset({"FAILED"})


@dataclass(frozen=True)
class FixSource:
    run_id: uuid.UUID
    mode: str
    objective: str
    failed_gates: Sequence[Mapping[str, Any]]
    infra_errors: Sequence[Mapping[str, Any]]
    findings: Sequence[Mapping[str, Any]]
    findings_total: int

    @property
    def has_work(self) -> bool:
        return bool(self.failed_gates or self.findings)


def _one_line(value: object, limit: int = _LINE_MAX) -> str:
    flat = " ".join(str(value or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def compose_objective(source: FixSource, note: str | None = None) -> str:
    """Текст цели run-исправления (детерминированный, в пределах лимита цели run)."""
    lines = [
        f"Исправление после неуспешного запуска {source.run_id} (режим {source.mode}).",
        "",
        "Исходная цель:",
        str(source.objective).strip()[:_SOURCE_OBJECTIVE_MAX],
        "",
        "Требуется: выполнить исходную цель и устранить перечисленные ниже причины отказа. Изменения исходного"
        " запуска в рабочую копию не попали — работа начинается от текущего состояния репозитория.",
    ]
    if note and note.strip():
        lines += ["", "Уточнение пользователя:", note.strip()[:_NOTE_MAX]]
    if source.failed_gates:
        lines += ["", "Проваленные проверки (gates) исходного запуска:"]
        lines += [
            f"- {g['name']} [{g['gate_type']}]: {_one_line(g['error_message']) or 'FAIL'}" for g in source.failed_gates
        ]
    if source.findings:
        shown = len(source.findings)
        suffix = f", показаны первые {shown}" if source.findings_total > shown else ""
        lines += ["", f"Открытые находки ({source.findings_total}{suffix}):"]
        for f in source.findings:
            where = f" ({f['file_path']}{':' + str(f['line']) if f['line'] else ''})" if f["file_path"] else ""
            lines.append(f"- [{f['severity']}] {_one_line(f['rule_id'], 120)} — {_one_line(f['title'], 240)}{where}")
    if source.infra_errors:
        lines += [
            "",
            "Справка: сбои инфраструктуры проверок (изменением кода не устраняются, исправлять их не нужно):",
        ]
        lines += [
            f"- {g['name']} [{g['gate_type']}]: {_one_line(g['error_message'], 200)}" for g in source.infra_errors
        ]
    return "\n".join(lines)[:OBJECTIVE_MAX_CHARS]


async def load_source(conn: AsyncConnection, run_id: uuid.UUID) -> tuple[Mapping[str, Any], FixSource] | None:
    """Факты исходного run (в контексте его workspace); None — run не найден."""
    run = (
        (
            await conn.execute(
                text(
                    "SELECT id, workspace_id, project_id, mode, objective, status, quality_profile, approval_policy,"
                    " policy_snapshot FROM runs WHERE id = :r"
                ),
                {"r": run_id},
            )
        )
        .mappings()
        .first()
    )
    if run is None:
        return None
    gates = (
        (
            await conn.execute(
                text(
                    "SELECT g.name, e.gate_type, e.status::text AS status, e.error_message FROM gate_executions e"
                    " JOIN quality_gates g ON g.id = e.gate_id"
                    " WHERE e.run_id = :r AND e.superseded_at IS NULL AND e.required"
                    " AND e.status::text IN ('FAIL', 'ERROR') ORDER BY e.status::text DESC, g.name LIMIT :n"
                ),
                {"r": run_id, "n": MAX_GATES * 2},
            )
        )
        .mappings()
        .all()
    )
    findings = (
        (
            await conn.execute(
                text(
                    "SELECT f.severity::text AS severity, f.rule_id, f.title, f.file_path, f.line FROM findings f"
                    " WHERE f.run_id = :r AND f.status::text = 'OPEN' ORDER BY"
                    " array_position(ARRAY['critical','high','medium','low','info'], f.severity::text),"
                    " f.file_path, f.line"
                    " LIMIT :n"
                ),
                {"r": run_id, "n": MAX_FINDINGS},
            )
        )
        .mappings()
        .all()
    )
    total = (
        await conn.execute(
            text("SELECT count(*) FROM findings WHERE run_id = :r AND status::text = 'OPEN'"), {"r": run_id}
        )
    ).scalar_one()
    gate_rows = [dict(g) for g in gates]
    return dict(run), FixSource(
        run_id=run_id,
        mode=str(run["mode"]),
        objective=str(run["objective"]),
        failed_gates=[g for g in gate_rows if g["status"] == "FAIL"][:MAX_GATES],
        infra_errors=[g for g in gate_rows if g["status"] == "ERROR"][:MAX_GATES],
        findings=[dict(f) for f in findings],
        findings_total=int(total),
    )


async def create_fix_run(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    requested_by: uuid.UUID,
    note: str | None = None,
    repository_roots: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    loaded = await load_source(conn, run_id)
    if loaded is None:
        raise ProblemError("not_found")
    run, source = loaded
    if run["status"] not in FIXABLE_STATUSES:
        raise ProblemError(
            "invalid_state_transition", detail="Исправление создаётся только для запуска в статусе FAILED"
        )
    if not source.has_work:
        # Только сбои инфраструктуры проверок: агенту нечего исправлять, повтор дал бы тот же отказ.
        raise ProblemError(
            "invalid_state_transition",
            detail="У запуска нет проваленных проверок и открытых находок: отказ вызван инфраструктурой проверок",
        )
    routing = (run["policy_snapshot"] or {}).get("routing") or {}
    return await runs_service.create_run(
        conn,
        workspace_id=run["workspace_id"],
        project_id=run["project_id"],
        requested_by=requested_by,
        mode="DEVELOP",
        objective=compose_objective(source, note),
        quality_profile=run["quality_profile"],
        approval_policy=run["approval_policy"],
        allowed_providers=list(routing.get("allowed_providers") or []) or None,
        routing_strategy=str(routing.get("strategy") or "adaptive"),
        repository_roots=repository_roots,
    )
