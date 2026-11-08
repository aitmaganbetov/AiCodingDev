# IMPLEMENTATION_BACKLOG — AiCodingDev v1.0

**Назначение:** единый реестр задач разработки (мастер-промпт §6). Источник требований — [REQUIREMENTS_REGISTRY.md](REQUIREMENTS_REGISTRY.md); фазы — [DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md).

## Правила ведения

1. Статусы: `TODO` → `IN_PROGRESS` → `IN_REVIEW` → `DONE`; также `BLOCKED` (с причиной/OQ).
2. **`DONE` только с доказательствами:** commit SHA, команда проверки, фактический результат (exit code / сводка отчёта), ссылка на evidence/CI run. Без этого статус не выше `IN_REVIEW`.
3. При переходе в `IN_PROGRESS`/`DONE` заполняются колонки «Файлы» и «Проверки» в разделе [Журнал выполнения](#журнал-выполнения).
4. Каждая задача ссылается на ≥ 1 требование; новые требования сначала вносятся в реестр.
5. Общий DoD (ТЗ §21) для каждой задачи с кодом: код + тесты + контракты + миграции (если затронуты) + threat assessment изменения + документация + evidence + review + approvals. Голословные заявления AI не заменяют ни один пункт.

Приоритет: **P0** — критический путь; **P1** — обязательно для v1.0; **P2** — опционально.

---

## Phase 0 — Architecture & Planning

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P0-01 | Анализ ТЗ и реестр FR/NFR | все | — | P0 | Реестр покрывает §1–§23 ТЗ | DONE |
| P0-02 | Системная архитектура, модули, потоки данных, state machines | FR-ORCH-*, NFR-MAINT-* | P0-01 | P0 | SYSTEM_ARCHITECTURE, MODULE_BOUNDARIES, DATA_FLOW | DONE |
| P0-03 | ADR-0001…0014 | все | P0-02 | P0 | ADR в `docs/architecture/adr/` | DONE |
| P0-04 | Логическая схема PostgreSQL + ERD | FR-IAM-04, ТЗ §7 | P0-02 | P0 | Все таблицы каталога + дополнения, RLS, индексы | DONE |
| P0-05 | OpenAPI 3.1 + API design | FR-API-* | P0-04 | P0 | `openapi.yaml` проходит валидатор | DONE |
| P0-06 | CLI contract, ACP v1.0, event schemas | FR-CLI-03, FR-ACP-01, FR-CHAT-07 | P0-02 | P0 | JSON Schema валидны (Draft 2020-12) | DONE |
| P0-07 | Роли агентов, routing policy, quality gates, standards | FR-AGENT-*, FR-ROUTE-*, FR-QG-*, FR-STD-* | P0-06 | P0 | 12 ролей, L1–L5, профили gates | DONE |
| P0-08 | Security architecture, threat model, controls | NFR-SEC-* | P0-02 | P0 | STRIDE, каталог SC-* | DONE |
| P0-09 | Roadmap, backlog, dependency graph, acceptance criteria | все | P0-01 | P0 | Документы planning/ | DONE |
| P0-10 | Утверждение владельцем: OQ-01…OQ-16, ADR Proposed → Accepted | — | P0-01…09 | P0 | Записи в журнале решений OPEN_QUESTIONS §3 | DONE |

## Phase 1 — Project Foundation

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P1-01 | Скелет монорепозитория: `apps/`, `packages/`, Python 3.12 (uv workspace), pnpm для web; ruff, mypy --strict, pre-commit (gitleaks) | NFR-MAINT-02 | P0-10 | P0 | `uv sync`, `ruff check`, `mypy` проходят на пустых модулях | DONE |
| P1-02 | `docker compose` профиль `local`: postgres 17, redis 7, artifacts FS/MinIO, reverse proxy; `.env.example`, секреты через файлы | NFR-PORT-01 | P1-01 | P0 | `docker compose up` → health всех сервисов | IN_REVIEW |
| P1-03 | `packages/contracts`: перенос OpenAPI/JSON Schema, codegen Pydantic/TS, CI drift-check, `oasdiff` | FR-API-01, FR-API-04, ADR-0013 | P1-01 | P0 | CI падает при расхождении схем и кода | DONE |
| P1-04 | CI: lint, typecheck, unit, contract, migration tests, osv-scanner, gitleaks, Syft SBOM, deny-gate SDK AI-провайдеров | NFR-SEC-01, NFR-SEC-05, NFR-TEST-01 | P1-01 | P0 | Зелёный pipeline; тестовый PR с `openai`/`anthropic` SDK блокируется | IN_REVIEW |
| P1-05 | `import-linter` контракты границ + тест владения таблицами | NFR-MAINT-02 | P1-01 | P0 | Нарушение границы → CI fail (негативный тест) | DONE |
| P1-06 | `platform`: DB-сессия + RLS-контекст, UUIDv7, problem+json, конфиг, JSON-логи с redaction и корреляцией, OTel baseline | FR-OBS-01, FR-ORCH-12, NFR-OBS-01 | P1-01 | P0 | Unit-тесты redaction; лог содержит `run_id/task_id` при наличии | DONE |
| P1-07 | Alembic + harness тестов миграций (upgrade/downgrade/seeded) + DB-роли `aicd_*` | NFR-REL-04 | P1-02 | P0 | `pytest tests/migrations` зелёный | DONE |
| P1-08 | Миграции IAM/projects/audit/idempotency/artifacts + RLS | FR-IAM-01, FR-IAM-04, FR-AUD-01 | P1-07 | P0 | RLS-тесты на каждую таблицу | DONE |
| P1-09 | `identity`: локальный вход (argon2id), сессии, CSRF, lockout, `aicd admin create-owner` | FR-IAM-02 | P1-08 | P0 | Негативные тесты CSRF/lockout/фиксации сессии | DONE |
| P1-10 | RBAC: матрица прав как код, `authorize()`, тест-матрица endpoint × роль | FR-IAM-03, FR-IAM-05 | P1-09 | P0 | 100% эндпоинтов покрыты матрицей (тест генерируется из OpenAPI `x-required-permission`) | DONE |
| P1-11 | API skeleton: middleware (auth, CSRF, Idempotency-Key, rate limit, ETag), health, workspaces, members, projects, audit-events | FR-API-01, FR-API-02, FR-PRJ-01, FR-AUD-02 | P1-10, P1-03 | P0 | Schemathesis по реализованным операциям без ошибок | DONE |
| P1-12 | Привязка локального репозитория: allowlist путей, проверка Git, защита от traversal/symlink | FR-PRJ-02 | P1-11 | P0 | Security-тесты path traversal | DONE |
| P1-13 | Web shell: React/TS/Vite/Tailwind, роутинг, вход, layout, i18n `ru`, клиент из OpenAPI, axe baseline | NFR-UX-01, ADR-0014 | P1-03 | P0 | Playwright smoke: вход → список проектов; axe без нарушений уровня serious | DONE |
| P1-14 | Скрипт матрицы трассировки требования → задачи → тесты; CI-проверка покрытия P0/P1 | NFR-HONEST-01, NFR-AUD-01 | P1-04 | P1 | Отчёт генерируется в CI | DONE |
| P1-15 | Artifact store: FS + S3/MinIO адаптеры, sha256, presigned download с TTL | FR-QG-05 | P1-06 | P0 | Integration-тест обоих адаптеров | DONE |
| P1-16 | Cross-tenant negative test suite (API + RLS) | FR-IAM-05, NFR-ISO-01 | P1-11 | P0 | Все тесты отрицательны (доступ запрещён) | DONE |
| P1-17 | Документы ТЗ §21: `data-model.md`, `state-machines.md`, `permissions-matrix.md` (ссылки на нормативные разделы), каркас `docs/runbooks/` | NFR-MAINT-01 | P0-10 | P1 | Ссылки валидны (link-check в CI) | DONE |
| P1-18 | `oasdiff breaking` в CI против спецификации целевой ветки; генерация Pydantic-моделей из JSON Schema (`datamodel-code-generator`) для CLI/ACP/событий (перенесено из P1-03: потребители появляются в P2) | FR-API-04, NFR-MAINT-01 | P1-03 | P0 | CI блокирует ломающее изменение контракта; сгенерированные модели без расхождений | TODO |

## Phase 2 — CLI Integration

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P2-01 | **Spike:** probe реальных Codex/Claude CLI на Linux — noninteractive режимы, машинный вывод, хранение авторизации, взаимодействие со встроенным sandbox CLI; compat matrix v0; подтверждение/правка ADR-0010 | FR-CLI-04, RISK-02/03 | P0-10 (OQ-01, OQ-03) | P0 | Отчёт spike + compat matrix + обновлённый ADR | TODO |
| P2-02 | Миграции: `cli_providers`, `worker_hosts`, `cli_installations`, `cli_sessions`, `cli_executions`, `provider_health`, `usage_metrics`, `worktrees` | FR-CLI-02 | P1-08 | P0 | Migration + RLS-тесты | TODO |
| P2-03 | `CLIExecutor` Protocol, модели контракта, нормализованные события и таксономия ошибок | FR-CLI-02, FR-CLI-03, FR-CLI-05 | P1-03 | P0 | Contract-тесты JSON Schema request/result | TODO |
| P2-04 | `FakeCLIExecutor` со сценариями: success, timeout, auth required, rate limited, crash, parse error, большая выдача | FR-CLI-09 | P2-03 | P0 | Используется всеми последующими integration-тестами | TODO |
| P2-05 | Codex adapter: probe, check_auth, command profiles по compat matrix, парсер, usage если доступно | FR-CLI-01, FR-CLI-04, FR-CLI-07 | P2-01, P2-03 | P0 | Contract-тесты на sanitized фикстурах; opt-in smoke | TODO |
| P2-06 | Claude Code adapter (аналогично P2-05) | FR-CLI-01, FR-CLI-04, FR-CLI-07 | P2-01, P2-03 | P0 | То же | TODO |
| P2-07 | Runner-образы (сборка у пользователя, pinned, SBOM), builder контейнерной спецификации (`security`), docker-socket-proxy | FR-SBX-02, FR-SBX-03, OQ-12 | P2-01 | P0 | `docker inspect` подтверждает cap-drop, read-only, limits | TODO |
| P2-08 | Egress proxy + сети compose (`sandbox` internal) + allowlist провайдеров | FR-SBX-02 | P2-07 | P0 | Тест: исходящее соединение на неразрешённый хост блокируется | TODO |
| P2-09 | Worktree manager: create/lock/remove, ветки `aicd/...`, обнаружение осиротевших, path guard | FR-SBX-01 | P2-02 | P0 | Integration-тесты на временном репозитории | TODO |
| P2-10 | Жизненный цикл процесса: timeout, cancel, kill дерева, heartbeat, raw log → artifact, redaction, лимиты вывода | FR-CLI-05, FR-CLI-06, NFR-SEC-02 | P2-04, P1-15 | P0 | Тесты: timeout → `TIMED_OUT`, секрет в выводе не попадает в события | TODO |
| P2-11 | `worker-exec` gateway: очередь, лимиты параллелизма на установку, Execution FSM | FR-ORCH-03 | P2-10 | P0 | Integration с Fake: все исходы FSM | TODO |
| P2-12 | Providers API: `GET /providers`, `POST /providers/{id}/probe`, `PATCH /providers/{id}/settings`, периодические health probes | FR-CLI-10 | P2-05, P2-06 | P0 | API-тесты + RBAC; кредов нет в ответах | TODO |
| P2-13 | Opt-in suite с реальными CLI (`pytest -m real_cli`), документированный запуск | FR-CLI-09, NFR-TEST-01 | P2-05, P2-06 | P0 | Протокол запуска на машине с подписками (evidence) | TODO |
| P2-14 | Security-тесты sandbox: запись вне worktree, сеть, отсутствие кредов в verification-контейнерах, отсутствие privileged | FR-SBX-02, FR-AGENT-04 | P2-08, P2-09 | P0 | Все попытки отклонены | TODO |

## Phase 3 — Core Orchestrator

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P3-01 | Миграции: runs, tasks, task_dependencies (+trigger), task_attempts, policies, approvals, checkpoints, outbox_events, worker_leases, external_actions, run_event_log, processed_events, user_directives, directive_impacts, directive_acknowledgements, agent_decisions | FR-ORCH-01…13 | P1-08 | P0 | Migration + RLS + constraint-тесты (self-dependency, cross-run) | TODO |
| P3-02 | FSM-движок Run/Task/Execution/Directive/Approval (таблицы переходов + guards) | FR-ORCH-01, FR-ORCH-02 | P3-01 | P0 | Property-based тесты (Hypothesis): ни одна последовательность не нарушает таблицы | TODO |
| P3-03 | Outbox writer + dispatcher + Dramatiq + базовый идемпотентный consumer | FR-ORCH-08 | P3-01 | P0 | Тест дублирующей доставки: эффект один | TODO |
| P3-04 | Leases + fencing token + reaper | FR-ORCH-09 | P3-01 | P0 | Тест «зомби-воркера»: запись со старым токеном отклонена | TODO |
| P3-05 | Валидация DAG (Кан, лимиты, plan_version) | FR-ORCH-04 | P3-02 | P0 | Unit: циклы, self-loop, cross-run, глубина | TODO |
| P3-06 | Scheduler: готовность, приоритет критического пути, лимиты параллелизма | FR-ORCH-04 | P3-05, P3-04 | P0 | Integration: задачи стартуют только после зависимостей | TODO |
| P3-07 | Retry/backoff/retry budget/timeouts, `failure_category` | FR-ORCH-02, AC-07 | P3-06, P2-11 | P0 | Fake timeout → RETRY_WAIT → FAILED по бюджету; успех не объявлен | TODO |
| P3-08 | Runs API: POST/GET runs, GET tasks, pause/resume/cancel, activity; policy snapshot | FR-ORCH-06, FR-ORCH-13, FR-API-01 | P3-02, P1-11 | P0 | Schemathesis + idempotency + ETag тесты | TODO |
| P3-09 | SSE: run_event_log, Last-Event-ID, heartbeat, периодическая проверка прав, Redis pub/sub | FR-CHAT-07 | P3-03 | P0 | Тест реконнекта: нет дублей и пропусков (AC-08) | TODO |
| P3-10 | Approvals core: policy, four-eyes, expiry, привязка к plan_version; API `/approvals` | FR-APR-01, FR-APR-02, FR-APR-03 | P3-02, P1-10 | P0 | Тесты self-approval запрещён; истечение; RBAC | TODO |
| P3-11 | Ledger `external_actions` + reconciler-интерфейс | FR-ORCH-10 | P3-03 | P0 | Тест: crash после CONFIRMED → действие не повторяется | TODO |
| P3-12 | Checkpoints + recovery reconciler (executions, worktrees, outbox, actions) | FR-ORCH-07 | P3-11, P2-09 | P0 | Chaos: kill worker в RUNNING → корректное восстановление | TODO |
| P3-13 | Directives backend: intake, детерминированный impact analysis, confirm, safe point, replan, dispatch, ack; API `/runs/{id}/directives` | FR-CHAT-03, FR-CHAT-04 | P3-12, P3-10 | P0 | Integration AC-06 на Fake | TODO |
| P3-14 | Verification Engine v0: обобщённый `command` gate в verification-контейнере + evidence (расширяется в P6) | FR-QG-02, FR-QG-05, FR-ORCH-11 | P2-07, P1-15 | P0 | Evidence содержит commit, команду, exit code, время, sha256 | TODO |
| P3-15 | Минимальный Run view UI (вертикальный срез): статус run, задачи, лента событий SSE | FR-CHAT-07 | P3-09, P1-13 | P1 | Playwright: события отображаются, реконнект без дублей | TODO |
| P3-16 | Chaos suite v1: kill worker, рестарт Redis, потеря аренды, дубль сообщения, разрыв SSE | NFR-REL-01, NFR-REL-02 | P3-12 | P0 | Все сценарии зелёные в CI (nightly) | TODO |
| P3-17 | E2E-срез на Fake CLI: run → plan → DAG → exec → gate v0 → COMPLETED / BLOCKED / FAILED | AC-07, AC-11, AC-16 | P3-13, P3-14 | P0 | E2E-тест зелёный | TODO |
| P3-18 | Retention/архивирование: партиции и job'ы для `run_event_log`, `agent_messages`, `agent_activity_events`, `outbox_events`, `audit_events` (роль `aicd_maintenance`, аудит) | NFR-REL-05 | P3-01 | P1 | Тест job'а на seeded БД; аудит использования BYPASSRLS | TODO |

## Phase 4 — Multi-Agent System

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P4-01 | Миграции: agent_profiles, agent_assignments, routing_decisions, agent_messages, message_deliveries, chat_threads, chat_messages, agent_activity_events | FR-AGENT-01, FR-ACP-01 | P3-01 | P0 | Migration + RLS + CHECK 32 KiB | TODO |
| P4-02 | 12 профилей ролей + шаблоны инструкций v1 + версионирование + seed | FR-AGENT-01, FR-AGENT-02 | P4-01 | P0 | Snapshot-тесты шаблонов; ревью человеком | TODO |
| P4-03 | Сборка промпта с fencing недоверенных данных; контракт файла результата `.aicd/result.json`; парсер claims/ACP | FR-ACP-06, NFR-SEC-03 | P4-02, P2-03 | P0 | Prompt-injection corpus не меняет policy-блок | TODO |
| P4-04 | ACP: 12 payload-схем, брокер (schema → size → ACL → policy → correlation → loop limits → persist+outbox) | FR-ACP-01, FR-ACP-02, FR-ACP-03 | P4-01, P3-03 | P0 | Contract + negative тесты по каждому типу | TODO |
| P4-05 | Доставка ACP: deliveries, retries, DLQ, проекция в чат и activity | FR-ACP-05, FR-CHAT-02 | P4-04 | P0 | Дубль доставки → один эффект | TODO |
| P4-06 | Классификатор сложности L1–L5 + риск-скоринг | FR-ROUTE-01 | P4-02 | P0 | Табличные unit-тесты | TODO |
| P4-07 | Router: hard filters, scoring, tie-break, `routing_decisions`, policy snapshot | FR-ROUTE-02, FR-ROUTE-03, FR-ROUTE-05, FR-CLI-08 | P4-06, P2-12 | P0 | Оба недоступны → BLOCKED (AC-16) | TODO |
| P4-08 | Независимое ревью и fallback-политики (`MANUAL_REVIEW`, `SELF_REVIEW_FALLBACK`) | FR-ROUTE-04, FR-AGENT-03 | P4-07, P3-10 | P0 | Самопроверка никогда не маркируется independent | TODO |
| P4-09 | Шаблоны пайплайнов 11 режимов для planner | FR-ORCH-05 | P4-02 | P0 | Для каждого режима — тест генерации валидного DAG | TODO |
| P4-10 | Review/fix loops и лимиты | FR-ACP-04 | P4-05 | P0 | 4-й цикл → BLOCKED REVIEW_LOOP_EXHAUSTED | TODO |
| P4-11 | Метрика истории качества (verified completion rate, сглаживание) | FR-ROUTE-02 | P4-07, P3-14 | P1 | Unit-тесты формулы | TODO |
| P4-12 | API: `GET /tasks/{id}`, `/tasks/{id}/messages`, `/tasks/{id}/attempts`, `/runs/{id}/chat`, `POST /runs/{id}/chat/messages` | FR-API-01, FR-CHAT-01 | P4-05 | P0 | Schemathesis + RBAC | TODO |
| P4-13 | Handoff-тесты (Fake) + real-CLI smoke: Codex developer → Claude reviewer (AC-03, AC-04) | AC-03, AC-04, AC-05 | P4-08, P2-13 | P0 | Evidence opt-in прогона | TODO |

## Phase 5 — Code Intelligence

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P5-01 | Миграции code intelligence, documentation, technologies | FR-CI-01, FR-DOC-01 | P1-08 | P0 | Migration + RLS тесты | TODO |
| P5-02 | Snapshots по commit, file tree, инкрементальное хэширование, `indexing_jobs`, `worker-index` | FR-CI-01, FR-CI-05 | P5-01 | P0 | Повторная индексация меняет только изменённые файлы | TODO |
| P5-03 | tree-sitter символы для языков OQ-09 | FR-CI-02 | P5-02 | P0 | Golden-тесты на фикстурах каждого языка | TODO |
| P5-04 | ripgrep (безопасный argv) + FTS/pg_trgm | FR-CI-03 | P5-02 | P0 | Тест command injection через запрос | TODO |
| P5-05 | Граф зависимостей + test links | FR-CI-04 | P5-03 | P0 | Golden-тесты | TODO |
| P5-06 | Ранжирование, бюджет, Context Pack builder, кэш, маркировка stale | FR-CI-06, FR-CI-07 | P5-04, P5-05 | P0 | Тест: фрагмент другого commit помечен/исключён | TODO |
| P5-07 | Определение стека (`technologies`, `project_technologies`), `GET /projects/{id}/technologies` | FR-PRJ-04 | P5-02 | P0 | Фикстуры Python/TS/PHP | TODO |
| P5-08 | LSP-интеграция (опционально, по наличию) | FR-CI-02 | P5-03 | P1 | Отключение LSP не ломает индекс | TODO |
| P5-09 | Documentation policy OFF/SMART/STRICT + approval `DOCS_DOWNLOAD`; `GET/PATCH /projects/{id}/documentation-policy` | FR-DOC-01, FR-DOC-02, FR-DOC-03 | P5-01, P3-10 | P0 | AC-10 тесты | TODO |
| P5-10 | Безопасный импортёр документации: SSRF guard, MIME/size, архивы, HTML sanitize, лицензии; `/documentation/sources`, refresh | FR-DOC-04, FR-DOC-05, FR-DOC-06 | P5-09 | P1 | Security-тесты SSRF/zip bomb/traversal | TODO |
| P5-11 | Интеграция context packs в исполнение (ro mount) + метрики размера | FR-CI-06 | P5-06, P2-11 | P0 | Integration на Fake | TODO |
| P5-12 | Freshness/retrieval тесты, AC-02, AC-10 | AC-02, AC-10, NFR-DOC-01 | P5-11 | P0 | Зелёные | TODO |

## Phase 6 — Quality, Security & Standards

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P6-01 | Миграции: quality_profiles, quality_gates, gate_executions, test_results, findings, evidence, evidence_links, standards*, compliance_results | FR-QG-*, FR-STD-* | P3-01 | P0 | Migration + RLS | TODO |
| P6-02 | Verification Engine: профили, определение применимости по стеку, план gates | FR-QG-01, FR-QG-03 | P6-01, P3-14, P5-07 | P0 | Матрица применимости покрыта тестами | TODO |
| P6-03 | Gate runners: Ruff, ESLint, PHPStan, typecheck, build, pytest, Vitest, Playwright | FR-QG-03 | P6-02 | P0 | Фикстуры pass/fail для каждого | TODO |
| P6-04 | Security gates: Semgrep, Gitleaks, Trivy, OSV-Scanner, Checkov, OPA | FR-QG-03 | P6-02 | P0 | Фикстуры с заведомыми находками | TODO |
| P6-05 | Migration validation gate (эфемерный PG, upgrade/downgrade) | FR-QG-03 | P6-02 | P0 | Деструктивная миграция требует approval | TODO |
| P6-06 | Performance gate (k6/бенчмарк) при заявлении об ускорении | FR-QG-03 | P6-02 | P1 | Воспроизводимые измерения в evidence | TODO |
| P6-07 | Supply chain gates: Syft SBOM, Cosign verify | NFR-SEC-05 | P6-02 | P1 | SBOM как artifact | TODO |
| P6-08 | Evidence, verdict rules (SKIPPED≠PASS, ERROR≠PASS), жизненный цикл findings | FR-QG-04, FR-QG-05, FR-QG-07 | P6-02 | P0 | Unit-тесты правил | TODO |
| P6-09 | Guard завершения run + coverage gate (core ≥ 85%) | FR-ORCH-11, FR-QG-06 | P6-08 | P0 | AC-11 | TODO |
| P6-10 | Standards registry seed (собственные формулировки), применимость проекта, compliance evaluation | FR-STD-01, FR-STD-02, FR-STD-03, FR-STD-04 | P6-01 | P0 | Трассировка standard→…→verdict в отчёте | TODO |
| P6-11 | API: `/runs/{id}/quality`, `/compliance`, `/report`, `/tasks/{id}/evidence`, `/evidence/{id}` | FR-QG-07, FR-STD-05, FR-API-03 | P6-08, P6-10 | P0 | Schemathesis + RBAC | TODO |
| P6-12 | False-pass prevention suite | FR-QG-04, RISK-09 | P6-09 | P0 | Ни один сценарий не даёт PASS | TODO |

## Phase 7 — Mission Control & Live Agent Chat

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P7-01 | Дизайн-система, layout, состояния пусто/ошибка/загрузка, responsive, клавиатура | FR-CHAT-08, NFR-UX-01 | P1-13 | P0 | axe + keyboard Playwright | TODO |
| P7-02 | SSE-клиент: менеджер потоков, Last-Event-ID, дедуп, реконнект | FR-CHAT-07 | P3-09 | P0 | Тест обрыва сети | TODO |
| P7-03 | Dashboard | FR-CHAT-08 | P7-01 | P0 | E2E | TODO |
| P7-04 | Projects + Repository Explorer | FR-PRJ-05 | P7-01, P5-02 | P0 | E2E | TODO |
| P7-05 | Run Graph (React Flow, статусы, текущий исполнитель) | FR-CHAT-06 | P7-02 | P0 | E2E | TODO |
| P7-06 | Live Agent Chat: потоки, фильтры, поиск, timeline, упоминания ролей | FR-CHAT-01, FR-CHAT-02, FR-CHAT-06, FR-CHAT-09 | P7-02, P4-12 | P0 | AC-05 E2E | TODO |
| P7-07 | Директивы UI: команды, impact preview, подтверждение, статусы delivered/acknowledged/completed | FR-CHAT-03, FR-CHAT-05 | P7-06, P3-13 | P0 | AC-06 E2E | TODO |
| P7-08 | Task Detail + diff viewer + ссылки на evidence | FR-CHAT-06 | P7-05, P6-11 | P0 | E2E | TODO |
| P7-09 | CLI Providers screen | FR-CLI-10 | P2-12 | P0 | E2E | TODO |
| P7-10 | Evidence & Quality, Standards & Compliance screens | FR-QG-07, FR-STD-05 | P6-11 | P0 | E2E | TODO |
| P7-11 | Approvals screen/поток | FR-APR-03 | P3-10 | P0 | AC-13 E2E | TODO |
| P7-12 | Knowledge/Documentation screen | FR-DOC-06 | P5-10 | P1 | E2E | TODO |
| P7-13 | Settings/Audit screen | FR-AUD-02 | P1-11 | P0 | E2E | TODO |
| P7-14 | E2E-набор Mission Control + impact/replan UI тесты | AC-05, AC-06, AC-08 | P7-* | P0 | Зелёный | TODO |

## Phase 8 — GitHub Integrations & Analytics

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P8-01 | Миграции GitHub-таблиц | FR-GH-01 | P1-08 | P0 | Migration + RLS | TODO |
| P8-02 | GitHub App (минимальные права), установка, привязка репозиториев | FR-GH-01, FR-PRJ-03 | P8-01 | P0 | Integration на тестовой организации (opt-in) | TODO |
| P8-03 | Webhooks: HMAC SHA-256, constant-time, дедуп, защита от replay | FR-GH-02 | P8-01 | P0 | Security-тесты spoofing/replay | TODO |
| P8-04 | PR workflow через `external_actions`: push, PR, комментарии ревью, статусы | FR-GH-03 | P8-02, P3-11 | P0 | Crash-тест: PR не создаётся дважды | TODO |
| P8-05 | Merge только с approval, с уважением branch protection | FR-GH-03, FR-APR-01 | P8-04 | P0 | AC-13 | TODO |
| P8-06 | Метрики ТЗ §16 (Prometheus), OTel трассы end-to-end | FR-OBS-02, FR-OBS-03 | P1-06 | P0 | Метрики видны в Prometheus | TODO |
| P8-07 | Analytics API + экран | FR-OBS-05, FR-CI-08 | P8-06, P7-01 | P0 | E2E | TODO |
| P8-08 | Grafana dashboards + алерты | FR-OBS-04 | P8-06 | P1 | Тестовый алерт срабатывает | TODO |
| P8-09 | Permission-тесты GitHub-действий | FR-GH-03 | P8-05 | P0 | Зелёные | TODO |

## Phase 9 — Verification & Release

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P9-01 | Полный acceptance-набор AC-01…AC-16 (Fake + opt-in real CLI) | все AC | P1…P8 | P0 | 100% зелёные, evidence | TODO |
| P9-02 | Security suite ТЗ §20 (SSRF, prompt injection, traversal, command injection, cross-tenant, secret leakage, webhook spoofing) | NFR-SEC-*, NFR-ISO-01 | P1…P8 | P0 | 0 открытых critical | TODO |
| P9-03 | Chaos/recovery suite полный | NFR-REL-01, NFR-REL-02 | P3-16 | P0 | Зелёный | TODO |
| P9-04 | Performance baseline и предложение SLO | NFR-PERF-01 | P9-01 | P1 | Отчёт с методикой | TODO |
| P9-05 | Backup/restore + DR drill | NFR-REL-03 | P1-02 | P0 | Протокол восстановления | TODO |
| P9-06 | Runbooks: recovery, backups, CLI auth, incident response | NFR-MAINT-01 | P9-05 | P0 | Ревью оператором | TODO |
| P9-07 | Ревизия threat model, закрытие critical/high | NFR-SEC-04, NFR-SEC-06 | P9-02 | P0 | Release security gate | TODO |
| P9-08 | Supply chain релиза: SBOM, подпись, provenance | NFR-SEC-05 | P6-07 | P1 | Проверяемая подпись | TODO |
| P9-09 | Release checklist, инструкция установки, README | NFR-MAINT-01 | P9-01…08 | P0 | Утверждение владельцем | TODO |
| P9-10 | Подтверждение coverage core ≥ 85% и 100% обязательных тестов | FR-QG-06 | P9-01 | P0 | Отчёт покрытия | TODO |

---

## Журнал выполнения

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) | Commit |
|---|---|---|---|---|
| P0-01…P0-10 | DONE (утверждено 2026-11-07) | `docs/**` (см. `docs/README.md`) | `python -m openapi_spec_validator docs/api/openapi.yaml` → `OK`; встроенные JSON Schema (22) → `Draft202012Validator.check_schema`: 0 ошибок; примеры против схем + 3 негативных кейса → OK; `$ref` (OpenAPI 184, протоколы 111) → 0 неразрешённых; Mermaid (протоколы/агенты, 13 блоков) → parse OK; FK в DATABASE_DESIGN → все ссылаются на определённые таблицы (76 `CREATE TABLE`); покрытие требований backlog-ом → 0 непокрытых. Исполняемого кода и тестов нет. | ветка `ccr-69fbf9a8-p2650i` |

