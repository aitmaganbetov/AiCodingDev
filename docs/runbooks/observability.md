# Наблюдаемость: метрики, трассы, дашборды, алерты (P8-06, P8-08)

**Требования:** ТЗ §16 (метрики, алерты), FR-OBS-02..04, SC-OBS-02, SC-OBS-03. Экран Analytics (FR-OBS-05) — `GET /analytics/summary` по представлениям `v_*` (миграция 0013), см. [API_DESIGN §15b](../api/API_DESIGN.md).

## 1. Запуск профиля observability

```sh
infrastructure/docker/init-secrets.sh          # создаёт недостающие секреты, в т.ч. metrics_token и grafana_admin_password
docker compose -f infrastructure/docker/compose.yaml --profile observability up -d
```

* Grafana — `http://127.0.0.1:3000`, пользователь `admin`, пароль — `infrastructure/docker/secrets/grafana_admin_password`. Анонимный доступ и регистрация выключены. Дашборды (папка AiCodingDev, только чтение): «метрики ТЗ §16» и «эксплуатация».
* Prometheus и Alertmanager портов не публикуют (сеть `monitoring`, internal). Доступ: Grafana → Explore либо `docker compose exec prometheus wget -qO- localhost:9090/api/v1/alerts`.
* Существующей инсталляции после обновления нужно один раз выполнить `init-secrets.sh` (появился секрет `metrics_token`, без него `api` не стартует).

## 2. `/metrics`

| Процесс | Адрес | Доступ |
|---|---|---|
| api | `http://api:8000/metrics` (вне `/api/v1`, reverse-proxy его не проксирует) | `Authorization: Bearer <metrics_token>` |
| `aicd-worker <outbox, orchestrator, exec, index>` | `:${AICD_METRICS_PORT}` (рекомендуется 9464; 0 — выключено) | тот же токен |

Без `AICD_METRICS_TOKEN_FILE` в `production` endpoint отвечает `404` (fail closed); в `development`/`test` — открыт.
Prometheus находит воркеры по DNS-именам сервисов `worker-outbox`, `worker-orchestrator`, `worker-exec`, `worker-index` (порт 9464); сервисы воркеров в compose появятся вместе с их профилем развёртывания — до этого цели просто отсутствуют.

Каталог метрик — `packages/platform/aicd_platform/metrics.py` (все с префиксом `aicd_`). Соответствие ТЗ §16:

| Метрика ТЗ §16 | Ряд Prometheus | Где считается |
|---|---|---|
| run success rate | `aicd_runs_finished_total{final_status}` | worker-outbox, по опубликованному `run.completed` |
| verified completion rate | `aicd_task_transitions_total{to_status=~"SUCCEEDED\|FAILED"}` | worker-outbox (`task.state_changed`) |
| gate failures | `aicd_gate_executions_total{gate_type,status,required}`, `aicd_gate_duration_seconds` | worker-outbox (`gate.completed`) |
| retry count | `aicd_task_transitions_total{to_status="RETRY_WAIT"}` | worker-outbox |
| provider availability | `aicd_provider_available{provider}`, `aicd_provider_probes_total` | worker-outbox (выборка `provider_health`), probe |
| task latency | `aicd_worker_message_duration_seconds{actor}`, `aicd_cli_execution_duration_seconds{provider}`; распределение по задачам — Analytics | воркеры |
| context pack size | `aicd_context_pack_size_bytes` | worker-exec |
| index freshness | `aicd_index_snapshot_age_seconds`, `aicd_context_pack_fragments_total{stale}` | worker-exec (возраст снимка — время UUIDv7 его id) |
| agent message count | `aicd_agent_messages_total{message_type}` | worker-outbox (`agent.message`) |
| queue lag | `aicd_outbox_lag_seconds`, `aicd_outbox_oldest_pending_age_seconds`, `aicd_queue_depth{queue}` | worker-outbox |
| approval wait time | `aicd_approval_wait_seconds{action_type}` | worker-outbox (`approval.decided.wait_seconds`) |
| CLI token usage (если достоверно) | `aicd_cli_tokens_total{provider}` — только `source=cli_reported` | worker-exec |

Бэкапы (P9-05): `aicd_backup_last_success_timestamp_seconds{component}`, `aicd_backup_last_attempt_failed{component}` (файл статуса утилиты бэкапа), `aicd_postgres_wal_archived`, `aicd_postgres_wal_archive_failures` (`pg_stat_archiver`) — worker-outbox.

