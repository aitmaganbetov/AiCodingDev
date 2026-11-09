"""Отчёт run в `text/markdown` (GET /runs/{id}/report, Accept: text/markdown; API_DESIGN §1, QUALITY_GATES §5.3).

Строится из того же словаря `RunReport`, что и JSON, — содержимое не расходится. Любое значение из БД
(в том числе свободный текст агентов и инструментов) редактируется (`redact_text`) и экранируется: символы
разметки Markdown и HTML не могут сломать таблицу или внедрить ссылку/HTML в отображение отчёта.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from aicd_platform.clock import isoformat_z
from aicd_platform.redaction import redact_text

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]|~])")
_HTML = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def md(value: Any) -> str:
    """Безопасная ячейка/фрагмент: редактирование секретов, HTML-сущности, экранирование Markdown,
    перевод строк — пробелом (таблица не разрывается). None — «—»."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return isoformat_z(value)
    text = redact_text(str(value))
    text = "".join(_HTML.get(ch, ch) for ch in text)
    text = _MD_SPECIAL.sub(r"\\\1", text)
    return " ".join(text.split())


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    body = ["| " + " | ".join(md(c) for c in row) + " |" for row in rows]
    return [*lines, *(body or ["| " + " | ".join(["—"] * len(headers)) + " |"]), ""]


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{md(k)}: {int(v)}" for k, v in counts.items()) or "—"


def render_run_report(report: dict[str, Any]) -> str:
    run, quality = report["run"], report["quality"]
    out: list[str] = [
        f"# Отчёт run {md(run['id'])}",
        "",
        f"- Режим: {md(run['mode'])}; статус: {md(run['status'])}; профиль качества: {md(quality['quality_profile'])}",
        f"- Цель: {md(run['objective'])}",
        f"- Базовый commit: {md(run.get('base_commit_sha'))}",
        f"- Сформирован: {md(report['generated_at'])}",
        "",
        f"## Вердикт качества: {md(quality['verdict'])}",
        "",
        "Вердикт — агрегат по обязательным gates; SKIPPED обязательного gate и PASS без VERIFIED evidence не дают PASS.",
        "",
        "### Gates (по задаче и commit)",
        "",
    ]
    out += _table(
        ["Задача", "Commit", "Gate", "Обязательный", "Статус", "Код ошибки", "Причина", "Evidence", "Завершён"],
        (
            (
                g["task_id"] or "run",
                g["commit_sha"],
                g["gate_type"],
                g["required"],
                g["status"],
                g["error_code"],
                g["not_applicable_reason"],
                g["evidence_id"],
                g["finished_at"],
            )
            for g in sorted(
                quality["gate_executions"],
                key=lambda g: (str(g["task_id"] or ""), g["commit_sha"] or "", g["gate_type"]),
            )
        ),
    )
    out += [
        "### Находки",
        "",
        f"- Открытые по severity: {_counts(quality['findings_summary'])}",
        f"- По состоянию жизненного цикла: {_counts(quality.get('findings_by_status', {}))}",
        f"- Всего: {int(quality['findings_total'])}",
        "",
    ]
    out += _table(
        ["Severity", "Статус", "Правило", "CWE", "Файл", "Строка", "Описание", "Evidence"],
        (
            (f["severity"], f["status"], f["rule_id"], f.get("cwe_id"), f["file_path"], f["line"], f.get("title"),
             f["evidence_id"])
            for f in report.get("findings", quality["findings"])
        ),
    )  # fmt: skip
    out += ["## Evidence (AC-14)", ""]
    out += _table(
        ["Evidence", "Задача", "Вид", "Статус", "Commit", "Команда", "Exit code", "Начало", "Конец", "Окружение",
         "Artifact sha256"],
        (
            (e["id"], e["task_id"], e["kind"], e["verification_status"], e["commit_sha"], e["command"], e["exit_code"],
             e["started_at"], e["finished_at"], _environment(e["environment"]), e["artifact"]["sha256"])
            for e in report["evidence"]
        ),
    )  # fmt: skip
    out += [
        "## Standards & Compliance",
        "",
        f"> {md(report['disclaimer'])}",
        "",
        f"Сводка: {_counts(report.get('compliance_summary') or {})}",
        "",
        "Трассировка: стандарт → контроль → применимость → роль → проверка → evidence → вердикт.",
        "",
    ]
    out += _table(
        ["Стандарт", "Контроль", "Раздел", "Применимость", "Роль", "Проверка", "Gate", "Commit", "Evidence",
         "sha256", "Вердикт", "Проверил"],
        (
            (f"{t['standard']['code']} {t['standard']['edition']}", t["control"]["code"], t["control"]["clause_ref"],
             t["applicability"], t["agent_role"], t["check"]["method"],
             f"{t['check']['gate_type'] or '—'} {t['check']['gate_status'] or ''}".strip(), t["check"]["commit_sha"],
             (t["evidence"] or {}).get("id"), (t["evidence"] or {}).get("artifact_sha256"), t["verdict"]["status"],
             (t["verdict"]["reviewed_by"] or {}).get("display_name"))
            for t in report.get("compliance_matrix", [])
        ),
    )  # fmt: skip
    out += ["## Задачи", ""]
    out += _table(
        ["Задача", "Роль", "Название", "Статус", "Попытки"],
        ((t["id"], t["agent_role"], t["title"], t["status"], f"{t['attempt_count']}/{t['max_attempts']}")
         for t in report["tasks"]),
    )  # fmt: skip
    out += ["## Approvals", ""]
    out += _table(
        ["Approval", "Действие", "Статус", "Риск", "Решение принял"],
        ((a["id"], a["action_type"], a["status"], a["risk_level"], (a["decided_by"] or {}).get("display_name"))
         for a in report["approvals"]),
    )  # fmt: skip
    if report.get("external_actions"):
        out += ["## Внешние действия", ""]
        out += _table(
            ["Действие", "Тип", "Статус", "Ссылка"],
            ((x["id"], x["action_type"], x["status"], x["external_ref"]) for x in report["external_actions"]),
        )
    return "\n".join(out).rstrip() + "\n"


def _environment(env: dict[str, Any]) -> str:
    """Компактно: runner, образ и версии инструментов (полное окружение — в JSON-отчёте)."""
    parts = []
    for key in ("runner", "image", "network"):
        if env.get(key):
            parts.append(f"{key}={env[key]}")
    versions = env.get("tool_versions")
    if isinstance(versions, dict):
        parts.extend(f"{k}={v}" for k, v in sorted(versions.items()))
    return "; ".join(parts) or "—"
