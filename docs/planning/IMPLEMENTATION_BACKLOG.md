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
| P1-18 | `oasdiff breaking` в CI против спецификации целевой ветки; генерация Pydantic-моделей из JSON Schema (`datamodel-code-generator`) для CLI/ACP/событий (перенесено из P1-03: потребители появляются в P2) | FR-API-04, NFR-MAINT-01 | P1-03 | P0 | CI блокирует ломающее изменение контракта; сгенерированные модели без расхождений | DONE |

## Phase 2 — CLI Integration

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P2-01 | **Spike:** probe реальных Codex/Claude CLI на Linux — noninteractive режимы, машинный вывод, хранение авторизации, взаимодействие со встроенным sandbox CLI; compat matrix v0; подтверждение/правка ADR-0010 | FR-CLI-04, RISK-02/03 | P0-10 (OQ-01, OQ-03) | P0 | Отчёт spike + compat matrix + обновлённый ADR | BLOCKED |
| P2-02 | Миграции: `cli_providers`, `worker_hosts`, `cli_installations`, `cli_sessions`, `cli_executions`, `provider_health`, `usage_metrics`, `worktrees` | FR-CLI-02 | P1-08 | P0 | Migration + RLS-тесты | DONE |
| P2-03 | `CLIExecutor` Protocol, модели контракта, нормализованные события и таксономия ошибок | FR-CLI-02, FR-CLI-03, FR-CLI-05 | P1-03 | P0 | Contract-тесты JSON Schema request/result | DONE |
| P2-04 | `FakeCLIExecutor` со сценариями: success, timeout, auth required, rate limited, crash, parse error, большая выдача | FR-CLI-09 | P2-03 | P0 | Используется всеми последующими integration-тестами | DONE |
| P2-05 | Codex adapter: probe, check_auth, command profiles по compat matrix, парсер, usage если доступно | FR-CLI-01, FR-CLI-04, FR-CLI-07 | P2-01, P2-03 | P0 | Contract-тесты на sanitized фикстурах; opt-in smoke | BLOCKED |
| P2-06 | Claude Code adapter (аналогично P2-05) | FR-CLI-01, FR-CLI-04, FR-CLI-07 | P2-01, P2-03 | P0 | То же | BLOCKED |
| P2-07 | Runner-образы (сборка у пользователя, pinned, SBOM), builder контейнерной спецификации (`security`), docker-socket-proxy | FR-SBX-02, FR-SBX-03, OQ-12 | P2-01 | P0 | `docker inspect` подтверждает cap-drop, read-only, limits | IN_PROGRESS |
| P2-08 | Egress proxy + сети compose (`sandbox` internal) + allowlist провайдеров | FR-SBX-02 | P2-07 | P0 | Тест: исходящее соединение на неразрешённый хост блокируется | DONE |
| P2-09 | Worktree manager: create/lock/remove, ветки `aicd/...`, обнаружение осиротевших, path guard | FR-SBX-01 | P2-02 | P0 | Integration-тесты на временном репозитории | DONE |
| P2-10 | Жизненный цикл процесса: timeout, cancel, kill дерева, heartbeat, raw log → artifact, redaction, лимиты вывода | FR-CLI-05, FR-CLI-06, NFR-SEC-02 | P2-04, P1-15 | P0 | Тесты: timeout → `TIMED_OUT`, секрет в выводе не попадает в события | DONE |
| P2-11 | `worker-exec` gateway: очередь, лимиты параллелизма на установку, Execution FSM | FR-ORCH-03 | P2-10 | P0 | Integration с Fake: все исходы FSM | DONE |
| P2-12 | Providers API: `GET /providers`, `POST /providers/{id}/probe`, `PATCH /providers/{id}/settings`, периодические health probes | FR-CLI-10 | P2-05, P2-06 | P0 | API-тесты + RBAC; кредов нет в ответах | IN_PROGRESS |
| P2-13 | Opt-in suite с реальными CLI (`pytest -m real_cli`), документированный запуск | FR-CLI-09, NFR-TEST-01 | P2-05, P2-06 | P0 | Протокол запуска на машине с подписками (evidence) | BLOCKED |
| P2-14 | Security-тесты sandbox: запись вне worktree, сеть, отсутствие кредов в verification-контейнерах, отсутствие privileged | FR-SBX-02, FR-AGENT-04 | P2-08, P2-09 | P0 | Все попытки отклонены | DONE |

## Phase 3 — Core Orchestrator

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P3-01 | Миграции: runs, tasks, task_dependencies (+trigger), task_attempts, policies, approvals, checkpoints, outbox_events, worker_leases, external_actions, run_event_log, processed_events, user_directives, directive_impacts, directive_acknowledgements, agent_decisions | FR-ORCH-01…13 | P1-08 | P0 | Migration + RLS + constraint-тесты (self-dependency, cross-run) | DONE |
| P3-02 | FSM-движок Run/Task/Execution/Directive/Approval (таблицы переходов + guards) | FR-ORCH-01, FR-ORCH-02 | P3-01 | P0 | Property-based тесты (Hypothesis): ни одна последовательность не нарушает таблицы | DONE |
| P3-03 | Outbox writer + dispatcher + Dramatiq + базовый идемпотентный consumer | FR-ORCH-08 | P3-01 | P0 | Тест дублирующей доставки: эффект один | DONE |
| P3-04 | Leases + fencing token + reaper | FR-ORCH-09 | P3-01 | P0 | Тест «зомби-воркера»: запись со старым токеном отклонена | DONE |
| P3-05 | Валидация DAG (Кан, лимиты, plan_version) | FR-ORCH-04 | P3-02 | P0 | Unit: циклы, self-loop, cross-run, глубина | DONE |
| P3-06 | Scheduler: готовность, приоритет критического пути, лимиты параллелизма | FR-ORCH-04 | P3-05, P3-04 | P0 | Integration: задачи стартуют только после зависимостей | DONE |
| P3-07 | Retry/backoff/retry budget/timeouts, `failure_category` | FR-ORCH-02, AC-07 | P3-06, P2-11 | P0 | Fake timeout → RETRY_WAIT → FAILED по бюджету; успех не объявлен | DONE |
| P3-08 | Runs API: POST/GET runs, GET tasks, pause/resume/cancel, activity; policy snapshot | FR-ORCH-06, FR-ORCH-13, FR-API-01 | P3-02, P1-11 | P0 | Schemathesis + idempotency + ETag тесты | DONE |
| P3-09 | SSE: run_event_log, Last-Event-ID, heartbeat, периодическая проверка прав, Redis pub/sub | FR-CHAT-07 | P3-03 | P0 | Тест реконнекта: нет дублей и пропусков (AC-08) | IN_REVIEW |
| P3-10 | Approvals core: policy, four-eyes, expiry, привязка к plan_version; API `/approvals` | FR-APR-01, FR-APR-02, FR-APR-03 | P3-02, P1-10 | P0 | Тесты self-approval запрещён; истечение; RBAC | DONE |
| P3-11 | Ledger `external_actions` + reconciler-интерфейс | FR-ORCH-10 | P3-03 | P0 | Тест: crash после CONFIRMED → действие не повторяется | DONE |
| P3-12 | Checkpoints + recovery reconciler (executions, worktrees, outbox, actions) | FR-ORCH-07 | P3-11, P2-09 | P0 | Chaos: kill worker в RUNNING → корректное восстановление | DONE |
| P3-13 | Directives backend: intake, детерминированный impact analysis, confirm, safe point, replan, dispatch, ack; API `/runs/{id}/directives` | FR-CHAT-03, FR-CHAT-04 | P3-12, P3-10 | P0 | Integration AC-06 на Fake | DONE |
| P3-14 | Verification Engine v0: обобщённый `command` gate в verification-контейнере + evidence (расширяется в P6) | FR-QG-02, FR-QG-05, FR-ORCH-11 | P2-07, P1-15 | P0 | Evidence содержит commit, команду, exit code, время, sha256 | DONE |
| P3-15 | Минимальный Run view UI (вертикальный срез): статус run, задачи, лента событий SSE | FR-CHAT-07 | P3-09, P1-13 | P1 | Playwright: события отображаются, реконнект без дублей | DONE |
| P3-16 | Chaos suite v1: kill worker, рестарт Redis, потеря аренды, дубль сообщения, разрыв SSE | NFR-REL-01, NFR-REL-02 | P3-12 | P0 | Все сценарии зелёные в CI (nightly) | IN_REVIEW |
| P3-17 | E2E-срез на Fake CLI: run → plan → DAG → exec → gate v0 → COMPLETED / BLOCKED / FAILED | AC-07, AC-11, AC-16 | P3-13, P3-14 | P0 | E2E-тест зелёный | DONE |
| P3-18 | Retention/архивирование: партиции и job'ы для `run_event_log`, `agent_messages`, `agent_activity_events`, `outbox_events`, `audit_events` (роль `aicd_maintenance`, аудит) | NFR-REL-05 | P3-01 | P1 | Тест job'а на seeded БД; аудит использования BYPASSRLS | IN_PROGRESS |

