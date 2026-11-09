# Runbook: развёртывание профиля local

Проверено в Phase 1 (2026-11-08): `docker compose` поднимает postgres, redis, migrate, api, web; `/api/v1/health/ready` → `ok`; вход, создание workspace/проекта и E2E-смоук веб-оболочки через nginx проходят.

## Предусловия
- Linux-хост с Docker Engine ≥ 24 и плагином compose.
- Каталог с локальными Git-репозиториями, которые разрешено привязывать (allowlist).

## Шаги
1. Сборка образов:
   ```sh
   docker build -f infrastructure/docker/backend.Dockerfile -t aicodingdev/backend:local .
   docker build -f infrastructure/docker/web.Dockerfile -t aicodingdev/web:local .
   ```
   За корпоративным TLS-прокси: `--secret id=ca_bundle,src=<ca.pem>` (CA используется только на этапе сборки и в образ не попадает) и, при необходимости, `--build-arg https_proxy=…`.
2. Секреты (случайные, создаются один раз, каталог `0700`, в git не попадает):
   ```sh
   infrastructure/docker/init-secrets.sh
   ```
3. Каталог репозиториев: `export AICD_REPOS_DIR=/srv/git` (по умолчанию `infrastructure/docker/repos`), монтируется в api, worker-orchestrator и worker-index только для чтения, в worker-exec — на запись (worktrees), как корень `main`.
4. Запуск:
   ```sh
   docker compose -f infrastructure/docker/compose.yaml up -d --no-build --wait
   ```
   UI: `http://localhost:8080`. Без TLS cookie `Secure` принимаются браузерами только для `localhost`; для доступа по сети разместите перед `web` TLS-терминатор и задайте `AICD_PUBLIC_ORIGIN=https://<host>`.
5. Первый владелец (пароль ≥ 12 символов, без имени учётной записи; дефолтных паролей нет):
   ```sh
   docker compose -f infrastructure/docker/compose.yaml exec api \
     aicd admin create-owner --email you@corp.local --name "Имя" --workspace-slug main --workspace-name "Main"
   ```
6. Проверка: `curl -fsS http://localhost:8080/api/v1/health/ready` → `{"status":"ok",…}`.

## Обслуживание
- Партиции журнала аудита на 3 месяца вперёд (ежемесячно, роль мигратора):
  `AICD_MIGRATOR_DATABASE_URL=… aicd db ensure-partitions --months 3` (автоматизация — P3-18).
- Миграции выполняются сервисом `migrate` при каждом `up`; dry-run: `alembic -c infrastructure/migrations/alembic.ini upgrade head --sql`.

## Известные ограничения Phase 1
- Хранилище артефактов — локальный том (`AICD_ARTIFACT_BACKEND=fs`); S3/MinIO настраивается переменными `AICD_ARTIFACT_S3_*` (OQ-11).
- Воркеры — сервисы `worker-outbox`, `worker-orchestrator`, `worker-exec`, `worker-index` в `compose.yaml` (gates worker-exec — с оверлеем `compose.sandbox.yaml`); в production worker-exec не стартует до реальных адаптеров CLI (Fake CLI запрещён) — подробности и переменные каталогов — [installation.md §3](installation.md).
