"""Правила платформы для контролей seed-реестра (миграция 0008, QUALITY_GATES §8.3).

Ключ — `standard_controls.control_code` (уникален в seed). Сами контроли (формулировки, метод проверки,
роль, тип gate) хранятся в БД; здесь — только то, чего нет в схеме §13: теги стека, при которых контроль
применим, режимы run, к которым он относится, и связь с находками по CWE. Контроль, которого нет в
каталоге (добавлен admin CLI), применим всегда и ко всем режимам.

Теги стека выводятся из `project_technologies` (`applicability.stack_tags`):
`code`, `dependencies`, `web`, `database`, `container`, `typed`.
"""

from __future__ import annotations

from aicd_standards.model import ControlRule

DEFAULT_RULE = ControlRule()

_DESIGN_MODES = frozenset({"CREATE", "DEVELOP", "MODERNIZE"})
_CHANGE_MODES = frozenset({"CREATE", "DEVELOP", "DEBUG", "MODERNIZE", "SECURITY", "OPTIMIZE", "MAINTAIN", "DOCUMENT"})


def _rule(
    *tags: str,
    modes: frozenset[str] | None = None,
    cwe: tuple[str, ...] = (),
    min_severity: str | None = None,
) -> ControlRule:
    return ControlRule(frozenset(tags), modes, frozenset(cwe), min_severity)


RULES: dict[str, ControlRule] = {
    "REQ-29148-01": _rule(modes=_DESIGN_MODES),
    "REQ-29148-02": _rule(modes=_DESIGN_MODES),
    "ARCH-42010-01": _rule(modes=_DESIGN_MODES),
    "ARCH-42010-02": _rule(modes=frozenset({"CREATE", "MODERNIZE"})),
    "LC-12207-01": _rule(),
    "QM-25010-01": _rule("code"),
    "QM-25010-02": _rule("code"),
    "QM-25010-03": _rule("typed"),
    "QM-25010-04": _rule("code"),
    "QM-25010-05": _rule("code", modes=frozenset({"OPTIMIZE"})),
    "QM-25023-01": _rule("code"),
    "REV-1028-01": _rule(),
    "SEC-27001-01": _rule(),
    "SEC-27002-01": _rule(),
    "SEC-27002-02": _rule("code"),
    "SSDF-RV.1-01": _rule("dependencies"),
    "SSDF-PW.7-01": _rule("code"),
    "SSDF-PW.8-01": _rule("code"),
    "ASVS-V2-01": _rule("web"),
    "ASVS-V5-01": _rule("web"),
    "ASVS-V14-01": _rule("container"),
    "CWE-INJ-01": _rule("code", cwe=("CWE-89", "CWE-78"), min_severity="high"),
    "CWE-XSS-01": _rule("web", cwe=("CWE-79",), min_severity="high"),
    "CWE-PATH-01": _rule("code", cwe=("CWE-22",), min_severity="high"),
    "TEST-29119-01": _rule(modes=frozenset({"CREATE", "DEVELOP", "MODERNIZE", "TEST"})),
    "TEST-29119-02": _rule("code"),
    "SQL-9075-01": _rule("database"),
    "SLSA-BUILD-01": _rule("code"),
    "SLSA-PROV-01": _rule("container"),
    "OSSF-SBOM-01": _rule("dependencies"),
    "MAINT-14764-01": _rule(modes=frozenset({"MAINTAIN", "MODERNIZE", "DEBUG", "OPTIMIZE"})),
    "DOC-15289-01": _rule(),
    "DOC-26514-01": _rule("code", modes=_CHANGE_MODES),
}


def rule_for(control_code: str) -> ControlRule:
    return RULES.get(control_code, DEFAULT_RULE)