## Phase 4 — Multi-Agent System

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P4-01 | Миграции: agent_profiles, agent_assignments, routing_decisions, agent_messages, message_deliveries, chat_threads, chat_messages, agent_activity_events | FR-AGENT-01, FR-ACP-01 | P3-01 | P0 | Migration + RLS + CHECK 32 KiB | DONE |
| P4-02 | 12 профилей ролей + шаблоны инструкций v1 + версионирование + seed | FR-AGENT-01, FR-AGENT-02 | P4-01 | P0 | Snapshot-тесты шаблонов; ревью человеком | IN_REVIEW |
| P4-03 | Сборка промпта с fencing недоверенных данных; контракт файла результата `.aicd/result.json`; парсер claims/ACP | FR-ACP-06, NFR-SEC-03 | P4-02, P2-03 | P0 | Prompt-injection corpus не меняет policy-блок | DONE |
| P4-04 | ACP: 12 payload-схем, брокер (schema → size → ACL → policy → correlation → loop limits → persist+outbox) | FR-ACP-01, FR-ACP-02, FR-ACP-03 | P4-01, P3-03 | P0 | Contract + negative тесты по каждому типу | DONE |
| P4-05 | Доставка ACP: deliveries, retries, DLQ, проекция в чат и activity | FR-ACP-05, FR-CHAT-02 | P4-04 | P0 | Дубль доставки → один эффект | DONE |
| P4-06 | Классификатор сложности L1–L5 + риск-скоринг | FR-ROUTE-01 | P4-02 | P0 | Табличные unit-тесты | DONE |
| P4-07 | Router: hard filters, scoring, tie-break, `routing_decisions`, policy snapshot | FR-ROUTE-02, FR-ROUTE-03, FR-ROUTE-05, FR-CLI-08 | P4-06, P2-12 | P0 | Оба недоступны → BLOCKED (AC-16) | DONE |
| P4-08 | Независимое ревью и fallback-политики (`MANUAL_REVIEW`, `SELF_REVIEW_FALLBACK`) | FR-ROUTE-04, FR-AGENT-03 | P4-07, P3-10 | P0 | Самопроверка никогда не маркируется independent | DONE |
| P4-09 | Шаблоны пайплайнов 11 режимов для planner | FR-ORCH-05 | P4-02 | P0 | Для каждого режима — тест генерации валидного DAG | DONE |
| P4-10 | Review/fix loops и лимиты | FR-ACP-04 | P4-05 | P0 | 4-й цикл → BLOCKED REVIEW_LOOP_EXHAUSTED | DONE |
| P4-11 | Метрика истории качества (verified completion rate, сглаживание) | FR-ROUTE-02 | P4-07, P3-14 | P1 | Unit-тесты формулы | DONE |
| P4-12 | API: `GET /tasks/{id}`, `/tasks/{id}/messages`, `/tasks/{id}/attempts`, `/runs/{id}/chat`, `POST /runs/{id}/chat/messages` | FR-API-01, FR-CHAT-01 | P4-05 | P0 | Schemathesis + RBAC | DONE |
| P4-13 | Handoff-тесты (Fake) + real-CLI smoke: Codex developer → Claude reviewer (AC-03, AC-04) | AC-03, AC-04, AC-05 | P4-08, P2-13 | P0 | Evidence opt-in прогона | BLOCKED |

## Phase 5 — Code Intelligence

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P5-01 | Миграции code intelligence, documentation, technologies | FR-CI-01, FR-DOC-01 | P1-08 | P0 | Migration + RLS тесты | DONE |
| P5-02 | Snapshots по commit, file tree, инкрементальное хэширование, `indexing_jobs`, `worker-index` | FR-CI-01, FR-CI-05 | P5-01 | P0 | Повторная индексация меняет только изменённые файлы | DONE |
| P5-03 | tree-sitter символы для языков OQ-09 | FR-CI-02 | P5-02 | P0 | Golden-тесты на фикстурах каждого языка | DONE |
| P5-04 | ripgrep (безопасный argv) + FTS/pg_trgm | FR-CI-03 | P5-02 | P0 | Тест command injection через запрос | DONE |
| P5-05 | Граф зависимостей + test links | FR-CI-04 | P5-03 | P0 | Golden-тесты | DONE |
| P5-06 | Ранжирование, бюджет, Context Pack builder, кэш, маркировка stale | FR-CI-06, FR-CI-07 | P5-04, P5-05 | P0 | Тест: фрагмент другого commit помечен/исключён | DONE |
| P5-07 | Определение стека (`technologies`, `project_technologies`), `GET /projects/{id}/technologies` | FR-PRJ-04 | P5-02 | P0 | Фикстуры Python/TS/PHP | DONE |
| P5-08 | LSP-интеграция (опционально, по наличию) | FR-CI-02 | P5-03 | P1 | Отключение LSP не ломает индекс | TODO |
| P5-09 | Documentation policy OFF/SMART/STRICT + approval `DOCS_DOWNLOAD`; `GET/PATCH /projects/{id}/documentation-policy` | FR-DOC-01, FR-DOC-02, FR-DOC-03 | P5-01, P3-10 | P0 | AC-10 тесты | DONE |
| P5-10 | Безопасный импортёр документации: SSRF guard, MIME/size, архивы, HTML sanitize, лицензии; `/documentation/sources`, refresh | FR-DOC-04, FR-DOC-05, FR-DOC-06 | P5-09 | P1 | Security-тесты SSRF/zip bomb/traversal | DONE |
| P5-11 | Интеграция context packs в исполнение (ro mount) + метрики размера | FR-CI-06 | P5-06, P2-11 | P0 | Integration на Fake | DONE |
| P5-12 | Freshness/retrieval тесты, AC-02, AC-10 | AC-02, AC-10, NFR-DOC-01 | P5-11 | P0 | Зелёные | DONE |

