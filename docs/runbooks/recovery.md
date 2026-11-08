# Runbook: восстановление workflow после сбоев (P9-06)

**Требования:** ТЗ §16, §19 AC-07/AC-09, NFR-REL-01/02, DATA_FLOW §5–§7, ADR-0002/0007/0012. **Статус:** IN_REVIEW (ревью оператором).
Связанные: [backups.md](backups.md) (потеря данных/тома), [incident-response.md](incident-response.md), [observability.md](observability.md) (алерты).

```sh
C="docker compose -f infrastructure/docker/compose.yaml"
PSQL="$C exec -T postgres psql -U aicd_admin -d aicd -P pager=off"   # суперпользователь: только чтение/диагностика
```

**Главное правило.** Состояние — в PostgreSQL, переходы FSM охраняются триггерами (state-machines.md); ручные `UPDATE` таблиц
`runs/tasks/task_attempts/cli_executions/worker_leases/approvals` запрещены — только API/UI и штатные механизмы воркеров
(recovery reconciler, reaper аренд, tick оркестратора). Redis — только транспорт: его потеря не теряет работу.
Повторная доставка безопасна: потребители идемпотентны (`processed_events`), результат пишется только под актуальным
fencing token, внешние действия (push/PR/merge) проходят ledger `external_actions` и не повторяются вслепую (AC-09).

## 1. Встроенные механизмы (что происходит без оператора)

| Механизм | Где | Период | Что делает |
|---|---|---|---|
| Reaper аренд | `aicd-worker exec` | 15 с | аренды с истёкшим `lease_until`: убить осиротевший процесс CLI/контейнер этого хоста, исполнение → `CRASHED`, попытка → `FAILED(LEASE_LOST)`, задача → `RETRY_WAIT` (не расходует `max_attempts`) |
| Recovery reconciler | `aicd-worker exec` | при старте и каждые 60 с | сверка процессов/контейнеров/worktree хоста с БД (DATA_FLOW §5), worktree упавшей попытки → `ORPHANED` → удаление (diff сохраняется в artifacts) |
| Tick оркестратора | `aicd-worker orchestrator` | `AICD_TICK_INTERVAL_SECONDS` (5 с) | READY-задачи, не взятые дольше `AICD_STALE_READY_SECONDS` (30 с), ставятся в очередь повторно (потерянные сообщения Redis) |
| Повторы outbox | `aicd-worker outbox` | 0,5 с | неопубликованные события (`published_at IS NULL`) — с backoff (`available_at`, `publish_attempts`, `last_error`) |
| Повторы Dramatiq | все потребители | экспоненциально | после исчерпания — dead-letter (`<очередь>.XQ`), §5 |

## 2. Run «завис» (нет прогресса)

Признаки: run `RUNNING/VERIFYING` без событий в Mission Control; алерты `AicdStuckLeases`, `AicdQueueBacklog`, `AicdOutboxLag`.

```sh
$PSQL -c "SELECT id, status, blocked_reason, updated_at FROM aicd.runs WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED') ORDER BY updated_at LIMIT 20"
$PSQL -c "SELECT id, status, blocked_reason, attempt_count, max_attempts, next_attempt_at, updated_at FROM aicd.tasks WHERE run_id = '<run_id>' ORDER BY updated_at"
```

| Статус задачи | Значит | Действие |
|---|---|---|
| `BLOCKED` + `AUTH_REQUIRED` | CLI не авторизован (AC-16) | вход оператора в CLI ([cli-auth.md](cli-auth.md)), затем `POST /api/v1/providers/{id}/probe`. Автоматический возврат таких задач в `READY` по событию `provider.auth_restored` (CLI_ADAPTER_CONTRACT §14) в коде пока **не реализован** (вместе с реальными адаптерами, P2-01): отменить run и запустить заново |
| `BLOCKED` + `PROVIDER_UNAVAILABLE` / `RATE_LIMITED` | нет допустимой установки / cooldown лимитов | экран CLI Providers; `POST /api/v1/providers/{id}/probe`; ждать cooldown (обход лимитов запрещён) |
| `BLOCKED` + `MANUAL_REVIEW` / ожидание approval | нужен человек | экран Approvals; решение принимают люди с правами (four-eyes) |
| `READY` долго | нет живого `aicd-worker exec` или потеряно сообщение | проверить процесс exec и Redis; tick переотправит через 30 с |
| `LEASED/RUNNING` долго | исполнение идёт или воркер умер | §3 (аренды) |
| `RETRY_WAIT` | ждёт `next_attempt_at` | ничего; после `max_attempts` → `FAILED` с категорией ошибки (AC-07) |

Управление run — только через UI/API: пауза `POST /api/v1/runs/{id}/pause`, продолжение `.../resume`, отмена `.../cancel`
(права `run:control`, Idempotency-Key, If-Match). Отмена RUNNING-задачи доставляется исполнителю (наблюдатель exec, опрос 5 с).

