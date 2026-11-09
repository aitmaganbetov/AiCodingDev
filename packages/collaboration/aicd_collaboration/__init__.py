"""Модуль collaboration (MODULE_BOUNDARIES §3): ACP-брокер, доставка, Live Agent Chat и лента активности.

ACP 1.0 (P4-04/P4-05) — подпакет `aicd_collaboration.acp`. Таблицы — миграции 0003 (чат) и 0006 (ACP);
inline-комментарии ревью кода (ADR-0015) — миграция 0019, `aicd_collaboration.review_comments`.
"""

OWNED_TABLES = frozenset(
    {"agent_messages", "message_deliveries", "agent_activity_events", "chat_threads", "chat_messages"}
    | {  # inline-комментарии ревью кода (UI redesign B5, ADR-0015, миграция 0019)
        "review_threads",
        "review_comments",
        "review_comment_revisions",
        "review_submissions",
        "review_submission_threads",
    }
    | {"notifications"}  # персональные уведомления (UI redesign B8, миграция 0020, `aicd_collaboration.notifications`)
)