## Phase 6 — Quality, Security & Standards

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P6-01 | Миграции: quality_profiles, quality_gates, gate_executions, test_results, findings, evidence, evidence_links, standards*, compliance_results | FR-QG-*, FR-STD-* | P3-01 | P0 | Migration + RLS | DONE |
| P6-02 | Verification Engine: профили, определение применимости по стеку, план gates | FR-QG-01, FR-QG-03 | P6-01, P3-14, P5-07 | P0 | Матрица применимости покрыта тестами | DONE |
| P6-03 | Gate runners: Ruff, ESLint, PHPStan, typecheck, build, pytest, Vitest, Playwright | FR-QG-03 | P6-02 | P0 | Фикстуры pass/fail для каждого | IN_PROGRESS |
| P6-04 | Security gates: Semgrep, Gitleaks, Trivy, OSV-Scanner, Checkov, OPA | FR-QG-03 | P6-02 | P0 | Фикстуры с заведомыми находками | DONE |
| P6-05 | Migration validation gate (эфемерный PG, upgrade/downgrade) | FR-QG-03 | P6-02 | P0 | Деструктивная миграция требует approval | DONE |
| P6-06 | Performance gate (k6/бенчмарк) при заявлении об ускорении | FR-QG-03 | P6-02 | P1 | Воспроизводимые измерения в evidence | DONE |
| P6-07 | Supply chain gates: Syft SBOM, Cosign verify | NFR-SEC-05 | P6-02 | P1 | SBOM как artifact | DONE |
| P6-08 | Evidence, verdict rules (SKIPPED≠PASS, ERROR≠PASS), жизненный цикл findings | FR-QG-04, FR-QG-05, FR-QG-07 | P6-02 | P0 | Unit-тесты правил | DONE |
| P6-09 | Guard завершения run + coverage gate (core ≥ 85%) | FR-ORCH-11, FR-QG-06 | P6-08 | P0 | AC-11 | DONE |
| P6-10 | Standards registry seed (собственные формулировки), применимость проекта, compliance evaluation | FR-STD-01, FR-STD-02, FR-STD-03, FR-STD-04 | P6-01 | P0 | Трассировка standard→…→verdict в отчёте | DONE |
| P6-11 | API: `/runs/{id}/quality`, `/compliance`, `/report`, `/tasks/{id}/evidence`, `/evidence/{id}` | FR-QG-07, FR-STD-05, FR-API-03 | P6-08, P6-10 | P0 | Schemathesis + RBAC | DONE |
| P6-12 | False-pass prevention suite | FR-QG-04, RISK-09 | P6-09 | P0 | Ни один сценарий не даёт PASS | DONE |

## Phase 7 — Mission Control & Live Agent Chat

| ID | Задача | Требования | Зависит от | Pri | Критерии готовности | Статус |
|---|---|---|---|---|---|---|
| P7-01 | Дизайн-система, layout, состояния пусто/ошибка/загрузка, responsive, клавиатура | FR-CHAT-08, NFR-UX-01 | P1-13 | P0 | axe + keyboard Playwright | DONE |
| P7-02 | SSE-клиент: менеджер потоков, Last-Event-ID, дедуп, реконнект | FR-CHAT-07 | P3-09 | P0 | Тест обрыва сети | DONE |
| P7-03 | Dashboard | FR-CHAT-08 | P7-01 | P0 | E2E | DONE |
| P7-04 | Projects + Repository Explorer | FR-PRJ-05 | P7-01, P5-02 | P0 | E2E | DONE |
| P7-05 | Run Graph (React Flow, статусы, текущий исполнитель) | FR-CHAT-06 | P7-02 | P0 | E2E | DONE |
| P7-06 | Live Agent Chat: потоки, фильтры, поиск, timeline, упоминания ролей | FR-CHAT-01, FR-CHAT-02, FR-CHAT-06, FR-CHAT-09 | P7-02, P4-12 | P0 | AC-05 E2E | DONE |
| P7-07 | Директивы UI: команды, impact preview, подтверждение, статусы delivered/acknowledged/completed | FR-CHAT-03, FR-CHAT-05 | P7-06, P3-13 | P0 | AC-06 E2E | DONE |
| P7-08 | Task Detail + diff viewer + ссылки на evidence | FR-CHAT-06 | P7-05, P6-11 | P0 | E2E | DONE |
| P7-09 | CLI Providers screen | FR-CLI-10 | P2-12 | P0 | E2E | DONE |
| P7-10 | Evidence & Quality, Standards & Compliance screens | FR-QG-07, FR-STD-05 | P6-11 | P0 | E2E | DONE |
| P7-11 | Approvals screen/поток | FR-APR-03 | P3-10 | P0 | AC-13 E2E | DONE |
| P7-12 | Knowledge/Documentation screen | FR-DOC-06 | P5-10 | P1 | E2E | DONE |
| P7-13 | Settings/Audit screen | FR-AUD-02 | P1-11 | P0 | E2E | DONE |
| P7-14 | E2E-набор Mission Control + impact/replan UI тесты | AC-05, AC-06, AC-08 | P7-* | P0 | Зелёный | DONE |

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

### P1-18 и Phase 2 — CLI Integration без реальных CLI (2026-11-08)

