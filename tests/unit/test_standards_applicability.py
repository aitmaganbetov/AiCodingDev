"""P6-10: применимость контролей по стеку проекта и режиму run (ТЗ §13, QUALITY_GATES §8.1)."""

from __future__ import annotations

import pytest

from aicd_standards.applicability import StackFacts, propose, relevant_for_mode, stack_tags
from aicd_standards.catalog import DEFAULT_RULE, rule_for
from aicd_standards.model import RUN_MODES
from tests.support.standards import seed_controls

PY_WEB_DB = StackFacts.of(
    [("lang", "python"), ("python", "FastAPI"), ("python", "sqlalchemy"), ("tool", "docker"), ("python", "mypy")]
)
STATIC_DOCS = StackFacts.of([])


@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        ([], set()),
        ([("lang", "python")], {"code"}),
        ([("python", "requests")], {"code", "dependencies"}),
        ([("npm", "express")], {"code", "dependencies", "web"}),
        ([("npm", "@prisma/client")], {"code", "dependencies", "database"}),
        ([("docker", "library/postgres")], {"container", "database"}),
        ([("docker", "nginx")], {"container"}),
        ([("tool", "docker-compose")], {"container"}),
        ([("lang", "typescript")], {"code", "typed"}),
        ([("composer", "laravel/framework")], {"code", "dependencies", "web", "database"}),
        ([("composer", "phpstan/phpstan")], {"code", "dependencies", "typed"}),
        ([("ci", "github-actions")], set()),
    ],
)
def test_stack_tags(pairs: list[tuple[str, str]], expected: set[str]) -> None:
    assert stack_tags(StackFacts.of(pairs)) == expected


def _by_code(facts: StackFacts) -> dict[str, tuple[str, str]]:
    return {p.control_code: (p.applicability, p.rationale) for p in propose(seed_controls(), facts)}


def test_python_web_service_with_database() -> None:
    result = _by_code(PY_WEB_DB)
    assert len(result) == len(seed_controls())
    for code in ("SQL-9075-01", "ASVS-V2-01", "CWE-XSS-01", "QM-25010-03", "SSDF-RV.1-01", "ASVS-V14-01"):
        assert result[code][0] == "applicable", code
    assert "база данных или ORM" in result["SQL-9075-01"][1]
    assert result["SEC-27002-01"] == ("applicable", "Применим к любому проекту (правило платформы).")


def test_project_without_code_gets_rationale_for_exclusions() -> None:
    result = _by_code(STATIC_DOCS)
    for code in ("QM-25010-04", "SQL-9075-01", "ASVS-V2-01", "SSDF-RV.1-01", "CWE-INJ-01"):
        applicability, rationale = result[code]
        assert applicability == "not_applicable" and rationale.startswith("В стеке проекта не обнаружено"), code
    # Секреты, область изменений, ревью и процессные контроли применимы всегда.
    for code in ("SEC-27002-01", "SEC-27001-01", "REV-1028-01", "LC-12207-01", "DOC-15289-01"):
        assert result[code][0] == "applicable", code


def test_proposals_are_deterministic_and_keep_roles() -> None:
    first = propose(seed_controls(), PY_WEB_DB)
    second = propose(list(reversed(seed_controls())), PY_WEB_DB)
    assert first == second
    roles = {p.control_code: p.agent_role for p in first}
    assert roles["SQL-9075-01"] == "database" and roles["ASVS-V2-01"] == "security"


def test_relevance_for_run_mode() -> None:
    assert relevant_for_mode("QM-25010-05", "OPTIMIZE") and not relevant_for_mode("QM-25010-05", "DEVELOP")
    assert relevant_for_mode("MAINT-14764-01", "MAINTAIN") and not relevant_for_mode("MAINT-14764-01", "CREATE")
    assert not relevant_for_mode("ARCH-42010-02", "DEBUG")
    assert all(relevant_for_mode("SEC-27002-01", mode) for mode in RUN_MODES)
    assert relevant_for_mode("UNKNOWN-CONTROL", "AUDIT")  # не из каталога — применим всегда
    assert rule_for("UNKNOWN-CONTROL") == DEFAULT_RULE
    for code in (c.control_code for c in seed_controls()):
        modes = rule_for(code).modes
        assert modes is None or (modes and modes <= RUN_MODES), code
