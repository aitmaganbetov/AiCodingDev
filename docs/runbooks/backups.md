# Runbook: резервное копирование и восстановление (P9-05, P9-06)

**Требования:** ТЗ §15–16, NFR-REL-03, SC-DATA-03, RG-13, DATABASE_DESIGN §18.6. **Статус:** IN_REVIEW (ревью оператором).
Проверено DR drill на одноразовых контейнерах: [reports/dr/DR_DRILL.md](../../reports/dr/DR_DRILL.md) (там же измеренные RPO/RTO);
процедура `compose.yaml` + `compose.restore.yaml` (§5) проверена отдельным прогоном на копии профиля local.

Инструмент — `uv run python -m scripts.backup <команда>` на хосте оператора (нужны Docker и `uv sync`; утилиты
PostgreSQL берутся из того же образа по digest, что у сервиса `postgres`). Вывод — JSON, код 0/1.

```sh
C="docker compose -f infrastructure/docker/compose.yaml"   # профиль local; имя проекта compose — aicodingdev
B="uv run python -m scripts.backup"
export AICD_BACKUP_REPO=/srv/aicd-backup/repo            # репозиторий бэкапов: отдельный диск/NFS, затем офсайт-копия
export AICD_BACKUP_KEY_FILE=/run/aicd-backup/backup.key  # ключ: НЕ рядом с репозиторием (см. §2)
export AICD_BACKUP_STATUS_FILE=/srv/aicd-backup/status/status.json  # для метрик (§6)
```

## 1. Что и как копируется

| Компонент | Способ | Частота (рекомендация) | Команда |
|---|---|---|---|
| PostgreSQL, физически | `pg_basebackup` (tar+gzip, WAL `fetch`, manifest SHA256) через `docker exec` в контейнер сервера | ежедневно | `$B pg-base --pg-container aicodingdev-postgres-1` |
| PostgreSQL, WAL (PITR) | сервер архивирует WAL сам (`archive_command`, `archive_timeout=${AICD_PG_ARCHIVE_TIMEOUT:-60}`) в том `aicodingdev_pgwal`; перенос в репозиторий | каждую минуту | `$B pg-wal-sync --wal-volume aicodingdev_pgwal --delete-synced` |
| PostgreSQL, логически | `pg_dump -Fc` (переносимость, выборочное восстановление) | ежедневно | `$B pg-dump --pg-container aicodingdev-postgres-1 --pg-user aicd_maintenance` (см. примечание) |
| Артефакты | снимок хранилища (FS-том `aicodingdev_artifacts` или S3), сверка каждого объекта с `artifacts.sha256`; объекты content-addressed, инкрементально | каждые 15 мин; `--full` еженедельно | `$B artifacts --fs-root "$(docker volume inspect -f '{{.Mountpoint}}' aicodingdev_artifacts)" --pg-container aicodingdev-postgres-1` |
| Секреты/конфигурация | зашифрованный tar каталога `infrastructure/docker/secrets` (+ `.env`/override-файлы оператора — отдельной копией каталога) | при каждом изменении и ежедневно | `$B secrets --dir infrastructure/docker/secrets` |
| Redis | **не копируется**: только транспорт Dramatiq и pub/sub SSE (ADR-0002). Состояние — в PostgreSQL: outbox, `tick` оркестратора повторно ставит READY-задачи, recovery reconciler разбирает аренды. Проверено дриллом (пустой Redis после восстановления, run «в полёте» завершились) | — | — |
| Учётные данные CLI (`aicd-cred-*`, OS credential store) | **никогда** (SC-CLI-03): после восстановления — повторный вход оператора, [cli-auth.md](cli-auth.md) | — | — |
| Офлайн-БД уязвимостей | не копируется: воспроизводится [vulndb-update.md](vulndb-update.md) | — | — |
| Локальные Git-репозитории проектов (`AICD_REPOS_DIR`) | данные оператора вне платформы (обычно у них есть remote) | по политике оператора | — |