Решение владельца: Phase 2 в части, не требующей реальных CLI; P2-01, P2-05, P2-06, P2-13 ждут машину с установленными и авторизованными Codex CLI и Claude Code CLI.

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) |
|---|---|---|---|
| P1-18 | DONE | `packages/contracts/openapi.yaml`, `scripts/gen_contract_models.py`, `packages/contracts/aicd_contracts/generated/**`, `.github/workflows/ci.yml`, `apps/web/**` | OQ-31 закрыт: 500/503 у всех 55 операций, `Problem.code` открытая строка, описан `FieldError.pointer`, slug = правило API/БД, `actor_type` + `github`, getSession без 422, login + 400; `openapi_spec_validator` → valid; `gen_contract_models.py --check` → OK; oasdiff (tufin/oasdiff v1.35.0) против HEAD с severity-файлом → 0 error, 1 warning; web: check:api, typecheck, lint, build, 29 unit, 6 E2E — passed |
| P2-01 | BLOCKED | — | Нужна машина с официальными CLI и выполненным оператором login (OQ-01, OQ-03) |
| P2-02 | DONE | `infrastructure/migrations/versions/0002_cli_gateway.py` | 9 объектов (worker_hosts, cli_providers+seed, cli_installations, cli_sessions, worktrees, cli_executions, provider_health, usage_metrics, allowed_transitions), RLS, `sys_active_executions`; upgrade/downgrade/upgrade, RLS-инварианты, владение таблицами — passed. FK на runs/tasks/task_attempts — в миграции P3 |
| P2-03 | DONE | `packages/cli_adapters/aicd_cli_adapters/{models,executor,errors,profiles,compat,output}.py`, `packages/contracts/cli-compatibility.yaml` | Модели — из JSON Schema; argv только из allowlist-плейсхолдеров, `<candidate-…>` не исполняется, ключи AI API в env → отказ; matrix пуста до P2-01 (любая версия UNTESTED); `tests/unit/test_cli_core.py` passed |
| P2-04 | DONE | `aicd_cli_adapters/fake/**` | Реальный процесс-эмулятор, 13 сценариев; `tests/integration/test_fake_adapter.py` → 17 passed (результат валиден по схеме; exit 0 → `verification: pending`; claims без evidence; ошибки нормализованы; wall/idle timeout; отмена с эскалацией до SIGKILL; идемпотентность; редакция секретов в событиях и raw log; лимит вывода; allowlist env) |
| P2-05 / P2-06 | BLOCKED | — | Общий `SubprocessCLIAdapter` готов; нужны подтверждённые probe флаги, парсер вывода и сигнатуры ошибок реальных CLI (P2-01) |
| P2-07 | IN_PROGRESS | `packages/security/aicd_security/{container_policy,docker_runner,sandbox}.py` | Политика контейнера + линтер (51 запрещённая конфигурация), `DockerSandboxRunner` через Engine API: non-root 10001, CapEff=0, NoNewPrivs, seccomp, read-only rootfs, limits, OOM, timeout, удаление контейнера — 75 тестов против Docker 29.9.2; адаптер исполняет Fake CLI в runner-контейнере (`tests/security/test_adapter_in_sandbox.py`). **Остаток:** runner-образы с официальными CLI (pinned, SBOM) — после P2-01 |
| P2-08 | DONE | `infrastructure/docker/egress-proxy/**`, `infrastructure/docker/compose.sandbox.yaml` | Squid (ubuntu/squid 6.10): только CONNECT:443 к хостам allowlist, IP-литералы/приватные адреса/HTTP/поддомены → 403; docker-socket-proxy: 12 запрещённых эндпоинтов → 403. Allowlist провайдеров пуст до P2-01 |
| P2-09 | DONE | `packages/orchestration/aicd_orchestration/worktrees.py`, `packages/security/aicd_security/git.py` | Ветка `aicd/<run>/<task>-<worktree>` (уникальна на попытку) от полного SHA; хук репозитория не исполняется; diff (вкл. новые файлы) и WIP-коммит платформой; orphans; пути вне корня отклоняются — `tests/integration/test_worktrees.py` passed |
| P2-10 | DONE | `packages/security/aicd_security/local_runner.py`, `aicd_cli_adapters/output.py` | Группа процессов, SIGTERM → grace → SIGKILL (включая потомков), чистое окружение, redaction, лимит raw log — `tests/integration/test_local_runner.py` passed |
| P2-11 | DONE | `aicd_cli_adapters/gateway.py` | Execution FSM в БД с fencing token (устаревший токен отклонён, недопустимый переход отклонён триггером), лимит параллелизма учитывает все workspace, артефакты с sha256, diff от платформы, usage `not_exposed_by_cli`, повтор доставки не запускает процесс — `tests/integration/test_gateway.py` 8 passed. Очередь заданий — P3 |
| P2-12 | IN_PROGRESS | `aicd_cli_adapters/registry.py`, `apps/api/aicd_api/routers/providers.py` | `GET /providers` по контракту, без учётных данных; запись probe/health — `tests/integration/test_providers.py` passed. **Остаток:** `POST /providers/{id}/probe` и `PATCH …/settings` требуют очереди (P3-03) и таблицы policies (P3-01); до этого `workspace_settings` — значения по умолчанию (1 сессия, OQ-01) |
| P2-13 | BLOCKED | — | Opt-in набор с реальными CLI — после P2-01 |
| P2-14 | DONE | `tests/security/test_sandbox.py`, `tests/security/test_adapter_in_sandbox.py` | Запись вне worktree, сеть, секреты хоста, docker.sock, privileged — отклонены/недоступны |

**Итог:** `uv run pytest --cov` → **421 passed** (PostgreSQL 17, Redis, S3-совместимый RustFS, Docker), покрытие 88%; ruff, mypy --strict (117 файлов), import-linter (4 контракта), deny-gate (123 py / 498 js пакетов) — без замечаний.

### Phase 3 — Core Orchestrator (2026-11-08)

