"""Release security gate (P9-07): `scripts/security_gate.py` на текущей модели угроз и на синтетических нарушениях."""

from __future__ import annotations

import pytest

from scripts import security_gate as gate
from tests.support.contract import ROOT

pytestmark = pytest.mark.security


def _model(rows: str, registry: str) -> str:
    return (
        "# T\n\n## 5. Таблица угроз\n\n| ID | Э | S | О | L | I | Risk | Митигации | Residual | Проверка |\n|---|---|---|---|---|---|---|---|---|---|\n"
        f"{rows}\n\n## 6. Abuse\n\n## 10. Реестр\n\n| Угроза | Risk | Residual | Статус | Закрытие |\n|---|---|---|---|---|\n"
        f"{registry}\n\n## 11. Пересмотр\n"
    )


OQ = "| ID | В | Вар | Рек | Бл |\n|---|---|---|---|---|\n| OQ-90 | x | y | z | — |\n\n## 3. Журнал решений\n| Дата | Вопрос | Решение | Кем |\n|---|---|---|---|\n"
ROW = "| TH-90 | P2 | I | d | 2 | 4 | high | SC-IAM-08 | {res} | t |"


def test_current_threat_model_has_no_open_high_or_critical() -> None:
    errors, warnings = gate.check(
        (ROOT / "docs/security/THREAT_MODEL.md").read_text(encoding="utf-8"),
        (ROOT / "docs/planning/OPEN_QUESTIONS.md").read_text(encoding="utf-8"),
    )
    assert errors == []
    assert all("ожидает решения владельца" in w for w in warnings)
    threats = gate.parse_threat_tables((ROOT / "docs/security/THREAT_MODEL.md").read_text(encoding="utf-8"))
    assert len(threats) >= 65 and all(t.risk and t.residual for t in threats.values())


@pytest.mark.parametrize(
    ("residual", "status", "closure", "expected"),
    [
        ("low", "open", "SC-IAM-08", "открытая угроза"),
        ("low", "", "SC-IAM-08", "открытая угроза"),
        ("high", "closed", "SC-IAM-08", "не может быть closed"),
        ("low", "closed", "SC-AUD-02", "без контроля с автоматическими тестами"),
        ("low", "closed", "SC-XXX-01", "неизвестные контроли"),
        ("low", "accepted:OQ-77", "—", "несуществующий OQ-77"),
    ],
)
def test_violations_fail_the_gate(residual: str, status: str, closure: str, expected: str) -> None:
    text = _model(ROW.format(res=residual), f"| TH-90 | high | {residual} | `{status}` | {closure} |")
    errors, _ = gate.check(text, OQ)
    assert any(expected in e for e in errors), errors


def test_registry_must_match_threat_tables() -> None:
    errors, _ = gate.check(_model(ROW.format(res="low"), "| TH-90 | critical | low | `closed` | SC-IAM-08 |"), OQ)
    assert any("уровни в §10" in e for e in errors)
    errors, _ = gate.check(_model(ROW.format(res="low"), ""), OQ)
    assert any("нет в реестре" in e for e in errors)


def test_closed_requires_passing_tests_when_junit_given() -> None:
    text = _model(ROW.format(res="low"), "| TH-90 | high | low | `closed` | SC-IAM-08 |")
    assert gate.check(text, OQ, control_status={"SC-IAM-08": "PASS"})[0] == []
    assert any("упали" in e for e in gate.check(text, OQ, control_status={"SC-IAM-08": "FAIL"})[0])
    assert any("не подтверждён" in e for e in gate.check(text, OQ, control_status={"SC-IAM-08": "NOT RUN"})[0])


def test_accepted_risk_needs_owner_decision_in_release_mode() -> None:
    text = _model(ROW.format(res="high"), "| TH-90 | high | high | `accepted:OQ-90` | обоснование |")
    errors, warnings = gate.check(text, OQ)
    assert errors == [] and warnings
    errors, _ = gate.check(text, OQ, require_decisions=True)
    assert any("ожидает решения" in e for e in errors)
    decided = OQ + "| 2026-11-09 | OQ-90 | Риск принят | Владелец продукта |\n"
    assert gate.check(text, decided, require_decisions=True) == ([], [])
    ranged = OQ + "| 2026-11-09 | OQ-85…OQ-95 | Приняты рекомендации | Владелец продукта |\n"
    assert gate.check(text, ranged, require_decisions=True) == ([], [])