## 3. Зависшие аренды (AicdStuckLeases)

```sh
$PSQL -c "SELECT resource_type, resource_id, worker_id, fencing_token, lease_until, now()-heartbeat_at AS silent FROM aicd.worker_leases WHERE released_at IS NULL AND lease_until < now() ORDER BY lease_until"
$PSQL -c "SELECT id, hostname, kind, status, now()-last_heartbeat_at AS silent FROM aicd.worker_hosts ORDER BY last_heartbeat_at DESC"
```

1. Убедиться, что `aicd-worker exec` хоста, указанного в `worker_id`, жив (процесс/контейнер, его лог `recovery`).
   Reaper подбирает только аренды своего хоста — если хост исполнения умер навсегда, запустите `aicd-worker exec` на
   любом хосте с доступом к тому же каталогу worktrees: reconciler при старте разбирает осиротевшие записи.
2. Не освобождать аренду вручную: процесс CLI может быть жив (две записи результата). Fencing token делает запись
   «старого» владельца невозможной, но лишний процесс расходует подписку.
3. Если аренда не снимается > 2 × TTL при живом exec — собрать лог exec и `cli_executions` задачи, эскалация (инцидент SEV-3).

## 4. Отставание outbox (AicdOutboxLag)

```sh
$PSQL -c "SELECT count(*), min(created_at), max(publish_attempts) FROM aicd.outbox_events WHERE published_at IS NULL"
$PSQL -c "SELECT event_type, publish_attempts, left(last_error, 200) FROM aicd.outbox_events WHERE published_at IS NULL ORDER BY created_at LIMIT 10"
$C exec redis sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli ping'
```

* Нет процесса `aicd-worker outbox` → запустить; события доставятся в порядке записи (at-least-once).
* `last_error` — Redis недоступен/полон → восстановить Redis (данные очередей не нужны: §6); проекция в `run_event_log`
  ошибкой схемы — баг, эскалация с `event_type` (payload событий не копировать в тикет: может содержать идентификаторы).

## 5. Dead-letter очереди (AicdDeadLetters) — просмотр и повтор

Инструмент `python -m aicd_worker.dlq` (в образе backend; подключение — `AICD_REDIS_URL(_FILE)`):

```sh
$C run --rm --no-deps api python -m aicd_worker.dlq list                       # все очереди: актор, повторы, последняя ошибка, run/task
$C run --rm --no-deps api python -m aicd_worker.dlq list --queue exec
$C run --rm --no-deps api python -m aicd_worker.dlq replay --queue exec --message-id <id>          # план (dry run)
$C run --rm --no-deps api python -m aicd_worker.dlq replay --queue exec --message-id <id> --yes    # повтор
$C run --rm --no-deps api python -m aicd_worker.dlq replay --queue orchestrator --all --yes
$C run --rm --no-deps api python -m aicd_worker.dlq drop --queue docs --message-id <id> --yes      # выбросить (после разбора)
```

1. Сначала причина (`last_error`, лог воркера по `run_id/task_id`), затем исправление (конфигурация, доступность сервиса,
   исправленный код), затем `replay`. Повтор безопасен (идемпотентность, fencing, ledger), но бессмыслен до устранения причины.
2. Сообщение без ценности (например, событие отменённого run) — `drop`; состояние в БД это не меняет.
3. Записи DLQ хранятся в Redis ограниченное время (`dead_message_ttl` Dramatiq, 7 дней): разбирайте их в пределах суток;
   если записи пропали, работу всё равно восстановит tick (READY-задачи) — потеряны только диагностические следы.

## 6. Потеря/перезапуск Redis

Перезапустить/пересоздать: `$C up -d redis`. Очереди пусты — это нормально: outbox доставит неопубликованные события,
tick переотправит READY-задачи, SSE-клиенты переподключаются с `Last-Event-ID` из `run_event_log` (ADR-0011).
Проверено: `tests/chaos/test_redis_restart.py` и DR drill (пустой Redis после восстановления, run завершились).

## 7. Падение воркера во время исполнения CLI

Ничего не делать руками: после истечения аренды reaper/reconciler переводит исполнение в `CRASHED`, задачу — в `RETRY_WAIT`,
затем новая попытка (проверено `tests/chaos/test_recovery.py`). Прерванное внешнее действие (`external_actions.status =
EXECUTING`) не повторяется вслепую: recovery сверяет состояние GitHub и только потом подтверждает/компенсирует.
Проверить: `$PSQL -c "SELECT id, action_type, status, left(last_error,200) FROM aicd.external_actions WHERE status = 'EXECUTING'"`.

## 8. Восстановление после аварии хранилища/БД

См. [backups.md §5](backups.md). После восстановления — шаги этого runbook §2–§5 для run «в полёте».
