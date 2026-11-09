# SECURITY_REPORT — security suite AiCodingDev (P9-02)

**Сгенерировано:** 2026-11-08 18:10 UTC скриптом `scripts/security_report.py` из junit-XML фактического прогона (`reports/security/junit.xml`). Не редактировать вручную.
**Команда прогона:** `uv run pytest -m security --junitxml=reports/security/junit.xml`
**Сопоставление:** `tests/security/catalog.py` (контроль → тесты); описание контролей — [SECURITY_CONTROLS.md](../../docs/security/SECURITY_CONTROLS.md), угрозы — [THREAT_MODEL.md](../../docs/security/THREAT_MODEL.md).

## 1. Итоги прогона

| Тестов | passed | failed | error | skipped | Время, с |
|---|---|---|---|---|---|
| 1745 | 1744 | 0 | 0 | 1 | 338 |

| Статус контроля | Количество |
|---|---|
| PASS | 101 |
| MANUAL | 9 |
| **Всего контролей** | **110** |

Статусы: PASS — все сопоставленные тесты прошли; PARTIAL-SKIP — часть пропущена (например, нет Docker); SKIP — все пропущены; FAIL — есть упавшие; NOT RUN — тесты не выполнялись в этом прогоне; MANUAL — автоматического теста нет (организационный/инфраструктурный контроль, см. примечание). Вид `partial` — тесты покрывают часть контроля, остаток — в примечании.

## 2. Категории ТЗ §20 «Security»

| Категория | Контроли | Тестов | Результат |
|---|---|---|---|
| SSRF | SC-NET-01, SC-NET-05, SC-DOC-01, SC-DOC-02, SC-DOC-03, SC-DOC-04, SC-DOC-05 | 91 | PASS |
| prompt injection | SC-INJ-01, SC-INJ-02, SC-INJ-03, SC-INJ-04, SC-INJ-05, SC-DOC-07 | 741 | PASS |
| path traversal | SC-TEN-05, SC-SBX-12, SC-DOC-06 | 66 | PASS |
| command injection | SC-SBX-11, SC-CLI-01, SC-CLI-02, SC-INJ-07 | 49 | PASS |
| cross-tenant | SC-TEN-01, SC-TEN-03, SC-TEN-04, SC-TEN-05, SC-API-07 | 131 | PASS |
| secret leakage | SC-SEC-01, SC-SEC-03, SC-SEC-04, SC-SEC-05, SC-OBS-01, SC-OBS-03, SC-API-04 | 26 | PASS |
| webhook spoofing | SC-GH-01, SC-GH-02, SC-GH-03 | 4 | PASS |

## 3. Контроль → тесты → результат

