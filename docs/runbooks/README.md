# Runbooks

Операторские процедуры (ТЗ §21, NFR-MAINT-01). Команды проверены на момент написания (см. «Проверено» в каждом runbook);
статус P9-06 — IN_REVIEW: требуется ревью оператором.

| Runbook | Содержание | Статус |
|---|---|---|
| [installation.md](installation.md) | установка (профиль local/single host), первый владелец, обновление проверенного релиза | P9-09 (черновик) |
| [local-deployment.md](local-deployment.md) | развёртывание профиля local, первый владелец, проверка | Phase 1 |
| [observability.md](observability.md) | метрики Prometheus, трассы, Grafana, алерты (профиль observability) | P8-06/P8-08, P9-05 |
| [recovery.md](recovery.md) | зависшие run и аренды, отставание outbox, DLQ (`python -m aicd_worker.dlq`), потеря Redis, падение воркера | P9-06, IN_REVIEW |
| [backups.md](backups.md) | бэкап PostgreSQL (base + WAL, PITR) / артефактов / секретов, ключ, расписание, восстановление, алерты | P9-05/P9-06, IN_REVIEW |
| [cli-auth.md](cli-auth.md) | вход оператора в Codex CLI / Claude Code CLI (только подписка, без API-ключей) | P9-06, IN_REVIEW |
| [incident-response.md](incident-response.md) | уровни, первые 15 минут, сценарии, разбор | P9-06, IN_REVIEW |
| [github-app.md](github-app.md) | создание и подключение GitHub App, ротация ключа и webhook secret | P9-06, IN_REVIEW |
| [secrets-rotation.md](secrets-rotation.md) | ротация паролей БД, Redis, ключей приложения, токенов | P9-06, IN_REVIEW |
| [upgrades.md](upgrades.md) | обновление версии, dry-run миграций, откат | P9-06, IN_REVIEW |
| [release.md](release.md) | сборка релиза, SBOM, подпись/provenance (cosign, ключ), проверка; образы verification платформы | P9-08 |
| [vulndb-update.md](vulndb-update.md) | одобренное обновление офлайн-БД уязвимостей (OSV, Trivy) | OQ-43 (f), IN_REVIEW |