### Phase 1 — Project Foundation (2026-11-08)

Окружение проверки: PostgreSQL 17.11 (контейнер), Redis 7, RustFS (S3-совместимый), Python 3.13 (requires-python ≥ 3.12), Node 22, Chromium (Playwright 1.63.0).

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) |
|---|---|---|---|
| P1-01 | DONE | `pyproject.toml`, `uv.lock`, `apps/*`, `packages/*`, `.pre-commit-config.yaml` | `uv sync --frozen`; `ruff check .` → All checks passed; `mypy` (strict) → no issues in 77 files |
| P1-02 | IN_REVIEW | `infrastructure/docker/*`, `infrastructure/postgres/*` | `docker compose up` → postgres/redis healthy, migrate exit 0, api healthy, web up; `/health/ready` → `ok`; `aicd admin create-owner` + вход + проект через nginx; `pnpm e2e:live` → 1 passed. **Оговорка:** шаг `apt-get install git` рабочего Dockerfile не проверен — egress-политика среды блокирует deb.debian.org; стек проверен образом-вариантом без этого шага. Закрыть после сборки в CI (job `images`) |
| P1-03 | DONE | `packages/contracts/**`, `docs/api/openapi.yaml` (symlink), `apps/web/src/api/schema.d.ts` | `tests/contract/test_contracts.py` → 29 passed (OpenAPI 3.1 валидна; 22 JSON Schema валидны и совпадают с документами; 18 операций приложения совпадают с контрактом по пути/методу/праву); каждый ответ API в интеграционных тестах валидируется по контракту; `pnpm check:api` → соответствует. oasdiff и Pydantic-codegen → P1-18 |
| P1-04 | IN_REVIEW | `.github/workflows/ci.yml`, `scripts/check_no_ai_sdk.py`, `.gitleaks.toml` | Workflow на GitHub ещё не запускался. Локально выполнены эквиваленты всех шагов: ruff, mypy, import-linter, deny-gate (110 py / 498 js пакетов, 0 нарушений; негативный тест), pytest, alembic `--sql`, gitleaks (no leaks), pip-audit и pnpm audit (no known vulnerabilities), Syft SBOM (687 пакетов) |
| P1-05 | DONE | `pyproject.toml [tool.importlinter]`, `OWNED_TABLES`, `tests/contract/test_architecture.py` | `lint-imports` → 4 kept, 0 broken; негативный тест нарушения → BROKEN; каждая таблица БД принадлежит ровно одному модулю |
| P1-06 | DONE | `packages/platform/**` | `tests/unit/test_redaction.py`, `test_ids.py`, `test_pagination.py`, `test_config.py` — passed; лог содержит request_id/run_id, секреты маскируются. OTel: локальный провайдер + console-экспорт; OTLP — P8-06 |
| P1-07 | DONE | `infrastructure/migrations/**`, `infrastructure/postgres/bootstrap-*.sql` | `tests/migrations` → upgrade→downgrade→upgrade, offline dry-run, роли без BYPASSRLS — passed |
| P1-08 | DONE | `0001_foundation.py` (12 таблиц, домены, функции, RLS, SECURITY DEFINER) | `tests/security/test_rls.py` → 22 passed (A/B × app/worker × таблицы, fail-closed без контекста, составные FK, sys_* функции, last owner). Исправлен дефект документа БД: CHECK `{1,1024}` превышает лимит повторений PostgreSQL (255) |
| P1-09 | DONE | `packages/identity/**`, `apps/api/aicd_api/routers/auth.py`, `apps/cli/**` | `tests/integration/test_api_auth.py` → cookie HttpOnly/Secure/SameSite=Strict, CSRF и Origin, блокировка после 5 неудач, idle-timeout, logout; `test_cli.py` — bootstrap владельца, слабый пароль отклонён |
| P1-10 | DONE | `packages/identity/aicd_identity/rbac.py`, `apps/api/aicd_api/deps.py` | `tests/unit/test_rbac.py` → 135 ячеек матрицы + approvals/директивы; контрактный тест прав всех 55 операций OpenAPI; HTTP-тесты ролей |
| P1-11 | DONE | `apps/api/**` | `tests/integration/test_api_*.py` → 30 passed: idempotency (повтор/реюз/отсутствие/сохранение 409), ETag/If-Match/428/412/304, cursor-пагинация и подделка курсора, problem+json, 413/415, заголовки безопасности, health, аудит |
| P1-12 | DONE | `packages/security/**`, `packages/projects/aicd_projects/api.py` | traversal, вне корня, symlink-побег, не-Git, вложенный каталог → 422 без раскрытия путей хоста; Git запускается без хуков/конфигов репозитория |
| P1-13 | DONE | `apps/web/**` | `pnpm typecheck`, `lint`, `build` → 0; `pnpm test` → 29 passed; `pnpm e2e` → 6 passed (axe без serious/critical, 360px, клавиатура); `pnpm e2e:live` против compose → 1 passed |
| P1-14 | DONE | `scripts/traceability.py` | 125 требований, 130 задач, непокрытых P0/P1: 0; тест скрипта |
| P1-15 | DONE | `packages/platform/aicd_platform/artifacts.py` | FS: неизменяемость, проверка sha256, отклонение чужих/побеговых ссылок; S3: put/get/presigned URL/integrity на RustFS (S3-совместимый). MinIO недоступен в среде (образы не скачиваются) |
| P1-16 | DONE | `tests/security/test_cross_tenant_api.py`, `test_rls.py` | 11 операций против чужого workspace → 404 (как для несуществующего ID), изменений нет; удалённый участник теряет доступ |
| P1-17 | DONE | `docs/architecture/{data-model,state-machines,permissions-matrix}.md`, `docs/runbooks/*` | `tests/contract/test_docs_links.py` → все относительные ссылки разрешаются |

**Итог прогона Python-тестов:** `uv run pytest --cov` → **299 passed**, покрытие 88% (branch), из них с реальными PostgreSQL 17, Redis и S3.
