# SECURITY_CONTROLS — каталог контролей безопасности AiCodingDev v1.0

**Статус:** Каталог из Phase 0; реализация и проверка — по фазам P1…P9. **Phase 9 (P9-02):** каждый контроль сопоставлен с тестами в `tests/security/catalog.py` (вид: automated / partial / manual с описанием остатка), набор запускается `pytest -m security`, результат фактического прогона по каждому контролю — `reports/security/SECURITY_REPORT.md` (генерирует `scripts/security_report.py` из junit-XML); статусы угроз и release security gate — THREAT_MODEL §10, `scripts/security_gate.py`. Колонка «Проверка» ниже — исходный план (`ST-…`), фактические тесты — в каталоге.
**Основание:** ТЗ §7.1, §8, §9, §10, §12, §14, §15, §16, §20, §21, §23; brief Phase 0; ADR-0003, ADR-0006, ADR-0008–ADR-0012.
**Связанные документы:** [SECURITY_ARCHITECTURE.md](../architecture/SECURITY_ARCHITECTURE.md), [THREAT_MODEL.md](THREAT_MODEL.md).

---

## 1. Как читать каталог

| Поле | Смысл |
|---|---|
| **ID** | `SC-<AREA>-NN`. Области: `IAM` (идентификация, сессии, RBAC), `TEN` (изоляция арендаторов), `SBX` (sandbox), `CLI` (CLI Gateway и учётные данные провайдеров), `SEC` (секреты), `INJ` (prompt/command injection), `NET` (сеть), `DOC` (импорт документации), `GH` (GitHub), `APR` (approvals), `AUD` (аудит), `SUP` (supply chain), `DATA` (данные и целостность workflow), `OBS` (наблюдаемость), `API` (HTTP API/SSE) |
| **Реализация** | Модуль/компонент по [MODULE_BOUNDARIES.md](../architecture/MODULE_BOUNDARIES.md) (`packages/<module>`, `apps/<app>`, `infrastructure/…`) |
| **ASVS** | Глава OWASP ASVS **4.0.3 / 5.0**. Сопоставление выполнено **на уровне глав**, без цитирования требований; точные номера требований уточняются при утверждении редакции стандарта для проекта (ТЗ §13). Это ориентир, а не заявление о соответствии |
| **SSDF** | Практики NIST SP 800-218 (SSDF v1.1): `PO.x`, `PS.x`, `PW.x`, `RV.x` — на уровне практики |
| **Угрозы** | `TH-NN` из THREAT_MODEL.md. Список может быть шире, чем ссылки из таблицы угроз (контроль может косвенно снижать и другие угрозы) |
| **Проверка** | Тип теста по ТЗ §20 + идентификатор-заглушка `ST-<AREA>-NN` (номер совпадает с контролем) |
| **Фаза** | P1…P9 по brief (P1 Foundation, P2 CLI, P3 Orchestrator, P4 Multi-Agent, P5 Code Intelligence, P6 Quality, P7 Mission Control, P8 GitHub, P9 Verification & Release) |
| **Приоритет** | `P0` — блокирует релиз v1.0; `P1` — обязателен до релиза, допускается отставание внутри фаз; `P2` — hardening/опция, может быть перенесён после v1.0 по решению владельца |

> Сопоставления с OWASP ASVS и NIST SSDF не являются заявлением о соответствии или сертификации (ТЗ §1, §13).

---

## 2. Каталог контролей

### 2.1 IAM — аутентификация, сессии, авторизация

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-IAM-01 | Хеширование паролей argon2id (m=64 MiB, t=3, p=1, соль 16 Б), PHC-строка, rehash при входе; семафор ≤4 параллельных хешей на процесс; фиктивная проверка для неизвестного пользователя | `identity` (`passwords.py`) | V2 / V6, V11 | PW.5 | TH-23 | Unit: ST-IAM-01 (параметры PHC, rehash, тайминг) | P1 | P0 |
| SC-IAM-02 | Политика паролей: 12–128 символов, NFKC, офлайн-список скомпрометированных паролей, без правил состава | `identity` | V2 / V6 | PW.5, PW.9 | TH-23 | Unit: ST-IAM-02 | P1 | P1 |
| SC-IAM-03 | Серверные сессии: токен 32 Б CSPRNG, в БД только SHA-256; cookie `__Host-aicd_session` `Secure; HttpOnly; SameSite=Strict; Path=/`; idle 8 ч, absolute 7 дней (ADR-0009) | `identity` (`sessions.py`), `apps/api` middleware | V3 / V7 | PW.5 | TH-20, TH-21, TH-46 | Integration: ST-IAM-03 (атрибуты cookie, истечение) | P1 | P0 |
| SC-IAM-04 | Ротация сессии при входе, смене пароля/роли, step-up; отзыв всех сессий при смене пароля; logout = `revoked_at`; принудительное завершение owner/admin | `identity` | V3 / V7 | PW.5 | TH-21 | Integration: ST-IAM-04 (fixation, старый токен после смены пароля → 401) | P1 | P0 |
| SC-IAM-05 | CSRF: синхронизирующий токен (hash в `user_sessions.csrf_token_hash`) через cookie `__Host-aicd_csrf` + заголовок `X-CSRF-Token` для всех небезопасных методов; проверка `Origin`/`Sec-Fetch-Site`; login CSRF; без CORS | `apps/api` middleware `csrf.py` | V4 (4.2.2), V13 / V3, V7 | PW.5 | TH-20 | Security: ST-IAM-05 (без токена, чужой токен, чужой Origin → 403) | P1 | P0 |
| SC-IAM-06 | Защита от подбора: блокировка учётной записи после 10 неудач (экспонента, max 24 ч), лимиты на IP и (аккаунт, IP), единый ответ 401, аудит | `identity`, `apps/api` rate limiter (Redis) | V2 / V6 | PW.5 | TH-23 | Security: ST-IAM-06 | P1 | P0 |
| SC-IAM-07 | Единый PDP `identity.api.authorize(actor, permission, resource)`, deny-by-default; матрица прав в коде как единственный источник; генерация `permissions-matrix.md` (ТЗ §21); повторная проверка в слое сервиса | `identity`, все сервисы доменных модулей | V4 / V8 | PW.1, PW.5 | TH-04, TH-16, TH-24 | Unit: ST-IAM-07 (каждая пара роль×permission); Contract: каждая операция OpenAPI имеет `x-permission` | P1 | P0 |
| SC-IAM-08 | Step-up re-auth (повторный пароль, если > 10 мин) для критических approvals, управления участниками, политик безопасности, `SECRET_CHANGE` | `identity`, `orchestration.approvals`, `apps/api` (`require_step_up`, `POST /auth/reauthenticate`) | V2, V3 / V6, V7 | PW.5 | TH-33, TH-21, TH-61, TH-64 | Integration: ST-IAM-08 (`tests/security/test_step_up.py`; реализовано в P9, миграция 0014) | P3 → P9 | P1 |
| SC-IAM-09 | Bootstrap первого owner только командой `aicd admin create-owner`; нет учётных записей/паролей по умолчанию; самостоятельная регистрация отключена; управление участниками — owner (приглашение ≤ developer — admin) | `apps/cli`, `identity` | V2, V14 / V6, V13 | PW.9 | TH-23, TH-24 | Integration: ST-IAM-09 (пустая инсталляция не принимает входы) | P1 | P0 |

