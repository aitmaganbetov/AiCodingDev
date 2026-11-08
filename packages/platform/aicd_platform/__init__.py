"""Платформенный слой AiCodingDev: БД и RLS-контекст, ошибки, логирование, аудит, артефакты.

Модуль не зависит ни от одного доменного модуля (MODULE_BOUNDARIES §2).
"""

# Таблицы, которыми владеет модуль (MODULE_BOUNDARIES §3); проверяется тестом архитектуры.
OWNED_TABLES = frozenset(
    {
        "audit_events",
        "idempotency_keys",
        "artifacts",
        "outbox_events",
        "processed_events",
        "worker_leases",
        "run_event_log",
    }
)
