"""Модуль integrations_github: GitHub App, вебхуки (HMAC), PR workflow (MODULE_BOUNDARIES §3, P8).

Секреты (ключ App, webhook secret, installation tokens) в БД не хранятся (DATABASE_DESIGN §18.9):
installation token живёт только в памяти процесса (`client.InstallationToken`), в git передаётся через
одноразовый GIT_ASKPASS (`aicd_security.git.push_ref`) и никогда не попадает в runner агента.
"""

OWNED_TABLES = frozenset({"github_installations", "github_webhook_deliveries", "pull_requests"})