### 2.2 TEN — изоляция арендаторов

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-TEN-01 | RLS `ENABLE` + `FORCE` на всех tenant-таблицах; политика `USING`/`WITH CHECK` по `app.workspace_id`; роли `aicd_app`/`aicd_worker` NOBYPASSRLS и не владельцы; единственная `SECURITY DEFINER` функция `list_my_workspaces()` с фиксированным `search_path`; представления `v_*` с `security_invoker=true` | `infrastructure/migrations`, `platform.db` | V4 / V8 | PW.1, PW.5 | TH-16, TH-17 | Integration: ST-TEN-01 (тест-каталог: каждая таблица с `workspace_id` имеет RLS+FORCE; запрос без контекста → 0 строк) | P1 | P0 |
| SC-TEN-02 | `platform.db.transaction(ctx)` выполняет `SET LOCAL app.workspace_id/user_id/actor_type` в каждой транзакции; запрет `SET` без `LOCAL` (линтер); `RESET ALL` при возврате соединения в пул; воркеры устанавливают контекст из сообщения и перепроверяют существование ресурса | `platform.db`, `apps/worker` | V4 / V8 | PW.5 | TH-17 | Integration: ST-TEN-02 (утечка контекста между транзакциями пула) | P1 | P0 |
| SC-TEN-03 | Denormalized `workspace_id` + composite FK `(workspace_id, parent_id) → parent(workspace_id, id)` на всех tenant-ссылках (ADR-0008); триггер против связей задач разных runs (ТЗ §7.3) | `infrastructure/migrations` | V4 / V8 | PW.1 | TH-17 | Integration: ST-TEN-03 (вставка ссылки на чужой workspace → FK violation) | P1 | P0 |
| SC-TEN-04 | Чужой/несуществующий ресурс → одинаковый `404 problem+json` (тело, время ответа в пределах шума), включая SSE, artifacts, approvals | `apps/api` error handlers, `identity.authorize` | V4 / V8 | PW.5 | TH-16, TH-18 | Security: ST-TEN-04 (обход всех endpoint'ов OpenAPI с ID другого workspace; AC-12) | P1 | P0 |
| SC-TEN-05 | Ключи объектов `ws/<workspace_id>/<project_id>/<artifact_id>`; доступ к артефактам только через API после проверки строки `artifacts` под RLS; MinIO не доступен из сети `edge`; presigned URL (если используются) — TTL ≤ 60 с, только после authz | `platform.artifacts`, `apps/api` | V4, V12 / V8, V5 | PW.5 | TH-08, TH-16 | Security: ST-TEN-05 | P1 | P0 |
| SC-TEN-06 | Redis: префиксы ключей с `workspace_id`; pub/sub несёт только `(run_id, seq)`; данные клиенту — только из `run_event_log` после authz | `platform.outbox`, `apps/api` SSE | V4 / V8 | PW.5 | TH-18, TH-42 | Integration: ST-TEN-06 | P3 | P1 |
| SC-TEN-07 | Ключи кэшей context packs/индекса включают `workspace_id`; межтенантная дедупликация по content hash запрещена (кроме публичной документации) | `code_intelligence` | V8 / V14 | PW.1 | TH-19 | Integration: ST-TEN-07 | P5 | P1 |
| SC-TEN-08 | `aicd_maintenance` (BYPASSRLS) — отдельные учётные данные, только задачи retention/backup, каждое подключение аудируется и алертится | `infrastructure/migrations`, `apps/cli` | V4, V7 / V8, V16 | PO.5 | TH-17, TH-34 | Integration: ST-TEN-08 (роль приложения не может `SET ROLE aicd_maintenance`) | P1 | P1 |

### 2.3 SBX — sandbox исполнения

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-SBX-01 | Runner-контейнер: non-root UID 10001, без `--privileged`, `--cap-drop=ALL`, `no-new-privileges`, seccomp/AppArmor default, read-only rootfs, tmpfs `nosuid,nodev`, без host namespaces, `--init` (ADR-0010) | `security.sandbox` (container spec builder), `cli_adapters` | V1, V14 / V15, V13 | PO.5, PW.9 | TH-01, TH-09 | Integration: ST-SBX-01 (inspect spec); Security: ST-SBX-01b (mount/ptrace/raw socket/setuid внутри runner отклоняются) | P2 | P0 |
| SC-SBX-02 | Mounts: только worktree задачи rw, context pack ro, том учётных данных провайдера; общий `.git` ro, запись объектов через отдельный `GIT_OBJECT_DIRECTORY` + alternates; ничего иного (spike P2) | `security.sandbox`, `orchestration.worktrees` | V12 / V5 | PO.5 | TH-08, TH-09, TH-11 | Integration: ST-SBX-02 (запись вне `/workspace` → EROFS/EACCES) | P2 | P0 |
| SC-SBX-03 | Лимиты: `--pids-limit 512`, memory 4g без swap, `--cpus 2`, `nofile 4096`, wall-clock timeout, лимит вывода, квота размера worktree; профили лимитов в policy | `security.sandbox`, `cli_adapters` | V11 / V2, V15 | PW.9 | TH-37, TH-49 | Security: ST-SBX-03 (fork bomb, бесконечный вывод, заполнение диска) | P2 | P0 |
| SC-SBX-04 | Завершение дерева процессов: `docker stop -t 10` → SIGKILL namespace → `rm -f`; метки `aicd.execution_id`; reconciler осиротевших контейнеров и worktree (`ORPHANED`); в local fallback — `setsid` + `killpg` + cgroup scope | `cli_adapters`, `orchestration.recovery` | V11 / V2 | PW.5 | TH-37 | Chaos/recovery: ST-SBX-04 (kill worker-exec во время исполнения → контейнер убит, состояние сверено) | P2 | P0 |
| SC-SBX-05 | Docker только через `docker-socket-proxy` (разрешены CONTAINERS, IMAGES inspect, INFO, VERSION, PING; запрещены EXEC, BUILD, VOLUMES/NETWORKS create, SWARM, SECRETS, PLUGINS, SYSTEM, pull); доступ только exec/verify через сеть `docker-ctl`; **container spec linter** отклоняет Privileged, CapAdd, host modes, Devices, mounts вне allowlist, ослабление seccomp, образы не из allowlist digest | `infrastructure/docker`, `security.sandbox.lint` | V1, V14 / V15, V13 | PO.5, PW.9 | TH-09, TH-10 | Unit: ST-SBX-05 (линтер, таблица запрещённых спецификаций); Integration: запрещённые endpoint'ы → 403 | P2 | P0 |
| SC-SBX-06 | Сеть runner: Docker-сеть `sandbox` `internal: true`, выход только через `egress-proxy`; внешний DNS недоступен | `infrastructure/docker`, `security.egress` | V1, V14 / V15, V13 | PO.5 | TH-01, TH-12 | Security: ST-SBX-06 (прямое соединение/DNS-запрос наружу из runner — неуспешны) | P2 | P0 |
| SC-SBX-07 | Verification-контейнер: отдельный образ, без учётных данных CLI/секретов/GitHub-токенов, `--network none`, код — копия снимка по `commit_sha`; prefetch зависимостей — отдельный контейнер с allowlist реестров | `verification`, `security.sandbox` | V10, V14 / V15, V13 | PO.5, PW.8 | TH-30, TH-38, TH-51 | Integration: ST-SBX-07 (в контейнере нет cred-тома, сети, env-секретов; результат привязан к SHA) | P6 | P0 |
| SC-SBX-08 | Образы runner/verify pinned по digest, собраны в CI, подписаны Cosign; подпись и digest проверяются `worker-exec`/`worker-verify` перед запуском | `infrastructure/docker`, `security.sandbox` | V10, V14 / V15 | PS.2, PW.4 | TH-09, TH-39 | Integration: ST-SBX-08 (неподписанный образ → отказ, `SANDBOX_DENIED`) | P2 | P1 |
| SC-SBX-09 | Опция усиленного runtime: rootless Docker / `userns-remap`, gVisor `runsc` (`sandbox.runtime`); матрица совместимости с CLI | `infrastructure/docker`, docs runbooks | V14 / V13 | PO.5 | TH-09, TH-10 | Integration: ST-SBX-09 (smoke CLI на `runsc`, opt-in) | P9 | P2 |
| SC-SBX-10 | Local fallback выключен по умолчанию; включение = политика проекта + approval `LOCAL_EXECUTION_FALLBACK` (owner, four-eyes); отдельный OS-пользователь, очищенное окружение; метка `sandbox=local` в evidence/UI | `cli_adapters`, `orchestration.approvals`, `security.sandbox` | V4, V14 / V8, V13 | PW.9 | TH-52 | E2E: ST-SBX-10 (без approval → `BLOCKED`/`APPROVAL_REQUIRED`) | P2 | P0 |
| SC-SBX-11 | Безопасные платформенные git-операции над недоверенными worktree: `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null`, `core.fsmonitor=false`, protocol allowlist, `--no-ext-diff --no-textconv`, без submodules, argv без shell, `--` перед путями | `orchestration.worktrees`, `integrations_github`, `cli_adapters` (diff collector) | V5, V12 / V1, V5 | PW.5 | TH-07, TH-11 | Security: ST-SBX-11 (вредоносные `config`/hooks/`.gitattributes` не исполняются) | P2 | P0 |
| SC-SBX-12 | Path guard `security.paths.safe_join()`: realpath + проверка префикса, `O_NOFOLLOW`, запрет `..`, абсолютных путей, symlink наружу корня, специальных файлов; применяется в artifact collector, context pack builder, индексаторе, diff viewer API | `security.paths`, `code_intelligence`, `cli_adapters`, `platform.artifacts` | V12 / V5 | PW.5 | TH-08, TH-49 | Security: ST-SBX-12 (path traversal, ТЗ §20; symlink-фикстуры) | P2 | P0 |

### 2.4 CLI — CLI Gateway и учётные данные провайдеров

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-CLI-01 | Только allowlisted адаптеры `codex`/`claude`/`fake`; запуск argv-массивом без shell по `command_profile`-шаблонам, выбранным по результатам probe; нет AI API и API-ключей (ADR-0003) | `cli_adapters` | V5, V10 / V1, V15 | PW.5 | TH-06, TH-40 | Unit: ST-CLI-01; Contract: CLI request/result schemas | P2 | P0 |
| SC-CLI-02 | Недоверенный текст (objective, инструкции, контекст) передаётся только через stdin или файл в `/context`, никогда через argv; переменные части argv (ID, пути) — по строгим regex; `--` перед позиционными аргументами | `cli_adapters` (argv builder), `agent_runtime` | V5 / V1, V2 | PW.5 | TH-06 | Security: ST-CLI-02 (command injection, ТЗ §20: `$(…)`, `;`, `--flag` в objective) | P2 | P0 |
| SC-CLI-03 | Том `aicd-cred-<provider>` монтируется только в runner того же провайдера, права 0700 UID runner; никогда — в verification, artifacts, бэкапы, context packs; collector исключает путь тома | `security.sandbox`, `cli_adapters` | V8, V14 / V14, V13 | PO.5 | TH-13 | Integration: ST-CLI-03 (canary-файл в томе не появляется ни в одном артефакте/событии/verification-контейнере) | P2 | P0 |
| SC-CLI-04 | Авторизация CLI — только официальным способом CLI: `aicd cli login <provider>` в одноразовом контейнере с TTY оператора; веб-UI/API не принимают токены; PG хранит только `auth_status` и `credential_ref` | `apps/cli`, `cli_adapters` | V2, V8 / V6, V14 | PW.9 | TH-40, TH-13 | Integration: ST-CLI-04 (в схеме API нет полей для токенов; `AUTH_REQUIRED` → `BLOCKED`, AC-16) | P2 | P0 |
| SC-CLI-05 | Соблюдение лимитов подписок: `RATE_LIMITED` → `RETRY_WAIT`/`BLOCKED` (`PROVIDER_UNAVAILABLE`) без переключения аккаунтов; нет пулинга подписок; лимит параллельных сессий на провайдера; справедливая очередь между workspaces | `cli_adapters`, `orchestration.router` | V11 / V2 | PO.1 | TH-40, TH-45 | Unit: ST-CLI-05; E2E: AC-16 | P2 | P0 |
| SC-CLI-06 | Вывод CLI — недоверенный: потоковый парсер с лимитами (строка ≤ 64 KiB, событие `cli.output` ≤ 4 KiB, raw ≤ 50 MiB), удаление ANSI/OSC/управляющих символов, строгая схема нормализованных событий, `OUTPUT_PARSE_ERROR` при нарушении; статус определяется exit code контейнера + сбором платформы | `cli_adapters` (normalizer) | V5, V13 / V1, V4 | PW.5 | TH-05, TH-37 | Contract: ST-CLI-06 (fuzz нормализатора) | P2 | P0 |
| SC-CLI-07 | Defense in depth: адаптер включает наиболее строгий неинтерактивный режим разрешений/sandbox самого CLI, доступный по feature detection; отключает серверные web-инструменты (fetch/search), если CLI это поддерживает и роль их не требует; конфликт с контейнерным sandbox решается в P2 (ADR-0010 риски) | `cli_adapters` (compat matrix) | V1 / V15 | PW.9 | TH-01, TH-12 | Integration (opt-in реальные CLI): ST-CLI-07 | P2 | P1 |
| SC-CLI-08 | CLI pinned версии в runner-образе из официального канала с проверкой integrity; compatibility matrix; неизвестная версия → `UNSUPPORTED_VERSION`; обновление — PR + contract-тесты | `cli_adapters`, `infrastructure/docker` | V10, V14 / V15 | PW.4, PS.2 | TH-39 | Contract: ST-CLI-08 (probe против матрицы) | P2 | P0 |
| SC-CLI-09 | Только официальные документированные события CLI; без перехвата/MITM трафика CLI (egress-proxy — CONNECT-туннель без TLS-инспекции для провайдеров); шаблоны инструкций не запрашивают скрытые рассуждения | `cli_adapters`, `agent_runtime`, `infrastructure/docker` (proxy) | V1 / V15 | PO.1 | TH-41 | Contract: ST-CLI-09; review шаблонов | P2 | P0 |

### 2.5 SEC — секреты

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-SEC-01 | Нет секретов в PG: CI-линтер имён колонок миграций (allowlist `password_hash`, `token_hash`, `csrf_token_hash`); тест сканирования дампа тестовой БД gitleaks-правилами | `infrastructure/migrations`, CI | V6, V8 / V11, V14 | PW.1, PW.7 | TH-13, TH-47 | Integration: ST-SEC-01 | P1 | P0 |
| SC-SEC-02 | Секреты приложения — Docker secrets (`/run/secrets/*`), загрузка `platform.settings` как `SecretStr`, регистрация в known-secret registry; env только в dev; env runner-контейнеров — allowlist без секретов | `platform.settings`, `infrastructure/docker` | V6, V14 / V11, V13 | PO.5, PW.9 | TH-14, TH-13 | Unit: ST-SEC-02 (repr/логи не содержат значений) | P1 | P0 |
| SC-SEC-03 | Redaction pipeline `security.redaction` на всех выходах (логи, SSE/`run_event_log`, ACP/chat, artifacts, context packs, ошибки): known-secret registry (Aho-Corasick, отпечатки учётных файлов CLI), vendored правила gitleaks, энтропия в контексте ключевых слов, перекрытие чанков, замена `[REDACTED:<rule>:<hash8>]`, fail closed | `security.redaction`, вызовы в `cli_adapters`, `platform` (логгер, artifacts), `collaboration`, `code_intelligence` | V7, V8 / V16, V14 | PW.5 | TH-13, TH-14, TH-15 | Security: ST-SEC-03 (secret leakage, ТЗ §20: корпус секретов через все выходы, в т.ч. на границе чанков) | P2 | P0 |
| SC-SEC-04 | Secret scan каждого diff перед коммитом/push (gitleaks + отпечатки учётных данных CLI); находка → finding `critical`, блок `COMPLETED` и push | `verification` (gate `QG-SECRETS`), `orchestration.worktrees` | V10 / V15 | PW.7, PW.8 | TH-01, TH-13, TH-15 | Security: ST-SEC-04 (canary-учётные данные, скопированные агентом в worktree, блокируют push) | P6 (базовая проверка отпечатков — P2) | P0 |
| SC-SEC-05 | Исключение чувствительных файлов из индекса и context packs: `.env*`, `*.pem`, `*.key`, `id_*`, `*.p12`, `credentials*.json`, `.npmrc`/`.pypirc` с токенами, `*.tfstate`, kubeconfig; + redaction содержимого | `code_intelligence` (index, context pack builder) | V8 / V14 | PW.5 | TH-15 | Security: ST-SEC-05 | P5 | P0 |
| SC-SEC-06 | Ротация секретов: runbook; изменение управляемых платформой секретов — approval `SECRET_CHANGE` (owner, four-eyes); два активных webhook secret при ротации | `integrations_github`, `orchestration.approvals`, `docs/runbooks` | V6, V14 / V11, V13 | PO.5, RV.2 | TH-27, TH-13 | Integration: ST-SEC-06 | P8 | P1 |
| SC-SEC-07 | Нет core dumps (`RLIMIT_CORE=0`), debug-режим запрещён в prod (проверка при старте), без трассировок в ответах | `apps/api`, `apps/worker`, `infrastructure/docker` | V7, V14 / V16, V13 | PW.9 | TH-14 | Integration: ST-SEC-07 | P1 | P1 |

### 2.6 INJ — prompt injection и command injection

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-INJ-01 | Fencing недоверенных данных: блоки с меткой происхождения и случайным nonce на исполнение, удаление совпадающих nonce из контента; шаблоны ролей объявляют содержимое данными; эвристический сканер маркеров инъекции (фразы, Unicode tags U+E0000–E007F, bidi) → warning + флаг в evidence | `agent_runtime` (prompt assembly), `code_intelligence` | V5 / V1 | PW.5 | TH-01, TH-02, TH-03, TH-04 | Security: ST-INJ-01 (prompt injection, ТЗ §20) | P4 | P0 |
| SC-INJ-02 | Политика вне контроля модели: права (`network`, `write_scope`, `production_access=false`), инструменты, approvals берутся из `policy_snapshot` и реализуются контейнером/proxy/ledger; запрос прав агентом — только ACP `BLOCKED` → human approval | `orchestration` (policy engine), `security.sandbox` | V1, V4 / V15, V8 | PW.1 | TH-01, TH-02, TH-03, TH-41 | Security: ST-INJ-02 (корпус payload'ов на fake adapter: ни один вывод не меняет права/approvals/сеть) | P3 | P0 |
| SC-INJ-03 | ACP: JSON Schema по типу, `additionalProperties:false`, ≤ 32 KiB, ACL `(sender_role, recipient_role, type)`, отправитель из контекста исполнения, `DECISION_RECORDED` — только оркестратор, лимит review/fix циклов (3), идемпотентный consumer | `collaboration` (ACP Broker), `contracts` | V5, V13 / V2, V4 | PW.5 | TH-03, TH-35 | Contract: ST-INJ-03 (payload per type, negative tests) | P4 | P0 |
| SC-INJ-04 | Claims ≠ evidence: `claims`, `TASK_COMPLETED`, exit code 0 только запускают проверку; `VERIFIED`/`PASS` выставляет Verification Engine по сохранённому результату процесса; `SKIPPED ≠ PASS`; изменения тестов/конфигов gates в diff требуют ревью | `verification`, `orchestration` | V11 / V2 | PW.8 | TH-01, TH-05, TH-30 | Integration: ST-INJ-04 (false-pass prevention, AC-11) | P6 | P0 |
| SC-INJ-05 | Директивы: тип выбирается в UI из `directive_type`, не выводится моделью; RBAC; impact analysis; подтверждение критичных; текст директивы попадает агенту только через новый context pack после checkpoint | `orchestration.directives`, `apps/web` | V4, V11 / V8, V2 | PW.1 | TH-04, TH-32 | Security: ST-INJ-05; E2E: AC-06 | P3 (backend), P7 (UI) | P0 |
| SC-INJ-06 | Безопасное отображение: React-экранирование, Markdown без raw HTML через санитайзер, удаление ANSI, ссылки `rel="noopener noreferrer"` с предупреждением о внешнем переходе, без автозагрузки внешних изображений (CSP `img-src 'self' data:`) | `apps/web`, `cli_adapters` (ANSI) | V5, V14 / V1, V3 | PW.5 | TH-05, TH-21, TH-22 | Security: ST-INJ-06 (XSS-корпус через fake CLI output, diff, chat) | P7 (базовые правила — P1) | P0 |
| SC-INJ-07 | Платформенные команды (git, gates): argv без shell, `git check-ref-format` для веток, regex для SHA (`^[0-9a-f]{40,64}$`), `--` перед путями, команды gates из quality profile платформы с параметрами-массивами | `orchestration.worktrees`, `verification`, `integrations_github` | V5 / V1 | PW.5 | TH-06, TH-07 | Security: ST-INJ-07 (command injection) | P2 | P0 |

### 2.7 NET — сеть

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-NET-01 | Egress-proxy с allowlist по классу контейнера: runner-codex/runner-claude — только эндпоинты своего провайдера из compatibility matrix; verify — нет; prefetch — реестры пакетов из quality profile; docs fetcher — через SSRF guard + запрет private-диапазонов на proxy; GitHub adapter — `github.com`, `api.github.com`; только CONNECT на 443, запрет IP-литералов; расширение — approval `UNRESTRICTED_NETWORK`; лог отказов | `infrastructure/docker` (proxy config), `security.egress` | V1, V12, V14 / V15, V13 | PO.5, PW.9 | TH-12, TH-13, TH-25, TH-38, TH-40 | Security: ST-NET-01 | P2 | P0 |
| SC-NET-02 | Сегментация: сети `edge` (proxy↔api), `backend` (api/workers↔PG/Redis/MinIO), `sandbox` (internal), `docker-ctl` (exec/verify↔socket proxy); PG/Redis/MinIO/Docker не публикуются на хост | `infrastructure/docker` (compose) | V1, V14 / V15, V13 | PO.5 | TH-10, TH-25, TH-38 | Integration: ST-NET-02 (матрица достижимости сервисов) | P1 | P0 |
| SC-NET-03 | TLS на edge (1.2+/1.3, HSTS); внутренний TLS опционален на одном хосте, обязателен при многохостовом развёртывании (PG `verify-full`, Redis TLS, MinIO HTTPS) | `infrastructure/docker` (Caddy/nginx), runbooks | V9 / V12 | PO.5 | TH-21, TH-46 | Integration: ST-NET-03 | P1 (edge), P9 (внутренний) | P0 |
| SC-NET-04 | Hardening хранилищ: Redis ACL-пользователи по процессу, `requirepass`, отключение `FLUSHALL/FLUSHDB/CONFIG/DEBUG/MODULE/KEYS`; PG `scram-sha-256`, `pg_hba` по сетям; MinIO — отдельные ключи на процесс, bucket policies | `infrastructure/docker` | V14 / V13 | PO.5 | TH-42 | Integration: ST-NET-04 | P1 | P1 |
| SC-NET-05 | DNS: runner/verify не имеют внешнего резолвера (только embedded Docker DNS для `egress-proxy`); proxy резолвит сам → нет DNS-туннелирования из sandbox | `infrastructure/docker` | V14 / V13 | PO.5 | TH-12 | Security: ST-NET-05 | P2 | P1 |

### 2.8 DOC — импорт документации

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-DOC-01 | Только `https` (Git — `https` без userinfo), порт 443 (allowlist admin), запрет userinfo в URL; единая точка `security.ssrf.fetch()`, прямой HTTP-клиент в модуле запрещён линтером | `security.ssrf`, `code_intelligence.docs` | V12, V5 / V5, V1 | PW.5 | TH-25 | Security: ST-DOC-01 | P5 | P0 |
| SC-DOC-02 | DNS resolve всех A/AAAA + проверка каждого + соединение с pinned IP (SNI/Host исходного имени) — защита от DNS rebinding | `security.ssrf` | V12 / V5, V1 | PW.5 | TH-25 | Security: ST-DOC-02 (rebinding через тестовый DNS) | P5 | P0 |
| SC-DOC-03 | Denylist IPv4/IPv6 (SECURITY_ARCHITECTURE §9) включая metadata `169.254.169.254`/`fd00:ec2::254`, IPv4-mapped, NAT64, 6to4, Teredo с проверкой встроенного IPv4; запрет внутренних имён и имён сервисов compose | `security.ssrf` | V12 / V5, V1 | PW.5 | TH-25 | Security: ST-DOC-03 (таблица адресов, включая `[::ffff:127.0.0.1]`, десятичные/восьмеричные формы IPv4) | P5 | P0 |
| SC-DOC-04 | Редиректы ≤ 3, каждый проходит полный конвейер, запрет https→http | `security.ssrf` | V12 / V5 | PW.5 | TH-25 | Security: ST-DOC-04 | P5 | P0 |
| SC-DOC-05 | Лимиты: connect 10 с, idle 30 с, total 120 с; 50 MiB потоково (Content-Length не доверяется); MIME allowlist + сверка magic bytes | `security.ssrf`, `code_intelligence.docs` | V12 / V5 | PW.5 | TH-25, TH-26 | Security: ST-DOC-05 | P5 | P0 |
| SC-DOC-06 | Валидация архивов: ≤ 10 000 записей, ≤ 500 MiB распаковано, ratio ≤ 100:1 (на файл и общий, при потоковой распаковке), без вложенных архивов, отказ при абсолютных путях/`..`/symlink/hardlink/device/NUL/коллизиях после NFC; `safe_join` | `code_intelligence.docs`, `security.paths` | V12 / V5 | PW.5 | TH-08, TH-26 | Security: ST-DOC-06 (zip bomb, zip-slip) | P5 | P0 |
| SC-DOC-07 | HTML → текст/Markdown с удалением активного контента и пометкой скрытого текста; PDF и прочие парсеры — в одноразовом контейнере без сети с лимитами | `code_intelligence.docs`, `security.sandbox` | V5, V12 / V1, V5 | PW.5 | TH-02, TH-26 | Security: ST-DOC-07 | P5 | P1 |
| SC-DOC-08 | Режимы `OFF/SMART/STRICT` (ТЗ §12): `SMART` скачивает только после approval `DOCS_DOWNLOAD`; `STRICT` — только утверждённые источники; `trust_level`, `license_info`; Git-источники — shallow, без submodules/LFS/hooks, в sandbox; содержимое для агента всегда fenced | `code_intelligence.docs`, `orchestration.approvals` | V4, V11 / V8, V2 | PW.1 | TH-02 | E2E: ST-DOC-08 (AC-10) | P5 | P0 |

### 2.9 GH — GitHub

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-GH-01 | Webhook: HMAC-SHA256 `X-Hub-Signature-256` по сырому телу до парсинга, `hmac.compare_digest`, два активных секрета при ротации, лимит тела 25 MiB, `401` + запись `signature_valid=false` | `integrations_github` (webhook handler в `apps/api`) | V13 / V4 | PW.5 | TH-27 | Security: ST-GH-01 (webhook spoofing, ТЗ §20) | P8 | P0 |
| SC-GH-02 | Дедупликация `X-GitHub-Delivery` (`delivery_guid UNIQUE`), обработка асинхронно через outbox, идемпотентные обработчики | `integrations_github`, `platform.outbox` | V11, V13 / V2, V4 | PW.5 | TH-28 | Integration: ST-GH-02 | P8 | P0 |
| SC-GH-03 | Payload webhook — подсказка; решения принимаются по состоянию, повторно запрошенному через GitHub API | `integrations_github` | V11 / V2 | PW.1 | TH-27, TH-28 | Integration: ST-GH-03 (подлинный, но устаревший payload не меняет решение) | P8 | P1 |
| SC-GH-04 | GitHub App: `contents:write`, `pull_requests:write`, `checks:read`, `statuses:read`, `metadata:read`; без `workflows`, `administration`, `secrets`, `actions:write`; installation token на репозиторий, TTL ≤ 1 ч, только в памяти, `GIT_ASKPASS`-хелпер | `integrations_github` | V4, V14 / V8, V13 | PO.5, PW.9 | TH-50 | Integration: ST-GH-04 (манифест прав App; токен не попадает в `.git/config`/логи) | P8 | P0 |
| SC-GH-05 | Защищённые действия: push только `aicd/*`, без force-push в чужие ветки, merge только по approval `MERGE` + ledger + branch protection без bypass; изменения CI-конфигурации (`.github/workflows/**` и аналоги) — отдельный approval `MANUAL_REVIEW` до push | `integrations_github`, `orchestration` | V4, V11 / V8, V2 | PW.9 | TH-50 | Integration (opt-in, sandbox-репозиторий): ST-GH-05 | P8 | P0 |
| SC-GH-06 | Ключ App и webhook secret — Docker secrets, доступны только процессам, которым нужны; ротация по runbook/`SECRET_CHANGE` | `integrations_github`, `infrastructure/docker` | V6, V14 / V11, V13 | PO.5 | TH-50, TH-27 | Integration: ST-GH-06 | P8 | P1 |

### 2.10 APR — approvals и опасные действия

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-APR-01 | Таблица опасных действий → `approval_action_type` → минимальная роль (SECURITY_ARCHITECTURE §11) реализуется в policy engine; действие без `APPROVED` approval не исполняется | `orchestration.approvals`, `orchestration` policy engine | V4, V11 / V8, V2 | PW.1 | TH-04, TH-32, TH-50, TH-51, TH-52 | E2E: ST-APR-01 (AC-13) | P3 | P0 |
| SC-APR-02 | Four-eyes для критического набора: `decided_by ≠ requested_by` (для агентских approvals `requested_by` = инициатор run); исключение single-member только по политике, со step-up и пометкой `self_approved`; проверка в сервисе + DB-триггер | `orchestration.approvals`, `infrastructure/migrations` | V4, V11 / V8, V2 | PW.1, PW.5 | TH-33, TH-52 | Unit + Integration: ST-APR-02 | P3 | P0 |
| SC-APR-03 | Привязка approval к `params_hash` действия (PR+`head_sha`, hash миграции, список ресурсов…) и `expires_at`; изменение параметров → approval недействителен (TOCTOU) | `orchestration.approvals`, `orchestration.external_actions` | V11 / V2 | PW.5 | TH-32, TH-50 | Security: ST-APR-03 (push после approve → merge отклонён) | P3 | P0 |
| SC-APR-04 | Все внешние/опасные действия — через ledger `external_actions` (`idempotency_key UNIQUE`, PLANNED→EXECUTING→CONFIRMED/FAILED, fencing token, reconcile запросом внешнего состояния; CONFIRMED не повторяется) (ADR-0012) | `orchestration.external_actions`, `integrations_github` | V11 / V2 | PW.5 | TH-29, TH-32, TH-43, TH-44 | Chaos/recovery: ST-APR-04 (kill worker на каждой фазе; AC-09) | P3 | P0 |
| SC-APR-05 | Независимость ревью вычисляется платформой (`review_independence`: `independent` при другом провайдере; `self_review_fallback` требует approval `SELF_REVIEW_FALLBACK`; `manual`); агенты не могут заявить метку | `orchestration.router`, `verification` | V11 / V2 | PW.7 | TH-01, TH-30, TH-31 | Unit: ST-APR-05; E2E: AC-04 | P4 | P0 |
| SC-APR-06 | Решение по approval — только через аутентифицированный `POST /approvals/{id}/decision` (CSRF, step-up, `If-Match`); директивы `APPROVE`/`REJECT` из чата маршрутизируются в тот же сервис с теми же проверками; ответы агентов не могут создать решение | `apps/api`, `orchestration.approvals`, `apps/web` | V4 / V8 | PW.5 | TH-04, TH-32 | Security: ST-APR-06 | P7 (UI), P3 (API) | P0 |

### 2.11 AUD — аудит

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-AUD-01 | `audit_events` append-only: только `INSERT, SELECT` для ролей приложения, триггер против UPDATE/DELETE, месячные партиции, удаление только `aicd_maintenance` по retention с аудитом; запись в той же транзакции, что и изменение состояния | `platform.audit`, `infrastructure/migrations` | V7 / V16 | PW.1 | TH-34 | Integration: ST-AUD-01 | P1 | P0 |
| SC-AUD-02 | Hash chain (`seq`, `prev_hash`, `hash`) на workspace + ежедневный якорь в WORM (MinIO object lock); `aicd audit verify` | `platform.audit`, `apps/cli` | V7 / V16 | PS.3 | TH-34 | Integration: ST-AUD-02 (изменение строки обнаруживается) | P9 | P2 |
| SC-AUD-03 | Каталог обязательных событий аудита (SECURITY_ARCHITECTURE §12) + тест покрытия: каждая команда API/опасное действие порождает событие (ТЗ §23 «Аудит») | `platform.audit`, все модули | V7 / V16 | PW.1, PW.8 | TH-33, TH-34 | Integration: ST-AUD-03 (по OpenAPI: каждый небезопасный метод → событие) | P1 (каркас), каждая фаза | P0 |
| SC-AUD-04 | Чтение аудита — owner/admin своего workspace; экспорт с redaction; без секретов и кода в `metadata` (allowlist полей) | `apps/api`, `platform.audit` | V4, V7 / V8, V16 | PW.5 | TH-24 | Unit: ST-AUD-04 | P7 | P1 |

### 2.12 SUP — цепочка поставок

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-SUP-01 | Pinning: `uv.lock` с хешами, `pnpm-lock.yaml` + `--frozen-lockfile`, образы по digest, GitHub Actions по commit SHA | Корень репозитория, `infrastructure/docker`, `.github/workflows` | V10, V14 / V15 | PW.4, PO.3 | TH-39 | CI: ST-SUP-01 (проверка, что нет непиннованных Actions/образов) | P1 | P0 |
| SC-SUP-02 | SCA: OSV-Scanner/`pip-audit`/`pnpm audit`, Trivy для образов; блок при `critical`/`high` с доступным исправлением; исключения — с обоснованием и сроком | CI | V10, V14 / V15 | RV.1, PW.4 | TH-39 | CI: ST-SUP-02 | P1 | P0 |
| SC-SUP-03 | SBOM: Syft (SPDX + CycloneDX) для всех образов, прикладывается к релизу как attestation | CI | V10, V14 / V15 | PS.3 | TH-39 | CI: ST-SUP-03 | P9 (пилот — P2 для runner) | P1 |
| SC-SUP-04 | Подпись Cosign (keyless OIDC или key-pair в KMS); проверка при развёртывании (`aicd verify-images`) и перед запуском runner/verify | CI, `apps/cli`, `security.sandbox` | V10, V14 / V15 | PS.2 | TH-39 | Integration: ST-SUP-04 | P9 (runner — P2) | P1 |
| SC-SUP-05 | Provenance: **предлагаемая цель SLSA Build L2** для v1.0 (hosted CI, подписанная provenance), L3 — после v1.0; цель, не заявление о соответствии | CI (reusable workflow) | V10 / V15 | PS.3, PO.3 | TH-39 | CI: ST-SUP-05 (provenance верифицируется `slsa-verifier`/`cosign verify-attestation`) | P9 | P2 |
| SC-SUP-06 | Secure CI: `permissions:` минимальные (`contents: read` по умолчанию), без секретов для PR из форков, запрет `pull_request_target` с checkout кода PR, OIDC вместо долгоживущих ключей, CODEOWNERS для `security/`, `infrastructure/`, `migrations/`, `.github/`, защищённые теги | `.github/`, настройки репозитория | V10, V14 / V15 | PO.3, PO.5, PS.1 | TH-39 | CI: ST-SUP-06 (линтер workflow, например zizmor/actionlint) | P1 | P0 |
| SC-SUP-07 | Код и зависимости репозиториев пользователей считаются враждебными: исполняются только в verification-контейнерах без секретов и сети; prefetch с allowlist реестров; миграции валидируются только в эфемерной БД | `verification`, `security.sandbox` | V10 / V15 | PO.5, PW.8 | TH-38, TH-51 | Security: ST-SUP-07 (пакет с postinstall-эксфильтрацией) | P6 | P0 |
| SC-SUP-08 | Политика обновлений: Renovate/Dependabot еженедельно; SLA уязвимостей critical 7 дней, high 30 дней, medium — следующий релиз; cooldown 7 дней для не-security обновлений | `.github/`, процесс | V10, V14 / V15 | RV.1, RV.2 | TH-39 | Процесс: ST-SUP-08 (отчёт возраста зависимостей) | P1 | P1 |

### 2.13 DATA — данные и целостность workflow

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-DATA-01 | Классификация данных (S/C/I/P/U, SECURITY_ARCHITECTURE §15) и правила обращения по классам; класс указан в DATABASE_DESIGN для каждой таблицы | docs, `infrastructure/migrations` (комментарии) | V8 / V14 | PO.1 | TH-14, TH-48 | Review: ST-DATA-01 (каждая таблица имеет класс) | P0–P1 | P1 |
| SC-DATA-02 | Шифрование at rest: рекомендация дискового шифрования хоста (включая cred-тома и worktrees), MinIO SSE (KES) или шифрованный том; проверка в `aicd doctor` с предупреждением | runbooks, `apps/cli` | V6, V8 / V11, V14 | PO.5 | TH-47 | Integration: ST-DATA-02 (doctor) | P9 | P1 |
| SC-DATA-03 | Бэкапы PG (pgBackRest/WAL, AES-256) и MinIO (versioning + mirror), ключи отдельно; тома учётных данных CLI исключены; ежемесячный тест восстановления (ТЗ §16) | `infrastructure`, runbooks | V8 / V14 | PO.5, PS.3 | TH-13, TH-47 | Chaos/recovery: ST-DATA-03 (restore drill, DR в P9) | P9 (базовый бэкап — P1) | P0 |
| SC-DATA-04 | Retention (raw logs 30 д, `run_event_log` 90 д, audit 1 год — предложение) и удаление через `DATA_DELETION`; задачи retention под `aicd_maintenance` с аудитом | `platform`, `orchestration` | V8 / V14 | PO.1 | TH-48 | Integration: ST-DATA-04 | P9 | P1 |
| SC-DATA-05 | Целостность артефактов/evidence: `sha256` при записи, проверка при чтении evidence, неизменяемые ключи, опционально object lock; evidence содержит commit, команду, exit code, время, checksum (AC-14) | `platform.artifacts`, `verification` | V8, V10 / V14, V15 | PS.3, PW.8 | TH-30 | Integration: ST-DATA-05 (подмена объекта обнаруживается) | P6 | P0 |
| SC-DATA-06 | Безопасная выдача артефактов: `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, корректный `Content-Type`, HTML-артефакты не рендерятся inline (или в sandboxed iframe с CSP `sandbox`) | `apps/api` | V12, V14 / V5, V3 | PW.5 | TH-22 | Security: ST-DATA-06 | P7 | P1 |
| SC-DATA-07 | Минимизация ПДн: только e-mail, имя, IP/UA в сессиях и auth-аудите; IP/UA удаляются через 30 дней после истечения сессии; анонимизация удалённых пользователей | `identity`, `platform.audit` | V8 / V14 | PO.1 | TH-48 | Integration: ST-DATA-07 | P1 | P1 |
| SC-DATA-08 | Leases с fencing token: каждая запись результата и каждое внешнее действие проверяют текущий `fencing_token` (`UPDATE … WHERE fencing_token = $t`); устаревший воркер получает отказ и прекращает работу (ADR-0007) | `platform` (`worker_leases`), `orchestration` | V11 / V2 | PW.5 | TH-29, TH-44 | Chaos/recovery: ST-DATA-08 (lost lease, ТЗ §20) | P3 | P0 |
| SC-DATA-09 | Сообщения очереди и pub/sub — только подсказки: несут ID + `event_id`; потребитель перечитывает состояние из PG, проверяет допустимость FSM-перехода, дедуплицирует через `processed_events`/`message_deliveries`; SSE дедуплицируется по `seq` | `platform.outbox`, все consumers, `apps/api` SSE | V11 / V2 | PW.5 | TH-28, TH-29, TH-42, TH-43 | Chaos/recovery: ST-DATA-09 (duplicate message, Redis restart, поддельное сообщение) | P3 | P0 |

### 2.14 OBS — наблюдаемость безопасности

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-OBS-01 | Структурированные JSON-логи с корреляцией `run_id/task_id/execution_id/workspace_id/request_id`; тела запросов и заголовки `Cookie`/`Authorization`/`X-CSRF-Token` не логируются; redaction-фильтр в форматтере | `platform.telemetry` | V7 / V16 | PW.5 | TH-14 | Unit: ST-OBS-01 | P1 | P0 |
| SC-OBS-02 | Security-алерты: всплеск неудачных входов/блокировок, `tenant_denied_total`, `egress_denied_total`, `SANDBOX_DENIED`, `webhook_signature_invalid_total`, нарушения four-eyes, подключения `aicd_maintenance`, `redaction_failures_total`, stuck leases, сбои бэкапов (ТЗ §16) | `infrastructure/monitoring` | V7 / V16 | RV.1 | TH-16, TH-23, TH-27, TH-33, TH-34 | Integration: ST-OBS-02 (синтетические события → алерт) | P9 (метрики — по фазам) | P1 |
| SC-OBS-03 | Гигиена телеметрии: allowlist атрибутов OTel (без промптов, кода, вывода CLI, e-mail), ограничение кардинальности меток; Prometheus/Grafana только за reverse-proxy с аутентификацией | `platform.telemetry`, `infrastructure/monitoring` | V7, V8 / V16, V14 | PW.9 | TH-48 | Integration: ST-OBS-03 | P1 | P1 |

### 2.15 API — HTTP API и SSE

| ID | Контроль | Реализация | ASVS 4.0.3 / 5.0 | SSDF | Угрозы | Проверка | Фаза | Приор. |
|---|---|---|---|---|---|---|---|---|
| SC-API-01 | Строгая валидация: Pydantic v2 strict, `extra="forbid"`, лимит тела 1 MiB (chat ≤ 16 KiB, директива ≤ 16 KiB), UUID/enum/regex на все идентификаторы и пути | `apps/api`, `contracts` | V5, V13 / V2, V4 | PW.5 | TH-06, TH-08, TH-24 | Contract: ST-API-01 (negative tests, ТЗ §20, AC-15) | P1 | P0 |
| SC-API-02 | Rate limiting (Redis token bucket) по пользователю/IP/workspace: login, `POST /runs`, директивы, chat, probe, docs refresh; `429` + `Retry-After` | `apps/api` | V11, V13 / V2, V4 | PW.5 | TH-23, TH-35 | Integration: ST-API-02 | P1 | P0 |
| SC-API-03 | Квоты и лимиты ресурсов: одновременные runs на workspace, задачи в DAG (≤ 200), глубина replan, SSE-соединения на пользователя (≤ 10) и процесс, лимиты вывода CLI/артефактов, размеры индекса (файл ≤ 2 MiB для парсинга), справедливое распределение квоты провайдера | `apps/api`, `orchestration`, `cli_adapters`, `code_intelligence` | V11 / V2 | PW.5 | TH-05, TH-35, TH-36, TH-37, TH-45, TH-49 | Performance: ST-API-03 (concurrent runs, SSE fan-out) | P3 | P1 |
| SC-API-04 | Ошибки RFC 7807 без трассировок, SQL, путей и внутренних ID; `correlation_id` для поддержки | `apps/api` | V7 / V16 | PW.5 | TH-14 | Contract: ST-API-04 | P1 | P0 |
| SC-API-05 | `Idempotency-Key`: область `(workspace_id, user_id, key)`, привязка к `request_hash` (несовпадение → `422`), срок хранения, ответ из `idempotency_keys` | `platform` (`idempotency_keys`), `apps/api` | V11, V13 / V2, V4 | PW.5 | TH-29, TH-35 | Contract: ST-API-05 (idempotency, ТЗ §20) | P1 | P0 |
| SC-API-06 | Оптимистическая конкуренция: `ETag`/`If-Match` (`version`) на PATCH и критических командах (approval decision, директивы с `expected_run_version`) | `apps/api`, сервисы | V11 / V2 | PW.5 | TH-32 | Contract: ST-API-06 | P1 | P1 |
| SC-API-07 | SSE: authz при подключении (членство в workspace run'а); повторная проверка каждые 60 с и по событию `member.removed/role_changed` с закрытием потока; `Last-Event-ID` валиден только в пределах `run_id`; heartbeat, idle-timeout, лимит соединений | `apps/api` (SSE) | V4, V13 / V8, V4 | PW.5 | TH-18, TH-36 | Security: ST-API-07; E2E: AC-08 | P3 | P0 |
| SC-API-08 | Заголовки безопасности: CSP `default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; img-src 'self' data:; connect-src 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` минимальная, `Cross-Origin-Opener-Policy: same-origin`; без CORS | `infrastructure/docker` (reverse-proxy), `apps/api` | V14 / V3 | PW.9 | TH-20, TH-21, TH-22 | Integration: ST-API-08 | P1 | P0 |
| SC-API-09 | Защита от mass assignment: отдельные request/response-схемы; поля `role`, `status`, `requested_by`, `workspace_id`, `decided_by` никогда не принимаются из тела; response-модели фильтруют чувствительные поля | `apps/api`, `contracts` | V4, V5, V13 / V8, V2, V4 | PW.5 | TH-24 | Contract: ST-API-09 | P1 | P0 |

**Итого: 110 контролей** (IAM 9, TEN 8, SBX 12, CLI 9, SEC 7, INJ 7, NET 5, DOC 8, GH 6, APR 6, AUD 4, SUP 8, DATA 9, OBS 3, API 9); из них `P0` — 80, `P1` — 27, `P2` — 3 (подсчёт подлежит пересчёту при изменении каталога).

---

## 3. Сводка по фазам

| Фаза | Контроли (основная реализация) |
|---|---|
| P0 | SC-DATA-01 (классификация в документах) |
| P1 | SC-IAM-01…07, 09; SC-TEN-01…05, 08; SC-SEC-01, 02, 07; SC-NET-02…04; SC-AUD-01, 03; SC-SUP-01, 02, 06, 08; SC-DATA-07; SC-OBS-01, 03; SC-API-01, 02, 04…06, 08, 09; базовые правила SC-INJ-06 |
| P2 | SC-SBX-01…08, 10…12; SC-CLI-01…09; SC-SEC-03; SC-INJ-07; SC-NET-01, 05; пилот SBOM/подписи runner-образов |
| P3 | SC-IAM-08; SC-TEN-06; SC-INJ-02, 05 (backend); SC-APR-01…04, 06 (API); SC-DATA-08, 09; SC-API-03, 07 |
| P4 | SC-INJ-01, 03; SC-APR-05 |
| P5 | SC-TEN-07; SC-SEC-05; SC-DOC-01…08 |
| P6 | SC-SBX-07; SC-SEC-04; SC-INJ-04; SC-SUP-07; SC-DATA-05 |
| P7 | SC-INJ-05/06 (UI); SC-APR-06 (UI); SC-AUD-04; SC-DATA-06 |
| P8 | SC-GH-01…06; SC-SEC-06 |
| P9 | SC-SBX-09; SC-AUD-02; SC-SUP-03…05; SC-NET-03 (внутренний TLS); SC-DATA-02…04; SC-OBS-02; финальная проверка всех контролей |

---

## 4. Release security gate (чек-лист выпуска v1.0)

Выпуск разрешается только при выполнении **всех** пунктов; каждый пункт подтверждается evidence (ссылка на артефакт CI/отчёт, commit SHA, дата), а не заявлением (ТЗ §21 DoD, §23).

| # | Критерий | Evidence |
|---|---|---|
| RG-01 | Нет открытых findings `critical`; `high` — только `ACCEPTED_RISK` с подписью owner и сроком исправления | Отчёт findings на release commit |
| RG-02 | Все контроли `P0` реализованы, их тесты `ST-*` проходят на release commit; нереализованные `P1` перечислены с обоснованием и утверждены владельцем | Матрица контроль → тест → результат |
| RG-03 | Security-тесты ТЗ §20 (SSRF, prompt injection, path traversal, command injection, cross-tenant access, secret leakage, webhook spoofing) — 100% PASS; `SKIPPED` не засчитывается | Отчёт `tests/security` |
| RG-04 | Cross-tenant suite по всем операциям OpenAPI — все обращения к чужим ресурсам возвращают `404` (ТЗ §23 «Изоляция») | Отчёт ST-TEN-04 |
| RG-05 | Тесты отсутствия секретов в логах/SSE/артефактах/context packs/бэкапах PASS; gitleaks по репозиторию платформы — 0 находок | ST-SEC-03/04/05, CI |
| RG-06 | SCA/Trivy: нет `critical`/`high` с доступным исправлением без утверждённого исключения | Отчёты CI |
| RG-07 | SAST (Semgrep + линтеры безопасности): нет неразобранных находок `high`+ | Отчёт CI |
| RG-08 | SBOM сформирован для всех образов; образы подписаны; подписи и provenance проверяются скриптом развёртывания | Attestations |
| RG-09 | Fault-injection/recovery: нет повторных опасных действий, ledger сходится (AC-09); lost lease, duplicate message, Redis restart PASS | Отчёт chaos suite |
| RG-10 | Все опасные действия gated (AC-13): тест каждого `approval_action_type` из таблицы §11 SECURITY_ARCHITECTURE, включая four-eyes и TOCTOU | ST-APR-01…04 |
| RG-11 | Threat model пересмотрен на финальной архитектуре; все угрозы с residual `high`/`critical` имеют решение владельца | Обновлённый THREAT_MODEL.md с датой ревью |
| RG-12 | Конфигурация sandbox на целевом профиле развёртывания проверена (ST-SBX-01, ST-SBX-05, ST-NET-01/02) | Отчёт `aicd doctor` + тесты |
| RG-13 | Бэкап и восстановление проверены на release candidate; бэкапы зашифрованы и не содержат учётных данных CLI | ST-DATA-03 |
| RG-14 | Runbooks: incident response, ротация секретов, повторная авторизация CLI, восстановление — существуют и проверены табличным упражнением | `docs/runbooks/` |
| RG-15 | Нет заявлений о сертификации/соответствии стандартам в продукте и документации (ТЗ §1, §13) | Review документации |

---

## 5. Secure SDLC

| Практика | Решение | SSDF |
|---|---|---|
| Требования безопасности | Контроли этого каталога → задачи backlog с DoD; каждая фаза закрывается проверкой контролей своей фазы | PO.1, PO.4 |
| Threat modeling | Обновление THREAT_MODEL.md при изменении границ доверия, новых интеграциях, в конце каждой фазы; обязательный пункт шаблона ADR «Последствия для безопасности» | PW.1, PW.2 |
| Code review | Все изменения — через PR; ≥ 1 одобрение; для путей CODEOWNERS (`packages/security`, `packages/identity`, `infrastructure/`, `migrations/`, `.github/`, `apps/api` middleware) — одобрение security-владельца; автор не может одобрить свой PR; AI-сгенерированные изменения проходят тот же процесс | PW.7 |
| SAST | Semgrep (Python, TS, Dockerfile, правила для FastAPI/SQLAlchemy/React), Ruff `S`-правила (bandit), ESLint security; `import-linter` для границ модулей; линтеры: запрет `shell=True`, `SET` без `LOCAL`, прямого HTTP-клиента вне `security.ssrf`, `subprocess` вне `cli_adapters`/`verification`/`security` | PW.7, PO.3 |
| SCA | См. SC-SUP-02; лицензии зависимостей — проверка allowlist | PW.4, RV.1 |
| Secret scanning | Gitleaks в pre-commit (`pre-commit` hook) и в CI на каждом PR и по всей истории еженедельно; GitHub secret scanning + push protection, если доступны | PW.5, PS.1 |
| Branch protection | `main` защищена: обязательные статусы CI (тесты, SAST, SCA, secrets, contract), обязательное ревью, запрет force-push и удаления, linear history, подписанные коммиты (рекомендуется), обязательное обновление ветки перед merge | PS.1 |
| Тестирование | Unit/contract/integration/security в каждом PR; chaos/performance — nightly и перед релизом; тесты с реальными CLI — opt-in (ТЗ §20) | PW.8 |
| Безопасные значения по умолчанию | Docs `SMART` без скачивания без approval, local fallback выключен, egress только к провайдеру, auto-merge запрещён, debug выключен | PW.9 |
| Обновление зависимостей | SC-SUP-08 | RV.1, RV.2 |
| Управление уязвимостями | Приём сообщений (`SECURITY.md` с контактом), триаж ≤ 3 рабочих дня, SLA по SC-SUP-08, анализ первопричин для `high`+ с добавлением регрессионного теста | RV.1, RV.2, RV.3 |
| Защита релизов | Подписанные образы и теги, SBOM, provenance, хранение артефактов релиза | PS.2, PS.3 |
| Окружения | CI не имеет доступа к production-подобным данным; секреты CI — через OIDC/environments с обязательным ревью | PO.5 |

---

## 6. Предлагаемые дополнения

- Идентификаторы тестов безопасности `ST-<AREA>-NN` (номер совпадает с контролем; суффикс `b` — дополнительный тест того же контроля) — не входят в таблицу соглашений docs/README.md §2; предлагается добавить.
- Идентификаторы критериев release gate `RG-NN`.
- Поле `review_independence` (`independent`, `self_review_fallback`, `manual`) и прочие дополнения схемы — см. SECURITY_ARCHITECTURE §17.
