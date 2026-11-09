# Runbook: реагирование на инциденты (P9-06)

**Требования:** ТЗ §15–16 (алерты, аудит, dangerous actions), THREAT_MODEL, SECURITY_CONTROLS (SC-OBS-02, SC-AUD-*), NIST SSDF RV.1–RV.3.
**Статус:** IN_REVIEW (ревью оператором: назначить роли и каналы связи).

## 1. Роли и уровни

| Уровень | Примеры | Реакция |
|---|---|---|
| SEV-1 | компрометация (утечка секретов/учётных данных CLI, кросс-тенантный доступ), потеря данных, недоступность всей платформы | немедленно; owner + ответственный за безопасность; связь с затронутыми workspace |
| SEV-2 | деградация (CLI недоступен, gates в ERROR массово, outbox/очереди стоят), сбой бэкапов > 24 ч | в рабочее время ≤ 1 ч |
| SEV-3 | единичные зависшие run, DLQ, алерты info/warning | по очереди задач |

Роли: **координатор** (ведёт журнал и решения), **исполнитель** (команды), **коммуникация**. Dangerous actions в ходе
инцидента (удаление данных, смена секретов, восстановление из бэкапа, unrestricted network) — только с явным решением
owner (ТЗ §15), фиксируются в журнале инцидента.

## 2. Первые 15 минут

1. Завести журнал инцидента (время UTC, кто, что наблюдаем, что сделали). Не копировать в него секреты, токены, содержимое
   промптов/кода — только идентификаторы (`run_id`, `request_id`, `event_id`).
2. Оценить масштаб: Grafana «эксплуатация», Alertmanager (активные алерты), `curl -fsS http://localhost:8080/api/v1/health/ready`.
3. Сохранить свидетельства до изменений:
   ```sh
   C="docker compose -f infrastructure/docker/compose.yaml"
   mkdir -p incident-$(date -u +%Y%m%dT%H%M) && cd "$_"
   $C ps > ps.txt; $C logs --since 2h --no-color > compose-logs.txt
   $C exec -T postgres psql -U aicd_admin -d aicd -c "\copy (SELECT id, action, actor_type, actor_id, workspace_id, resource_type, resource_id, created_at FROM aicd.audit_events WHERE created_at > now() - interval '24 hours' ORDER BY created_at) TO STDOUT WITH CSV HEADER" > audit-24h.csv
   ```
   (журнал аудита append-only; логи уже редактируют секреты, но обращайтесь с ними как с конфиденциальными).
4. Сдерживание по типу (§3), затем устранение и восстановление, затем разбор (§4).

## 3. Сценарии

| Сигнал | Сдерживание | Дальше |
|---|---|---|
| `AicdLoginFailuresSpike`, `AicdCsrfFailuresSpike` | блокировка источника на reverse-proxy/TLS-терминаторе; аккаунты блокируются автоматически (5/10 неудач) | audit `auth.login_failed`; принудительная смена паролей затронутых пользователей (отзывает сессии) |
| `AicdTenantDeniedSpike` (перебор чужих ресурсов) | заблокировать сессию/пользователя (owner/admin завершает сессии участника; удаление из workspace) | проверить, что ответы — 404 без утечки (AC-12); cross-tenant тесты `tests/security/test_cross_tenant_api.py` |
| `AicdWebhookSignatureInvalid` | если не ротация — подделка доставок: ничего не принимается без подписи; ограничить источник | [github-app.md §3](github-app.md) — ротация webhook secret |
| Утечка секрета приложения/БД | ротация ([secrets-rotation.md](secrets-rotation.md)) — сначала отзыв | поиск использования в audit/логах; если затронута БД — уведомление владельцев workspace |
| Подозрение на утечку учётных данных CLI (TH-13: detector fingerprint, необычные артефакты) | остановить `aicd-worker exec`; `logout` CLI и завершение сессий у провайдера; том `aicd-cred-*` пересоздать | вход оператора ([cli-auth.md](cli-auth.md)); проверить артефакты/diff/PR run на наличие учётных данных (gitleaks gate), удалить из GitHub при необходимости |
| Prompt injection / агент пытается выйти за рамки (SANDBOX_DENIED, `aicd.gate-config-changed`, egress denied) | пауза run (`POST /api/v1/runs/{id}/pause`) или отмена; approvals по run не утверждать | разбор ACP-сообщений и diff; правило/профиль gate; THREAT_MODEL TH-01 |
| Потеря тома/хоста БД, повреждение данных | остановить запись (стоп api/воркеров) | [backups.md §5](backups.md) |
| `AicdBackupFailed`/`AicdWalSyncStale`/`AicdWalArchiveFailing` | — | [backups.md §6](backups.md); RPO растёт — SEV-2 |
| Зависшие run, outbox, DLQ, аренды | — | [recovery.md](recovery.md) |
| `AicdProviderUnavailable`, массовые `AUTH_REQUIRED`/`RATE_LIMITED` | обходов нет (ТЗ §1): ждать cooldown / вход оператора | [cli-auth.md](cli-auth.md); коммуникация пользователям о задержке |
| Найдена уязвимость зависимости/образа | — | SCA gates (офлайн-БД — [vulndb-update.md](vulndb-update.md)); новый релиз с исправлением и SBOM ([release.md](release.md)) |

## 4. После инцидента

* Разбор без поиска виноватых в течение 5 рабочих дней: хронология, причина, что сработало/нет, действия (владелец, срок).
* Обновить THREAT_MODEL / SECURITY_CONTROLS / runbook, если сценария не было; добавить тест (security/chaos), воспроизводящий
  проблему.
* Проверить, что бэкап после восстановления сделан (`python -m scripts.backup pg-base …`) и алерты погасли.