Окружение проверки: PostgreSQL 17 (контейнер), Redis 7 (общий + отдельный `redis-server` для chaos), Docker, Python 3.13; CLI — Fake adapter (реальные CLI недоступны, см. Phase 2).

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) |
|---|---|---|---|
| P3-01 | DONE | `infrastructure/migrations/versions/0003_orchestration.py`, `0004_seed_quality_gates.py` | `tests/migrations/test_orchestration_invariants.py` → 29 passed (DAG-триггер, переходы, RLS, fencing, append-only). Seed 0004 исправлен: под FORCE RLS вставлял 0 строк, теперь снимает FORCE в транзакции миграции и падает при 0 строк |
| P3-02 | DONE | `aicd_orchestration/fsm.py` | `tests/unit/test_orchestration_core.py` → 22 passed, в т.ч. Hypothesis: ни одна последовательность не нарушает FSM; таблица переходов кода = `allowed_transitions` БД |
| P3-03 | DONE | `aicd_platform/{outbox,consumer,outbox_dispatcher}.py`, `apps/worker/**` (Dramatiq) | `tests/integration/test_outbox_dispatcher.py` → 5 passed (дубли/сбой публикации → повтор без дублей, монотонный seq при конкурентных диспетчерах); `test_worker_actors.py` → 6 passed (дубль доставки → эффект один) |
| P3-04 | DONE | `aicd_platform/leases.py`, `aicd_orchestration/recovery.py` | `tests/chaos/test_recovery.py`: после reap старый fencing token не актуален («зомби» не пишет); reaper не освобождает аренду, если смерть процесса не подтверждена |
| P3-05 | DONE | `aicd_orchestration/{dag,plans}.py` | Unit: цикл с путём, self-loop, неизвестная зависимость, глубина, L3+ без проверки (Hypothesis) — passed |
| P3-06 | DONE | `aicd_orchestration/engine.py` (`_dispatchable`), `apps/worker/aicd_worker/handlers.py` | `tests/integration/test_scheduler.py` → 2 passed: READY только после зависимостей; порядок по критическому пути; лимит `max_parallel_tasks` из policy snapshot проверяется и в `execute_task` под блокировкой run |
| P3-07 | DONE | `aicd_orchestration/retry.py`, `engine.py` (`fail_attempt`) | E2E: `nonzero_exit` → 3 попытки `PROCESS_FAILED` → FAILED; секрет в изменении → 3 × `GATE_FAILED` → FAILED; backoff/jitter/RATE_LIMITED — unit. Исправлено: `attempt_no` берётся из истории (после неучитываемого повтора номер дублировался — найдено chaos-тестом) |
| P3-08 | DONE | `apps/api/aicd_api/routers/runs.py`, `aicd_orchestration/runs.py` | `tests/integration/test_runs_api.py` → 12 passed: Idempotency-Key (повтор/реюз с другим телом 422/отсутствие 400, одно событие `run.created`), ETag/304/If-Match 412, 404 для чужого run, 403 для viewer; Schemathesis-fuzz 9 операций (25 примеров каждая): нет 5xx, ответы по контракту — нашёл недокументированный 404 у `createRun` (контракт дополнен) |
| P3-09 | IN_REVIEW | `apps/api/aicd_api/routers/events.py`, `aicd_platform/sse.py` | `tests/integration/test_sse.py` → 5 passed (реконнект с Last-Event-ID: ровно один раз, без пропусков; heartbeat; закрытие после терминального события; 404 не участнику; отзыв доступа; 410 за окном). **Не сделано:** throttle `cli.output` 20/с, ограничение буфера медленного клиента; сигнал отзыва доступа из identity (сейчас — перепроверка раз в 60 с); противоречие лимитов — OQ-35 |
| P3-10 | DONE | `aicd_orchestration/approvals.py`, `apps/api/aicd_api/routers/approvals.py` | `tests/unit/test_approvals_rules.py` → 97 passed; `tests/integration/test_approvals_api.py` → 10 passed (four-eyes, матрица ролей, истечение, повтор по ключу, привязка к plan_version) |
| P3-11 | DONE | `aicd_orchestration/external_actions.py` | `tests/integration/test_external_actions.py` → 8 passed: крах после CONFIRMED → повторного вызова нет (AC-09); EXECUTING → probe |
| P3-12 | DONE | `aicd_orchestration/{recovery,checkpoints}.py`, `aicd_security/{local,docker}_runner.py` (`owns/kill`), `apps/worker/aicd_worker/{wiring,maintenance,main}.py` | `tests/chaos/test_recovery.py` → 2 passed: воркер-процесс убит SIGKILL во время работы CLI → пока аренда жива, reconciler ничего не трогает; после истечения — осиротевший CLI убит, исполнение CRASHED, попытка `LEASE_LOST`, задача RETRY_WAIT без расхода попытки, worktree ORPHANED→REMOVED, checkpoint `plan_approved` есть → run COMPLETED второй попыткой |
| P3-13 | DONE | `aicd_orchestration/directives.py`, `apps/api/aicd_api/routers/directives.py` | `tests/integration/test_directives.py` → 7 passed (AC-06 на Fake: директива → анализ → confirm → safe point + checkpoint → новая plan_version → DISPATCHED) |
| P3-14 | DONE | `packages/verification/**` | `tests/integration/test_verification_engine.py` → 7 passed, `tests/unit/test_gates_applicability.py` → 33 passed; E2E: evidence с commit, командой, exit code, временем, окружением, sha256 артефакта, `VERIFIED` |
| P3-15 | DONE | `apps/web/src/pages/{ProjectRunsPage,RunPage}.tsx`, `apps/web/src/api/{runEvents,useRunEvents,client}.ts`, `apps/web/e2e/mocked/run.spec.ts` | `pnpm typecheck`, `pnpm lint` → 0; `pnpm test` → 42 passed (10 — клиент потока: дедупликация по seq/event_id, Last-Event-ID, 410 → перечитать состояние и `after_seq`); `pnpm e2e` → 9 passed: поток обрывается посреди события → переподключение с `Last-Event-ID`, каждое событие ровно один раз, без пропусков; axe без замечаний; `--repeat-each 5` → 15/15. Клиент — fetch-стрим (EventSource не отдаёт HTTP-статус и не позволяет задать Last-Event-ID при ручном переподключении). Live-e2e не запускался (нужен развёрнутый стенд). Контракт выровнен по серверу и `sse-event.schema.json`: `run_version`/`task_version`, `title`, `ActivityEvent.seq`, `Problem.last_seq/min_available_seq` для 410 |
| P3-16 | IN_REVIEW | `tests/chaos/**`, `.github/workflows/ci.yml` (nightly) | Локально все сценарии зелёные: kill worker (`test_recovery`), потеря аренды, рестарт Redis под диспетчером — событие, записанное во время простоя, доставлено один раз, проекция SSE одна (`test_redis_restart`), дубль сообщения (`test_worker_actors`), разрыв SSE (`test_sse`). Критерий «зелёные в CI (nightly)» — workflow на GitHub ещё не запускался |
| P3-17 | DONE | `tests/e2e/test_phase3_slice.py`, `tests/support/phase3.py` | 7 passed: COMPLETED с VERIFIED evidence; FAILED (gate/CLI); BLOCKED `PROVIDER_UNAVAILABLE`/`AUTH_REQUIRED` без обхода; AWAITING_APPROVAL → решение → COMPLETED |

Исправления, найденные сквозными тестами: повтор задачи падал на существующей ветке worktree; run не переходил в BLOCKED, пока оставались PENDING-потомки заблокированной задачи; verifier не создавал staging-каталог.

### Phase 7 — Mission Control & Live Agent Chat (2026-11-08)

