"""Цель run-исправления («Передать ИИ на исправление»): состав и пределы текста."""

from __future__ import annotations

import uuid

from aicd_orchestration.fix_run import OBJECTIVE_MAX_CHARS, FixSource, compose_objective

RUN = uuid.UUID("01a1c970-026e-7535-856f-455e00d91864")


def _source(**kw: object) -> FixSource:
    base: dict[str, object] = {
        "run_id": RUN,
        "mode": "DEVELOP",
        "objective": "Исправить аудит",
        "failed_gates": [{"name": "checkov-iac", "gate_type": "IAC", "error_message": "THRESHOLD: 2\nfinding(s)"}],
        "infra_errors": [{"name": "trivy-vuln", "gate_type": "SCA", "error_message": "TOOL_UNAVAILABLE: no DB"}],
        "findings": [
            {"severity": "high", "rule_id": "DS-0002", "title": "root user", "file_path": "Dockerfile", "line": 3},
            {"severity": "low", "rule_id": "X-1", "title": "no file", "file_path": None, "line": None},
        ],
        "findings_total": 7,
    }
    return FixSource(**{**base, **kw})  # type: ignore[arg-type]


def test_objective_lists_failures_and_separates_infrastructure_errors() -> None:
    text = compose_objective(_source(), note="  начать с Dockerfile ")
    assert str(RUN) in text and "Исправить аудит" in text and "начать с Dockerfile" in text
    assert "- checkov-iac [IAC]: THRESHOLD: 2 finding(s)" in text  # перевод строки сообщения свёрнут
    assert "Открытые находки (7, показаны первые 2):" in text
    assert "- [high] DS-0002 — root user (Dockerfile:3)" in text and "- [low] X-1 — no file\n" in text
    failures, _, infra = text.partition("Справка: сбои инфраструктуры")
    assert "trivy-vuln" in infra and "trivy-vuln" not in failures and "исправлять их не нужно" in infra


def test_objective_fits_the_run_objective_limit_and_reports_work() -> None:
    huge = [{"severity": "high", "rule_id": "R" * 300, "title": "T" * 2000, "file_path": "a.py", "line": 1}] * 40
    assert len(compose_objective(_source(objective="O" * 20000, findings=huge))) <= OBJECTIVE_MAX_CHARS
    assert _source().has_work
    assert not _source(failed_gates=[], findings=[]).has_work  # одни сбои инфраструктуры — исправлять нечего
