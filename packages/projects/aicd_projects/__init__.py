"""Модуль projects: workspaces, проекты, настройки, репозитории, стек (MODULE_BOUNDARIES §3)."""

OWNED_TABLES = frozenset(
    {
        "workspaces",
        "projects",
        "project_settings",
        "repositories",
        "technologies",
        "technology_versions",
        "project_technologies",
    }
)