Окружение проверки: PostgreSQL 17, Redis 7, RustFS (S3), Docker; Chromium + Playwright; агенты — Fake CLI. Итог: `uv run pytest` → **2411 passed, 3 skipped** (PHPStan ×2, офлайн-БД Trivy — OQ-43); ruff, ruff format, mypy, import-linter (4 kept), запрет AI SDK, `gen_contract_models --check` — OK. Web (`apps/web`): `pnpm check:api`, `typecheck`, `lint` (0), `test` → 138 passed, `build`, `e2e` (mocked, ответы сверяются с контрактом, axe светлая/тёмная) → 56 passed, `e2e:live` (реальные API + воркеры outbox/orchestrator/exec + PG + Redis + S3 + Fake CLI) → 4 passed.

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) |
|---|---|---|---|
| P7-01 | DONE | `apps/web/src/{components/ui,layout,styles,routes,i18n}/**` | Токены WCAG AA (≥4.5:1, рамки/фокус ≥3:1) в обеих темах — unit; `e2e/mocked/layout.spec.ts`: axe без serious/critical на layout и всех разделах, навигация только клавиатурой, мобильное меню, тема |
| P7-02 | DONE | `src/api/{sse,streamManager,useEventStream}.ts*` | `streamManager.test.ts`, `e2e/mocked/sse.spec.ts`: офлайн → онлайн и обрыв посреди кадра — продолжение с `Last-Event-ID`, каждое событие ровно один раз, без пропусков; 410 → перечитывание REST |
| P7-03 | DONE | `src/features/dashboard/**` | `dashboard.spec.ts` (стартовая страница, «требуют внимания», состояния, axe) |
| P7-04 | DONE | `src/features/projects/**`, `aicd_projects/explorer.py`, `aicd_security/git.py` (`run_git_bytes_prefix`), операции `listRepositoryTree`/`getRepositoryFile` | `tests/integration/test_repository_explorer.py` → 19 passed (обход путей ×9, инъекции commit ×5, бинарный/усечённый файл, symlink, изоляция workspace); `projects.spec.ts` |
| P7-05 | DONE | `src/features/runs/**` (`@xyflow/react` 12.12.0, ленивая загрузка) | `run-graph.spec.ts`: живое обновление по SSE, табличный вид с клавиатуры, axe |
| P7-06 | DONE | `src/features/chat/**`; `getRunChat` refs/seq, `acp_type` в `chat.message` | `chat.spec.ts` (AC-05 mocked), `test_chat_api.py`; live AC-05 |
| P7-07 | DONE | `src/features/directives/**`; `Directive.impact_summary`; воркер обрабатывает `directive.state_changed` | `chat.spec.ts` (AC-06 mocked, 409 → повтор), `test_directives.py`, `test_worker_actors.py`; live AC-06 (PAUSE/RESUME: доставлено → подтверждено → выполнено; CHANGE_REQUEST: impact → confirm → план v2) |
| P7-08 | DONE | `src/features/tasks/**` (Monaco 0.57.0 локально, без CDN); `GET /artifacts/{id}/download` (поток через API), миграция 0011 | `tasks.spec.ts` (нет внешних запросов), `tests/integration/test_artifact_download.py` → 4 passed (RBAC `cli_raw_log`, 404 чужой/истёкший/пропавший, audit, обрыв при неверном sha256) |
| P7-09 | DONE | `src/features/providers/**` | `providers.spec.ts`: нет полей секретов, перепроверка с Idempotency-Key, настройки с If-Match |
| P7-10 | DONE | `src/features/{quality,compliance}/**` | `quality.spec.ts`, `compliance.spec.ts`: SKIPPED/ERROR ≠ PASS явно, переходы findings по FSM, ручной вердикт контроля, утверждение контролей проекта; обрыв SSE (AC-08 mocked) |
| P7-11 | DONE | `src/features/approvals/**` | `approvals.spec.ts`: AC-13 (утверждение плана → RUNNING по SSE), четыре глаза, отклонение с причиной |
| P7-12 | DONE | `src/features/knowledge/**` | `knowledge.spec.ts`: обновление кэша только после согласия, политика OFF/SMART/STRICT |
| P7-13 | DONE | `src/features/{settings,audit}/**` | `settings.spec.ts`, `audit.spec.ts`: участники, фильтры аудита в URL, фокус сохраняется |
| P7-14 | DONE | `apps/web/e2e/live/**`, `tests/support/web_live_stack.py`, `playwright.live.config.ts`; `aicd_orchestration/{directives,engine,cli_executor}.py` (доставка/подтверждение/выполнение поручений, наблюдатель отмены в exec) | `pnpm e2e:live` → 4 passed: AC-05; AC-06 — PAUSE/RESUME и CHANGE_REQUEST: impact → confirm → план v2 → доставлено → подтверждено агентами → выполнено; AC-08 — обрыв API посреди run, seq 1..N без дублей; smoke. `tests/integration/test_directive_delivery.py` → 8 passed (явное/неявное подтверждение, cannot_comply → FAILED, провал задачи замены, отмена run → EXPIRED, STOP, остановка RUNNING-задачи из другого процесса по поручению и паузе) |

Найдено и исправлено при сведении: compliance.evaluate(run) не вызывался перед guard завершения (блокирующие контроли не учитывались); шаги поручений не обрабатывались ни одним воркером (поручения оставались в IMPACT_ANALYSIS_PENDING); `downloadArtifact` не имел обработчика; заголовок страницы перехватывал фокус при смене вкладки/фильтра; поручения останавливались на DISPATCHED (DELIVERED/ACKNOWLEDGED/COMPLETED не были реализованы); перепланирование в утверждённом плане падало на триггере неизменяемых рёбер (зависимые задачи теперь пересоздаются в новой версии плана); взаимоблокировка tick ↔ переход задачи (порядок блокировок run → строки подтверждений).

### Phase 6 — Quality, Security & Standards (2026-11-08)

