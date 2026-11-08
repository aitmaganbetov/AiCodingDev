# Установка AiCodingDev (P9-09, черновик)

**Статус:** черновик для утверждения владельцем. Целевой профиль — single host (Linux + Docker, OQ-03). Production-развёртывание —
только с отдельного разрешения владельца. Подробности первого запуска — [local-deployment.md](local-deployment.md).

## 1. Требования

* Linux x86-64, Docker Engine ≥ 24 с плагином compose; 4 vCPU / 8 GiB RAM минимум (замеры — [PERF_BASELINE](../../reports/perf/PERF_BASELINE.md));
  шифрование дисков хоста (LUKS) — для томов БД, артефактов и учётных данных CLI.
* TLS-терминатор перед сервисом `web` для доступа по сети (`AICD_PUBLIC_ORIGIN=https://…`, cookie `Secure`).
* Официальные Codex CLI и/или Claude Code CLI с действующей подпиской на хосте исполнения ([cli-auth.md](cli-auth.md)) —
  без них задачи остаются `BLOCKED (AUTH_REQUIRED)` (AC-16).
* Отдельное хранилище для репозитория бэкапов и офлайн-место для ключей бэкапа и проверочного ключа релиза.
* Для обслуживания из исходников (бэкап, DR drill, проверка релиза): Python 3.12+ и `uv`.

## 2. Получение и проверка релиза

```sh
uv run python -m scripts.release.verify --release /srv/aicd-release/<версия> --public-key /etc/aicd/cosign.pub --expect-commit <sha>
docker load -i /srv/aicd-release/<версия>/images/backend.tar
docker load -i /srv/aicd-release/<версия>/images/web.tar
docker image inspect --format '{{.Id}}' aicodingdev/backend:<версия>   # = image_id в release-manifest.json
```

Проверка обязана завершиться `PASS` (подписи, SLSA provenance, SBOM-аттестации, SHA256SUMS) — иначе не устанавливать.
Сборка из исходников (без релиза) — [release.md](release.md) или `docker build` по [local-deployment.md](local-deployment.md).

## 3. Конфигурация и запуск

```sh
infrastructure/docker/init-secrets.sh                          # случайные секреты, каталог 0700, файлы 0444, вне git
export AICD_REPOS_DIR=/srv/git                                 # allowlist локальных репозиториев (монтируется ro)
export AICD_PUBLIC_ORIGIN=https://aicd.example.internal        # при доступе по сети через TLS-терминатор
# override оператора: версии образов (image: aicodingdev/backend:<версия>), GitHub App (github-app.md)
docker compose -f infrastructure/docker/compose.yaml up -d --no-build --wait
docker compose -f infrastructure/docker/compose.yaml exec api \
  aicd admin create-owner --email you@corp.example --name "Имя" --workspace-slug main --workspace-name "Main"
curl -fsS http://localhost:8080/api/v1/health/ready
```

* Сервис `postgres` архивирует WAL в том `aicodingdev_pgwal` (`AICD_PG_ARCHIVE_TIMEOUT`, по умолчанию 60 с).
* Воркеры `aicd-worker outbox|orchestrator|exec|index` (тот же образ backend) запускаются оператором рядом со стеком
  (в `compose.yaml` их пока нет — P1-02); `AICD_EXEC_ADAPTER=fake` в production запрещён — реальные адаптеры после P2-01.
* Наблюдаемость: `--profile observability` ([observability.md](observability.md)).

## 4. Сразу после установки

1. Бэкапы: ключ, репозиторий, расписание, мониторинг — [backups.md](backups.md) §2–§3, §6; первый `pg-base` и `verify`.
2. Вход оператора в CLI — [cli-auth.md](cli-auth.md); экран CLI Providers → «Проверить».
3. Офлайн-БД уязвимостей для SCA gates — [vulndb-update.md](vulndb-update.md); образы verification платформы
   (`migrations-python`, `phpstan`) — [release.md §4](release.md).
4. GitHub App (если нужен PR workflow) — [github-app.md](github-app.md).
5. DR drill на этой версии (одноразовые контейнеры): `uv run python -m scripts.dr.drill`.
6. Ознакомить дежурных с [incident-response.md](incident-response.md), [recovery.md](recovery.md), [secrets-rotation.md](secrets-rotation.md).

## 5. Обновление

[upgrades.md](upgrades.md): проверка релиза → dry-run миграций → свежий бэкап → `docker load` → `up -d --no-build --wait`.
