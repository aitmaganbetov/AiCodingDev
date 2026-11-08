"""Модуль collaboration (MODULE_BOUNDARIES §3): Live Agent Chat, позже — ACP-брокер и лента активности.

В Phase 3 — только схема потоков и сообщений чата (миграция 0003); логика появится в Phase 4.
"""

OWNED_TABLES = frozenset({"chat_threads", "chat_messages"})
