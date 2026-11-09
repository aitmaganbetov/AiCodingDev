"""P6-10: seed реестра стандартов — собственные краткие формулировки платформы (ТЗ §13, RISK-13)."""

from __future__ import annotations

import re
import uuid

from aicd_standards.catalog import RULES
from tests.support.standards import migration, seed_controls

GATE_TYPES = {
    "FORMAT", "LINT", "TYPECHECK", "BUILD", "UNIT", "INTEGRATION", "E2E", "SAST", "SCA", "SECRETS",
    "IAC", "POLICY", "MIGRATION", "PERF", "REVIEW", "COVERAGE", "SBOM", "SIGN", "SCOPE",
}  # fmt: skip
AGENT_ROLES = {
    "requirements", "architect", "planner", "developer", "reviewer", "security", "qa", "database",
    "performance", "devops", "maintenance", "documentation",
}  # fmt: skip
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def test_standards_metadata_only() -> None:
    m = migration()
    keys = [(code, edition) for _n, code, edition, *_ in m.STANDARDS]
    assert len(keys) == len(set(keys)) == 18
    for n, code, edition, title, uri in m.STANDARDS:
        assert re.fullmatch(r"[A-Za-z0-9/ ._:-]{2,100}", code), code
        assert 1 <= len(edition) <= 50 and 1 <= len(title) <= 60, code
        assert uri is None or uri.startswith("https://")
        assert uuid.UUID(m.standard_id(n)).version == 7
    note = m.LICENSE_NOTE
    assert "не текст стандарта" in note and "не являются сертификацией" in note


def test_controls_are_platform_authored_and_short() -> None:
    """Формулировки — свои, краткие (без длинных дословных фрагментов стандартов), на русском;
    clause_ref — только ссылка на раздел/идентификатор, не текст."""
    controls = seed_controls()
    assert len(controls) == 33
    for c in controls:
        assert 20 <= len(c.description) <= 120, c.control_code
        assert CYRILLIC.search(c.description), c.control_code  # формулировка платформы, не цитата оригинала
        assert len(c.description.split(".")) <= 2, c.control_code  # одно утверждение
        assert len(c.clause_ref) <= 40 and not CYRILLIC.search(c.clause_ref), c.control_code
    descriptions = [c.description for c in controls]
    assert len(descriptions) == len(set(descriptions))


def test_controls_consistent_with_schema() -> None:
    m = migration()
    standards = {code for _n, code, *_ in m.STANDARDS}
    controls = seed_controls()
    codes = [c.control_code for c in controls]
    assert len(codes) == len(set(codes)), "control_code уникален во всём seed (ключ каталога правил)"
    assert {c.standard_code for c in controls} == standards, "у каждого стандарта есть хотя бы один контроль"
    for c in controls:
        assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", c.control_code)
        assert c.id.version == 7
        assert c.verification_method in {"automated_gate", "agent_review", "manual", "document_check"}
        assert c.default_agent_role in AGENT_ROLES
        # ck_standard_controls_automated
        assert c.verification_method == "manual" or c.default_gate_type in GATE_TYPES
        if c.verification_method == "agent_review":
            assert c.default_gate_type == "REVIEW"


def test_catalog_covers_seed_exactly() -> None:
    assert set(RULES) == {c.control_code for c in seed_controls()}
    for code, rule in RULES.items():
        assert rule.requires_any <= {"code", "dependencies", "web", "database", "container", "typed"}, code
        if rule.cwe_ids:
            assert all(re.fullmatch(r"CWE-[0-9]{1,5}", cwe) for cwe in rule.cwe_ids)
            control = next(c for c in seed_controls() if c.control_code == code)
            assert all(cwe in control.clause_ref for cwe in rule.cwe_ids), "clause_ref ссылается на те же CWE"


def test_seed_sql_is_offline_literal() -> None:
    sql = migration().seed_sql()
    assert sql.count("01a1c008-0001-7000-8000-") == 33
    assert "INSERT INTO standards" in sql and "INSERT INTO standard_controls" in sql