Примечание к `pg-dump`: дамп выполняется ролью `aicd_maintenance` (DATABASE_DESIGN §19.6, BYPASSRLS, только чтение) —
миграция 0016 выдала ей `SELECT` на партиции `outbox_events_*`/`agent_activity_events_*` и на `worker_lease_fencing_seq`, а
`maint_ensure_monthly_partitions` выдаёт право на каждую новую партицию (OQ-51 (a); проверено DR drill и
`tests/migrations/test_backup_vulndb_migration.py`). В режиме `--pg-container` вход по unix-сокету контейнера (`--pg-user`),
в сетевом режиме — пароль роли из файла секрета `pg_maintenance_password`.

Все файлы в репозитории шифруются AES-256-GCM (потоково, защита от подмены/усечения/перестановки записей и подмены
файла другим), манифесты подписаны HMAC-SHA256; сегменты WAL раскладываются по `system_identifier` кластера
(файлы другого кластера, оставшиеся в томе архива, не попадают в цепочку восстановления).

## 2. Ключ бэкапа

```sh
$B keygen --out /run/aicd-backup/backup.key      # 32 случайных байта, base64, 0600; существующий файл не перезаписывается
$B init                                          # REPO.json: формат и key_id (сам ключ в репозиторий не пишется)
```

* Ключ хранится **отдельно** от репозитория и от хоста бэкапов: две офлайн-копии (менеджер паролей/HSM/сейф),
  доступ — owner и ответственный за DR. Без ключа бэкапы не восстанавливаются; утечка ключа + копии репозитория =
  утечка данных (код, аудит, ПДн — TH-47).
* `key_id` (в `REPO.json`, в каждом манифесте и в выводе `keygen`) позволяет сверить, тот ли ключ у вас, не раскрывая его.
* Ротация: новый ключ → новый репозиторий (`init` в новый каталог) → новые наборы; старый ключ хранится, пока в старом
  репозитории есть бэкапы в пределах срока хранения.
* Права хоста: ключ `0600` (инструмент отказывается читать ключ, доступный группе/другим).

## 3. Расписание (systemd timers / cron) и хранение

```cron
* * * * *    cd /opt/aicodingdev && uv run python -m scripts.backup pg-wal-sync --wal-volume aicodingdev_pgwal --delete-synced
*/15 * * * * cd /opt/aicodingdev && uv run python -m scripts.backup artifacts --fs-root /var/lib/docker/volumes/aicodingdev_artifacts/_data --pg-container aicodingdev-postgres-1
17 2 * * *   cd /opt/aicodingdev && uv run python -m scripts.backup pg-base --pg-container aicodingdev-postgres-1 && uv run python -m scripts.backup verify --kind base --backup-id latest
37 2 * * *   cd /opt/aicodingdev && uv run python -m scripts.backup pg-dump --pg-container aicodingdev-postgres-1 --pg-user aicd_maintenance
47 3 * * 0   cd /opt/aicodingdev && uv run python -m scripts.backup artifacts --full --fs-root /var/lib/docker/volumes/aicodingdev_artifacts/_data --pg-container aicodingdev-postgres-1
57 3 * * *   cd /opt/aicodingdev && uv run python -m scripts.backup prune --keep-base 7 --keep-dumps 7 --keep-artifact-snapshots 14 && uv run python -m scripts.backup verify --wal
```

(Переменные `AICD_BACKUP_*` — в окружении задания. Только одна команда работает с репозиторием одновременно — блокировка `.lock`.)
Офсайт: репозиторий целиком зашифрован — синхронизируйте его в другое хранилище (rsync/rclone/объектное хранилище с
версионированием), не синхронизируя ключ. Для S3-хранилища артефактов вместо `--fs-root`: `--s3-endpoint URL --s3-bucket
aicd-artifacts --s3-access-key-file F --s3-secret-key-file F`.

RPO = `archive_timeout` + интервал `pg-wal-sync` (по умолчанию ≤ 2 мин) для БД; для артефактов — интервал `artifacts`.
Строки артефактов, созданные после последнего снимка, после PITR остаются без объекта: при восстановлении они
перечисляются отдельно (`missing_after_snapshot`) — это потеря в пределах RPO, не повреждение (§5, шаг 6).

