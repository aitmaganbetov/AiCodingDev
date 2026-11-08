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
export AICD_BACKEND_IMAGE=aicodingdev/backend:<версия> AICD_WEB_IMAGE=aicodingdev/web:<версия>  # из релиза (§2)
export AICD_REPOS_DIR=/srv/git                                 # allowlist локальных репозиториев (см. права ниже)
export AICD_WORKTREES_DIR=/srv/aicd/worktrees AICD_VERIFICATION_STAGING_DIR=/srv/aicd/verification
export AICD_BACKUP_STATUS_DIR=/srv/aicd-backup/status          # каталог файла статуса бэкапов (backups.md §6)
export AICD_PUBLIC_ORIGIN=https://aicd.example.internal        # при доступе по сети через TLS-терминатор
install -d -o 10001 -g 10001 -m 0750 "$AICD_WORKTREES_DIR" "$AICD_VERIFICATION_STAGING_DIR"
install -d -m 0755 "$AICD_BACKUP_STATUS_DIR"
C="docker compose -f infrastructure/docker/compose.yaml -f infrastructure/docker/compose.sandbox.yaml"
$C up -d --no-build --wait                                     # override оператора: GitHub App (github-app.md)
$C exec api aicd admin create-owner --email you@corp.example --name "Имя" --workspace-slug main --workspace-name "Main"
curl -fsS http://localhost:8080/api/v1/health/ready
```

* Сервис `postgres` архивирует WAL в том `aicodingdev_pgwal` (`AICD_PG_ARCHIVE_TIMEOUT`, по умолчанию 60 с).
* **Воркеры** — сервисы `worker-outbox`, `worker-orchestrator`, `worker-exec`, `worker-index` (тот же образ backend,
  команда `aicd-worker <вид>`; OQ-46 (3), OQ-51 (d)). Все — роль БД `aicd_worker` (секрет `worker_database_url`), без
  ключа подписи API и без роли `aicd_maintenance`; rootfs только для чтения, `cap_drop: ALL`, портов не публикуют;
  healthcheck — `/metrics` воркера (`:9464`, bearer `metrics_token`), его же скрейпит Prometheus. Остановка — SIGTERM,
  текущие сообщения дорабатываются (`stop_grace_period` 60 с).
* **worker-exec** — единственный воркер с записью в `AICD_REPOS_DIR` (`git worktree add` пишет в `.git/worktrees`;
  каталог и клоны должны быть доступны на запись uid 10001) и с путём к Docker API — только через `docker-socket-proxy`
  оверлея `compose.sandbox.yaml` (сеть `aicd-docker-ctl`; сокет Docker в воркер не монтируется, SECURITY_ARCHITECTURE
  §7.5). Каталоги worktrees и staging gates монтируются по **тому же пути**, что на хосте: контейнеры gates создаёт
  dockerd хоста и монтирует их по пути хоста. Образы gates должны быть загружены заранее (pull через прокси запрещён,
  [release.md §4](release.md)). Без оверлея gates в контейнере не запускаются (имя прокси не разрешается).
* **Fake CLI в production невозможен:** все воркеры работают с `AICD_ENVIRONMENT=production`, единственный адаптер
  исполнения — `fake`, и worker-exec завершается с кодом 2 (`AICD_EXEC_ADAPTER=fake запрещён в production`) и
  перезапускается с нарастающей паузой, пока не подключены реальные адаптеры Codex/Claude (P2-01, блокер релиза).
  До этого `up --wait` сообщает о worker-exec как о неуспешном — остальные сервисы работают; оценочный стенд вне
  production — override только для worker-exec (`AICD_ENVIRONMENT: test`), как в проверке ниже.
* worker-orchestrator и worker-index читают репозитории только для чтения; загрузка документации и GitHub App требуют
  исходящего доступа (сеть `backend` — internal): `AICD_EGRESS_PROXY_URL` и сеть — override оператора ([github-app.md](github-app.md)).
* Наблюдаемость: `--profile observability` ([observability.md](observability.md)).

**Проверено (2026-11-08, одноразовый проект compose `-p` с собственными томами, портами и секретами, затем удалён):**
`compose.yaml` + `compose.sandbox.yaml` + `--profile observability`, образ backend из текущего дерева. В production
api/worker-outbox/worker-orchestrator/worker-index — `healthy`, worker-exec — код 2 с сообщением о запрете Fake CLI;
Prometheus — цели `aicd-worker` (outbox, orchestrator, index) `up`, `aicd_backup_last_attempt_failed{component}` из
смонтированного файла статуса. С override `AICD_ENVIRONMENT=test` (+ `AICD_FAKE_SCENARIO=agent`) для worker-exec:
probe провайдеров через API → установки записаны worker-exec; DEVELOP run через nginx → `COMPLETED` (developer codex и
независимый reviewer claude `SUCCEEDED`, gates SECRETS/SCOPE в контейнерах через docker-socket-proxy — `PASS`);
SIGTERM — все воркеры завершились с кодом 0. Ограничения проверки: образ собран с `AICD_INSTALL_GIT=0` (deb.debian.org
недоступен, OQ-51 (f)) и для проверки дополнен git хоста (не релизный образ); установки Fake CLI получают
`compatibility=untested` (матрица пуста) — как живой стенд E2E, проверка отметила их `supported` в БД; gate SAST
(semgrep) исключён профилем workspace — в контейнере без сети probe `semgrep --version` превышает 60 с (переменные
gate `SEMGREP_ENABLE_VERSION_CHECK=0` не передаются в контейнер — дефект verification, передан на исправление).

## 4. Сразу после установки

1. Бэкапы: ключ, репозиторий, расписание, мониторинг — [backups.md](backups.md) §2–§3, §6; первый `pg-base` и `verify`.
2. Вход оператора в CLI — [cli-auth.md](cli-auth.md); экран CLI Providers → «Проверить».
3. Офлайн-БД уязвимостей для SCA gates — [vulndb-update.md](vulndb-update.md); образы verification платформы
   (`migrations-python`, `phpstan`) — [release.md §4](release.md).
4. GitHub App (если нужен PR workflow) — [github-app.md](github-app.md).
5. **TLS/HSTS на edge (TH-46, OQ-48 (b))** — после настройки TLS-терминатора и при каждом изменении edge:
   `uv run python -m scripts.release.preflight_tls --url https://aicd.example.internal [--ca-file corp-ca.pem]` →
   все проверки `PASS` ([release.md §5](release.md)); результат — в журнал развёртывания.
6. DR drill на этой версии (одноразовые контейнеры): `uv run python -m scripts.dr.drill`.
7. Ознакомить дежурных с [incident-response.md](incident-response.md), [recovery.md](recovery.md), [secrets-rotation.md](secrets-rotation.md).

## 5. Обновление

[upgrades.md](upgrades.md): проверка релиза → dry-run миграций → свежий бэкап → `docker load` → `up -d --no-build --wait`.
