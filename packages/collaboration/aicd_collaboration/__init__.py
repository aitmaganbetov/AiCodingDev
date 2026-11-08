"""Модуль collaboration (MODULE_BOUNDARIES §3): ACP-брокер, доставка, Live Agent Chat и лента активности.

ACP 1.0 (P4-04/P4-05) — подпакет `aicd_collaboration.acp`. Таблицы — миграции 0003 (чат) и 0006 (ACP).
"""

OWNED_TABLES = frozenset(
    {"agent_messages", "message_deliveries", "agent_activity_events", "chat_threads", "chat_messages"}
)