## 4. Проверка бэкапов (ежемесячно — тест восстановления, ТЗ §16)

```sh
$B list                         # наборы, архив WAL по кластерам, status.json
$B verify --wal                 # полная расшифровка base/dump/secrets + сверка sha256 с подписанными манифестами; цепочка WAL без разрывов
uv run python -m scripts.dr.drill --report reports/dr/DR_DRILL.md   # DR drill: бэкап→авария→восстановление→проверки (одноразовые контейнеры)
```

## 5. Восстановление (авария хоста/тома БД)

Dangerous action (ТЗ §15): решение о восстановлении и удалении/замене томов принимает owner; ход действий — в журнал инцидента
([incident-response.md](incident-response.md)). Время — по дриллу; в изоляции от рабочего стека.

1. **Остановить стек**, сохранить то, что осталось (тома не удалять, пока восстановление не проверено):
   `$C stop` (или `$C down` без `-v`).
2. **Секреты** (если каталог утерян): `$B restore-secrets --to infrastructure/docker/secrets` (каталог создаётся `0700`,
   файлы `0600`; существующие не перезаписываются). Затем `chmod 0444 infrastructure/docker/secrets/*` (как `init-secrets.sh`:
   compose монтирует file-based secrets с правами хоста).
3. **PostgreSQL** — базовый бэкап + архив WAL в новые пустые тома (до конца архива; PITR — `--target-time 2026-11-08T10:15:00Z`):
   ```sh
   $B pg-wal-sync --wal-volume aicodingdev_pgwal      # если том архива уцелел — забрать последние сегменты
   $B restore-pg --data-volume aicodingdev_pgdata_restored --wal-volume aicodingdev_wal_restore
   AICD_RESTORE_PGDATA_VOLUME=aicodingdev_pgdata_restored AICD_RESTORE_WAL_VOLUME=aicodingdev_wal_restore \
     $C -f infrastructure/docker/compose.restore.yaml up -d --wait postgres
   $C exec postgres psql -U aicd_admin -d aicd -Atc 'SELECT pg_is_in_recovery()'   # ждать f (promote)
   $B post-restore --pg-container aicodingdev-postgres-1
   ```
   `restore-pg` проверяет бэкап `pg_verifybackup` и не пишет в непустой том. `post-restore` снимает recovery-настройки и
   сдвигает `worker_lease_fencing_seq` на `max(fencing token) + 1 000 000` (DATABASE_DESIGN §18.6) — аренды, выданные после
   точки восстановления ещё живым воркерам, не повторятся. Сервер переходит на новый таймлайн и продолжает архивировать WAL.
   Дальше стек поднимается той же парой файлов (`-f compose.yaml -f compose.restore.yaml`), пока owner не решит
   переименовать том в штатный `aicodingdev_pgdata` (новый том + копирование `docker run --rm -v A:/a -v B:/b … cp -a`).
4. **Redis** — пустой (контейнер пересоздаётся `$C up`), ничего восстанавливать не нужно.
5. **Артефакты:**
   ```sh
   $B restore-artifacts --fs-root "$(docker volume inspect -f '{{.Mountpoint}}' aicodingdev_artifacts)"
   $B verify-artifacts --snapshot latest --fs-root "$(docker volume inspect -f '{{.Mountpoint}}' aicodingdev_artifacts)" \
     --pg-container aicodingdev-postgres-1
   ```
   `missing`/`mismatched` должны быть пусты (иначе — повреждение: стоп, эскалация). `missing_after_snapshot` — артефакты,
   созданные после последнего снимка: их run/задачи перечислить в журнале инцидента и перезапустить (скачивание таких
   артефактов вернёт ошибку, sha256 не подменяется).
6. **Приложение:** `$C -f infrastructure/docker/compose.restore.yaml up -d --no-build --wait` (вместе с сервисами
   воркеров `worker-*`; для gates worker-exec добавить `-f infrastructure/docker/compose.sandbox.yaml`), затем
   `curl -fsS http://localhost:8080/api/v1/health/ready`.
