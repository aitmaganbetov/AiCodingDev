"""Модуль verification (MODULE_BOUNDARIES §3): профили качества, gates, их исполнения и evidence.

Схема — миграции 0003 (профили, gates, исполнения, evidence) и 0008 (test_results, findings, evidence_links).
"""

OWNED_TABLES = frozenset(
    {
        "quality_profiles",
        "quality_gates",
        "gate_executions",
        "evidence",
        "test_results",
        "findings",
        "evidence_links",
    }
)