| Контроль | Содержание | Вид | Тестов (pass/fail/skip) | Результат | Примечание |
|---|---|---|---|---|---|
| SC-IAM-01 | Хеширование паролей argon2id (m=64 MiB, t=3, p=1, соль 16 Б), PHC-строка, rehash при входе; семафор ≤4 паралле… | automated | 1 (1/0/0) | **PASS** |  |
| SC-IAM-02 | Политика паролей: 12–128 символов, NFKC, офлайн-список скомпрометированных паролей, без правил состава | automated | 5 (5/0/0) | **PASS** |  |
| SC-IAM-03 | Серверные сессии: токен 32 Б CSPRNG, в БД только SHA-256; cookie __Host-aicd_session Secure; HttpOnly; SameSit… | automated | 3 (3/0/0) | **PASS** |  |
| SC-IAM-04 | Ротация сессии при входе, смене пароля/роли, step-up; отзыв всех сессий при смене пароля; logout = revoked_at;… | partial | 2 (2/0/0) | **PASS** | Ротация при входе и step-up, отзыв при logout проверены; смены пароля и принудительного завершения сессий owner/admin в API v1 нет (функции отсутствуют, OQ-04). |
| SC-IAM-05 | CSRF: синхронизирующий токен (hash в user_sessions.csrf_token_hash) через cookie __Host-aicd_csrf + заголовок … | automated | 3 (3/0/0) | **PASS** |  |
| SC-IAM-06 | Защита от подбора: блокировка учётной записи после 10 неудач (экспонента, max 24 ч), лимиты на IP и (аккаунт, … | automated | 4 (4/0/0) | **PASS** |  |
| SC-IAM-07 | Единый PDP identity.api.authorize(actor, permission, resource), deny-by-default; матрица прав в коде как единс… | automated | 201 (201/0/0) | **PASS** |  |
| SC-IAM-08 | Step-up re-auth (повторный пароль, если > 10 мин) для критических approvals, управления участниками, политик б… | automated | 5 (5/0/0) | **PASS** |  |
| SC-IAM-09 | Bootstrap первого owner только командой aicd admin create-owner; нет учётных записей/паролей по умолчанию; сам… | automated | 1 (1/0/0) | **PASS** |  |
| SC-TEN-01 | RLS ENABLE + FORCE на всех tenant-таблицах; политика USING/WITH CHECK по app.workspace_id; роли aicd_app/aicd_… | automated | 35 (35/0/0) | **PASS** |  |
| SC-TEN-02 | platform.db.transaction(ctx) выполняет SET LOCAL app.workspace_id/user_id/actor_type в каждой транзакции; запр… | partial | 15 (15/0/0) | **PASS** | SET LOCAL в каждой транзакции и fail-closed без контекста проверены; линтер «SET без LOCAL» не реализован. |
| SC-TEN-03 | Denormalized workspace_id + composite FK (workspace_id, parent_id) → parent(workspace_id, id) на всех tenant-с… | automated | 2 (2/0/0) | **PASS** |  |
| SC-TEN-04 | Чужой/несуществующий ресурс → одинаковый 404 problem+json (тело, время ответа в пределах шума), включая SSE, a… | automated | 86 (86/0/0) | **PASS** |  |
| SC-TEN-05 | Ключи объектов ws/<workspace_id>/<project_id>/<artifact_id>; доступ к артефактам только через API после провер… | automated | 8 (8/0/0) | **PASS** |  |
| SC-TEN-06 | Redis: префиксы ключей с workspace_id; pub/sub несёт только (run_id, seq); данные клиенту — только из run_even… | partial | 2 (2/0/0) | **PASS** | Данные клиенту — только из run_event_log после authz (проверено); префиксы ключей Redis по workspace тестом не проверяются. |
| SC-TEN-07 | Ключи кэшей context packs/индекса включают workspace_id; межтенантная дедупликация по content hash запрещена (… | automated | 2 (2/0/0) | **PASS** |  |
| SC-TEN-08 | aicd_maintenance (BYPASSRLS) — отдельные учётные данные, только задачи retention/backup, каждое подключение ау… | automated | 2 (2/0/0) | **PASS** |  |
| SC-SBX-01 | Runner-контейнер: non-root UID 10001, без --privileged, --cap-drop=ALL, no-new-privileges, seccomp/AppArmor de… | automated | 3 (3/0/0) | **PASS** |  |
| SC-SBX-02 | Mounts: только worktree задачи rw, context pack ro, том учётных данных провайдера; общий .git ro, запись объек… | automated | 5 (5/0/0) | **PASS** |  |
| SC-SBX-03 | Лимиты: --pids-limit 512, memory 4g без swap, --cpus 2, nofile 4096, wall-clock timeout, лимит вывода, квота р… | automated | 5 (5/0/0) | **PASS** |  |
| SC-SBX-04 | Завершение дерева процессов: docker stop -t 10 → SIGKILL namespace → rm -f; метки aicd.execution_id; reconcile… | automated | 3 (3/0/0) | **PASS** |  |
| SC-SBX-05 | Docker только через docker-socket-proxy (разрешены CONTAINERS, IMAGES inspect, INFO, VERSION, PING; запрещены … | automated | 55 (55/0/0) | **PASS** |  |
| SC-SBX-06 | Сеть runner: Docker-сеть sandbox internal: true, выход только через egress-proxy; внешний DNS недоступен | automated | 3 (3/0/0) | **PASS** |  |
| SC-SBX-07 | Verification-контейнер: отдельный образ, без учётных данных CLI/секретов/GitHub-токенов, --network none, код —… | automated | 11 (11/0/0) | **PASS** |  |
| SC-SBX-08 | Образы runner/verify pinned по digest, собраны в CI, подписаны Cosign; подпись и digest проверяются worker-exe… | partial | 2 (2/0/0) | **PASS** | Pinning по digest проверен; подпись Cosign runner-образов при запуске не проверяется (P9 ops, OQ). |
| SC-SBX-09 | Опция усиленного runtime: rootless Docker / userns-remap, gVisor runsc (sandbox.runtime); матрица совместимост… | manual | 0 | **MANUAL** | gVisor/rootless — опция профиля server, opt-in; автоматического теста нет. |
| SC-SBX-10 | Local fallback выключен по умолчанию; включение = политика проекта + approval LOCAL_EXECUTION_FALLBACK (owner,… | partial | 2 (2/0/0) | **PASS** | Local fallback без approval — запрет конфигурацией; E2E AC с approval LOCAL_EXECUTION_FALLBACK не автоматизирован. |
| SC-SBX-11 | Безопасные платформенные git-операции над недоверенными worktree: GIT_CONFIG_NOSYSTEM, GIT_CONFIG_GLOBAL=/dev/… | automated | 4 (4/0/0) | **PASS** |  |
| SC-SBX-12 | Path guard security.paths.safe_join(): realpath + проверка префикса, O_NOFOLLOW, запрет .., абсолютных путей, … | automated | 30 (30/0/0) | **PASS** |  |
| SC-CLI-01 | Только allowlisted адаптеры codex/claude/fake; запуск argv-массивом без shell по command_profile-шаблонам, выб… | automated | 6 (6/0/0) | **PASS** |  |
| SC-CLI-02 | Недоверенный текст (objective, инструкции, контекст) передаётся только через stdin или файл в /context, никогд… | automated | 6 (6/0/0) | **PASS** |  |
| SC-CLI-03 | Том aicd-cred-<provider> монтируется только в runner того же провайдера, права 0700 UID runner; никогда — в ve… | partial | 2 (2/0/0) | **PASS** | Canary-файл в томе учётных данных реального провайдера — opt-in suite реальных CLI. |
| SC-CLI-04 | Авторизация CLI — только официальным способом CLI: aicd cli login <provider> в одноразовом контейнере с TTY оп… | automated | 2 (2/0/0) | **PASS** |  |
| SC-CLI-05 | Соблюдение лимитов подписок: RATE_LIMITED → RETRY_WAIT/BLOCKED (PROVIDER_UNAVAILABLE) без переключения аккаунт… | automated | 2 (2/0/0) | **PASS** |  |
| SC-CLI-06 | Вывод CLI — недоверенный: потоковый парсер с лимитами (строка ≤ 64 KiB, событие cli.output ≤ 4 KiB, raw ≤ 50 M… | automated | 23 (23/0/0) | **PASS** |  |
| SC-CLI-07 | Defense in depth: адаптер включает наиболее строгий неинтерактивный режим разрешений/sandbox самого CLI, досту… | manual | 0 | **MANUAL** | Строгий режим разрешений реального CLI — opt-in suite реальных CLI (подписки). |
| SC-CLI-08 | CLI pinned версии в runner-образе из официального канала с проверкой integrity; compatibility matrix; неизвест… | automated | 1 (1/0/0) | **PASS** |  |
| SC-CLI-09 | Только официальные документированные события CLI; без перехвата/MITM трафика CLI (egress-proxy — CONNECT-тунне… | partial | 1 (1/0/0) | **PASS** | Только документированные события — ревью шаблонов и адаптеров. |
| SC-SEC-01 | Нет секретов в PG: CI-линтер имён колонок миграций (allowlist password_hash, token_hash, csrf_token_hash); тес… | automated | 2 (2/0/0) | **PASS** |  |
| SC-SEC-02 | Секреты приложения — Docker secrets (/run/secrets/*), загрузка platform.settings как SecretStr, регистрация в … | automated | 2 (2/0/0) | **PASS** |  |
| SC-SEC-03 | Redaction pipeline security.redaction на всех выходах (логи, SSE/run_event_log, ACP/chat, artifacts, context p… | automated | 17 (17/0/0) | **PASS** |  |
| SC-SEC-04 | Secret scan каждого diff перед коммитом/push (gitleaks + отпечатки учётных данных CLI); находка → finding crit… | automated | 3 (3/0/0) | **PASS** |  |
| SC-SEC-05 | Исключение чувствительных файлов из индекса и context packs: .env*, *.pem, *.key, id_*, *.p12, credentials*.js… | automated | 1 (1/0/0) | **PASS** |  |
| SC-SEC-06 | Ротация секретов: runbook; изменение управляемых платформой секретов — approval SECRET_CHANGE (owner, four-eye… | manual | 0 | **MANUAL** | Runbook ротации секретов (ops); approval SECRET_CHANGE — правило four-eyes (SC-APR-02). |
| SC-SEC-07 | Нет core dumps (RLIMIT_CORE=0), debug-режим запрещён в prod (проверка при старте), без трассировок в ответах | partial | 1 (1/0/0) | **PASS** | Без трассировок в ответах — проверено; RLIMIT_CORE и запрет debug в prod — конфигурация образа. |
| SC-INJ-01 | Fencing недоверенных данных: блоки с меткой происхождения и случайным nonce на исполнение, удаление совпадающи… | automated | 74 (74/0/0) | **PASS** |  |
| SC-INJ-02 | Политика вне контроля модели: права (network, write_scope, production_access=false), инструменты, approvals бе… | automated | 54 (54/0/0) | **PASS** |  |
| SC-INJ-03 | ACP: JSON Schema по типу, additionalProperties:false, ≤ 32 KiB, ACL (sender_role, recipient_role, type), отпра… | automated | 577 (577/0/0) | **PASS** |  |
| SC-INJ-04 | Claims ≠ evidence: claims, TASK_COMPLETED, exit code 0 только запускают проверку; VERIFIED/PASS выставляет Ver… | automated | 85 (85/0/0) | **PASS** |  |
| SC-INJ-05 | Директивы: тип выбирается в UI из directive_type, не выводится моделью; RBAC; impact analysis; подтверждение к… | automated | 2 (2/0/0) | **PASS** |  |
| SC-INJ-06 | Безопасное отображение: React-экранирование, Markdown без raw HTML через санитайзер, удаление ANSI, ссылки rel… | partial | 19 (19/0/0) | **PASS** | Экранирование в UI — unit-тесты web (vitest) и React; XSS-корпус через e2e браузера не автоматизирован. |
| SC-INJ-07 | Платформенные команды (git, gates): argv без shell, git check-ref-format для веток, regex для SHA (^[0-9a-f]{4… | automated | 33 (33/0/0) | **PASS** |  |
| SC-NET-01 | Egress-proxy с allowlist по классу контейнера: runner-codex/runner-claude — только эндпоинты своего провайдера… | automated | 1 (1/0/0) | **PASS** |  |
| SC-NET-02 | Сегментация: сети edge (proxy↔api), backend (api/workers↔PG/Redis/MinIO), sandbox (internal), docker-ctl (exec… | partial | 1 (1/0/0) | **PASS** | Матрица достижимости сетей compose — ops/smoke профиля server. |
| SC-NET-03 | TLS на edge (1.2+/1.3, HSTS); внутренний TLS опционален на одном хосте, обязателен при многохостовом развёртыв… | manual | 0 | **MANUAL** | TLS/HSTS на edge — конфигурация reverse-proxy (ops). |
| SC-NET-04 | Hardening хранилищ: Redis ACL-пользователи по процессу, requirepass, отключение FLUSHALL/FLUSHDB/CONFIG/DEBUG/… | manual | 0 | **MANUAL** | Redis ACL/requirepass, PG TLS — конфигурация развёртывания (ops). |
| SC-NET-05 | DNS: runner/verify не имеют внешнего резолвера (только embedded Docker DNS для egress-proxy); proxy резолвит с… | automated | 1 (1/0/0) | **PASS** |  |
| SC-DOC-01 | Только https (Git — https без userinfo), порт 443 (allowlist admin), запрет userinfo в URL; единая точка secur… | automated | 36 (36/0/0) | **PASS** |  |
| SC-DOC-02 | DNS resolve всех A/AAAA + проверка каждого + соединение с pinned IP (SNI/Host исходного имени) — защита от DNS… | automated | 3 (3/0/0) | **PASS** |  |
| SC-DOC-03 | Denylist IPv4/IPv6 (SECURITY_ARCHITECTURE §9) включая metadata 169.254.169.254/fd00:ec2::254, IPv4-mapped, NAT… | automated | 31 (31/0/0) | **PASS** |  |
| SC-DOC-04 | Редиректы ≤ 3, каждый проходит полный конвейер, запрет https→http | automated | 9 (9/0/0) | **PASS** |  |
| SC-DOC-05 | Лимиты: connect 10 с, idle 30 с, total 120 с; 50 MiB потоково (Content-Length не доверяется); MIME allowlist +… | automated | 10 (10/0/0) | **PASS** |  |
| SC-DOC-06 | Валидация архивов: ≤ 10 000 записей, ≤ 500 MiB распаковано, ratio ≤ 100:1 (на файл и общий, при потоковой расп… | automated | 28 (28/0/0) | **PASS** |  |
| SC-DOC-07 | HTML → текст/Markdown с удалением активного контента и пометкой скрытого текста; PDF и прочие парсеры — в одно… | automated | 2 (2/0/0) | **PASS** |  |
| SC-DOC-08 | Режимы OFF/SMART/STRICT (ТЗ §12): SMART скачивает только после approval DOCS_DOWNLOAD; STRICT — только утвержд… | automated | 48 (48/0/0) | **PASS** |  |
| SC-GH-01 | Webhook: HMAC-SHA256 X-Hub-Signature-256 по сырому телу до парсинга, hmac.compare_digest, два активных секрета… | automated | 3 (3/0/0) | **PASS** |  |
| SC-GH-02 | Дедупликация X-GitHub-Delivery (delivery_guid UNIQUE), обработка асинхронно через outbox, идемпотентные обрабо… | automated | 2 (2/0/0) | **PASS** |  |
| SC-GH-03 | Payload webhook — подсказка; решения принимаются по состоянию, повторно запрошенному через GitHub API | automated | 1 (1/0/0) | **PASS** |  |
| SC-GH-04 | GitHub App: contents:write, pull_requests:write, checks:read, statuses:read, metadata:read; без workflows, adm… | partial | 4 (4/0/0) | **PASS** | Отступление OQ-47 (1): `statuses: write` вместо `read` — принято владельцем. |
| SC-GH-05 | Защищённые действия: push только aicd/*, без force-push в чужие ветки, merge только по approval MERGE + ledger… | automated | 3 (3/0/0) | **PASS** |  |
| SC-GH-06 | Ключ App и webhook secret — Docker secrets, доступны только процессам, которым нужны; ротация по runbook/SECRE… | automated | 3 (3/0/0) | **PASS** |  |
| SC-APR-01 | Таблица опасных действий → approval_action_type → минимальная роль (SECURITY_ARCHITECTURE §11) реализуется в p… | automated | 62 (62/0/0) | **PASS** |  |
| SC-APR-02 | Four-eyes для критического набора: decided_by ≠ requested_by (для агентских approvals requested_by = инициатор… | automated | 19 (19/0/0) | **PASS** |  |
| SC-APR-03 | Привязка approval к params_hash действия (PR+head_sha, hash миграции, список ресурсов…) и expires_at; изменени… | automated | 3 (3/0/0) | **PASS** |  |
| SC-APR-04 | Все внешние/опасные действия — через ledger external_actions (idempotency_key UNIQUE, PLANNED→EXECUTING→CONFIR… | automated | 16 (16/0/0) | **PASS** |  |
| SC-APR-05 | Независимость ревью вычисляется платформой (review_independence: independent при другом провайдере; self_revie… | automated | 4 (4/0/0) | **PASS** |  |
| SC-APR-06 | Решение по approval — только через аутентифицированный POST /approvals/{id}/decision (CSRF, step-up, If-Match)… | automated | 2 (2/0/0) | **PASS** |  |
| SC-AUD-01 | audit_events append-only: только INSERT, SELECT для ролей приложения, триггер против UPDATE/DELETE, месячные п… | automated | 1 (1/0/0) | **PASS** |  |
| SC-AUD-02 | Hash chain (seq, prev_hash, hash) на workspace + ежедневный якорь в WORM (MinIO object lock); aicd audit verif… | manual | 0 | **MANUAL** | Hash chain + WORM-якорь — P9 приоритет P2, не реализовано (THREAT_MODEL TH-34 residual). |
| SC-AUD-03 | Каталог обязательных событий аудита (SECURITY_ARCHITECTURE §12) + тест покрытия: каждая команда API/опасное де… | automated | 3 (3/0/0) | **PASS** |  |
| SC-AUD-04 | Чтение аудита — owner/admin своего workspace; экспорт с redaction; без секретов и кода в metadata (allowlist п… | partial | 1 (1/0/0) | **PASS** | Чтение аудита только owner/admin — матрица прав (SC-IAM-07); allowlist metadata — ревью кода. |
| SC-SUP-01 | Pinning: uv.lock с хешами, pnpm-lock.yaml + --frozen-lockfile, образы по digest, GitHub Actions по commit SHA | partial | 1 (1/0/0) | **PASS** | Pinning Actions/образов — CI-линтер (ops). |
| SC-SUP-02 | SCA: OSV-Scanner/pip-audit/pnpm audit, Trivy для образов; блок при critical/high с доступным исправлением; иск… | partial | 2 (2/0/0) | **PASS** | Блок релиза по SCA — CI-пайплайн платформы (ops). |
| SC-SUP-03 | SBOM: Syft (SPDX + CycloneDX) для всех образов, прикладывается к релизу как attestation | partial | 1 (1/0/0) | **PASS** | SBOM релиза — scripts ops (P9-06). |
| SC-SUP-04 | Подпись Cosign (keyless OIDC или key-pair в KMS); проверка при развёртывании (aicd verify-images) и перед запу… | partial | 1 (1/0/0) | **PASS** | Подпись образов релиза и проверка при развёртывании — ops. |
| SC-SUP-05 | Provenance: **предлагаемая цель SLSA Build L2** для v1.0 (hosted CI, подписанная provenance), L3 — после v1.0;… | manual | 0 | **MANUAL** | SLSA provenance — CI релиза (ops), приоритет P2. |
| SC-SUP-06 | Secure CI: permissions: минимальные (contents: read по умолчанию), без секретов для PR из форков, запрет pull_… | manual | 0 | **MANUAL** | Линтер workflow CI (zizmor/actionlint) — CI платформы. |
| SC-SUP-07 | Код и зависимости репозиториев пользователей считаются враждебными: исполняются только в verification-контейне… | automated | 10 (10/0/0) | **PASS** |  |
| SC-SUP-08 | Политика обновлений: Renovate/Dependabot еженедельно; SLA уязвимостей critical 7 дней, high 30 дней, medium — … | partial | 2 (2/0/0) | **PASS** | Политика обновлений/SLA — процесс. |
| SC-DATA-01 | Классификация данных (S/C/I/P/U, SECURITY_ARCHITECTURE §15) и правила обращения по классам; класс указан в DAT… | partial | 4 (4/0/0) | **PASS** | Класс данных каждой таблицы — ревью DATABASE_DESIGN. |
| SC-DATA-02 | Шифрование at rest: рекомендация дискового шифрования хоста (включая cred-тома и worktrees), MinIO SSE (KES) и… | manual | 0 | **MANUAL** | Шифрование at rest — doctor/ops (P9-05). |
| SC-DATA-03 | Бэкапы PG (pgBackRest/WAL, AES-256) и MinIO (versioning + mirror), ключи отдельно; тома учётных данных CLI иск… | partial | 12 (12/0/0) | **PASS** | Restore drill — ops (P9-05). |
| SC-DATA-04 | Retention (raw logs 30 д, run_event_log 90 д, audit 1 год — предложение) и удаление через DATA_DELETION; задач… | automated | 7 (7/0/0) | **PASS** |  |
| SC-DATA-05 | Целостность артефактов/evidence: sha256 при записи, проверка при чтении evidence, неизменяемые ключи, опционал… | automated | 4 (4/0/0) | **PASS** |  |
| SC-DATA-06 | Безопасная выдача артефактов: Content-Disposition: attachment, X-Content-Type-Options: nosniff, корректный Con… | automated | 2 (2/0/0) | **PASS** |  |
| SC-DATA-07 | Минимизация ПДн: только e-mail, имя, IP/UA в сессиях и auth-аудите; IP/UA удаляются через 30 дней после истече… | partial | 1 (1/0/0) | **PASS** | Удаление IP/UA через 30 дней — retention сессий. |
| SC-DATA-08 | Leases с fencing token: каждая запись результата и каждое внешнее действие проверяют текущий fencing_token (UP… | automated | 10 (10/0/0) | **PASS** |  |
| SC-DATA-09 | Сообщения очереди и pub/sub — только подсказки: несут ID + event_id; потребитель перечитывает состояние из PG,… | automated | 4 (4/0/0) | **PASS** |  |
| SC-OBS-01 | Структурированные JSON-логи с корреляцией run_id/task_id/execution_id/workspace_id/request_id; тела запросов и… | automated | 2 (2/0/0) | **PASS** |  |
| SC-OBS-02 | Security-алерты: всплеск неудачных входов/блокировок, tenant_denied_total, egress_denied_total, SANDBOX_DENIED… | automated | 4 (4/0/0) | **PASS** |  |
| SC-OBS-03 | Гигиена телеметрии: allowlist атрибутов OTel (без промптов, кода, вывода CLI, e-mail), ограничение кардинально… | automated | 3 (3/0/0) | **PASS** |  |
| SC-API-01 | Строгая валидация: Pydantic v2 strict, extra="forbid", лимит тела 1 MiB (chat ≤ 16 KiB, директива ≤ 16 KiB), U… | automated | 19 (19/0/0) | **PASS** |  |
| SC-API-02 | Rate limiting (Redis token bucket) по пользователю/IP/workspace: login, POST /runs, директивы, chat, probe, do… | automated | 4 (4/0/0) | **PASS** |  |
| SC-API-03 | Квоты и лимиты ресурсов: одновременные runs на workspace, задачи в DAG (≤ 200), глубина replan, SSE-соединения… | partial | 2 (2/0/0) | **PASS** | Квоты runs/DAG — unit-лимиты DAG; нагрузочные пороги — P9 performance. |
| SC-API-04 | Ошибки RFC 7807 без трассировок, SQL, путей и внутренних ID; correlation_id для поддержки | automated | 3 (3/0/0) | **PASS** |  |
| SC-API-05 | Idempotency-Key: область (workspace_id, user_id, key), привязка к request_hash (несовпадение → 422), срок хран… | automated | 2 (2/0/0) | **PASS** |  |
| SC-API-06 | Оптимистическая конкуренция: ETag/If-Match (version) на PATCH и критических командах (approval decision, дирек… | automated | 2 (2/0/0) | **PASS** |  |
| SC-API-07 | SSE: authz при подключении (членство в workspace run'а); повторная проверка каждые 60 с и по событию member.re… | automated | 3 (3/0/0) | **PASS** |  |
| SC-API-08 | Заголовки безопасности: CSP default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; … | automated | 1 (1/0/0) | **PASS** |  |
| SC-API-09 | Защита от mass assignment: отдельные request/response-схемы; поля role, status, requested_by, workspace_id, de… | automated | 2 (2/0/0) | **PASS** |  |

## 4. Тесты по контролям

<details><summary>SC-IAM-01 — PASS (1)</summary>

- `tests/unit/test_passwords.py::test_argon2id_hash_and_verify` — passed

</details>

<details><summary>SC-IAM-02 — PASS (5)</summary>

- `tests/integration/test_cli.py::test_weak_password_rejected` — passed
- `tests/unit/test_passwords.py::test_policy_rejects_weak[aaaaaaaaaaaaaaaaaaaa]` — passed
- `tests/unit/test_passwords.py::test_policy_rejects_weak[aliceAlice-1234]` — passed
- `tests/unit/test_passwords.py::test_policy_rejects_weak[pw2]` — passed
- `tests/unit/test_passwords.py::test_policy_rejects_weak[short]` — passed

</details>

<details><summary>SC-IAM-03 — PASS (3)</summary>

- `tests/integration/test_api_auth.py::test_idle_timeout_expires_session` — passed
- `tests/integration/test_api_auth.py::test_login_sets_hardened_cookies_and_matches_contract` — passed
- `tests/unit/test_passwords.py::test_tokens_hash_only` — passed

</details>

<details><summary>SC-IAM-04 — PASS (2)</summary>

- `tests/integration/test_api_auth.py::test_logout_revokes_session` — passed
- `tests/security/test_step_up.py::test_stale_session_requires_step_up_then_reauth_rotates_session` — passed

</details>

<details><summary>SC-IAM-05 — PASS (3)</summary>

- `tests/integration/test_api_auth.py::test_login_rejects_cross_site_origin` — passed
- `tests/integration/test_api_auth.py::test_unsafe_requests_require_csrf_and_same_origin` — passed
- `tests/security/test_step_up.py::test_stale_session_requires_step_up_then_reauth_rotates_session` — passed

</details>

<details><summary>SC-IAM-06 — PASS (4)</summary>

- `tests/integration/test_api_auth.py::test_bad_credentials_are_indistinguishable[]` — passed
- `tests/integration/test_api_auth.py::test_bad_credentials_are_indistinguishable[wrong-password-123]` — passed
- `tests/integration/test_api_auth.py::test_lockout_after_failures_even_with_correct_password` — passed
- `tests/security/test_rate_limits.py::test_login_is_rate_limited_per_ip` — passed

</details>

<details><summary>SC-IAM-07 — PASS (201)</summary>

- `tests/contract/test_contracts.py::test_every_contract_permission_is_known_to_rbac` — passed
- `tests/integration/test_api_projects.py::test_role_restrictions` — passed
- `tests/integration/test_github_api.py::test_rbac_matrix_and_cross_workspace` — passed
- `tests/integration/test_runs_api.py::test_runs_are_tenant_isolated_and_role_checked` — passed
- `tests/unit/test_rbac.py::test_approval_rules[admin-CRITICAL_DIRECTIVE-False-False-expected8]` — passed
- `tests/unit/test_rbac.py::test_approval_rules[admin-DATA_DELETION-False-False-expected5]` — passed
- `tests/unit/test_rbac.py::test_approval_rules[admin-MERGE-True-False-expected3]` — passed
- `tests/unit/test_rbac.py::test_approval_rules[admin-MERGE-True-True-expected4]` — passed
- `tests/unit/test_rbac.py::test_approval_rules[developer-PLAN_APPROVAL-True-False-expected2]` — passed
- `tests/unit/test_rbac.py::test_approval_rules[owner-LOCAL_EXECUTION_FALLBACK-False-False-expected6]` — passed
- `tests/unit/test_rbac.py::test_approval_rules[owner-PRODUCTION_DEPLOY-True-True-expected7]` — passed
- `tests/unit/test_rbac.py::test_approval_rules[reviewer-MANUAL_REVIEW-False-False-expected0]` — passed
- `tests/unit/test_rbac.py::test_approval_rules[reviewer-PLAN_APPROVAL-False-False-expected1]` — passed
- `tests/unit/test_rbac.py::test_catalog_matches_api_design` — passed
- `tests/unit/test_rbac.py::test_directive_rules` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[admin-ACCEPTED_RISK-True]` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[developer-ACCEPTED_RISK-False]` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[owner-ACCEPTED_RISK-True]` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[owner-VERIFIED_FIXED-False]` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[reviewer-ACCEPTED_RISK-False]` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[reviewer-ACKNOWLEDGED-True]` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[reviewer-FALSE_POSITIVE-True]` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[reviewer-OPEN-True]` — passed
- `tests/unit/test_rbac.py::test_finding_transition_rules[viewer-ACKNOWLEDGED-False]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-approval:decide]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-approval:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-artifact:download]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-artifact:download_raw]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-audit:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-chat:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-chat:write]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-compliance:review]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-directive:confirm]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-directive:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-documentation:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-documentation:refresh]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-documentation:register]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-evidence:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-finding:triage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-github:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-github:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-member:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-member:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-project:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-project:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-project:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-provider:probe]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-provider:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-provider:settings]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-pull_request:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-pull_request:merge]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-quality:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-repository:attach]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-run:control]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-run:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-run:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-workspace:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-workspace:policy]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[admin-workspace:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-approval:decide]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-approval:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-artifact:download]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-artifact:download_raw]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-audit:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-chat:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-chat:write]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-compliance:review]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-directive:confirm]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-directive:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-documentation:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-documentation:refresh]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-documentation:register]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-evidence:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-finding:triage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-github:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-github:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-member:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-member:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-project:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-project:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-project:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-provider:probe]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-provider:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-provider:settings]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-pull_request:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-pull_request:merge]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-quality:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-repository:attach]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-run:control]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-run:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-run:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-workspace:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-workspace:policy]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[developer-workspace:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-approval:decide]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-approval:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-artifact:download]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-artifact:download_raw]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-audit:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-chat:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-chat:write]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-compliance:review]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-directive:confirm]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-directive:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-documentation:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-documentation:refresh]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-documentation:register]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-evidence:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-finding:triage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-github:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-github:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-member:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-member:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-project:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-project:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-project:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-provider:probe]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-provider:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-provider:settings]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-pull_request:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-pull_request:merge]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-quality:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-repository:attach]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-run:control]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-run:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-run:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-workspace:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-workspace:policy]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[owner-workspace:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-approval:decide]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-approval:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-artifact:download]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-artifact:download_raw]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-audit:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-chat:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-chat:write]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-compliance:review]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-directive:confirm]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-directive:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-documentation:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-documentation:refresh]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-documentation:register]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-evidence:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-finding:triage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-github:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-github:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-member:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-member:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-project:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-project:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-project:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-provider:probe]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-provider:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-provider:settings]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-pull_request:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-pull_request:merge]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-quality:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-repository:attach]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-run:control]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-run:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-run:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-workspace:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-workspace:policy]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[reviewer-workspace:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-approval:decide]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-approval:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-artifact:download]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-artifact:download_raw]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-audit:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-chat:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-chat:write]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-compliance:review]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-directive:confirm]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-directive:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-documentation:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-documentation:refresh]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-documentation:register]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-evidence:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-finding:triage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-github:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-github:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-member:manage]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-member:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-project:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-project:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-project:update]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-provider:probe]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-provider:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-provider:settings]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-pull_request:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-pull_request:merge]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-quality:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-repository:attach]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-run:control]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-run:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-run:read]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-workspace:create]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-workspace:policy]` — passed
- `tests/unit/test_rbac.py::test_matrix_cell[viewer-workspace:update]` — passed
- `tests/unit/test_rbac.py::test_project_override_only_downgrades` — passed
- `tests/unit/test_rbac.py::test_unknown_permission_is_error` — passed

</details>

<details><summary>SC-IAM-08 — PASS (5)</summary>

- `tests/security/test_step_up.py::test_critical_approval_requires_step_up_non_critical_does_not` — passed
- `tests/security/test_step_up.py::test_fresh_login_allows_policy_change` — passed
- `tests/security/test_step_up.py::test_reauthenticate_is_rate_limited_per_user` — passed
- `tests/security/test_step_up.py::test_reauthenticate_requires_session` — passed
- `tests/security/test_step_up.py::test_stale_session_requires_step_up_then_reauth_rotates_session` — passed

</details>

<details><summary>SC-IAM-09 — PASS (1)</summary>

- `tests/integration/test_cli.py::test_create_owner_then_login` — passed

</details>

<details><summary>SC-TEN-01 — PASS (35)</summary>

- `tests/migrations/test_migrations.py::test_app_roles_cannot_bypass_rls` — passed
- `tests/migrations/test_migrations.py::test_every_tenant_table_has_forced_rls_and_policy` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[approvals-aicd_app]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[approvals-aicd_worker]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[run_event_log-aicd_app]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[run_event_log-aicd_worker]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[runs-aicd_app]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[runs-aicd_worker]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[task_dependencies-aicd_app]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[task_dependencies-aicd_worker]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[tasks-aicd_app]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_tenant_tables_isolated[tasks-aicd_worker]` — passed
- `tests/migrations/test_security_hardening_migration.py::test_verification_table_rls_grants_and_terminal_states` — passed
- `tests/security/test_rls.py::test_cannot_modify_other_tenant[artifacts]` — passed
- `tests/security/test_rls.py::test_cannot_modify_other_tenant[projects]` — passed
- `tests/security/test_rls.py::test_cannot_modify_other_tenant[repositories]` — passed
- `tests/security/test_rls.py::test_cannot_modify_other_tenant[workspace_members]` — passed
- `tests/security/test_rls.py::test_composite_fk_blocks_cross_tenant_reference` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[artifacts-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[artifacts-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[audit_events-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[audit_events-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[project_settings-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[project_settings-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[projects-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[projects-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[repositories-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[repositories-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[workspace_members-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[workspace_members-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[workspaces-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[workspaces-aicd_worker]` — passed
- `tests/security/test_rls.py::test_insert_into_foreign_workspace_rejected` — passed
- `tests/security/test_rls.py::test_last_owner_invariant` — passed
- `tests/security/test_rls.py::test_security_definer_functions_only_reveal_own_memberships` — passed

</details>

<details><summary>SC-TEN-02 — PASS (15)</summary>

- `tests/migrations/test_orchestration_invariants.py::test_app_cannot_write_foreign_run` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[artifacts-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[artifacts-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[audit_events-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[audit_events-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[project_settings-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[project_settings-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[projects-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[projects-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[repositories-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[repositories-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[workspace_members-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[workspace_members-aicd_worker]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[workspaces-aicd_app]` — passed
- `tests/security/test_rls.py::test_context_sees_only_own_workspace[workspaces-aicd_worker]` — passed

</details>

<details><summary>SC-TEN-03 — PASS (2)</summary>

- `tests/migrations/test_orchestration_invariants.py::test_cross_run_dependency_rejected` — passed
- `tests/security/test_rls.py::test_composite_fk_blocks_cross_tenant_reference` — passed

</details>

<details><summary>SC-TEN-04 — PASS (86)</summary>

- `tests/integration/test_artifact_download.py::test_expired_missing_foreign_and_unknown_are_404` — passed
- `tests/integration/test_findings_standards_api.py::test_non_members_get_404_and_anonymous_401` — passed
- `tests/integration/test_quality_api.py::test_non_members_get_404_and_anonymous_401` — passed
- `tests/integration/test_repository_explorer.py::test_workspace_isolation` — passed
- `tests/integration/test_sse.py::test_non_member_gets_404_and_activity` — passed
- `tests/integration/test_workspace_events.py::test_workspace_stream_errors_and_isolation` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_every_operation_is_classified` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[addMember]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[approveProjectControl]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[attachRepository]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[cancelDirective]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[cancelRun]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[confirmDirective]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[connectGitHubInstallation]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[createDirective]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[createProject]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[createRun]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[decideApproval]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[disconnectGitHubInstallation]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[downloadArtifact]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getAnalyticsSummary]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getApproval]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getDirective]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getDocumentationPolicy]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getEvidence]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getFinding]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getGitHubVerification]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getProject]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getProviderProbe]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRepositoryFile]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRunActivity]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRunChat]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRunCompliance]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRunQuality]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRunReport]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRunTasks]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getRun]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getTask]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[getWorkspace]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listApprovals]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listAuditEvents]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listDirectives]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listDocumentationSources]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listGitHubInstallations]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listMembers]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listProjectControls]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listProjects]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listProviders]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listPullRequests]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listRepositories]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listRepositoryBranches]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listRepositoryCommits]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listRepositoryTree]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listRunEvidence]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listRunFindings]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listRuns]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listTaskAttempts]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listTaskEvidence]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listTaskMessages]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listTechnologies]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[listWorkspaceTechnologies]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[pauseRun]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[postChatMessage]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[probeProvider]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[refreshDocumentationSource]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[registerDocumentationSource]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[removeMember]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[requestPullRequestMerge]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[requestPullRequest]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[resumeRun]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[reviewComplianceControl]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[startGitHubInstallation]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[streamRunEvents]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[streamWorkspaceEvents]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[transitionFinding]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[updateDocumentationPolicy]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[updateMember]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[updateProject]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[updateProviderSettings]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_foreign_ids_yield_indistinguishable_404[updateWorkspace]` — passed
- `tests/security/test_cross_tenant_all_operations.py::test_victim_data_unchanged` — passed
- `tests/security/test_cross_tenant_api.py::test_foreign_workspace_is_invisible` — passed
- `tests/security/test_cross_tenant_api.py::test_removed_member_loses_access` — passed
- `tests/security/test_cross_tenant_api.py::test_unknown_resource_and_foreign_resource_look_identical` — passed
- `tests/security/test_cross_tenant_api.py::test_workspace_header_mismatch_on_resource_is_404` — passed
- `tests/security/test_cross_tenant_api.py::test_workspace_list_only_own` — passed

</details>

<details><summary>SC-TEN-05 — PASS (8)</summary>

- `tests/integration/test_artifact_download.py::test_download_streams_content_with_headers_and_audit` — passed
- `tests/integration/test_artifact_download.py::test_expired_missing_foreign_and_unknown_are_404` — passed
- `tests/integration/test_artifact_download.py::test_integrity_mismatch_aborts_stream` — passed
- `tests/integration/test_artifact_download.py::test_raw_cli_log_requires_admin` — passed
- `tests/integration/test_artifacts_s3.py::test_foreign_bucket_ref_rejected` — passed
- `tests/unit/test_artifacts_fs.py::test_bad_refs[file://artifacts/../../etc/passwd]` — passed
- `tests/unit/test_artifacts_fs.py::test_bad_refs[file://artifacts/x]` — passed
- `tests/unit/test_artifacts_fs.py::test_bad_refs[s3://b/k]` — passed

</details>

<details><summary>SC-TEN-06 — PASS (2)</summary>

- `tests/integration/test_sse.py::test_revoked_member_stream_closes` — passed
- `tests/integration/test_workspace_events.py::test_workspace_stream_closes_after_member_removed` — passed

</details>

<details><summary>SC-TEN-07 — PASS (2)</summary>

- `tests/integration/test_code_index_db.py::test_rls_isolates_index` — passed
- `tests/integration/test_context_pack.py::test_cache_hit_reuses_artifact` — passed

</details>

<details><summary>SC-TEN-08 — PASS (2)</summary>

- `tests/integration/test_retention.py::test_retention_requires_maintenance_role` — passed
- `tests/migrations/test_migrations.py::test_app_roles_cannot_bypass_rls` — passed

</details>

<details><summary>SC-SBX-01 — PASS (3)</summary>

- `tests/security/test_sandbox.py::test_inspect_confirms_hardening_and_limits` — passed
- `tests/security/test_sandbox.py::test_runs_as_non_root_without_capabilities` — passed
- `tests/unit/test_container_policy.py::test_build_host_config_hardening` — passed

</details>

<details><summary>SC-SBX-02 — PASS (5)</summary>

- `tests/security/test_adapter_in_sandbox.py::test_fake_cli_runs_in_container_and_writes_only_worktree` — passed
- `tests/security/test_adapter_in_sandbox.py::test_network_is_unavailable_in_runner` — passed
- `tests/security/test_adapter_in_sandbox.py::test_read_only_role_writes_result_to_output_dir_in_container` — passed
- `tests/security/test_adapter_in_sandbox.py::test_writing_role_result_moved_out_of_worktree_in_container` — passed
- `tests/security/test_sandbox.py::test_writes_only_inside_worktree` — passed

</details>

<details><summary>SC-SBX-03 — PASS (5)</summary>

- `tests/integration/test_fake_adapter.py::test_output_limit_truncates_without_killing` — passed
- `tests/security/test_sandbox.py::test_memory_limit_kills_allocation` — passed
- `tests/security/test_sandbox.py::test_output_truncated_but_process_accounted` — passed
- `tests/security/test_sandbox.py::test_pids_limit_enforced` — passed
- `tests/security/test_sandbox.py::test_timeout_terminates_and_removes` — passed

</details>

<details><summary>SC-SBX-04 — PASS (3)</summary>

- `tests/chaos/test_recovery.py::test_reaper_defers_when_execution_cannot_be_confirmed_dead` — passed
- `tests/chaos/test_recovery.py::test_worker_killed_while_cli_running_is_recovered` — passed
- `tests/integration/test_local_runner.py::test_terminate_escalates_to_sigkill_and_kills_children` — passed

</details>

<details><summary>SC-SBX-05 — PASS (55)</summary>

- `tests/security/test_sandbox.py::test_policy_violation_rejected_before_create` — passed
- `tests/security/test_sandbox.py::test_socket_proxy_denies_forbidden_endpoints` — passed
- `tests/unit/test_container_policy.py::test_linter_image_allowlist_and_digest` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[anon_volumes]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[apparmor_unconfined]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[bind_etc]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[bind_root_itself]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[bind_traversal]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[binds]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[cap_add]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[cap_drop_missing]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[container_network]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[default_bridge]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[devices]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[docker_sock]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[docker_sock_in_root]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[empty_user]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[foreign_volume]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[gpus]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[host_cgroupns]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[host_ipc]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[host_network]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[host_pid]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[host_userns]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[host_uts]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[label_disable]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[no_cpus]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[no_execution_label]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[no_hostconfig]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[no_init]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[no_memory]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[no_new_privs_missing]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[no_pids]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[npipe]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[oom_kill_disable]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[ports]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[privileged]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[proc_target]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[publish_all]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[root_user]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[root_user_name]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[rootfs_rw]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[runtime]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[rw_scratch_volume]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[seccomp_unconfined]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[second_rw_bind]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[shared_propagation]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[swap]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[sysctls]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[tmp_exec]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[tmpfs_mount_type]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[tmpfs_unbounded]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[tty]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[unmask_proc]` — passed
- `tests/unit/test_container_policy.py::test_linter_rejects_forbidden_config[volumes_from]` — passed

</details>

<details><summary>SC-SBX-06 — PASS (3)</summary>

- `tests/security/test_adapter_in_sandbox.py::test_network_is_unavailable_in_runner` — passed
- `tests/security/test_sandbox.py::test_no_direct_egress_on_internal_network` — passed
- `tests/security/test_sandbox.py::test_no_network_on_none` — passed

</details>

<details><summary>SC-SBX-07 — PASS (11)</summary>

- `tests/integration/test_gate_runners_docker.py::test_destructive_migration_applies_but_requires_approval` — passed
- `tests/integration/test_gate_runners_docker.py::test_eslint_in_pinned_container[fail-FAIL]` — passed
- `tests/integration/test_gate_runners_docker.py::test_eslint_in_pinned_container[pass-PASS]` — passed
- `tests/integration/test_gate_runners_docker.py::test_migrations_on_ephemeral_postgres[broken_down-FAIL-migration/downgrade-incomplete]` — passed
- `tests/integration/test_gate_runners_docker.py::test_migrations_on_ephemeral_postgres[failing-FAIL-migration/upgrade1-failed]` — passed
- `tests/integration/test_gate_runners_docker.py::test_migrations_on_ephemeral_postgres[pass-PASS-None]` — passed
- `tests/integration/test_gate_runners_docker.py::test_ruff_in_pinned_container[fail-FAIL]` — passed
- `tests/integration/test_gate_runners_docker.py::test_ruff_in_pinned_container[pass-PASS]` — passed
- `tests/integration/test_gate_runners_docker.py::test_unpinned_image_is_rejected_by_server_policy` — passed
- `tests/integration/test_verification_engine.py::test_scope_gate_in_verification_container` — passed
- `tests/unit/test_security_gates.py::test_container_spec_is_read_only_offline_and_passes_policy_lint` — passed

</details>

<details><summary>SC-SBX-08 — PASS (2)</summary>

- `tests/integration/test_gate_runners_docker.py::test_unpinned_image_is_rejected_by_server_policy` — passed
- `tests/unit/test_security_gates.py::test_images_are_pinned_by_digest_without_latest` — passed

</details>

<details><summary>SC-SBX-10 — PASS (2)</summary>

- `tests/integration/test_local_runner.py::test_environment_is_not_inherited` — passed
- `tests/integration/test_worker_actors.py::test_exec_worker_refuses_fake_adapter_in_production` — passed

</details>

<details><summary>SC-SBX-11 — PASS (4)</summary>

- `tests/security/test_command_injection.py::test_platform_git_ignores_malicious_repository_config` — passed
- `tests/security/test_command_injection.py::test_worktree_gitattributes_filters_do_not_run` — passed
- `tests/security/test_command_injection.py::test_worktree_gitlink_redirect_is_refused` — passed
- `tests/unit/test_code_index_builder.py::test_read_blobs_does_not_apply_filters` — passed

</details>

<details><summary>SC-SBX-12 — PASS (30)</summary>

- `tests/integration/test_api_projects.py::test_attach_rejects_unsafe_paths[github]` — passed
- `tests/integration/test_api_projects.py::test_attach_rejects_unsafe_paths[nested]` — passed
- `tests/integration/test_api_projects.py::test_attach_rejects_unsafe_paths[not_git]` — passed
- `tests/integration/test_api_projects.py::test_attach_rejects_unsafe_paths[outside]` — passed
- `tests/integration/test_api_projects.py::test_attach_rejects_unsafe_paths[symlink]` — passed
- `tests/integration/test_api_projects.py::test_attach_rejects_unsafe_paths[traversal]` — passed
- `tests/integration/test_repository_explorer.py::test_binary_large_and_symlink_files` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[../etc/passwd]` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[./README.md]` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[.git/config]` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[/etc/passwd]` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[a//b]` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[a\\b]` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[src/../README.md]` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[src/.GIT/x]` — passed
- `tests/integration/test_repository_explorer.py::test_unsafe_paths_rejected[x\x01]` — passed
- `tests/integration/test_retention.py::test_archive_sink_rejects_traversal` — passed
- `tests/integration/test_worktrees.py::test_paths_outside_root_rejected` — passed
- `tests/unit/test_agent_result.py::test_declared_output_must_exist_and_not_escape` — passed
- `tests/unit/test_code_index_builder.py::test_tree_parsing_drops_unsafe_paths` — passed
- `tests/unit/test_gates_applicability.py::test_scanner_allows_symlink_inside_worktree` — passed
- `tests/unit/test_paths.py::test_absolute_and_ref_forms` — passed
- `tests/unit/test_paths.py::test_absolute_symlink_escape_rejected` — passed
- `tests/unit/test_paths.py::test_traversal_and_escapes_rejected[/etc]` — passed
- `tests/unit/test_paths.py::test_traversal_and_escapes_rejected[]` — passed
- `tests/unit/test_paths.py::test_traversal_and_escapes_rejected[main/../../etc]` — passed
- `tests/unit/test_paths.py::test_traversal_and_escapes_rejected[main/escape]` — passed
- `tests/unit/test_paths.py::test_traversal_and_escapes_rejected[main/proj/\n]` — passed
- `tests/unit/test_paths.py::test_traversal_and_escapes_rejected[main/proj\x00x]` — passed
- `tests/unit/test_paths.py::test_traversal_and_escapes_rejected[other/proj]` — passed

</details>

<details><summary>SC-CLI-01 — PASS (6)</summary>

- `tests/unit/test_cli_core.py::test_argv_is_array_from_allowlisted_placeholders` — passed
- `tests/unit/test_cli_core.py::test_argv_rejections[template0-values0]` — passed
- `tests/unit/test_cli_core.py::test_argv_rejections[template1-values1]` — passed
- `tests/unit/test_cli_core.py::test_argv_rejections[template2-values2]` — passed
- `tests/unit/test_cli_core.py::test_argv_rejections[template3-values3]` — passed
- `tests/unit/test_cli_core.py::test_argv_rejections[template4-values4]` — passed

</details>

<details><summary>SC-CLI-02 — PASS (6)</summary>

- `tests/security/test_command_injection.py::test_objective_never_reaches_argv_or_shell[$(touch {marker})]` — passed
- `tests/security/test_command_injection.py::test_objective_never_reaches_argv_or_shell[--dangerously-bypass-approvals-and-sandbox $(touch {marker})]` — passed
- `tests/security/test_command_injection.py::test_objective_never_reaches_argv_or_shell[--scenario=env_dump\ntouch {marker}]` — passed
- `tests/security/test_command_injection.py::test_objective_never_reaches_argv_or_shell[`touch {marker}`]` — passed
- `tests/security/test_command_injection.py::test_objective_never_reaches_argv_or_shell[ok && touch {marker} || true]` — passed
- `tests/security/test_command_injection.py::test_objective_never_reaches_argv_or_shell[ok; touch {marker}]` — passed

</details>

<details><summary>SC-CLI-03 — PASS (2)</summary>

- `tests/integration/test_fake_adapter.py::test_process_environment_is_allowlisted` — passed
- `tests/security/test_sandbox.py::test_host_secrets_not_visible` — passed

</details>

<details><summary>SC-CLI-04 — PASS (2)</summary>

- `tests/integration/test_providers.py::test_probe_recorded_and_listed` — passed
- `tests/unit/test_cli_core.py::test_env_built_from_allowlist_and_api_keys_refused` — passed

</details>

<details><summary>SC-CLI-05 — PASS (2)</summary>

- `tests/unit/test_cli_core.py::test_signature_priority_and_retry_after` — passed
- `tests/unit/test_routing.py::test_both_unavailable_blocks_with_right_reason_ac16` — passed

</details>

<details><summary>SC-CLI-06 — PASS (23)</summary>

- `tests/integration/test_fake_adapter.py::test_agent_io_dir_outside_worktree_and_prompt_text` — passed
- `tests/integration/test_fake_adapter.py::test_cancel_is_idempotent` — passed
- `tests/integration/test_fake_adapter.py::test_chatty_process_not_idle_but_wall_clock_enforced` — passed
- `tests/integration/test_fake_adapter.py::test_errors_are_normalized[auth_required-AUTH_REQUIRED-AUTH_REQUIRED-False]` — passed
- `tests/integration/test_fake_adapter.py::test_errors_are_normalized[garbage_output-COLLECTED-OUTPUT_PARSE_ERROR-True]` — passed
- `tests/integration/test_fake_adapter.py::test_errors_are_normalized[nonzero_exit-COLLECTED-PROCESS_FAILED-True]` — passed
- `tests/integration/test_fake_adapter.py::test_errors_are_normalized[rate_limited-RATE_LIMITED-RATE_LIMITED-True]` — passed
- `tests/integration/test_fake_adapter.py::test_execute_is_idempotent` — passed
- `tests/integration/test_fake_adapter.py::test_idle_timeout_while_wall_clock_remains` — passed
- `tests/integration/test_fake_adapter.py::test_missing_worktree_is_crash_not_success` — passed
- `tests/integration/test_fake_adapter.py::test_output_limit_truncates_without_killing` — passed
- `tests/integration/test_fake_adapter.py::test_probe_and_auth_match_contract` — passed
- `tests/integration/test_fake_adapter.py::test_process_environment_is_allowlisted` — passed
- `tests/integration/test_fake_adapter.py::test_secrets_redacted_in_events_and_raw_log` — passed
- `tests/integration/test_fake_adapter.py::test_success_claims_are_unverified` — passed
- `tests/integration/test_fake_adapter.py::test_usage_only_when_reported` — passed
- `tests/integration/test_fake_adapter.py::test_wall_clock_timeout` — passed
- `tests/unit/test_agent_result.py::test_strict_json[\xff\xfe-UTF-8]` — passed
- `tests/unit/test_agent_result.py::test_strict_json[not json-\u043d\u0435 \u044f\u0432\u043b\u044f\u0435\u0442\u0441\u044f JSON]` — passed
- `tests/unit/test_agent_result.py::test_strict_json[raw4-\u0433\u043b\u0443\u0431\u0438\u043d\u0430]` — passed
- `tests/unit/test_agent_result.py::test_strict_json[raw5-\u043b\u0438\u043c\u0438\u0442 \u0440\u0430\u0437\u043c\u0435\u0440\u0430]` — passed
- `tests/unit/test_agent_result.py::test_strict_json[{"a": 1, "a": 2}-\u043f\u043e\u0432\u0442\u043e\u0440\u044f\u044e\u0449\u0438\u0439\u0441\u044f \u043a\u043b\u044e\u0447]` — passed
- `tests/unit/test_agent_result.py::test_strict_json[{"x": NaN}-\u043a\u043e\u043d\u0441\u0442\u0430\u043d\u0442\u0430]` — passed

</details>

<details><summary>SC-CLI-08 — PASS (1)</summary>

- `tests/unit/test_cli_core.py::test_compat_matrix` — passed

</details>

<details><summary>SC-CLI-09 — PASS (1)</summary>

- `tests/unit/test_cli_core.py::test_compat_matrix` — passed

</details>

<details><summary>SC-SEC-01 — PASS (2)</summary>

- `tests/migrations/test_migrations.py::test_no_secret_columns` — passed
- `tests/security/test_secret_leakage.py::test_cli_output_secrets_absent_from_db_and_artifacts` — passed

</details>

<details><summary>SC-SEC-02 — PASS (2)</summary>

- `tests/unit/test_config.py::test_short_secret_rejected` — passed
- `tests/unit/test_redaction.py::test_structures_redact_sensitive_keys` — passed

</details>

<details><summary>SC-SEC-03 — PASS (17)</summary>

- `tests/integration/test_fake_adapter.py::test_secrets_redacted_in_events_and_raw_log` — passed
- `tests/integration/test_quality_api.py::test_evidence_fields_links_and_redaction` — passed
- `tests/security/test_secret_leakage.py::test_cli_output_secrets_absent_from_db_and_artifacts` — passed
- `tests/security/test_secret_leakage.py::test_error_responses_and_logs_never_echo_secrets` — passed
- `tests/security/test_secret_leakage.py::test_metrics_labels_carry_no_secrets_or_unbounded_ids` — passed
- `tests/security/test_secret_leakage.py::test_sse_and_activity_never_carry_secrets` — passed
- `tests/unit/test_redaction.py::test_json_log_line_has_correlation_and_no_secrets` — passed
- `tests/unit/test_redaction.py::test_key_value_and_url_credentials` — passed
- `tests/unit/test_redaction.py::test_known_token_formats_are_redacted[AKIAABCDEFGHIJKLMNOP]` — passed
- `tests/unit/test_redaction.py::test_known_token_formats_are_redacted[eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U]` — passed
- `tests/unit/test_redaction.py::test_known_token_formats_are_redacted[ghp_abcdefghijklmnopqrstuvwxyz0123456789]` — passed
- `tests/unit/test_redaction.py::test_known_token_formats_are_redacted[github_pat_11ABCDEFG0123456789_abcdefghijklmnop]` — passed
- `tests/unit/test_redaction.py::test_known_token_formats_are_redacted[sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345]` — passed
- `tests/unit/test_redaction.py::test_known_token_formats_are_redacted[sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX]` — passed
- `tests/unit/test_redaction.py::test_private_key_block` — passed
- `tests/unit/test_redaction.py::test_redaction_keeps_json_valid` — passed
- `tests/unit/test_redaction.py::test_structures_redact_sensitive_keys` — passed

</details>

<details><summary>SC-SEC-04 — PASS (3)</summary>

- `tests/integration/test_verification_engine.py::test_secret_in_diff_fails_scope_without_leaking_value` — passed
- `tests/security/test_security_gates_docker.py::test_gitleaks_detects_secrets_ignoring_repo_suppressions` — passed
- `tests/unit/test_gates_applicability.py::test_scanner_detects_secret_without_printing_value` — passed

</details>

<details><summary>SC-SEC-05 — PASS (1)</summary>

- `tests/unit/test_code_index_builder.py::test_full_build_exclusions_and_flags` — passed

</details>

<details><summary>SC-SEC-07 — PASS (1)</summary>

- `tests/security/test_secret_leakage.py::test_error_responses_and_logs_never_echo_secrets` — passed

</details>

<details><summary>SC-INJ-01 — PASS (74)</summary>

- `tests/unit/test_prompt_assembly.py::test_attributes_encoded_and_binary_excluded` — passed
- `tests/unit/test_prompt_assembly.py::test_blocks_order_and_manifest` — passed
- `tests/unit/test_prompt_assembly.py::test_budgets_truncate_and_omit_by_priority` — passed
- `tests/unit/test_prompt_assembly.py::test_directive_fence_kept_compatible` — passed
- `tests/unit/test_prompt_assembly.py::test_escape_rules` — passed
- `tests/unit/test_prompt_assembly.py::test_large_base64_replaced` — passed
- `tests/unit/test_prompt_assembly.py::test_new_nonce_is_128_bit_and_unique` — passed
- `tests/unit/test_prompt_assembly.py::test_policy_block_from_profile_only` — passed
- `tests/unit/test_prompt_assembly.py::test_task_fields_validated[change0]` — passed
- `tests/unit/test_prompt_assembly.py::test_task_fields_validated[change1]` — passed
- `tests/unit/test_prompt_assembly.py::test_task_fields_validated[change2]` — passed
- `tests/unit/test_prompt_assembly.py::test_task_fields_validated[change3]` — passed
- `tests/unit/test_prompt_assembly.py::test_task_fields_validated[change4]` — passed
- `tests/unit/test_prompt_assembly.py::test_unknown_section_and_foreign_template_rejected` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_corpus_size_and_diversity` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-01_ignore_previous_en]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-02_ignore_previous_ru]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-03_closing_fence_forgery]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-04_fake_policy_header]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-05_roleplay_admin]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-06_fullwidth_fence]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-07_unicode_tags_hidden]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-08_bidi_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-09_zero_width_split]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-10_nested_untrusted_open]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-11_many_angle_brackets]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-12_lookalike_brackets]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-13_ansi_escape]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-14_crlf_injection]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-15_fake_agentresult]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-16_secret_exfil]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-17_ai_api_bypass]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-18_self_approval]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-19_markdown_heading_spoof]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-20_base64_payload]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-21_json_policy_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-22_xml_system_tag]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-23_nul_and_controls]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-24_line_separator]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-25_nonce_echo]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-26_mixed_math_lt]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-01_ignore_previous_en]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-02_ignore_previous_ru]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-03_closing_fence_forgery]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-04_fake_policy_header]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-05_roleplay_admin]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-06_fullwidth_fence]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-07_unicode_tags_hidden]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-08_bidi_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-09_zero_width_split]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-10_nested_untrusted_open]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-11_many_angle_brackets]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-12_lookalike_brackets]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-13_ansi_escape]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-14_crlf_injection]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-15_fake_agentresult]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-16_secret_exfil]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-17_ai_api_bypass]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-18_self_approval]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-19_markdown_heading_spoof]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-20_base64_payload]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-21_json_policy_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-22_xml_system_tag]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-23_nul_and_controls]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-24_line_separator]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-25_nonce_echo]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-26_mixed_math_lt]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_suspected_injection_flagged[01_ignore_previous_en]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_suspected_injection_flagged[02_ignore_previous_ru]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_suspected_injection_flagged[03_closing_fence_forgery]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_suspected_injection_flagged[06_fullwidth_fence]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_suspected_injection_flagged[07_unicode_tags_hidden]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_suspected_injection_flagged[08_bidi_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_suspected_injection_flagged[20_base64_payload]` — passed

</details>

<details><summary>SC-INJ-02 — PASS (54)</summary>

- `tests/integration/test_agent_io.py::test_acl_violations_rejected_valid_message_accepted` — passed
- `tests/unit/test_prompt_assembly.py::test_policy_block_from_profile_only` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-01_ignore_previous_en]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-02_ignore_previous_ru]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-03_closing_fence_forgery]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-04_fake_policy_header]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-05_roleplay_admin]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-06_fullwidth_fence]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-07_unicode_tags_hidden]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-08_bidi_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-09_zero_width_split]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-10_nested_untrusted_open]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-11_many_angle_brackets]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-12_lookalike_brackets]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-13_ansi_escape]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-14_crlf_injection]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-15_fake_agentresult]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-16_secret_exfil]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-17_ai_api_bypass]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-18_self_approval]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-19_markdown_heading_spoof]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-20_base64_payload]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-21_json_policy_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-22_xml_system_tag]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-23_nul_and_controls]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-24_line_separator]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-25_nonce_echo]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[developer-26_mixed_math_lt]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-01_ignore_previous_en]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-02_ignore_previous_ru]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-03_closing_fence_forgery]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-04_fake_policy_header]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-05_roleplay_admin]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-06_fullwidth_fence]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-07_unicode_tags_hidden]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-08_bidi_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-09_zero_width_split]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-10_nested_untrusted_open]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-11_many_angle_brackets]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-12_lookalike_brackets]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-13_ansi_escape]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-14_crlf_injection]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-15_fake_agentresult]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-16_secret_exfil]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-17_ai_api_bypass]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-18_self_approval]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-19_markdown_heading_spoof]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-20_base64_payload]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-21_json_policy_override]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-22_xml_system_tag]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-23_nul_and_controls]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-24_line_separator]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-25_nonce_echo]` — passed
- `tests/unit/test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence[reviewer-26_mixed_math_lt]` — passed

</details>

<details><summary>SC-INJ-03 — PASS (577)</summary>

- `tests/unit/test_acp_acl.py::test_malformed_narrowing_fails_closed_for_role` — passed
- `tests/unit/test_acp_acl.py::test_matrix_covers_all_participants` — passed
- `tests/unit/test_acp_acl.py::test_plan_ready_only_planner_to_orchestrator` — passed
- `tests/unit/test_acp_acl.py::test_policy_can_only_narrow` — passed
- `tests/unit/test_acp_acl.py::test_qa_handoff_only_test_plan_ready` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[architect-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[database-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[developer-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[developer-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[developer-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[developer-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[developer-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[developer-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[developer-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[developer-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[devops-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[devops-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[devops-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[devops-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[devops-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[devops-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[devops-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[devops-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[documentation-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[documentation-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[documentation-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[documentation-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[documentation-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[documentation-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[documentation-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[documentation-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[maintenance-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[maintenance-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[maintenance-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[maintenance-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[maintenance-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[maintenance-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[maintenance-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[maintenance-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[orchestrator-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[orchestrator-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[orchestrator-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[performance-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[planner-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[planner-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[planner-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[planner-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[planner-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[planner-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[planner-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[qa-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[requirements-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[requirements-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[requirements-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[requirements-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[requirements-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[requirements-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[requirements-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[reviewer-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[reviewer-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[reviewer-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[reviewer-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[reviewer-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[reviewer-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[reviewer-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[reviewer-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[security-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[security-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[security-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[security-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[security-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[security-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[security-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[security-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[user-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_recipients_by_type[user-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_self_review_denied` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[architect-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[database-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[developer-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[devops-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[documentation-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[maintenance-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[orchestrator-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[performance-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[planner-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[qa-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[requirements-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[reviewer-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[security-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-ANSWER]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-BLOCKED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-FINDING]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-FIX_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-HANDOFF]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-QUESTION]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_acl.py::test_send_matrix_role_by_type[user-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_acl.py::test_type_matrix_4_1_consistent_with_4_2` — passed
- `tests/unit/test_acp_acl.py::test_unknown_role_and_type_denied` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_allowed[APPROVAL_REQUIRED]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_allowed[DEPENDENCY_FAILED]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_allowed[DOCS_REQUIRED]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_allowed[MANUAL_REVIEW]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_restricted[AUTH_REQUIRED]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_restricted[DIRECTIVE_REPLAN]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_restricted[PROVIDER_UNAVAILABLE]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_restricted[REVIEW_LOOP_EXHAUSTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_blocked_reason_restricted[RUN_PAUSED]` — passed
- `tests/unit/test_acp_pipeline.py::test_agent_without_task_denied` — passed
- `tests/unit/test_acp_pipeline.py::test_answer_rules` — passed
- `tests/unit/test_acp_pipeline.py::test_attempt_message_limit` — passed
- `tests/unit/test_acp_pipeline.py::test_decision_recorded_rules` — passed
- `tests/unit/test_acp_pipeline.py::test_envelope_over_32k_rejected_after_schema` — passed
- `tests/unit/test_acp_pipeline.py::test_evidence_sha256_must_match_artifact` — passed
- `tests/unit/test_acp_pipeline.py::test_finding_recipient_is_review_author` — passed
- `tests/unit/test_acp_pipeline.py::test_fix_request_needs_review_request` — passed
- `tests/unit/test_acp_pipeline.py::test_foreign_refs_denied` — passed
- `tests/unit/test_acp_pipeline.py::test_fourth_fix_request_exhausts_review_loop` — passed
- `tests/unit/test_acp_pipeline.py::test_handoff_requires_dag_edge` — passed
- `tests/unit/test_acp_pipeline.py::test_inactive_attempt_and_wrong_role_denied` — passed
- `tests/unit/test_acp_pipeline.py::test_injection_flagged_not_executed` — passed
- `tests/unit/test_acp_pipeline.py::test_limits_from_snapshot` — passed
- `tests/unit/test_acp_pipeline.py::test_loop_counter_follows_origin_chain_of_fix_tasks` — passed
- `tests/unit/test_acp_pipeline.py::test_non_mapping_candidate_is_schema_invalid` — passed
- `tests/unit/test_acp_pipeline.py::test_open_blocking_questions_limit` — passed
- `tests/unit/test_acp_pipeline.py::test_orchestrator_may_send_while_run_blocked` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[architect-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[database-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[developer-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[devops-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[documentation-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[maintenance-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[orchestrator-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[performance-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[planner-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[qa-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[requirements-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[reviewer-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[security-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-ANSWER]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-DECISION_RECORDED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-FINDING]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-FIX_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-HANDOFF]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-QUESTION]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-TASK_ASSIGN]` — passed
- `tests/unit/test_acp_pipeline.py::test_pipeline_acl_role_by_type[user-TASK_COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_policy_narrowing_from_run_snapshot` — passed
- `tests/unit/test_acp_pipeline.py::test_question_to_role_without_task_denied` — passed
- `tests/unit/test_acp_pipeline.py::test_raised_task_limit_allows_more_loops` — passed
- `tests/unit/test_acp_pipeline.py::test_recipient_task_must_match_role` — passed
- `tests/unit/test_acp_pipeline.py::test_rejection_codes_are_stable` — passed
- `tests/unit/test_acp_pipeline.py::test_reply_depth_limit` — passed
- `tests/unit/test_acp_pipeline.py::test_reply_to_missing_other_run_and_wrong_type` — passed
- `tests/unit/test_acp_pipeline.py::test_requires_human_approval_raised_never_lowered` — passed
- `tests/unit/test_acp_pipeline.py::test_review_verdict_required_for_reviewer` — passed
- `tests/unit/test_acp_pipeline.py::test_run_hourly_limit` — passed
- `tests/unit/test_acp_pipeline.py::test_run_status_denied_for_agents[BLOCKED]` — passed
- `tests/unit/test_acp_pipeline.py::test_run_status_denied_for_agents[CANCELLED]` — passed
- `tests/unit/test_acp_pipeline.py::test_run_status_denied_for_agents[COMPLETED]` — passed
- `tests/unit/test_acp_pipeline.py::test_run_status_denied_for_agents[CREATED]` — passed
- `tests/unit/test_acp_pipeline.py::test_run_status_denied_for_agents[FAILED]` — passed
- `tests/unit/test_acp_pipeline.py::test_run_status_denied_for_agents[PAUSED]` — passed
- `tests/unit/test_acp_pipeline.py::test_schema_before_size_before_acl` — passed
- `tests/unit/test_acp_pipeline.py::test_sender_cannot_be_spoofed` — passed
- `tests/unit/test_acp_pipeline.py::test_task_assign_rules` — passed
- `tests/unit/test_acp_pipeline.py::test_transport_limit_checked_first` — passed
- `tests/unit/test_acp_pipeline.py::test_unknown_correlation_rejected` — passed
- `tests/unit/test_acp_pipeline.py::test_unknown_run_denied` — passed
- `tests/unit/test_acp_pipeline.py::test_valid_question_accepted` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[ANSWER]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[BLOCKED]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[DECISION_RECORDED]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[FINDING]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[FIX_REQUEST]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[HANDOFF]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[QUESTION]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[TASK_ASSIGN]` — passed
- `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected[TASK_COMPLETED]` — passed
- `tests/unit/test_acp_schema.py::test_all_twelve_types_have_schemas` — passed
- `tests/unit/test_acp_schema.py::test_answer_requires_reply_to` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[agent_with_user_id-overrides9]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[agent_without_provider-overrides8]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[bad_created_at-overrides5]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[bad_protocol-overrides1]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[bad_uuid-overrides2]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[extra_field-overrides3]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[missing_correlation-overrides4]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[non_object_payload-overrides11]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[orchestrator_with_provider-overrides7]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[to_unknown_role-overrides6]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[unknown_type-overrides0]` — passed
- `tests/unit/test_acp_schema.py::test_negative_envelope_rejected[user_without_user_id-overrides10]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[ANSWER-empty_answer]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[ANSWER-no_answer]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[BLOCKED-no_needed_from]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[BLOCKED-unknown_reason]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[DECISION_PROPOSAL-bad_option_id]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[DECISION_PROPOSAL-no_options]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[DECISION_PROPOSAL-no_requires_human_approval]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[DECISION_RECORDED-bad_outcome]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[DECISION_RECORDED-decided_by_agent]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[DECISION_RECORDED-user_without_user_id]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[EVIDENCE_SUBMITTED-bad_sha256]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[EVIDENCE_SUBMITTED-empty_items]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[EVIDENCE_SUBMITTED-verified_claim_field]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[FINDING-bad_severity]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[FINDING-line_zero]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[FINDING-no_evidence]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[FIX_REQUEST-no_file_key]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[FIX_REQUEST-no_loop_iteration]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[FIX_REQUEST-traversal_file]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[HANDOFF-empty_artifact_refs]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[HANDOFF-free_form_ref]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[HANDOFF-plan_ready_without_plan_version]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[QUESTION-no_blocking]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[QUESTION-too_many_options]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[QUESTION-unknown_kind]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[REVIEW_REQUEST-absolute_path]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[REVIEW_REQUEST-loop_iteration_11]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[REVIEW_REQUEST-path_traversal]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[REVIEW_REQUEST-short_sha]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[TASK_ASSIGN-attempt_no_zero]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[TASK_ASSIGN-bad_template_version]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[TASK_ASSIGN-no_task_id]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[TASK_ASSIGN-permission_escalation_field]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[TASK_COMPLETED-claim_without_evidence_refs]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[TASK_COMPLETED-no_claims]` — passed
- `tests/unit/test_acp_schema.py::test_negative_payload_rejected[TASK_COMPLETED-self_verified]` — passed
- `tests/unit/test_acp_schema.py::test_orchestrator_only_types_rejected_from_agent[DECISION_RECORDED]` — passed
- `tests/unit/test_acp_schema.py::test_orchestrator_only_types_rejected_from_agent[TASK_ASSIGN]` — passed
- `tests/unit/test_acp_schema.py::test_schema_errors_do_not_leak_long_values` — passed
- `tests/unit/test_acp_schema.py::test_unknown_payload_type` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[ANSWER]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[BLOCKED]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[DECISION_PROPOSAL]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[DECISION_RECORDED]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[EVIDENCE_SUBMITTED]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[FINDING]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[FIX_REQUEST]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[HANDOFF]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[QUESTION]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[REVIEW_REQUEST]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[TASK_ASSIGN]` — passed
- `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass[TASK_COMPLETED]` — passed

</details>

<details><summary>SC-INJ-04 — PASS (85)</summary>

- `tests/integration/test_false_pass_prevention.py::test_artifact_tampering_detected_by_completion_guard` — passed
- `tests/integration/test_false_pass_prevention.py::test_destructive_migration_requires_approval` — passed
- `tests/integration/test_false_pass_prevention.py::test_findings_lifecycle_open_then_verified_fixed` — passed
- `tests/integration/test_false_pass_prevention.py::test_gate_on_different_commit_and_open_finding_block_completion` — passed
- `tests/integration/test_false_pass_prevention.py::test_green_run_completes_with_test_results_and_links` — passed
- `tests/integration/test_false_pass_prevention.py::test_not_applicable_without_reason_is_rejected_by_db` — passed
- `tests/integration/test_false_pass_prevention.py::test_tool_missing_writes_error_code_and_blocks_completion` — passed
- `tests/unit/test_agent_result.py::test_valid_result_claims_unverified_and_candidates` — passed
- `tests/unit/test_verdict_rules.py::test_advisory_failure_does_not_block_but_required_does` — passed
- `tests/unit/test_verdict_rules.py::test_all_required_pass_completes` — passed
- `tests/unit/test_verdict_rules.py::test_artifact_sha_mismatch_on_reread` — passed
- `tests/unit/test_verdict_rules.py::test_complete_evidence_has_no_problems` — passed
- `tests/unit/test_verdict_rules.py::test_critical_profile_forbids_accepting_critical_risk` — passed
- `tests/unit/test_verdict_rules.py::test_failed_blocking_controls_block_completion` — passed
- `tests/unit/test_verdict_rules.py::test_final_commit_is_latest_execution_per_task` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[ACCEPTED_RISK-OPEN-user-kw11-True]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[ACKNOWLEDGED-OPEN-user-kw12-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[FIXED-OPEN-system-kw9-True]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[FIXED-VERIFIED_FIXED-system-kw7-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[FIXED-VERIFIED_FIXED-system-kw8-True]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[OPEN-ACCEPTED_RISK-system-kw3-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[OPEN-ACCEPTED_RISK-user-kw4-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[OPEN-ACKNOWLEDGED-user-kw0-True]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[OPEN-FALSE_POSITIVE-agent-kw2-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[OPEN-FALSE_POSITIVE-user-kw1-True]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[OPEN-FIXED-agent-kw5-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[OPEN-FIXED-agent-kw6-True]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_lifecycle[VERIFIED_FIXED-OPEN-system-kw10-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_status_rules_in_completion[ACCEPTED_RISK-critical-critical-True]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_status_rules_in_completion[ACCEPTED_RISK-critical-high-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_status_rules_in_completion[ACCEPTED_RISK-strict-critical-False]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_status_rules_in_completion[ACKNOWLEDGED-strict-high-True]` — passed
- `tests/unit/test_verdict_rules.py::test_finding_status_rules_in_completion[FIXED-strict-high-True]` — passed
- `tests/unit/test_verdict_rules.py::test_fingerprint_ignores_line_and_dedupe_keeps_strictest` — passed
- `tests/unit/test_verdict_rules.py::test_fp_coverage_missing_and_unparsable` — passed
- `tests/unit/test_verdict_rules.py::test_fp_empty_verification_and_missing_required_gate` — passed
- `tests/unit/test_verdict_rules.py::test_fp_evidence_sha_mismatch` — passed
- `tests/unit/test_verdict_rules.py::test_fp_exit_zero_with_failures_in_output` — passed
- `tests/unit/test_verdict_rules.py::test_fp_gate_on_different_commit_and_stale_after_new_commit` — passed
- `tests/unit/test_verdict_rules.py::test_fp_not_applicable_without_reason` — passed
- `tests/unit/test_verdict_rules.py::test_fp_required_skipped` — passed
- `tests/unit/test_verdict_rules.py::test_fp_tool_crash_and_timeout` — passed
- `tests/unit/test_verdict_rules.py::test_fp_tool_missing` — passed
- `tests/unit/test_verdict_rules.py::test_fp_zero_tests_and_all_skipped` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change0-not VERIFIED]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change1-producer is agent]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change10-no sha256]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change2-commit differs]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change3-no commit_sha]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change4-no command]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change5-no exit code]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change6-timestamps]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change7-finished before]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change8-runner/tool versions]` — passed
- `tests/unit/test_verdict_rules.py::test_incomplete_evidence[change9-container image]` — passed
- `tests/unit/test_verdict_rules.py::test_non_pass_statuses_never_count[ERROR]` — passed
- `tests/unit/test_verdict_rules.py::test_non_pass_statuses_never_count[FAIL]` — passed
- `tests/unit/test_verdict_rules.py::test_non_pass_statuses_never_count[PENDING]` — passed
- `tests/unit/test_verdict_rules.py::test_non_pass_statuses_never_count[RUNNING]` — passed
- `tests/unit/test_verdict_rules.py::test_non_pass_statuses_never_count[SKIPPED]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_path[-None]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_path[../etc/passwd-None]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_path[./src/a.py-src/a.py]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_path[/etc/passwd-None]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_path[/repo/x.py-x.py]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_path[/workspace/src/a.py-src/a.py]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_path[src\\win.py-src/win.py]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[1-medium]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[2-high]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[7-medium]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[None-medium]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[WARNING-medium]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[blocker-critical]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[error-high]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[note-info]` — passed
- `tests/unit/test_verdict_rules.py::test_normalize_severity[weird-medium]` — passed
- `tests/unit/test_verdict_rules.py::test_not_applicable_requires_reason` — passed
- `tests/unit/test_verdict_rules.py::test_open_findings_threshold_by_profile[basic-critical-True]` — passed
- `tests/unit/test_verdict_rules.py::test_open_findings_threshold_by_profile[basic-high-False]` — passed
- `tests/unit/test_verdict_rules.py::test_open_findings_threshold_by_profile[critical-low-False]` — passed
- `tests/unit/test_verdict_rules.py::test_open_findings_threshold_by_profile[critical-medium-True]` — passed
- `tests/unit/test_verdict_rules.py::test_open_findings_threshold_by_profile[strict-high-True]` — passed
- `tests/unit/test_verdict_rules.py::test_open_findings_threshold_by_profile[strict-medium-False]` — passed
- `tests/unit/test_verdict_rules.py::test_pending_approvals_block_completion` — passed
- `tests/unit/test_verdict_rules.py::test_stale_result_on_old_commit` — passed

</details>

<details><summary>SC-INJ-05 — PASS (2)</summary>

- `tests/integration/test_directives.py::test_rbac_version_mismatch_and_idempotency` — passed
- `tests/unit/test_rbac.py::test_directive_rules` — passed

</details>

<details><summary>SC-INJ-06 — PASS (19)</summary>

- `tests/unit/test_agent_result.py::test_suspicious_text_flagged_not_blocked` — passed
- `tests/unit/test_quality_report.py::test_aggregate_verdict[statuses0-PENDING]` — passed
- `tests/unit/test_quality_report.py::test_aggregate_verdict[statuses1-PASS]` — passed
- `tests/unit/test_quality_report.py::test_aggregate_verdict[statuses2-FAIL]` — passed
- `tests/unit/test_quality_report.py::test_aggregate_verdict[statuses3-ERROR]` — passed
- `tests/unit/test_quality_report.py::test_aggregate_verdict[statuses4-FAIL]` — passed
- `tests/unit/test_quality_report.py::test_aggregate_verdict[statuses5-RUNNING]` — passed
- `tests/unit/test_quality_report.py::test_aggregate_verdict[statuses6-PENDING]` — passed
- `tests/unit/test_quality_report.py::test_aggregate_verdict[statuses7-FAIL]` — passed
- `tests/unit/test_quality_report.py::test_markdown_cell_is_redacted_and_escaped` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[*/*-False]` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[None-False]` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[application/json, text/markdown;q=0.5-False]` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[application/json-False]` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[text/*-True]` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[text/markdown, application/json;q=0.9-True]` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[text/markdown-True]` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[text/markdown;q=0-False]` — passed
- `tests/unit/test_quality_report.py::test_report_content_negotiation[text/markdown;q=abc-False]` — passed

</details>

<details><summary>SC-INJ-07 — PASS (33)</summary>

- `tests/integration/test_worktrees.py::test_base_commit_must_be_existing_full_sha[0000000000000000000000000000000000000000]` — passed
- `tests/integration/test_worktrees.py::test_base_commit_must_be_existing_full_sha[deadbeef]` — passed
- `tests/integration/test_worktrees.py::test_base_commit_must_be_existing_full_sha[main]` — passed
- `tests/security/test_command_injection.py::test_git_argument_values_are_not_shell_interpreted` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[$(id)]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[--upload-pack=touch /tmp/x]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[-bad]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[-o]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[`id`]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[a b]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[a..b]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[a\nb]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[branch.lock]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[main;rm -rf /]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[refs/heads/../../etc]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[trailing/]` — passed
- `tests/security/test_command_injection.py::test_untrusted_branch_names_rejected[x@{1}]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[$(touch PWNED)]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[&& rm -rf /]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal['quoted' "double"]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[*.py]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[--]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[--files]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[--pre=/bin/sh]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[--pre=sh]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[-]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[-e touch]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[-e]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[; touch PWNED]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[`touch PWNED`]` — passed
- `tests/unit/test_code_index_search.py::test_injection_is_literal[| touch PWNED]` — passed
- `tests/unit/test_github_app.py::test_push_ref_guards` — passed

</details>

<details><summary>SC-NET-01 — PASS (1)</summary>

- `tests/security/test_sandbox.py::test_egress_proxy_enforces_allowlist` — passed

</details>

<details><summary>SC-NET-02 — PASS (1)</summary>

- `tests/contract/test_observability.py::test_observability_profile_is_pinned_and_not_public` — passed

</details>

<details><summary>SC-NET-05 — PASS (1)</summary>

- `tests/security/test_sandbox.py::test_no_direct_egress_on_internal_network` — passed

</details>

<details><summary>SC-DOC-01 — PASS (36)</summary>

- `tests/security/test_ssrf.py::test_url_pipeline_blocks[]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[data:text/html,hi]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[file:///etc/passwd]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[ftp://example.com/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[gopher://example.com/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[http://example.com/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://0177.0.0.1/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://017700000001/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://0251.0376.0251.0376/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://0x7f.0.0.1/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://0x7f000001/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://127.0.0.1/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://127.1/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://169.254.169.254/latest/meta-data/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://2130706433/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://2852039166/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://8.8.8.8/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://[::1]/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://[::ffff:127.0.0.1]/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://[::ffff:7f00:1]/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://app.localhost/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://db.internal/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://exa mple.com/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://example.com:22/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://example.com:8443/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://example.com\\@evil.test/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://intranet/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://localhost/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://minio:443/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://postgres/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://printer.local/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://redis/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://user:pass@example.com/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[https://user@example.com/]` — passed
- `tests/security/test_ssrf.py::test_url_pipeline_blocks[jar:https://example.com!/]` — passed
- `tests/security/test_ssrf.py::test_valid_url_normalized` — passed

</details>

<details><summary>SC-DOC-02 — PASS (3)</summary>

- `tests/security/test_ssrf.py::test_any_private_answer_blocks_whole_name` — passed
- `tests/security/test_ssrf.py::test_connection_uses_pinned_ip_not_second_resolution` — passed
- `tests/security/test_ssrf.py::test_redirect_rebinding_is_rechecked` — passed

</details>

<details><summary>SC-DOC-03 — PASS (31)</summary>

- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[0.0.0.0]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[10.1.2.3]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[100.64.0.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[127.0.0.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[127.255.255.254]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[169.254.0.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[169.254.169.254]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[172.16.0.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[172.31.255.255]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[192.0.0.8]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[192.168.1.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[198.18.0.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[2001:0:4136:e378:8000:63bf:80ff:fffe]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[2001:db8::1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[2002:7f00:1::1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[224.0.0.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[255.255.255.255]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[64:ff9b::7f00:1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[64:ff9b::a9fe:a9fe]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[::1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[::]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[::ffff:10.0.0.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[::ffff:127.0.0.1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[::ffff:169.254.169.254]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[fc00::1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[fd00:ec2::254]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[fe80::1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[fec0::1]` — passed
- `tests/security/test_ssrf.py::test_denylist_blocks_private_and_special_addresses[ff02::1]` — passed
- `tests/security/test_ssrf.py::test_ip_literals_checked_against_denylist_when_allowed` — passed
- `tests/security/test_ssrf.py::test_metadata_addresses_blocked_even_when_allowlisted` — passed

</details>

<details><summary>SC-DOC-04 — PASS (9)</summary>

- `tests/security/test_ssrf.py::test_redirect_from_local_server_to_metadata_blocked` — passed
- `tests/security/test_ssrf.py::test_redirect_to_blocked_target[file:///etc/passwd]` — passed
- `tests/security/test_ssrf.py::test_redirect_to_blocked_target[http://docs.example.com/x]` — passed
- `tests/security/test_ssrf.py::test_redirect_to_blocked_target[https://169.254.169.254/latest/meta-data/]` — passed
- `tests/security/test_ssrf.py::test_redirect_to_blocked_target[https://2130706433/]` — passed
- `tests/security/test_ssrf.py::test_redirect_to_blocked_target[https://[::1]/]` — passed
- `tests/security/test_ssrf.py::test_redirect_to_blocked_target[https://internal-host/]` — passed
- `tests/security/test_ssrf.py::test_redirect_to_blocked_target[https://localhost/admin]` — passed
- `tests/security/test_ssrf.py::test_too_many_redirects` — passed

</details>

<details><summary>SC-DOC-05 — PASS (10)</summary>

- `tests/security/test_ssrf.py::test_gzip_content_encoding_bomb_capped` — passed
- `tests/security/test_ssrf.py::test_oversized_body_aborted_regardless_of_content_length` — passed
- `tests/security/test_ssrf.py::test_wrong_mime_rejected[-hello-MIME_REJECTED]` — passed
- `tests/security/test_ssrf.py::test_wrong_mime_rejected[application/pdf-<html>not a pdf</html>-MIME_MISMATCH]` — passed
- `tests/security/test_ssrf.py::test_wrong_mime_rejected[application/x-msdownload-MZ\x90\x00-MIME_REJECTED]` — passed
- `tests/security/test_ssrf.py::test_wrong_mime_rejected[application/zip-%PDF-1.7-MIME_MISMATCH]` — passed
- `tests/security/test_ssrf.py::test_wrong_mime_rejected[image/svg+xml-<svg/>-MIME_REJECTED]` — passed
- `tests/security/test_ssrf.py::test_wrong_mime_rejected[text/html-\x7fELF\x02\x01\x01-MIME_MISMATCH]` — passed
- `tests/security/test_ssrf.py::test_wrong_mime_rejected[text/markdown-abc\x00def-MIME_MISMATCH]` — passed
- `tests/security/test_ssrf.py::test_wrong_mime_rejected[text/plain-PK\x03\x04binary-MIME_MISMATCH]` — passed

</details>

<details><summary>SC-DOC-06 — PASS (28)</summary>

- `tests/security/test_doc_archives.py::test_archive_sections_and_license` — passed
- `tests/security/test_doc_archives.py::test_corrupted_zip` — passed
- `tests/security/test_doc_archives.py::test_gzip_single_bomb` — passed
- `tests/security/test_doc_archives.py::test_html_document_hidden_count_reported` — passed
- `tests/security/test_doc_archives.py::test_html_sanitization` — passed
- `tests/security/test_doc_archives.py::test_html_unbalanced_markup_does_not_leak_script` — passed
- `tests/security/test_doc_archives.py::test_malicious_archive_quarantined` — passed
- `tests/security/test_doc_archives.py::test_pdf_not_parsed_in_process` — passed
- `tests/security/test_doc_archives.py::test_safe_member_path_normalizes` — passed
- `tests/security/test_doc_archives.py::test_split_sections_skips_empty` — passed
- `tests/security/test_doc_archives.py::test_tar_gz_documents_and_bomb` — passed
- `tests/security/test_doc_archives.py::test_tar_traversal_absolute_and_links_rejected` — passed
- `tests/security/test_doc_archives.py::test_zip_bomb_lying_header_detected` — passed
- `tests/security/test_doc_archives.py::test_zip_bomb_ratio_detected_while_streaming` — passed
- `tests/security/test_doc_archives.py::test_zip_device_rejected` — passed
- `tests/security/test_doc_archives.py::test_zip_documents_extracted_and_others_skipped` — passed
- `tests/security/test_doc_archives.py::test_zip_member_size_cap` — passed
- `tests/security/test_doc_archives.py::test_zip_name_collision_after_normalization` — passed
- `tests/security/test_doc_archives.py::test_zip_slip_and_bad_names_rejected[../evil.md]` — passed
- `tests/security/test_doc_archives.py::test_zip_slip_and_bad_names_rejected[/etc/passwd]` — passed
- `tests/security/test_doc_archives.py::test_zip_slip_and_bad_names_rejected[C:/windows/x.md]` — passed
- `tests/security/test_doc_archives.py::test_zip_slip_and_bad_names_rejected[a\\..\\b.md]` — passed
- `tests/security/test_doc_archives.py::test_zip_slip_and_bad_names_rejected[a\x00b.md]` — passed
- `tests/security/test_doc_archives.py::test_zip_slip_and_bad_names_rejected[docs/../../evil.md]` — passed
- `tests/security/test_doc_archives.py::test_zip_slip_and_bad_names_rejected[x/\x01.md]` — passed
- `tests/security/test_doc_archives.py::test_zip_symlink_rejected` — passed
- `tests/security/test_doc_archives.py::test_zip_too_many_entries` — passed
- `tests/security/test_doc_archives.py::test_zip_total_size_cap` — passed

</details>

<details><summary>SC-DOC-07 — PASS (2)</summary>

- `tests/security/test_doc_archives.py::test_html_sanitization` — passed
- `tests/security/test_doc_archives.py::test_pdf_not_parsed_in_process` — passed

</details>

<details><summary>SC-DOC-08 — PASS (48)</summary>

- `tests/integration/test_docs_tech_api.py::test_importer_end_to_end` — passed
- `tests/unit/test_docs_policy.py::test_gate_requests_approval_only_when_decided` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-False-APPROVED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-False-CANCELLED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-False-EXPIRED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-False-NONE]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-False-PENDING]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-False-REJECTED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-True-APPROVED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-True-CANCELLED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-True-EXPIRED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-True-NONE]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-True-PENDING]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[False-True-REJECTED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-False-APPROVED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-False-CANCELLED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-False-EXPIRED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-False-NONE]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-False-PENDING]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-False-REJECTED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-True-APPROVED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-True-CANCELLED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-True-EXPIRED]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-True-NONE]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-True-PENDING]` — passed
- `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks[True-True-REJECTED]` — passed
- `tests/unit/test_docs_policy.py::test_off_without_any_source_proceeds` — passed
- `tests/unit/test_docs_policy.py::test_smart_downloads_only_after_approved` — passed
- `tests/unit/test_docs_policy.py::test_smart_never_downloads_without_approved[CANCELLED]` — passed
- `tests/unit/test_docs_policy.py::test_smart_never_downloads_without_approved[EXPIRED]` — passed
- `tests/unit/test_docs_policy.py::test_smart_never_downloads_without_approved[NONE]` — passed
- `tests/unit/test_docs_policy.py::test_smart_never_downloads_without_approved[PENDING]` — passed
- `tests/unit/test_docs_policy.py::test_smart_never_downloads_without_approved[REJECTED]` — passed
- `tests/unit/test_docs_policy.py::test_smart_pending_and_rejected` — passed
- `tests/unit/test_docs_policy.py::test_smart_requests_approval_when_missing_and_does_not_block` — passed
- `tests/unit/test_docs_policy.py::test_smart_unregistered_or_disabled_source_skips` — passed
- `tests/unit/test_docs_policy.py::test_smart_uses_cache_first` — passed
- `tests/unit/test_docs_policy.py::test_strict_not_required_behaves_like_smart` — passed
- `tests/unit/test_docs_policy.py::test_strict_required_approved_downloads_or_uses_cache` — passed
- `tests/unit/test_docs_policy.py::test_strict_required_disabled_source_blocks` — passed
- `tests/unit/test_docs_policy.py::test_strict_required_rejected_goes_to_manual_review[CANCELLED]` — passed
- `tests/unit/test_docs_policy.py::test_strict_required_rejected_goes_to_manual_review[EXPIRED]` — passed
- `tests/unit/test_docs_policy.py::test_strict_required_rejected_goes_to_manual_review[REJECTED]` — passed
- `tests/unit/test_docs_policy.py::test_strict_required_unapproved_cache_not_used` — passed
- `tests/unit/test_docs_policy.py::test_strict_required_unapproved_requests_blocking_approval` — passed
- `tests/unit/test_docs_policy.py::test_strict_required_without_source_blocks` — passed
- `tests/unit/test_docs_policy.py::test_subject_binds_source_and_url` — passed
- `tests/unit/test_docs_policy.py::test_unknown_mode_rejected` — passed

</details>

<details><summary>SC-GH-01 — PASS (3)</summary>

- `tests/integration/test_github_api.py::test_webhook_signature_dedup_replay_and_limits` — passed
- `tests/integration/test_observability_signals.py::test_rejected_webhook_signatures_are_counted` — passed
- `tests/unit/test_github_app.py::test_webhook_signature_checks` — passed

</details>

<details><summary>SC-GH-02 — PASS (2)</summary>

- `tests/integration/test_github_api.py::test_webhook_signature_dedup_replay_and_limits` — passed
- `tests/integration/test_github_workflow.py::test_webhook_processing_rereads_state_and_dedups` — passed

</details>

<details><summary>SC-GH-03 — PASS (1)</summary>

- `tests/integration/test_github_workflow.py::test_webhook_processing_rereads_state_and_dedups` — passed

</details>

<details><summary>SC-GH-04 — PASS (4)</summary>

- `tests/integration/test_github_api.py::test_installation_flow_state_and_permissions` — passed
- `tests/unit/test_github_app.py::test_app_permission_policy` — passed
- `tests/unit/test_github_app.py::test_installation_token_is_repo_scoped_and_minimal` — passed
- `tests/unit/test_github_app.py::test_push_uses_askpass_and_leaves_no_token` — passed

</details>

<details><summary>SC-GH-05 — PASS (3)</summary>

- `tests/integration/test_github_api.py::test_pull_request_request_and_ci_config_guard` — passed
- `tests/integration/test_github_workflow.py::test_merge_requires_approval_four_eyes_and_branch_protection` — passed
- `tests/unit/test_github_app.py::test_force_push_over_foreign_history_is_rejected` — passed

</details>

<details><summary>SC-GH-06 — PASS (3)</summary>

- `tests/integration/test_github_api.py::test_api_process_never_loads_github_app_key` — passed
- `tests/integration/test_github_api.py::test_attach_github_repository` — passed
- `tests/unit/test_github_app.py::test_agent_cli_environment_rejects_github_tokens` — passed

</details>

<details><summary>SC-APR-01 — PASS (62)</summary>

- `tests/integration/test_external_actions.py::test_ac13_merge_requires_approved_matching_approval` — passed
- `tests/integration/test_false_pass_prevention.py::test_destructive_migration_requires_approval` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-CRITICAL_DIRECTIVE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-DATA_DELETION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-DESTRUCTIVE_MIGRATION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-DOCS_DOWNLOAD]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-LOCAL_EXECUTION_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-MANUAL_REVIEW]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-MERGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-PLAN_APPROVAL]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-PRODUCTION_DEPLOY]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-SECRET_CHANGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-SELF_REVIEW_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[admin-UNRESTRICTED_NETWORK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-CRITICAL_DIRECTIVE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-DATA_DELETION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-DESTRUCTIVE_MIGRATION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-DOCS_DOWNLOAD]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-LOCAL_EXECUTION_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-MANUAL_REVIEW]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-MERGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-PLAN_APPROVAL]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-PRODUCTION_DEPLOY]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-SECRET_CHANGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-SELF_REVIEW_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[developer-UNRESTRICTED_NETWORK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-CRITICAL_DIRECTIVE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-DATA_DELETION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-DESTRUCTIVE_MIGRATION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-DOCS_DOWNLOAD]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-LOCAL_EXECUTION_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-MANUAL_REVIEW]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-MERGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-PLAN_APPROVAL]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-PRODUCTION_DEPLOY]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-SECRET_CHANGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-SELF_REVIEW_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[owner-UNRESTRICTED_NETWORK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-CRITICAL_DIRECTIVE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-DATA_DELETION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-DESTRUCTIVE_MIGRATION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-DOCS_DOWNLOAD]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-LOCAL_EXECUTION_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-MANUAL_REVIEW]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-MERGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-PLAN_APPROVAL]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-PRODUCTION_DEPLOY]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-SECRET_CHANGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-SELF_REVIEW_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[reviewer-UNRESTRICTED_NETWORK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-CRITICAL_DIRECTIVE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-DATA_DELETION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-DESTRUCTIVE_MIGRATION]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-DOCS_DOWNLOAD]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-LOCAL_EXECUTION_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-MANUAL_REVIEW]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-MERGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-PLAN_APPROVAL]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-PRODUCTION_DEPLOY]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-SECRET_CHANGE]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-SELF_REVIEW_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_role_matrix_per_action_type[viewer-UNRESTRICTED_NETWORK]` — passed

</details>

<details><summary>SC-APR-02 — PASS (19)</summary>

- `tests/integration/test_approvals_api.py::test_expiry_and_tenant_isolation` — passed
- `tests/integration/test_approvals_api.py::test_four_eyes_and_single_member_self_approval` — passed
- `tests/integration/test_approvals_api.py::test_pagination` — passed
- `tests/integration/test_approvals_api.py::test_plan_approval_flow_etag_idempotency_and_hook` — passed
- `tests/integration/test_approvals_api.py::test_role_matrix_by_action_type[CRITICAL_DIRECTIVE-allowed3]` — passed
- `tests/integration/test_approvals_api.py::test_role_matrix_by_action_type[DOCS_DOWNLOAD-allowed1]` — passed
- `tests/integration/test_approvals_api.py::test_role_matrix_by_action_type[MANUAL_REVIEW-allowed0]` — passed
- `tests/integration/test_approvals_api.py::test_role_matrix_by_action_type[MERGE-allowed2]` — passed
- `tests/integration/test_approvals_api.py::test_role_matrix_by_action_type[PRODUCTION_DEPLOY-allowed4]` — passed
- `tests/integration/test_approvals_api.py::test_role_matrix_by_action_type[SECRET_CHANGE-allowed5]` — passed
- `tests/migrations/test_orchestration_invariants.py::test_four_eyes_and_decider_role` — passed
- `tests/unit/test_approvals_rules.py::test_four_eyes_blocks_requester[CRITICAL_DIRECTIVE]` — passed
- `tests/unit/test_approvals_rules.py::test_four_eyes_blocks_requester[DATA_DELETION]` — passed
- `tests/unit/test_approvals_rules.py::test_four_eyes_blocks_requester[DESTRUCTIVE_MIGRATION]` — passed
- `tests/unit/test_approvals_rules.py::test_four_eyes_blocks_requester[LOCAL_EXECUTION_FALLBACK]` — passed
- `tests/unit/test_approvals_rules.py::test_four_eyes_blocks_requester[MERGE]` — passed
- `tests/unit/test_approvals_rules.py::test_four_eyes_blocks_requester[PRODUCTION_DEPLOY]` — passed
- `tests/unit/test_approvals_rules.py::test_four_eyes_blocks_requester[SECRET_CHANGE]` — passed
- `tests/unit/test_approvals_rules.py::test_four_eyes_blocks_requester[UNRESTRICTED_NETWORK]` — passed

</details>

<details><summary>SC-APR-03 — PASS (3)</summary>

- `tests/integration/test_external_actions.py::test_ac13_expired_approval_blocks_execution` — passed
- `tests/integration/test_github_workflow.py::test_merge_rejected_when_head_moved_and_crash_after_merge` — passed
- `tests/unit/test_approvals_rules.py::test_subject_hash_is_canonical` — passed

</details>

<details><summary>SC-APR-04 — PASS (16)</summary>

- `tests/chaos/test_github_recovery.py::test_orchestrator_schedule_runs_github_recovery` — passed
- `tests/chaos/test_github_recovery.py::test_unreachable_github_leaves_action_executing_and_reported` — passed
- `tests/chaos/test_github_recovery.py::test_worker_killed_after_merge_is_reconciled` — passed
- `tests/chaos/test_github_recovery.py::test_worker_killed_mid_pr_action_is_reconciled_without_redelivery[after_pr_create]` — passed
- `tests/chaos/test_github_recovery.py::test_worker_killed_mid_pr_action_is_reconciled_without_redelivery[after_push]` — passed
- `tests/chaos/test_github_recovery.py::test_worker_killed_mid_pr_action_is_reconciled_without_redelivery[after_review]` — passed
- `tests/chaos/test_github_recovery.py::test_worker_killed_mid_pr_action_is_reconciled_without_redelivery[after_status]` — passed
- `tests/chaos/test_github_recovery.py::test_worker_killed_mid_pr_action_is_reconciled_without_redelivery[before_pr_create]` — passed
- `tests/integration/test_external_actions.py::test_ac13_expired_approval_blocks_execution` — passed
- `tests/integration/test_external_actions.py::test_ac13_merge_requires_approved_matching_approval` — passed
- `tests/integration/test_external_actions.py::test_crash_before_external_call_executes_after_probe` — passed
- `tests/integration/test_external_actions.py::test_crash_during_executing_is_reconciled_by_probe` — passed
- `tests/integration/test_external_actions.py::test_execute_once_never_repeats_confirmed` — passed
- `tests/integration/test_external_actions.py::test_failure_is_recorded_and_retry_probes_first` — passed
- `tests/integration/test_external_actions.py::test_plan_is_idempotent_by_deterministic_key` — passed
- `tests/integration/test_external_actions.py::test_stale_token_cannot_confirm_or_take_over` — passed

</details>

<details><summary>SC-APR-05 — PASS (4)</summary>

- `tests/e2e/test_routing_review.py::test_basic_profile_self_review_only_after_explicit_approval` — passed
- `tests/e2e/test_routing_review.py::test_review_goes_to_the_other_provider_and_decisions_are_recorded` — passed
- `tests/e2e/test_routing_review.py::test_single_provider_strict_requires_manual_review` — passed
- `tests/unit/test_routing.py::test_self_review_is_never_marked_independent` — passed

</details>

<details><summary>SC-APR-06 — PASS (2)</summary>

- `tests/integration/test_approvals_api.py::test_plan_approval_flow_etag_idempotency_and_hook` — passed
- `tests/security/test_step_up.py::test_critical_approval_requires_step_up_non_critical_does_not` — passed

</details>

<details><summary>SC-AUD-01 — PASS (1)</summary>

- `tests/migrations/test_migrations.py::test_audit_events_append_only` — passed

</details>

<details><summary>SC-AUD-03 — PASS (3)</summary>

- `tests/integration/test_api_projects.py::test_audit_trail_records_actions` — passed
- `tests/security/test_audit_coverage.py::test_dynamic_actions_are_real_commands` — passed
- `tests/security/test_audit_coverage.py::test_every_unsafe_operation_is_audited` — passed

</details>

<details><summary>SC-AUD-04 — PASS (1)</summary>

- `tests/security/test_cross_tenant_api.py::test_foreign_workspace_is_invisible` — passed

</details>

<details><summary>SC-SUP-01 — PASS (1)</summary>

- `tests/unit/test_ci_gates.py::test_real_lockfiles_are_parsed` — passed

</details>

<details><summary>SC-SUP-02 — PASS (2)</summary>

- `tests/security/test_security_gates_docker.py::test_osv_scanner_offline_db_finds_vulnerability` — passed
- `tests/unit/test_security_gates.py::test_sca_and_iac_verdicts` — passed

</details>

<details><summary>SC-SUP-03 — PASS (1)</summary>

- `tests/security/test_security_gates_docker.py::test_syft_sbom_is_cyclonedx_artifact` — passed

</details>

<details><summary>SC-SUP-04 — PASS (1)</summary>

- `tests/security/test_security_gates_docker.py::test_cosign_verifies_blob_offline_and_rejects_tampering` — passed

</details>

<details><summary>SC-SUP-07 — PASS (10)</summary>

- `tests/integration/test_gate_runners_docker.py::test_destructive_migration_applies_but_requires_approval` — passed
- `tests/integration/test_gate_runners_docker.py::test_eslint_in_pinned_container[fail-FAIL]` — passed
- `tests/integration/test_gate_runners_docker.py::test_eslint_in_pinned_container[pass-PASS]` — passed
- `tests/integration/test_gate_runners_docker.py::test_migrations_on_ephemeral_postgres[broken_down-FAIL-migration/downgrade-incomplete]` — passed
- `tests/integration/test_gate_runners_docker.py::test_migrations_on_ephemeral_postgres[failing-FAIL-migration/upgrade1-failed]` — passed
- `tests/integration/test_gate_runners_docker.py::test_migrations_on_ephemeral_postgres[pass-PASS-None]` — passed
- `tests/integration/test_gate_runners_docker.py::test_ruff_in_pinned_container[fail-FAIL]` — passed
- `tests/integration/test_gate_runners_docker.py::test_ruff_in_pinned_container[pass-PASS]` — passed
- `tests/integration/test_gate_runners_docker.py::test_unpinned_image_is_rejected_by_server_policy` — passed
- `tests/unit/test_security_gates.py::test_container_spec_is_read_only_offline_and_passes_policy_lint` — passed

</details>

<details><summary>SC-SUP-08 — PASS (2)</summary>

- `tests/unit/test_ci_gates.py::test_current_dependencies_pass_gate` — passed
- `tests/unit/test_ci_gates.py::test_gate_detects_forbidden_sdks` — passed

</details>

<details><summary>SC-DATA-01 — PASS (4)</summary>

- `tests/contract/test_architecture.py::test_every_table_has_owner` — passed
- `tests/contract/test_architecture.py::test_import_contracts` — passed
- `tests/contract/test_architecture.py::test_import_linter_detects_violation` — passed
- `tests/contract/test_architecture.py::test_ownership_is_disjoint` — passed

</details>

<details><summary>SC-DATA-03 — PASS (12)</summary>

- `tests/unit/test_backup_crypto.py::test_artifact_compare_reports_missing_mismatch_and_orphans` — passed
- `tests/unit/test_backup_crypto.py::test_key_file_permissions_and_no_overwrite` — passed
- `tests/unit/test_backup_crypto.py::test_manifest_signature_and_repo_key_binding` — passed
- `tests/unit/test_backup_crypto.py::test_roundtrip_chunk_boundaries[0]` — passed
- `tests/unit/test_backup_crypto.py::test_roundtrip_chunk_boundaries[1048575]` — passed
- `tests/unit/test_backup_crypto.py::test_roundtrip_chunk_boundaries[1048576]` — passed
- `tests/unit/test_backup_crypto.py::test_roundtrip_chunk_boundaries[1048577]` — passed
- `tests/unit/test_backup_crypto.py::test_roundtrip_chunk_boundaries[1]` — passed
- `tests/unit/test_backup_crypto.py::test_roundtrip_chunk_boundaries[3145728]` — passed
- `tests/unit/test_backup_crypto.py::test_status_file_records_failure_then_success` — passed
- `tests/unit/test_backup_crypto.py::test_tamper_truncate_append_and_wrong_key_are_detected` — passed
- `tests/unit/test_backup_crypto.py::test_wal_helpers` — passed

</details>

<details><summary>SC-DATA-04 — PASS (7)</summary>

- `tests/integration/test_retention.py::test_archive_sink_rejects_traversal` — passed
- `tests/integration/test_retention.py::test_partition_cutoffs` — passed
- `tests/integration/test_retention.py::test_policy_overrides_are_validated` — passed
- `tests/integration/test_retention.py::test_retention_is_idempotent` — passed
- `tests/integration/test_retention.py::test_retention_job_on_seeded_db` — passed
- `tests/integration/test_retention.py::test_retention_requires_maintenance_role` — passed
- `tests/integration/test_retention.py::test_retention_webhooks_snapshots_and_artifacts` — passed

</details>

<details><summary>SC-DATA-05 — PASS (4)</summary>

- `tests/integration/test_artifact_download.py::test_integrity_mismatch_aborts_stream` — passed
- `tests/integration/test_artifacts_s3.py::test_integrity_mismatch_detected` — passed
- `tests/integration/test_false_pass_prevention.py::test_artifact_tampering_detected_by_completion_guard` — passed
- `tests/unit/test_artifacts_fs.py::test_immutable_and_integrity` — passed

</details>

<details><summary>SC-DATA-06 — PASS (2)</summary>

- `tests/integration/test_artifact_download.py::test_download_streams_content_with_headers_and_audit` — passed
- `tests/integration/test_artifact_download.py::test_raw_cli_log_requires_admin` — passed

</details>

<details><summary>SC-DATA-07 — PASS (1)</summary>

- `tests/integration/test_retention.py::test_retention_job_on_seeded_db` — passed

</details>

<details><summary>SC-DATA-08 — PASS (10)</summary>

- `tests/integration/test_external_actions.py::test_stale_token_cannot_confirm_or_take_over` — passed
- `tests/integration/test_gateway.py::test_concurrency_limit_counts_all_workspaces` — passed
- `tests/integration/test_gateway.py::test_db_rejects_invalid_transition_and_stale_token` — passed
- `tests/integration/test_gateway.py::test_execution_rows_isolated_by_workspace` — passed
- `tests/integration/test_gateway.py::test_failures_recorded[auth_required-AUTH_REQUIRED-AUTH_REQUIRED]` — passed
- `tests/integration/test_gateway.py::test_failures_recorded[nonzero_exit-COLLECTED-PROCESS_FAILED]` — passed
- `tests/integration/test_gateway.py::test_failures_recorded[rate_limited-RATE_LIMITED-RATE_LIMITED]` — passed
- `tests/integration/test_gateway.py::test_success_persists_fsm_artifacts_diff_usage` — passed
- `tests/integration/test_gateway.py::test_timeout_recorded` — passed
- `tests/migrations/test_orchestration_invariants.py::test_lease_fencing_token_monotonic` — passed

</details>

<details><summary>SC-DATA-09 — PASS (4)</summary>

- `tests/chaos/test_redis_restart.py::test_event_written_during_redis_outage_is_delivered_once_after_restart` — passed
- `tests/integration/test_acp_broker.py::test_duplicate_delivery_has_one_effect` — passed
- `tests/integration/test_outbox_dispatcher.py::test_publish_failure_backoff_then_redelivery_without_duplicates` — passed
- `tests/integration/test_worker_actors.py::test_stub_broker_duplicate_delivery_processed_once` — passed

</details>

<details><summary>SC-OBS-01 — PASS (2)</summary>

- `tests/security/test_secret_leakage.py::test_error_responses_and_logs_never_echo_secrets` — passed
- `tests/unit/test_redaction.py::test_json_log_line_has_correlation_and_no_secrets` — passed

</details>

<details><summary>SC-OBS-02 — PASS (4)</summary>

- `tests/integration/test_monitoring_promtool.py::test_promtool_rules_are_valid_and_alerts_fire` — passed
- `tests/integration/test_observability_signals.py::test_rejected_webhook_signatures_are_counted` — passed
- `tests/integration/test_observability_signals.py::test_signature_failure_reason_is_low_cardinality` — passed
- `tests/integration/test_observability_signals.py::test_sse_gauge_has_stream_kind_label` — passed

</details>

<details><summary>SC-OBS-03 — PASS (3)</summary>

- `tests/security/test_secret_leakage.py::test_metrics_labels_carry_no_secrets_or_unbounded_ids` — passed
- `tests/unit/test_metrics.py::test_label_values_are_bounded` — passed
- `tests/unit/test_telemetry.py::test_span_attributes_are_allowlisted` — passed

</details>

<details><summary>SC-API-01 — PASS (19)</summary>

- `tests/integration/test_api_workspaces.py::test_unknown_fields_rejected` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[connectGitHubInstallation]` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[disconnectGitHubInstallation]` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[getGitHubVerification]` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[listGitHubInstallations]` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[listPullRequests]` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[receiveGitHubWebhook]` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[requestPullRequestMerge]` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[requestPullRequest]` — passed
- `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform[startGitHubInstallation]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[cancelRun]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[createRun]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[getRunTasks]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[getRun]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[getTask]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[listRuns]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[listTaskAttempts]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[pauseRun]` — passed
- `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform[resumeRun]` — passed

</details>

<details><summary>SC-API-02 — PASS (4)</summary>

- `tests/integration/test_oq45_api.py::test_probe_rbac_rate_limit_and_audit` — passed
- `tests/security/test_rate_limits.py::test_login_is_rate_limited_per_ip` — passed
- `tests/security/test_rate_limits.py::test_mutations_are_rate_limited_per_user` — passed
- `tests/security/test_step_up.py::test_reauthenticate_is_rate_limited_per_user` — passed

</details>

<details><summary>SC-API-03 — PASS (2)</summary>

- `tests/integration/test_sse.py::test_replay_window_exceeded_and_stream_limit` — passed
- `tests/integration/test_workspace_events.py::test_workspace_stream_respects_connection_limit` — passed

</details>

<details><summary>SC-API-04 — PASS (3)</summary>

- `tests/integration/test_api_projects.py::test_security_headers_and_problem_format` — passed
- `tests/security/test_cross_tenant_api.py::test_unknown_resource_and_foreign_resource_look_identical` — passed
- `tests/security/test_secret_leakage.py::test_error_responses_and_logs_never_echo_secrets` — passed

</details>

<details><summary>SC-API-05 — PASS (2)</summary>

- `tests/integration/test_api_projects.py::test_idempotency_replay_reuse_and_missing` — passed
- `tests/integration/test_runs_api.py::test_create_run_is_idempotent_and_key_reuse_with_other_body_is_rejected` — passed

</details>

<details><summary>SC-API-06 — PASS (2)</summary>

- `tests/integration/test_api_projects.py::test_project_crud_with_etag` — passed
- `tests/integration/test_runs_api.py::test_get_run_etag_and_commands_with_if_match` — passed

</details>

<details><summary>SC-API-07 — PASS (3)</summary>

- `tests/integration/test_sse.py::test_non_member_gets_404_and_activity` — passed
- `tests/integration/test_sse.py::test_revoked_member_stream_closes` — passed
- `tests/integration/test_workspace_events.py::test_workspace_stream_closes_after_member_removed` — passed

</details>

<details><summary>SC-API-08 — PASS (1)</summary>

- `tests/integration/test_api_projects.py::test_security_headers_and_problem_format` — passed

</details>

<details><summary>SC-API-09 — PASS (2)</summary>

- `tests/integration/test_api_workspaces.py::test_unknown_fields_rejected` — passed
- `tests/security/test_cross_tenant_api.py::test_foreign_workspace_is_invisible` — passed

</details>

## 5. Упавшие тесты

Нет.

## 6. Пропуски (по причине)

- 1 × AICD_TRIVY_DB_DIR (офлайн-БД trivy) не задан

## 7. Тесты tests/security без сопоставленного контроля

- `tests/security/test_catalog.py::test_catalog_matches_security_controls_document`
- `tests/security/test_catalog.py::test_every_referenced_test_exists`
- `tests/security/test_catalog.py::test_every_tz20_category_has_automated_tests`
- `tests/security/test_catalog.py::test_kinds_are_consistent`
- `tests/security/test_catalog.py::test_nodeid_matching`
- `tests/security/test_command_injection.py::test_valid_branch_names_pass`
- `tests/security/test_sandbox.py::test_output_streams_separated`
- `tests/security/test_sandbox.py::test_runner_through_socket_proxy`
- `tests/security/test_security_gate.py::test_accepted_risk_needs_owner_decision_in_release_mode`
- `tests/security/test_security_gate.py::test_closed_requires_passing_tests_when_junit_given`
- `tests/security/test_security_gate.py::test_current_threat_model_has_no_open_high_or_critical`
- `tests/security/test_security_gate.py::test_registry_must_match_threat_tables`
- `tests/security/test_security_gate.py::test_violations_fail_the_gate[high-closed-SC-IAM-08-\u043d\u0435 \u043c\u043e\u0436\u0435\u0442 \u0431\u044b\u0442\u044c closed]`
- `tests/security/test_security_gate.py::test_violations_fail_the_gate[low--SC-IAM-08-\u043e\u0442\u043a\u0440\u044b\u0442\u0430\u044f \u0443\u0433\u0440\u043e\u0437\u0430]`
- `tests/security/test_security_gate.py::test_violations_fail_the_gate[low-accepted:OQ-77-\u2014-\u043d\u0435\u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0439 OQ-77]`
- `tests/security/test_security_gate.py::test_violations_fail_the_gate[low-closed-SC-AUD-02-\u0431\u0435\u0437 \u043a\u043e\u043d\u0442\u0440\u043e\u043b\u044f \u0441 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u043c\u0438 \u0442\u0435\u0441\u0442\u0430\u043c\u0438]`
- `tests/security/test_security_gate.py::test_violations_fail_the_gate[low-closed-SC-XXX-01-\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0435 \u043a\u043e\u043d\u0442\u0440\u043e\u043b\u0438]`
- `tests/security/test_security_gate.py::test_violations_fail_the_gate[low-open-SC-IAM-08-\u043e\u0442\u043a\u0440\u044b\u0442\u0430\u044f \u0443\u0433\u0440\u043e\u0437\u0430]`
- `tests/security/test_security_gates_docker.py::test_checkov_finds_iac_issues`
- `tests/security/test_security_gates_docker.py::test_checkov_timeout_is_error`
- `tests/security/test_security_gates_docker.py::test_conftest_policy_change_in_diff_is_flagged`
- `tests/security/test_security_gates_docker.py::test_conftest_policy_violation_and_compliant`
- `tests/security/test_security_gates_docker.py::test_cosign_unsigned_image_layout_is_not_pass`
- `tests/security/test_security_gates_docker.py::test_gitleaks_clean_passes`
- `tests/security/test_security_gates_docker.py::test_osv_scanner_without_db_is_error`
- `tests/security/test_security_gates_docker.py::test_perf_confirms_real_speedup`
- `tests/security/test_security_gates_docker.py::test_perf_rejects_unconfirmed_claim`
- `tests/security/test_security_gates_docker.py::test_semgrep_broken_config_is_error`
- `tests/security/test_security_gates_docker.py::test_semgrep_clean_passes_and_basic_profile_threshold`
- `tests/security/test_security_gates_docker.py::test_semgrep_finds_known_issues`
- `tests/security/test_security_gates_docker.py::test_trivy_config_finds_misconfigurations`
- `tests/security/test_security_gates_docker.py::test_trivy_vuln_with_offline_db`
- `tests/security/test_security_gates_docker.py::test_trivy_vuln_without_db_is_error`
- `tests/security/test_ssrf.py::test_default_policy_blocks_local_server`
- `tests/security/test_ssrf.py::test_dns_failure`
- `tests/security/test_ssrf.py::test_env_proxy_is_ignored`
- `tests/security/test_ssrf.py::test_fetch_through_pinned_transport`
- `tests/security/test_ssrf.py::test_http_error_status`
- `tests/security/test_ssrf.py::test_name_resolving_to_blocked_address[127.0.0.1]`
- `tests/security/test_ssrf.py::test_name_resolving_to_blocked_address[169.254.169.254]`
- `tests/security/test_ssrf.py::test_name_resolving_to_blocked_address[::1]`
- `tests/security/test_ssrf.py::test_name_resolving_to_blocked_address[::ffff:127.0.0.1]`
- `tests/security/test_ssrf.py::test_name_resolving_to_blocked_address[fd00:ec2::254]`
- `tests/security/test_ssrf.py::test_policy_cannot_be_widened_beyond_limits`
- `tests/security/test_ssrf.py::test_public_addresses_allowed[1.1.1.1]`
- `tests/security/test_ssrf.py::test_public_addresses_allowed[2606:4700:4700::1111]`
- `tests/security/test_ssrf.py::test_public_addresses_allowed[2a00:1450:4001:82a::200e]`
- `tests/security/test_ssrf.py::test_public_addresses_allowed[93.184.216.34]`
- `tests/security/test_ssrf.py::test_redirect_followed_through_full_pipeline`
