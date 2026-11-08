"""Модуль code_intelligence (SYSTEM_ARCHITECTURE, ADR-0005): индекс кода по commit, поиск, граф зависимостей,
Context Pack, определение стека и документация (политика OFF/SMART/STRICT, безопасный импорт).

Слой доменов (наравне с projects/verification): не импортирует orchestration и приложения.
Таблицы — миграция 0007 (P5-01, DATABASE_DESIGN §10–§11); technologies* принадлежат projects (§2).
"""

OWNED_TABLES: frozenset[str] = frozenset(
    {
        "code_index_snapshots",
        "code_files",
        "code_symbols",
        "code_dependencies",
        "knowledge_chunks",
        "retrieval_queries",
        "retrieval_results",
        "context_packs",
        "indexing_jobs",
        "knowledge_entries",
        "architecture_decisions",
        "documentation_sources",
        "documentation_snapshots",
        "documentation_sections",
    }
)