Окружение проверки: PostgreSQL 17, Redis 7, RustFS (S3), Docker с официальными образами инструментов, закреплёнными по digest (сеть в контейнерах gates отключена). Итог: `uv run pytest` → **2336 passed, 3 skipped** (2 — PHPStan недоступен в окружении, 1 — офлайн-БД Trivy не задана; обе ситуации в работе дают ERROR, не PASS); ruff, mypy --strict (325 файлов), import-linter, deny-gate, сверка контрактов, трассировка — без замечаний; `check:api` — веб-схема соответствует контракту.

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) |
|---|---|---|---|
| P6-01 | DONE | `infrastructure/migrations/versions/0008_quality_standards.py`, `0009_seed_phase6_gates.py` | 28 passed: up/down/up, offline, RLS, FSM findings, guard'ы compliance (PASS только с VERIFIED evidence того же run), CHECK `error_code` = `gates.ErrorCode`, seed 26 gates × 3 профиля = `registry.seed_rows()` |
| P6-02 | DONE | `aicd_verification/{registry,profiles}.py` | 74 passed: матрица QUALITY_GATES §2.2 × применимость по файлам commit и `project_technologies` |
| P6-03 | IN_PROGRESS | `aicd_verification/{runners,coverage_driver,images}.py`, `tests/fixtures/gates/**` | 111 passed, 2 skipped: ESLint, mypy, pyright, tsc, сборка, Vitest, Playwright, pytest/coverage — фикстуры pass/fail, ruff/eslint в закреплённых образах. **PHPStan:** раннер есть, отсутствие инструмента → ERROR; фикстурный тест пропущен — в окружении нет phpstan и образа (OQ-43) |
| P6-04 | DONE | `aicd_verification/security_gates.py`, `tools/**`, `tests/fixtures/security/**` | 74 passed, 1 skipped: Semgrep, Gitleaks, Trivy (config), OSV, Checkov, conftest/OPA на реальных образах по digest и фикстурах с заведомыми находками; подавления репозитория заменены платформенными конфигами. Trivy vuln/OSV без офлайн-БД → ERROR (тест с БД проходит при `AICD_TRIVY_DB_DIR`) |
| P6-05 | DONE | `aicd_verification/migrations.py` | Эфемерный postgres:17 (digest, сеть none, tmpfs): upgrade → downgrade → upgrade со сверкой схемы; деструктивные операции → finding high, PASS только после APPROVED `DESTRUCTIVE_MIGRATION` (`test_false_pass_prevention.py` с docker и БД). Alembic в контейнере ждёт образ платформы (OQ-43) |
| P6-06 | DONE | `tools/{perf,bench_runner}.py` | Заявление агента об ускорении → повторяемые замеры base vs head (N ≥ 5, медиана, квартили, окружение) в evidence |
| P6-07 | DONE | `security_gates.py` (syft, cosign) | SBOM CycloneDX как артефакт с sha256; cosign verify по ключу офлайн (без Rekor) |
| P6-08 | DONE | `aicd_verification/{verdict,findings}.py` | 76 passed: SKIPPED ≠ PASS, ERROR ≠ PASS, NOT_APPLICABLE только с причиной, полнота evidence (AC-14), нормализация и жизненный цикл findings |
| P6-09 | DONE | `verdict.run_completion_verdict`, `aicd_orchestration/engine.py` (блок завершения) | COMPLETED только при PASS всех обязательных gates профиля на итоговом commit, проверенном sha256 evidence, без открытых блокирующих findings и PENDING approvals; gate покрытия ядра ≥ 85% (strict/critical) |
| P6-10 | DONE | `packages/standards/aicd_standards/**`, seed в 0008 | 37 passed: 18 стандартов / 33 контроля собственными формулировками (только идентификаторы и пункты), применимость по стеку, трассировка стандарт → контроль → проверка → evidence → вердикт |
| P6-11 | DONE | `apps/api/aicd_api/routers/quality.py`, `report_markdown.py`, контракт (аддитивно) | `test_quality_api.py` → 17 passed: контракт, отчёт JSON + Markdown с экранированием, RBAC, 404 чужим, Schemathesis без 5xx |
| P6-12 | DONE | `tests/integration/test_false_pass_prevention.py`, `tests/e2e/test_completion_guard.py` | 8 passed: нет инструмента, сбой, таймаут, 0 собранных тестов, пропуски, нет/битый отчёт покрытия, exit 0 при падениях, другой commit, расхождение sha256, NOT_APPLICABLE без причины, устаревшее evidence — ни одного PASS/COMPLETED (AC-11) |

Сквозные тесты оркестрации используют профиль workspace без gates «только в контейнере»; полный профиль strict исполняется в контейнерах в `tests/e2e/test_full_profile_docker.py` (run COMPLETED, gitleaks PASS). Исправлено по ходу: `redact_text` портил JSON-отчёты (кавычка), движок не передавал gates безопасности их входные файлы, тест инвариантов засорял глобальный профиль.

### Phase 5 — Code Intelligence (2026-11-08)

Окружение проверки: PostgreSQL 17, Redis 7, RustFS (S3), Docker; tree-sitter 0.26 (Python/JS/TS/PHP), ripgrep; агенты — Fake CLI. Итог: `uv run pytest` → **1924 passed, 0 skipped**; ruff, mypy --strict, import-linter (4 контракта), deny-gate, сверка моделей контрактов, трассировка — без замечаний; `check:api` — веб-схема соответствует контракту.

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) |
|---|---|---|---|
| P5-01 | DONE | `infrastructure/migrations/versions/0007_code_intelligence.py` | `tests/migrations/test_code_intelligence_migration.py` → 46 passed: up/down/up, offline SQL, RLS по 15 таблицам арендатора, глобальные таблицы, FSM `indexing_jobs`, CHECK, FTS/pg_trgm, `sys_plan_docs_download` (узкая SECURITY DEFINER вместо INSERT для API), `sys_indexing_workspaces` |
| P5-02 | DONE | `aicd_code_intelligence/index/{gitsource,builder,store,jobs,policy}.py`, `aicd_security/git.py` (`run_git_bytes`) | Чтение commit через `ls-tree`/`cat-file` без checkout и фильтров; повторная индексация после изменения одного файла: `blobs_read == 1`, `files_parsed == 1`, остальное переиспользовано (`test_code_index_db.py`) |
| P5-03 | DONE | `index/{symbols,languages}.py`, `tests/fixtures/code/**` | Golden-тесты по каждому языку (Python, JS, TS/TSX, PHP, SQL, Markdown, YAML, JSON); YAML без конструирования объектов |
| P5-04 | DONE | `index/search.py` | ripgrep: фиксированный argv, запрос после `--`, fixed-strings по умолчанию, лимиты; корпус инъекций (`--pre=sh`, `-e`, `$(…)`, обратные кавычки, `;`, `|`, NUL, перевод строки) — как текст; FTS + pg_trgm на БД |
| P5-05 | DONE | `index/graph.py` | Golden-рёбра импортов (Python/JS/TS/PHP) и test links. Не реализовано: tsconfig `paths`, PSR-4, рёбра вызовов |
| P5-06 | DONE | `context/{ranking,retrieval,store,snapshot}.py` | 16 passed: порядок map > exact > dependency > relevance > recency, бюджеты по ролям, фрагмент другого commit исключён (или `stale=true` при политике mark, также в `retrieval_results`), кэш по content_hash+commit+policy |
| P5-07 | DONE | `technologies/{detect,store}.py`, `GET /projects/{id}/technologies` | 9 passed (фикстуры Python/TypeScript/PHP, версии из lock-файлов, evidence файл:строка); определение стека после каждого снимка в воркере |
| P5-08 | TODO | — | LSP (P1, опционально) не реализован; индекс от LSP не зависит |
| P5-09 | DONE | `docs/policy.py`, `aicd_orchestration/{docs_ports,docs_flow}.py`, `GET/PATCH documentation-policy` | 47 unit (решения OFF/SMART/STRICT); сквозные AC-10 — ниже |
| P5-10 | DONE | `aicd_security/ssrf.py`, `docs/{archives,html_text,content,importer}.py`, `/documentation/sources`, refresh, воркер-очередь `docs` | 133 security passed: приватные/metadata адреса вкл. встроенный IPv4, десятичные/восьмеричные формы, DNS-pinning, редиректы, zip-bomb, traversal, symlink, размер, MIME + magic bytes; импорт end-to-end на локальном сервере |
| P5-11 | DONE | `cli_executor.py`, `agent_io.py`, `apps/worker/aicd_worker/indexing.py` | Исполнитель на Fake: промпт содержит repo_map/code/tests, `ExecutionRequest.input.context_pack` ссылается на строку `context_packs`; индекс строится перед исполнением (ошибка не валит задачу); каталог промпта монтируется только для чтения; метрики пакета в manifest и `agent.prompt_assembled` |
| P5-12 | DONE | `tests/e2e/test_phase5_acceptance.py` | 7 passed: AC-02 (подключение → индекс HEAD → стек → DAG без циклов → Context Pack на снимке); AC-10 OFF (нет approval/загрузки/запросов, только локальный индекс; NFR-DOC-01); SMART (approval → до одобрения ноль запросов к серверу → загрузка воркером → docs в следующем промпте; отказ → без docs); STRICT (BLOCKED до одобрения; отказ → MANUAL_REVIEW); защита от зацикливания |

