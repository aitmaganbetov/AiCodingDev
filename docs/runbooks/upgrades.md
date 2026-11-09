# Runbook: обновление версии и миграции БД (P9-06)

**Требования:** ТЗ §7.1, §16 (миграции с dry-run), NFR-REL-04, NFR-MAINT-01, DATABASE_DESIGN §21 (миграции), ADR-0008.
**Статус:** IN_REVIEW (ревью оператором). Production-развёртывание — только с отдельного разрешения владельца.

```sh
C="docker compose -f infrastructure/docker/compose.yaml"
B="uv run python -m scripts.backup"
```

## 1. Перед обновлением

1. Релиз проверен: `uv run python -m scripts.release.verify --release <каталог релиза> --public-key <доверенный cosign.pub>
   --expect-commit <sha>` → `PASS` ([release.md](release.md)); `release-manifest.json` → `releasable: true`.
2. Прочитать примечания релиза: новые миграции, необратимые (`irreversible = True` в docstring миграции — план
   восстановления из бэкапа), новые секреты (`init-secrets.sh` создаёт недостающие), новые переменные окружения.
3. **Dry-run миграций** — SQL для ревью и прогон на восстановленной копии последнего бэкапа (DATABASE_DESIGN §21):
   ```sh
   docker run --rm -e AICD_MIGRATOR_DATABASE_URL=postgresql://aicd_migrator@offline/aicd --entrypoint alembic \
     aicodingdev/backend:<версия> -c infrastructure/migrations/alembic.ini upgrade head --sql > migration-<версия>.sql   # без подключения к БД
   uv run python -m scripts.dr.drill --report reports/dr/pre-upgrade.md   # восстановление+проверки на одноразовых контейнерах
   ```
   Деструктивные изменения (DROP/TRUNCATE/смена типа) — approval `DESTRUCTIVE_MIGRATION` (owner, four-eyes) до продолжения.
4. **Свежий бэкап и проверка его восстановимости** (обязательно, DATABASE_DESIGN §21):
   ```sh
   $B pg-base --pg-container aicodingdev-postgres-1 && $B pg-wal-sync --wal-volume aicodingdev_pgwal && $B verify --kind base --backup-id latest
   $B artifacts --fs-root "$(docker volume inspect -f '{{.Mountpoint}}' aicodingdev_artifacts)" --pg-container aicodingdev-postgres-1
   $B secrets --dir infrastructure/docker/secrets
   ```

## 2. Обновление

1. Загрузить проверенные образы (без реестра): `docker load -i images/backend.tar && docker load -i images/web.tar`;
   сверить `docker image inspect --format '{{.Id}}' aicodingdev/backend:<версия>` с `release-manifest.json` (`image_id`).
2. Указать версию образов (override оператора: `image: aicodingdev/backend:<версия>` для `migrate`, `api`, воркеров;
   `aicodingdev/web:<версия>` для `web`).
3. Остановить приём новой работы: экран Runs — пауза активных run (или дождаться завершения); остановить воркеры штатно
   (SIGTERM — текущие сообщения дорабатываются).
4. `$C up -d --no-build --wait` — сервис `migrate` выполняет `alembic upgrade head` под ролью `aicd_migrator` до старта `api`.
5. Проверка: `curl -fsS http://localhost:8080/api/v1/health/ready` (`migrations: ok` — схема на head),
   `$C exec -T postgres psql -U aicd_admin -d aicd -Atc 'SELECT version_num FROM aicd.alembic_version'`;
   `aicd db sync-agent-profiles` при изменении профилей ролей (роль мигратора):
   `$C run --rm --no-deps -e AICD_MIGRATOR_DATABASE_URL="$(cat infrastructure/docker/secrets/migrator_database_url)" api aicd db sync-agent-profiles`.
6. Возобновить run, запустить воркеры; наблюдать алерты 30 мин.

## 3. Откат

* Обратимые миграции: `docker run --rm --network aicodingdev_backend -e AICD_MIGRATOR_DATABASE_URL_FILE=/run/secrets/m
  -v "$PWD/infrastructure/docker/secrets/migrator_database_url:/run/secrets/m:ro" --entrypoint alembic aicodingdev/backend:<новая версия>
  -c infrastructure/migrations/alembic.ini downgrade <ревизия предыдущего релиза>`, затем образы предыдущей версии.
  Каждая миграция реализует `downgrade()`, CI проверяет цикл upgrade→downgrade→upgrade (`tests/migrations`).
* Необратимые/повреждённые данные: восстановление из бэкапа, сделанного в §1.4 ([backups.md §5](backups.md)), PITR до
  момента перед обновлением (`restore-pg --target-time …`).

## 4. Обслуживание между релизами

* Партиции журналов: `aicd maintenance retention` (роль `aicd_maintenance`, расписание `--every` или cron) создаёт партиции
  вперёд и применяет сроки хранения; `aicd db ensure-partitions --months 3` — вручную под ролью мигратора.
* Образы verification/инструментов обновляются только сменой закреплённых digest в `aicd_verification` (ревью +
  `tests/security/test_security_gates_docker.py`); офлайн-БД уязвимостей — [vulndb-update.md](vulndb-update.md).
