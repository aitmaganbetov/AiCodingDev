"""Модуль code_intelligence (SYSTEM_ARCHITECTURE, ADR-0005): индекс кода по commit, поиск, граф зависимостей,
Context Pack, определение стека и документация (политика OFF/SMART/STRICT, безопасный импорт).

Слой доменов (наравне с projects/verification): не импортирует orchestration и приложения.
Таблицы перечисляются миграцией 0007 (P5-01).
"""

OWNED_TABLES: frozenset[str] = frozenset()
