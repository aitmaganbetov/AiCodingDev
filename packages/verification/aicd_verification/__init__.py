"""Модуль verification (MODULE_BOUNDARIES §3): профили качества, gates, их исполнения и evidence.

В Phase 3 — только схема (миграция 0003); Verification Engine появится в Phase 6.
"""

OWNED_TABLES = frozenset({"quality_profiles", "quality_gates", "gate_executions", "evidence"})