7. **CLI:** повторный вход оператора в Codex/Claude CLI ([cli-auth.md](cli-auth.md)); до этого задачи с CLI — `BLOCKED (AUTH_REQUIRED)`.
8. **Проверки:** run «в полёте» продолжаются (tick + reconciler); `aicd_worker_leases_expired` → 0; outbox без отставания
   (`AicdOutboxLag` не горит); вход пользователей; аудит восстановления — запись в журнале инцидента с `backup_id`, `key_id`,
   точкой восстановления и результатами `verify-artifacts`.

Только БД из логического дампа (перенос/выборочно): роли — `infrastructure/postgres/bootstrap-roles.sql`, БД —
`bootstrap-database.sql`, затем расшифрованный дамп в `pg_restore -d <db>` (пример — `scripts/dr/drill.py::dump_restore`).

## 6. Мониторинг бэкапов (ТЗ §16 «failed backups»)

* Каждая команда обновляет `status.json` репозитория и `AICD_BACKUP_STATUS_FILE` (компоненты `postgres_base`, `postgres_wal`,
  `postgres_dump`, `artifacts`, `secrets`; время последней попытки/успеха, текст ошибки без секретов).
* worker-outbox читает файл, если смонтирован только для чтения и задан `AICD_BACKUP_STATUS_FILE`, и экспортирует
  (в `compose.yaml` — каталог `AICD_BACKUP_STATUS_DIR`, по умолчанию `/srv/aicd-backup/status`, смонтирован `ro` в
  `/run/aicd-backup`, `AICD_BACKUP_STATUS_FILE=/run/aicd-backup/status.json`; задания бэкапа на хосте пишут
  `--status-file "$AICD_BACKUP_STATUS_DIR/status.json"`, файл 0644, каталог 0755)
  `aicd_backup_last_success_timestamp_seconds{component}`, `aicd_backup_last_attempt_failed{component}`; из `pg_stat_archiver` —
  `aicd_postgres_wal_archived`, `aicd_postgres_wal_archive_failures`.
* Тот же каталог смонтирован `ro` в контейнер `api`: компонент `backup` в `GET /api/v1/system/health` (owner/admin, экран
  System Health) показывает последний статус и время успеха каждого компонента и флаг устаревания (пороги — как у алертов
  ниже) плюс `pg_stat_archiver`; без файла статус компонента — `unknown` (значения не выдумываются).
* Алерты (`infrastructure/monitoring/prometheus/rules/aicd-alerts.yml`, группа `aicd-backup`, promtool-тесты):

| Алерт | Условие | Действие |
|---|---|---|
| AicdBackupFailed | последняя попытка компонента — ошибка, 5 мин | `last_error` в status.json, вывод задания; повторить вручную; `verify` |
| AicdBaseBackupStale | нет успешного `pg-base` > 26 ч | расписание/место на диске репозитория |
| AicdWalSyncStale | нет успешного `pg-wal-sync` > 15 мин — RPO растёт | задание cron, доступ к тому `aicodingdev_pgwal`, место |
| AicdArtifactsBackupStale | нет снимка артефактов > 26 ч | задание `artifacts`, доступ к хранилищу/БД |
| AicdWalArchiveFailing | растёт `failed_count` archive_command | `$C logs postgres`; переполнен/недоступен том архива; файл с тем же именем от другого кластера (архив не очищен после пересоздания кластера) — перенести старые файлы и очистить том |

## 7. Связанные решения и ограничения

* Шифрование «на покое» самих томов хоста — LUKS/шифрование диска (SECURITY_ARCHITECTURE §14); бэкапы шифруются независимо.
* Права `aicd_maintenance` для `pg_dump` — миграция 0016 (см. §1).
* Метрики бэкапа экспортирует сервис `worker-outbox` из `compose.yaml` (OQ-51 (d), проверено на одноразовой копии
  профиля: `aicd_backup_last_attempt_failed{component}` в Prometheus из смонтированного файла статуса).