### Phase 4 — Multi-Agent System (2026-11-08)

Окружение проверки: PostgreSQL 17, Redis 7, RustFS (S3), Docker; агенты — процессы Fake CLI (реальные CLI недоступны). Итог: `uv run pytest` → **1601 passed, 0 skipped**; ruff, mypy --strict (228 файлов), import-linter (4 контракта), deny-gate, сверка моделей контрактов, трассировка — без замечаний; web: typecheck/lint, `pnpm test` 42 passed, `pnpm e2e` 9 passed, `check:api` — схема соответствует контракту.

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) |
|---|---|---|---|
| P4-01 | DONE | `infrastructure/migrations/versions/0006_agents_acp_chat.py`, `aicd_platform/retention.py` | `tests/migrations/test_agents_acp_migration.py` → 15 passed: up/down/up, offline SQL, RLS по каждой новой таблице, CHECK 32 KiB, append-only, отложенные FK 0003; retention архивирует run и партиции активности |
| P4-02 | IN_REVIEW | `aicd_orchestration/agents/{profiles.py,templates/**}`, `aicd db sync-agent-profiles` | `tests/unit/test_agent_profiles.py` → 54 passed (golden sha256, обязательные секции, запрещённое содержимое, ACP-раздел = матрица §4.2, подмена шаблона обнаруживается); `test_agent_profiles_sync.py` → 3 passed (только под ролью migrator). **Критерий «ревью человеком» шаблонов не выполнен** — нужен просмотр владельцем |
| P4-03 | DONE | `agents/{prompt,result,contracts}.py`, `tests/fixtures/prompt_injection/*` | 97 passed: 26 образцов инъекций в каждом недоверенном поле — блок policy и блоки 2/5 не меняются, fence не разрывается; `.aicd/result.json`: размер, глубина, дубли ключей, подмена `from`, побег путей; claims всегда `verified=false` |
| P4-04 | DONE | `aicd_collaboration/acp/**` | 577 passed (контракт + negative по каждому из 12 типов, полная матрица роль × тип, порядок шагов конвейера, корреляция, лимиты) + `test_acp_broker.py` 12 passed на БД |
| P4-05 | DONE | `acp/{delivery,projection}.py`, `apps/worker/aicd_worker/handlers.py` | Дубль/конкурентная доставка → одна строка чата; retry/backoff/DLQ/replay; воркер проецирует `agent.message` в чат и ленту активности (`test_review_loops.py`) |
| P4-06 | DONE | `aicd_orchestration/classification.py` | `tests/unit/test_routing.py`: правила F1–F6, риск-баллы, override не ниже минимума режима/пользователя, Hypothesis: уровень не ниже входов |
| P4-07 | DONE | `router.py`, `routing_service.py`, `engine.py` | 59 unit (фильтры H1–H8 с причинами, оценка, stickiness, tie-break, WAIT при занятости); решения пишутся в `routing_decisions`; оба недоступны → BLOCKED `AUTH_REQUIRED`/`PROVIDER_UNAVAILABLE` (AC-16) |
| P4-08 | DONE | `router.py`, `engine.py` (`on_task_approval_decided`) | `tests/e2e/test_routing_review.py` → 3 passed: ревью L3+ — другой провайдер; strict → MANUAL_REVIEW → решение человека; basic → SELF_REVIEW_FALLBACK только после одобрения, маркировка `self_review`; Hypothesis: самопроверка никогда не `independent` |
| P4-09 | DONE | `pipelines.py`, `planner.py` | `tests/unit/test_pipelines.py` → 70 passed: каждый режим × L1–L5 даёт валидный DAG, L3+ с проверкой, approvals L4/L5 |
| P4-10 | DONE | `review_flow.py`, `review_loops.py`, `acp/pipeline.py` | fix-цикл — новые задачи (origin_task_id) в новой версии плана; счётчик — у исходной реализации (fix-задачи его не обнуляют); 4-й цикл → BLOCKED `REVIEW_LOOP_EXHAUSTED` + approval MANUAL_REVIEW → решение человека (`test_review_flow.py`, `test_review_loops.py`, unit) |
| P4-11 | DONE | `quality.py` | Unit: затухание 30 дней, окно 90 дней, Beta(2,2), исключённые категории |
| P4-12 | DONE | `apps/api/aicd_api/routers/{chat,runs}.py` | `test_chat_api.py`, `test_task_messages_api.py` → 11 passed: контракт, RBAC, 404 чужим, идемпотентность, пагинация, Schemathesis без 5xx |
| P4-13 | BLOCKED | `tests/e2e/test_agent_handoff.py`, `tests/integration/test_agent_io.py`, Fake-сценарии агентов | Часть на Fake — passed: developer → независимый reviewer (замечания) → fix тем же исполнителем → повторное ревью другим провайдером (одобрено) → COMPLETED с evidence (AC-03/04/05 на Fake). **Opt-in прогон с реальными Codex/Claude CLI — после P2-01** |

### P3-18 — Retention/архивирование (2026-11-08)

| Задача | Статус | Изменённые файлы | Проверки (команда → результат) |
|---|---|---|---|
| P3-18 | IN_PROGRESS | `infrastructure/migrations/versions/0005_retention_and_resolvers.py`, `packages/platform/aicd_platform/{retention,config}.py`, `apps/cli/aicd_cli/main.py` (`aicd maintenance retention`), `tests/integration/test_retention.py`, `tests/migrations/test_retention_migration.py` | `uv run pytest tests/migrations tests/integration/test_retention.py tests/integration/test_approvals_api.py -q` → **56 passed** (PostgreSQL 17): на засеянной БД под `aicd_maintenance` — партиции вперёд, архив audit-партиции 2002-03 в NDJSON.gz (sha256 сверен) → DETACH+DROP, опубликованная outbox-партиция удалена, партиция с неопубликованным событием сохранена, `run_event_log` отменённого run удалён пакетами (активный run не тронут), `processed_events` старше срока удалены, политика `retention` переопределяет срок; каждое действие и сессия (`maintenance.session_started/…_finished`) в `audit_events`; без `maint_begin` удаление/drop отклоняются, под `aicd_app` job не стартует. ruff, mypy, lint-imports — без замечаний. **Не закрыто (поэтому IN_PROGRESS):** расписание job не подключено (cron/systemd/воркер); `agent_activity_events` и архив run (`agent_messages`, `message_deliveries`, `chat_*`) — таблиц ещё нет, job отмечает их пропущенными; S3-приёмник архива; расписание job (cron/systemd) |

В той же миграции: `sys_resolve_approval_workspace` (approvals API больше не перебирает workspace пользователя) и `sys_recovery_workspaces` для recovery reconciler (P3-12).