Дополнительно: HTTP (`aicd_http_requests_total{method,route,status}`, `aicd_http_request_duration_seconds`), безопасность (`aicd_auth_failures_total{reason}`, `aicd_authz_denied_total{reason}` (permission, tenant)), SSE (`aicd_sse_connections{stream}`, stream: run, workspace), вебхуки GitHub (`aicd_webhook_signature_invalid_total{reason}`: missing, malformed, mismatch), stuck leases (`aicd_worker_leases_expired{resource_type}`), хранилище (`aicd_artifact_storage_free_bytes/size_bytes`, `aicd_database_size_bytes`), Dramatiq (`aicd_worker_messages_total{actor,outcome}`, `aicd_queue_dead_letters`).

**Гигиена (SC-OBS-03):** метки — только из ограниченных множеств (статусы FSM, типы событий, провайдер, шаблон маршрута); неизвестное значение → `other`. Идентификаторы run/task/user/workspace, e-mail, пути, тексты ошибок в метки не попадают. Доменные счётчики считаются по **зафиксированным** outbox-событиям: откаченная транзакция не учитывается; at-least-once доставка может дать редкий двойной счёт.

## 3. Трассы OpenTelemetry

Одна трасса проходит API-запрос → outbox (`outbox_events.traceparent`, W3C) → публикация (`outbox.publish`, поле `traceparent` Dramatiq-сообщения) → обработчик воркера (`worker <actor>`, SpanKind CONSUMER) → сообщение exec → исполнение CLI (`cli.execution`). Атрибуты спанов — allowlist `aicd_platform.telemetry.ALLOWED_ATTRIBUTES` (идентификаторы корреляции и перечисления, без промптов/кода/вывода CLI). Экспорт: `AICD_OTEL_EXPORTER=none|console`; отправка в коллектор (OTLP) требует экспортёра-зависимости — открытый вопрос (OPEN_QUESTIONS, P8-06).

## 4. Алерты

Правила — `infrastructure/monitoring/prometheus/rules/aicd-alerts.yml`; маршрутизация — `infrastructure/monitoring/alertmanager/alertmanager.yml` (по умолчанию получатели без интеграций: алерты видны в Alertmanager/Grafana; webhook/e-mail добавляет оператор, секреты — файлами).

| Алерт | Условие (кратко) | Действие оператора |
|---|---|---|
| AicdLoginFailuresSpike | > 10 неудачных входов/мин 5 мин | audit `auth.login_failed`, блокировки, источник IP в access-логах reverse-proxy |
| AicdWebhookSignatureInvalid | > 5 доставок вебхука GitHub с неверной подписью за 10 мин | `github_webhook_deliveries` (REJECTED), ротация webhook secret (действуют текущий и предыдущий) |
| AicdTenantDeniedSpike, AicdCsrfFailuresSpike | всплеск обращений к чужим workspace / отказов CSRF | проверить клиента/сессию, при атаке — ограничить на reverse-proxy |
| AicdApiHighErrorRate | > 5% 5xx 10 мин | логи api по `request_id`, `/health/ready` |
| AicdCliErrorRate, AicdProviderUnavailable | > 50% исполнений CLI падают; нет доступной установки | экран CLI Providers, авторизация CLI оператором (cli-auth), версии |
| AicdGateInfrastructureErrors | > 20% gates в `ERROR` | образы verification, Docker, лимиты |
| AicdStuckLeases | истёкшие неосвобождённые аренды 10 мин | состояние worker-exec/orchestrator, reaper/recovery (логи `recovery`) |
| AicdOutboxLag, AicdQueueBacklog, AicdDeadLetters | outbox не доставляется 5 мин / очередь растёт / DLQ | worker-outbox и Redis; DLQ — разбор `last_error` |
| AicdArtifactStorageLow/Critical | < 10% / < 5% свободно в FS-хранилище | расширить том, retention (`aicd maintenance`) |
| AicdTargetDown | цель не скрейпится 5 мин | процесс/сеть/токен скрейпа |
| AicdBackupFailed, AicdBaseBackupStale, AicdWalSyncStale, AicdArtifactsBackupStale, AicdWalArchiveFailing | сбой/устаревание бэкапов (P9-05): файл статуса `python -m scripts.backup` (`AICD_BACKUP_STATUS_FILE` в worker-outbox) и `pg_stat_archiver` | [backups.md §6](backups.md) |

## 5. Проверки

* `uv run pytest tests/contract/test_observability.py` — выражения дашбордов и алертов ссылаются только на существующие метрики и их метки; образы профиля закреплены по digest; `/metrics` не публикуется.
* `uv run pytest tests/integration/test_monitoring_promtool.py` — `promtool check rules` и `promtool test rules` (`infrastructure/monitoring/prometheus/tests/aicd-alerts.test.yml`: тестовые алерты срабатывают, фон — нет), `amtool check-config`. Нужен Docker и заранее скачанные образы (тест их не скачивает):
  `docker pull prom/prometheus:v3.15.1@sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2` и
  `docker pull prom/alertmanager:v0.34.1@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a`.
