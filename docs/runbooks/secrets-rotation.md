# Runbook: ротация секретов (P9-06)

**Требования:** ТЗ §15 (смена секретов — dangerous action с approval), SECURITY_ARCHITECTURE §6, SC-SEC-01…07, SC-GH-06.
**Статус:** IN_REVIEW (ревью оператором). Все секреты профиля local — файлы `infrastructure/docker/secrets/*`
(каталог `0700`, файлы `0444`, вне git; создаются `infrastructure/docker/init-secrets.sh`).

Общий порядок для каждого секрета: approval `SECRET_CHANGE` (owner, four-eyes) → запись в журнал изменений (кто/когда/
какой секрет, без значения) → новая копия секретов в бэкап (`python -m scripts.backup secrets --dir infrastructure/docker/secrets`,
[backups.md](backups.md)) → проверка. Новое значение: `head -c 48 /dev/urandom | base64 | tr -d '/+=\n'`.

```sh
C="docker compose -f infrastructure/docker/compose.yaml"
S=infrastructure/docker/secrets
new() { head -c 48 /dev/urandom | base64 | tr -d '/+=\n'; }
put() { chmod u+w "$S/$1" 2>/dev/null; printf '%s' "$2" > "$S/$1"; chmod 0444 "$S/$1"; }
```

| Секрет | Файлы | Шаги | Влияние |
|---|---|---|---|
| Пароль роли `aicd_app` | `pg_app_password`, `app_database_url` | `P=$(new)`; `$C exec -T postgres psql -U aicd_admin -d aicd -c "ALTER ROLE aicd_app PASSWORD '$P'"`; `put pg_app_password "$P"`; `put app_database_url "postgresql://aicd_app:$P@postgres:5432/aicd"`; `$C up -d --force-recreate api` | разрыв соединений API на время перезапуска |
| Пароль `aicd_worker` | `pg_worker_password` (+ URL воркеров) | как выше для `aicd_worker`; перезапуск воркеров | воркеры дорабатывают сообщения после перезапуска (аренды с fencing) |
| Пароль `aicd_migrator` | `pg_migrator_password`, `migrator_database_url` | `ALTER ROLE aicd_migrator …`; оба файла; следующий `migrate` использует новый | нет |
| Пароль `aicd_maintenance` | `pg_maintenance_password` (+ DSN задания retention/бэкапа) | `ALTER ROLE aicd_maintenance …`; обновить файл DSN заданий | задания `aicd maintenance retention` до обновления DSN — с ошибкой (алерт) |
| Пароль суперпользователя `aicd_admin` | `pg_admin_password` | `ALTER ROLE aicd_admin PASSWORD …`; файл (используется только при инициализации тома и оператором) | нет |
| Redis | `redis_password`, `app_redis_url` | `P=$(new)`; `put redis_password "$P"`; `put app_redis_url "redis://:$P@redis:6379/0"`; `$C up -d --force-recreate redis api` + воркеры | очереди Redis теряются — безопасно (outbox/tick, [recovery.md §6](recovery.md)) |
| `app_secret_key` (HMAC курсоров пагинации и подписанных ссылок скачивания FS) | `app_secret_key` | `put app_secret_key "$(new)"`; `$C up -d --force-recreate api` | выданные курсоры и ссылки скачивания становятся недействительными (клиент перезапрашивает список); сессии пользователей не затрагиваются |
| `metrics_token` | `metrics_token` | `put metrics_token "$(new)"`; `$C up -d --force-recreate api prometheus` (+ воркеры) | краткий `AicdTargetDown`, пока не перезапущены обе стороны |
| Пароль Grafana `admin` | `grafana_admin_password` | смена в UI Grafana (Administration → Users) **или** удалить том `aicodingdev_grafana-data` (теряет настройки UI; дашборды — из provisioning) | — |
| S3 (MinIO/RustFS) ключи | файлы `AICD_ARTIFACT_S3_*_KEY_FILE` | создать новый ключ доступа в хранилище, обновить файлы, перезапустить api/воркеры, удалить старый ключ | — |
| GitHub App ключ / webhook secret | см. [github-app.md](github-app.md) §3–4 | два активных webhook secret на время ротации | — |
| Ключ бэкапов | см. [backups.md §2](backups.md) | новый репозиторий | — |
| Ключ подписи релизов (cosign) | см. [release.md §3](release.md) | новый ключ + публикация `cosign.pub` | старые релизы проверяются старым ключом |
| Учётные данные CLI | см. [cli-auth.md](cli-auth.md) | `logout` + вход | задачи `BLOCKED(AUTH_REQUIRED)` до входа |

Проверка после любой ротации: `curl -fsS http://localhost:8080/api/v1/health/ready`, вход пользователя, алерты
`AicdTargetDown`/`AicdApiHighErrorRate` не горят; в логах нет `password authentication failed`.
При компрометации — сначала отозвать (ALTER ROLE / удалить ключ у провайдера), затем расследование по
[incident-response.md](incident-response.md); сессии пользователей (если скомпрометирована БД) — отзыв всех: смена паролей
пользователей отзывает их сессии (SECURITY_ARCHITECTURE §3).
