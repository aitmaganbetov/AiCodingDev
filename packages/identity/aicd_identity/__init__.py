"""Модуль identity: пользователи, пароли, сессии, членство, RBAC (MODULE_BOUNDARIES §3).

Публичный интерфейс — `aicd_identity.api` и `aicd_identity.rbac`; `repository` приватен.
"""

OWNED_TABLES = frozenset(
    {"users", "user_credentials", "user_sessions", "workspace_members", "project_member_overrides"}
)
