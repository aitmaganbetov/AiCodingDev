"""Analytics (FR-OBS-05, FR-CI-08, P8-07): агрегаты метрик ТЗ §16 для экрана Analytics.

Модуль не владеет таблицами (MODULE_BOUNDARIES §3: `analytics` читает отчётные представления модулей-источников).
Все чтения выполняются под ролью `aicd_app` в RLS-контексте workspace (`app_current_workspace()`): межтенантная
агрегация невозможна (ROUTING_POLICY §7, ADR-0008).
"""

OWNED_TABLES: frozenset[str] = frozenset()
