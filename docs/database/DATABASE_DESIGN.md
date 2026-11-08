# DATABASE_DESIGN — логическая схема PostgreSQL 17

**Статус:** Proposed (Phase 0, ожидает утверждения владельцем). **Основание:** ТЗ §6, §7, §10, §12–§16; ADR-0002, ADR-0007, ADR-0008, ADR-0011, ADR-0012.
**Связанные документы:** [ER_DIAGRAM.md](ER_DIAGRAM.md), [../architecture/MODULE_BOUNDARIES.md](../architecture/MODULE_BOUNDARIES.md) (владение таблицами), [../README.md](../README.md) §3 (канонические перечисления).

> Документ описывает **проектируемую** схему. Ни одна таблица, триггер или политика ещё не реализованы и не протестированы. DDL ниже — нормативный проект для миграций Phase 1+ (`infrastructure/migrations/`), а не выгрузка существующей БД.

## Содержание

1. [Соглашения](#1-соглашения)
2. [Сводный каталог таблиц](#2-сводный-каталог-таблиц)
3. [Перечисления: домены канонических enum и локальные значения](#3-перечисления)
4. [Общие функции и триггеры](#4-общие-функции-и-триггеры)
5. [IAM и tenancy](#5-iam-и-tenancy)
6. [Проекты, репозитории, стек](#6-проекты-репозитории-стек)
7. [Оркестрация: runs, tasks, DAG, маршрутизация](#7-оркестрация)
8. [CLI Gateway](#8-cli-gateway)
9. [ACP, чат, поручения, активность](#9-acp-чат-поручения-активность)
10. [Code Intelligence](#10-code-intelligence)
11. [Документация и база знаний](#11-документация-и-база-знаний)
12. [Качество и evidence](#12-качество-и-evidence)
13. [Стандарты и compliance](#13-стандарты-и-compliance)
14. [Политики и approvals](#14-политики-и-approvals)
15. [Надёжность: outbox, leases, checkpoints, ledger, event log](#15-надёжность)
16. [GitHub](#16-github)
17. [Наблюдаемость и аудит](#17-наблюдаемость-и-аудит)
18. [Инварианты, обеспечиваемые БД](#18-инварианты-обеспечиваемые-бд)
19. [Row Level Security](#19-row-level-security)
20. [Retention, архивирование, партиционирование](#20-retention-архивирование-партиционирование)
21. [Стратегия миграций](#21-стратегия-миграций)
22. [Предлагаемые дополнения / открытые вопросы](#22-предлагаемые-дополнения--открытые-вопросы)

---

## 1. Соглашения

### 1.1 Идентификаторы и типы

| Правило | Решение | Основание |
|---|---|---|
| Первичные ключи | `uuid`, **UUIDv7**, генерируется приложением (`packages/platform` ids). Для PK используется домен `uuid7` с проверкой `uuid_extract_version(VALUE) = 7` (функция доступна с PostgreSQL 17). `DEFAULT` в БД нет — строка без явного id отклоняется. | ТЗ §7.1, ADR-0008 |
| Внешние идентификаторы | Отдельные колонки (`github_repo_id bigint`, `installation_id bigint`, `pr_number int`, `session_ref text`, `pid_ref text`), никогда не PK. | ТЗ §7.1 |
| Время | Только `timestamptz`; сервер и сессии в UTC (`timezone = 'UTC'`). `created_at timestamptz NOT NULL DEFAULT now()`. | ТЗ §7.1 |
| Git SHA | Домен `git_sha` — `text CHECK (VALUE ~ '^[0-9a-f]{40}([0-9a-f]{24})?$')` (SHA-1 и SHA-256 репозитории). | AC-14 |
| Хэши содержимого | Домен `sha256_hex` — `text CHECK (VALUE ~ '^[0-9a-f]{64}$')`. | AC-14 |
| Деньги/дробные метрики | `numeric`; счётчики — `bigint`/`int` с `CHECK (>= 0)`. | — |
| `jsonb` | Только для конфигураций (`settings`, `config`, `definition`), snapshot (`policy_snapshot`, `manifest`), payload событий/сообщений и `metadata`. Атрибуты, по которым фильтруют, сортируют или строят инварианты, — типизированные колонки. | ТЗ §7.1 |
| Секреты | **Ни одной колонки для секретов** (CLI-токены, cookies, API keys, SSH keys, GitHub private key, webhook secret). Хранятся только хэши (`password_hash` argon2id, `token_hash` сессии) и безопасные ссылки (`*_ref`). См. §18.9. | ТЗ §7.1, §15 |
| Крупные данные | В object storage (MinIO / file adapter); в БД — `artifacts` (ref + sha256 + size) или `*_ref`. | ТЗ §2 |

### 1.2 Именование

| Объект | Шаблон | Пример |
|---|---|---|
| Таблица | `snake_case`, множественное число | `task_attempts` |
| PK constraint | `pk_<table>` | `pk_tasks` |
| Внешний ключ | `fk_<table>_<column(s)>` | `fk_tasks_run` |
| Уникальность | `uq_<table>_<cols>` | `uq_task_attempts_task_attempt_no` |
| CHECK | `ck_<table>_<rule>` | `ck_tasks_blocked_reason` |
| Индекс | `ix_<table>_<cols>[_<predicate>]` | `ix_tasks_ready` |
| Триггер | `trg_<table>_<purpose>` | `trg_runs_transition` |
| Функция | `<domain>_<verb>` / `trg_fn_<purpose>` / `sys_<verb>` (SECURITY DEFINER) | `trg_fn_set_updated_at` |
| RLS-политика | `p_<table>_<scope>` | `p_runs_tenant` |
| Домен перечисления | `<enum>_t` | `run_status_t` |

В листингах ниже имена `pk_*`/`fk_*` для краткости указаны там, где это существенно; миграции обязаны именовать **все** ограничения по шаблону (проверяется тестом схемы, §21.4).

### 1.3 Tenancy и составные FK

- Каждая tenant-таблица содержит `workspace_id uuid NOT NULL` (ADR-0008), даже если workspace выводим через родителя. Это даёт RLS без JOIN.
- Каждая tenant-таблица с суррогатным `id`, на которую ссылаются другие таблицы, имеет `CONSTRAINT uq_<t>_ws_id UNIQUE (workspace_id, id)` (а где нужен инвариант «тот же run/снимок» — `UNIQUE (workspace_id, run_id, id)` / `UNIQUE (workspace_id, snapshot_id, id)`). Дочерние таблицы ссылаются **составным FK** `(workspace_id, <parent>_id) → <parent>(workspace_id, id)` — ссылка на строку чужого workspace физически невозможна (ТЗ §7.1 «межтенантные ссылки запрещены»).
- Ссылки tenant → global (например, `cli_executions.installation_id → cli_installations`) — обычные FK.
- Ссылки global → tenant запрещены (исключение: нет).
- Для nullable составных FK используется `MATCH SIMPLE` (по умолчанию): при `NULL` в `<parent>_id` FK не проверяется; `workspace_id` всегда `NOT NULL`.
- `ON DELETE SET NULL` для составного FK задаётся **со списком колонок** (`ON DELETE SET NULL (task_id)`, PostgreSQL ≥ 15), чтобы не обнулять `workspace_id`.

### 1.4 Перечисления

Перечисления — `text` + `CHECK` (ADR-0008). Для перечислений из `docs/README.md §3`, используемых в нескольких таблицах, CHECK оформлен как **SQL-домен** `<enum>_t` над `text` (§3.1): это тот же `text + CHECK`, но с единственным определением множества значений, что исключает расхождения между таблицами. Расширение множества — миграция `ALTER DOMAIN … DROP CONSTRAINT` + `ADD CONSTRAINT … NOT VALID` + `VALIDATE CONSTRAINT` (без `ALTER TYPE`, без долгой эксклюзивной блокировки). Локальные перечисления одной таблицы — inline `CHECK`. Значения, отсутствующие в README §3, перечислены в §3.2 и вынесены в §22 как предлагаемые дополнения.

### 1.5 Временные метки, `updated_at`, optimistic locking

- Изменяемые таблицы имеют `updated_at timestamptz NOT NULL DEFAULT now()` и триггер `trg_<t>_updated_at BEFORE UPDATE … EXECUTE FUNCTION trg_fn_set_updated_at()`.
- Агрегаты с конкурентными изменениями (`runs`, `tasks`, `user_directives`, `approvals`, `project_settings`, `workspaces`, `projects`, `external_actions`) имеют `version bigint NOT NULL DEFAULT 0` и триггер `trg_<t>_version BEFORE UPDATE … EXECUTE FUNCTION trg_fn_enforce_version()`, который требует `NEW.version = OLD.version + 1`. Приложение выполняет `UPDATE … SET …, version = version + 1 WHERE id = :id AND version = :expected` (ADR-0007); 0 строк → `VersionConflict` (HTTP 409/412 с ETag).
- `policies` и `quality_profiles` — неизменяемые версии: их `version` означает версию содержимого, а конкурентность обеспечивает `UNIQUE (…, version)` (§14.1).
- Append-only таблицы (`audit_events`, `run_event_log`, `agent_messages`, `agent_activity_events`, `outbox_events` кроме полей публикации) не имеют `updated_at`/`version`.

### 1.6 Политика `ON DELETE`

| Связь | Поведение | Пример |
|---|---|---|
| История/доказательства (runs, tasks, evidence, approvals, audit) | `RESTRICT` — удаление только через maintenance-процедуру архивирования (§20) | `tasks.run_id` |
| Строго принадлежащие дочерние строки без самостоятельной ценности | `CASCADE` | `task_dependencies`, `code_symbols` → `code_index_snapshots`, `retrieval_results`, `documentation_sections`, `message_deliveries` |
| Необязательная ссылка на вспомогательный объект | `SET NULL (<col>)` | `cli_executions.session_id` |
| Справочники global | `RESTRICT` | `technology_versions.technology_id` |

Удаление workspace (owner, `DATA_DELETION` approval) выполняется maintenance-процедурой `maint_purge_workspace(workspace_id)` в порядке, обратном зависимостям, с предварительным экспортом аудита; каскада от `workspaces` нет намеренно.

### 1.7 Расширения

`pg_trgm` (поиск по путям/символам, ТЗ §12), `btree_gin` (составные GIN-индексы `workspace_id` + `tsvector`/trgm). `pgvector` **не** устанавливается в v1 (ТЗ §3: P2, необязательно; ADR-0005). `pgcrypto`/`uuid-ossp` не нужны (UUIDv7 генерирует приложение).

---

## 2. Сводный каталог таблиц

Класс: **T** — tenant (`workspace_id NOT NULL`, RLS); **G** — global (без `workspace_id`, без RLS, доступ ограничен GRANT); **M** — смешанная (global-строки с `workspace_id IS NULL` + tenant-строки, RLS); **S** — системная (`workspace_id` для трассировки, RLS с отдельной политикой для `aicd_worker`, без доступа `aicd_app`). Источник: **ТЗ** — каталог ТЗ §7.2; **Д** — дополнение к ТЗ (обоснование в разделе таблицы). Владелец — модуль из MODULE_BOUNDARIES.md §3.

| # | Таблица | Домен | Класс | Источник | Модуль-владелец | Партиции |
|---|---|---|---|---|---|---|
| 1 | `users` | IAM | G | ТЗ | identity | — |
| 2 | `user_credentials` | IAM | G | Д | identity | — |
| 3 | `user_sessions` | IAM | G | Д | identity | — |
| 4 | `workspaces` | IAM | T* | ТЗ | projects | — |
| 5 | `workspace_members` | IAM | T | ТЗ | identity | — |
| 6 | `project_member_overrides` | IAM | T | Д | identity | — |
| 7 | `idempotency_keys` | IAM/API | M | Д | platform | — |
| 8 | `projects` | Projects | T | ТЗ | projects | — |
| 9 | `project_settings` | Projects | T | ТЗ | projects | — |
| 10 | `repositories` | Projects | T | ТЗ | projects | — |
| 11 | `worktrees` | Projects/Sandbox | T | Д | orchestration | — |
| 12 | `technologies` | Projects | G | ТЗ | projects | — |
| 13 | `technology_versions` | Projects | G | ТЗ | projects | — |
| 14 | `project_technologies` | Projects | T | ТЗ | projects | — |
| 15 | `runs` | Orchestration | T | ТЗ | orchestration | — |
| 16 | `tasks` | Orchestration | T | ТЗ | orchestration | — |
| 17 | `task_dependencies` | Orchestration | T | ТЗ | orchestration | — |
| 18 | `task_attempts` | Orchestration | T | ТЗ | orchestration | — |
| 19 | `agent_profiles` | Orchestration/Agents | G | ТЗ | agent_runtime | — |
| 20 | `agent_assignments` | Orchestration | T | ТЗ | orchestration | — |
| 21 | `routing_decisions` | Orchestration | T | ТЗ | orchestration | — |
| 22 | `allowed_transitions` | Orchestration | G | Д | orchestration | — |
| 23 | `worker_hosts` | CLI | G | Д | cli_adapters | — |
| 24 | `cli_providers` | CLI | G | ТЗ | cli_adapters | — |
| 25 | `cli_installations` | CLI | G | ТЗ | cli_adapters | — |
| 26 | `cli_sessions` | CLI | G | ТЗ | cli_adapters | — |
| 27 | `cli_executions` | CLI | T | ТЗ | cli_adapters | — |
| 28 | `provider_health` | CLI | G | ТЗ | cli_adapters | — |
| 29 | `usage_metrics` | CLI/Observability | T | ТЗ | cli_adapters | — |
| 30 | `agent_messages` | ACP | T | ТЗ | collaboration | — (§20) |
| 31 | `message_deliveries` | ACP | T | ТЗ | collaboration | — |
| 32 | `agent_decisions` | ACP | T | ТЗ | orchestration | — |
| 33 | `chat_threads` | Chat | T | ТЗ | collaboration | — |
| 34 | `chat_messages` | Chat | T | ТЗ | collaboration | — |
| 35 | `user_directives` | Directives | T | ТЗ | orchestration | — |
| 36 | `directive_impacts` | Directives | T | ТЗ | orchestration | — |
| 37 | `directive_acknowledgements` | Directives | T | ТЗ | orchestration | — |
| 38 | `agent_activity_events` | Chat/Activity | T | ТЗ | collaboration | месяц |
| 39 | `code_index_snapshots` | Code Intel | T | ТЗ | code_intelligence | — |
| 40 | `code_files` | Code Intel | T | Д | code_intelligence | — |
| 41 | `code_symbols` | Code Intel | T | ТЗ | code_intelligence | — |
| 42 | `code_dependencies` | Code Intel | T | ТЗ | code_intelligence | — |
| 43 | `knowledge_chunks` | Code Intel | T | ТЗ | code_intelligence | — |
| 44 | `retrieval_queries` | Code Intel | T | ТЗ | code_intelligence | — |
| 45 | `retrieval_results` | Code Intel | T | ТЗ | code_intelligence | — |
| 46 | `context_packs` | Code Intel | T | ТЗ | code_intelligence | — |
| 47 | `indexing_jobs` | Code Intel | T | ТЗ | code_intelligence | — |
| 48 | `knowledge_entries` | Docs/Knowledge | T | ТЗ | code_intelligence | — |
| 49 | `architecture_decisions` | Docs/Knowledge | T | ТЗ | code_intelligence | — |
| 50 | `documentation_sources` | Docs | T | ТЗ | code_intelligence | — |
| 51 | `documentation_snapshots` | Docs | T | ТЗ | code_intelligence | — |
| 52 | `documentation_sections` | Docs | T | ТЗ | code_intelligence | — |
| 53 | `artifacts` | Quality/Evidence | T | ТЗ | platform | — |
| 54 | `quality_profiles` | Quality | M | Д | verification | — |
| 55 | `quality_gates` | Quality | M | ТЗ | verification | — |
| 56 | `gate_executions` | Quality | T | ТЗ | verification | — |
| 57 | `test_results` | Quality | T | ТЗ | verification | — |
| 58 | `findings` | Quality | T | ТЗ | verification | — |
| 59 | `evidence` | Quality | T | ТЗ | verification | — |
| 60 | `evidence_links` | Quality | T | ТЗ | verification | — |
| 61 | `standards` | Standards | G | ТЗ | standards | — |
| 62 | `standard_controls` | Standards | G | ТЗ | standards | — |
| 63 | `project_controls` | Standards | T | ТЗ | standards | — |
| 64 | `compliance_results` | Standards | T | ТЗ | standards | — |
| 65 | `policies` | Approvals/Policies | M | ТЗ | orchestration | — |
| 66 | `approvals` | Approvals/Policies | T | ТЗ | orchestration | — |
| 67 | `checkpoints` | Reliability | T | ТЗ | orchestration | — |
| 68 | `outbox_events` | Reliability | S | ТЗ | platform | месяц |
| 69 | `processed_events` | Reliability | G | Д | platform | — |
| 70 | `worker_leases` | Reliability | S | ТЗ | platform | — |
| 71 | `external_actions` | Reliability | T | Д | orchestration | — |
| 72 | `run_event_log` | Reliability/SSE | T | Д | platform | — (§20) |
| 73 | `github_installations` | GitHub | T | Д | integrations_github | — |
| 74 | `github_webhook_deliveries` | GitHub | G | Д | integrations_github | — |
| 75 | `pull_requests` | GitHub | T | Д | integrations_github | — |
| 76 | `audit_events` | Audit | M | ТЗ | platform | месяц |

`T*` — `workspaces` сама является корнем tenancy: RLS-политика сравнивает `id` (а не `workspace_id`) с контекстом.

Итого: **76 таблиц** — 61 из каталога ТЗ §7.2 (все строки каталога) + 15 дополнений. Из них 12 зафиксированы архитектурным брифом/ADR (`user_credentials`, `user_sessions`, `idempotency_keys`, `worktrees`, `worker_hosts`, `quality_profiles`, `processed_events`, `external_actions`, `run_event_log`, `github_installations`, `github_webhook_deliveries`, `pull_requests`), 3 предложены этим документом (`project_member_overrides`, `allowed_transitions`, `code_files`, см. §22).

---

## 3. Перечисления

### 3.1 Домены канонических перечислений (`docs/README.md §3`)

```sql
-- Служебные домены
CREATE DOMAIN uuid7       AS uuid CHECK (uuid_extract_version(VALUE) = 7);
CREATE DOMAIN git_sha     AS text CHECK (VALUE ~ '^[0-9a-f]{40}([0-9a-f]{24})?$');
CREATE DOMAIN sha256_hex  AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

-- Канонические перечисления (значения строго из docs/README.md §3)
CREATE DOMAIN run_mode_t AS text CHECK (VALUE IN
  ('CREATE','DEVELOP','DEBUG','AUDIT','MODERNIZE','SECURITY','OPTIMIZE','TEST','MAINTAIN','DOCUMENT','REVIEW'));
CREATE DOMAIN run_status_t AS text CHECK (VALUE IN
  ('CREATED','DISCOVERY','PLANNED','AWAITING_APPROVAL','RUNNING','VERIFYING','COMPLETED','PAUSED','BLOCKED','FAILED','CANCELLED'));
CREATE DOMAIN task_status_t AS text CHECK (VALUE IN
  ('PENDING','READY','LEASED','RUNNING','VERIFYING','SUCCEEDED','BLOCKED','RETRY_WAIT','FAILED','CANCELLED'));
CREATE DOMAIN task_blocked_reason_t AS text CHECK (VALUE IN
  ('APPROVAL_REQUIRED','PROVIDER_UNAVAILABLE','AUTH_REQUIRED','DIRECTIVE_REPLAN','RUN_PAUSED','DOCS_REQUIRED','MANUAL_REVIEW','REVIEW_LOOP_EXHAUSTED','DEPENDENCY_FAILED'));
CREATE DOMAIN execution_status_t AS text CHECK (VALUE IN
  ('QUEUED','STARTING','RUNNING','EXITED','COLLECTED','TIMED_OUT','CANCELLED','CRASHED','AUTH_REQUIRED','RATE_LIMITED'));
CREATE DOMAIN cli_error_code_t AS text CHECK (VALUE IN
  ('CLI_NOT_INSTALLED','AUTH_REQUIRED','RATE_LIMITED','TIMEOUT','SANDBOX_DENIED','PROCESS_FAILED','OUTPUT_PARSE_ERROR','UNSUPPORTED_VERSION','CANCELLED'));
CREATE DOMAIN failure_category_t AS text CHECK (VALUE IN
  ('CLI_NOT_INSTALLED','AUTH_REQUIRED','RATE_LIMITED','TIMEOUT','SANDBOX_DENIED','PROCESS_FAILED','OUTPUT_PARSE_ERROR','UNSUPPORTED_VERSION','CANCELLED',
   'GATE_FAILED','REVIEW_REJECTED','LEASE_LOST','WORKER_CRASH','POLICY_DENIED','INTERNAL_ERROR'));
CREATE DOMAIN cli_provider_t AS text CHECK (VALUE IN ('codex','claude'));
CREATE DOMAIN agent_role_t AS text CHECK (VALUE IN
  ('requirements','architect','planner','developer','reviewer','security','qa','database','performance','devops','maintenance','documentation'));
CREATE DOMAIN risk_level_t AS text CHECK (VALUE IN ('low','medium','high','critical'));
CREATE DOMAIN workspace_role_t AS text CHECK (VALUE IN ('owner','admin','developer','reviewer','viewer'));
CREATE DOMAIN acp_message_type_t AS text CHECK (VALUE IN
  ('TASK_ASSIGN','HANDOFF','QUESTION','ANSWER','REVIEW_REQUEST','FINDING','FIX_REQUEST','EVIDENCE_SUBMITTED','DECISION_PROPOSAL','DECISION_RECORDED','BLOCKED','TASK_COMPLETED'));
CREATE DOMAIN directive_type_t AS text CHECK (VALUE IN
  ('ASK','NEW_TASK','CHANGE_REQUEST','STOP','PAUSE','RESUME','REVIEW','APPROVE','REJECT'));
CREATE DOMAIN directive_status_t AS text CHECK (VALUE IN
  ('RECEIVED','IMPACT_ANALYSIS_PENDING','IMPACT_READY','AWAITING_CONFIRMATION','CONFIRMED','CHECKPOINTING','REPLANNING','DISPATCHED','ACKNOWLEDGED','COMPLETED','REJECTED','CANCELLED','FAILED'));
CREATE DOMAIN directive_impact_type_t AS text CHECK (VALUE IN
  ('NONE','CONTEXT_ONLY','REPLAN_REQUIRED','TASK_CANCEL','TASK_ADD','RESTART_REQUIRED'));
CREATE DOMAIN approval_action_type_t AS text CHECK (VALUE IN
  ('PLAN_APPROVAL','MERGE','PRODUCTION_DEPLOY','DESTRUCTIVE_MIGRATION','DATA_DELETION','SECRET_CHANGE','UNRESTRICTED_NETWORK','DOCS_DOWNLOAD','CRITICAL_DIRECTIVE','MANUAL_REVIEW','SELF_REVIEW_FALLBACK','LOCAL_EXECUTION_FALLBACK'));
CREATE DOMAIN approval_status_t AS text CHECK (VALUE IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED'));
CREATE DOMAIN documentation_mode_t AS text CHECK (VALUE IN ('OFF','SMART','STRICT'));
CREATE DOMAIN quality_profile_t AS text CHECK (VALUE IN ('basic','strict','critical'));
CREATE DOMAIN gate_execution_status_t AS text CHECK (VALUE IN
  ('PENDING','RUNNING','PASS','FAIL','ERROR','SKIPPED','NOT_APPLICABLE'));
CREATE DOMAIN compliance_status_t AS text CHECK (VALUE IN ('PASS','FAIL','NOT_APPLICABLE','UNVERIFIED','MANUAL_REVIEW'));
CREATE DOMAIN finding_severity_t AS text CHECK (VALUE IN ('info','low','medium','high','critical'));
CREATE DOMAIN finding_status_t AS text CHECK (VALUE IN
  ('OPEN','ACKNOWLEDGED','FIXED','VERIFIED_FIXED','FALSE_POSITIVE','ACCEPTED_RISK'));
CREATE DOMAIN evidence_verification_status_t AS text CHECK (VALUE IN ('PENDING','VERIFIED','REJECTED','UNVERIFIED'));
CREATE DOMAIN auth_status_t AS text CHECK (VALUE IN ('UNKNOWN','AUTHENTICATED','AUTH_REQUIRED','EXPIRED','ERROR'));
CREATE DOMAIN provider_availability_t AS text CHECK (VALUE IN ('AVAILABLE','DEGRADED','RATE_LIMITED','UNAVAILABLE'));
CREATE DOMAIN normalized_cli_event_t AS text CHECK (VALUE IN ('started','output','tool_event','artifact','warning','finished'));
-- sse_event_type без 'heartbeat': heartbeat не персистится (§15.6)
CREATE DOMAIN sse_event_type_persisted_t AS text CHECK (VALUE IN
  ('run.state_changed','task.state_changed','agent.message','cli.output','directive.impact_ready','directive.state_changed',
   'approval.requested','approval.decided','gate.completed','run.completed','chat.message'));
```

### 3.2 Локальные перечисления (отсутствуют в README §3 — **предлагаемые дополнения**, см. §22.1)

Правило регистра: статусы жизненного цикла — `UPPER_CASE` (как канонические статусы); виды/типы — `lower_snake_case` (как `risk_level`, `quality_profile`). Исключение — `gate_type`: `UPPER_CASE`, чтобы совпадать с ID-конвенцией `QG-<TYPE>` (README §2).

| Таблица.колонка | Значения |
|---|---|
| `users.status` | `INVITED`, `ACTIVE`, `DISABLED`, `DELETED` |
| `workspaces.status`, `projects.status` | `ACTIVE`, `ARCHIVED`, `DELETION_PENDING` |
| `repositories.provider` | `local`, `github` |
| `repositories.status` | `ACTIVE`, `UNREACHABLE`, `DETACHED` |
| `worktrees.status` | `ACTIVE`, `LOCKED`, `REMOVED`, `ORPHANED` (бриф) |
| `project_settings.security_profile` | `standard`, `hardened` |
| `runs.approval_policy`, `project_settings.approval_policy` | `risk_based`, `always`, `critical_only` (синхронизировано с openapi.yaml) |
| `task_dependencies.dependency_type` | `finish` (ждать `SUCCEEDED`), `review` (зависимая задача — ревью результата), `artifact` (нужен артефакт предшественника; семантика ожидания та же, что `finish`) |
| `task_attempts.status` | `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED` |
| `agent_assignments.review_independence` | `not_applicable`, `independent_provider`, `same_provider_fallback`, `manual` |
| `routing_decisions.outcome` | `SELECTED`, `FALLBACK`, `BLOCKED` |
| `worker_hosts.kind` | `api`, `orchestrator`, `outbox`, `exec`, `verify`, `index` |
| `cli_sessions.status` | `ACTIVE`, `IDLE`, `CLOSED`, `INVALID` |
| `usage_metrics.source` | `cli_reported`, `platform_measured`, `not_exposed_by_cli` |
| `agent_messages.sender_role` | `agent_role` ∪ `orchestrator`, `user` (миграция 0006, ACP §1/§4.2) |
| `agent_messages.recipient_role` | `agent_role` ∪ `orchestrator`, `user` |
| `agent_decisions.decision_type` | `architecture`, `plan`, `routing`, `review_verdict`, `risk_acceptance`, `scope_change`, `other` |
| `agent_decisions.decided_by_type`, `*.actor_type` | `user`, `agent`, `system` (+ `github` для audit) |
| `chat_threads.scope` | `all_agents`, `provider`, `role`, `task`, `approvals` (потоки ТЗ §11) |
| `chat_messages.sender_type` | `user`, `agent`, `system` |
| `chat_messages.message_kind` | `text`, `directive`, `acp_message`, `activity`, `cli_excerpt`, `approval`, `system_notice` |
| `user_directives.target_type` | `run`, `role`, `task`, `provider` |
| `directive_impacts.resolution` | `PENDING`, `APPLIED`, `SKIPPED`, `SUPERSEDED` |
| `directive_acknowledgements.status` | `PENDING`, `DELIVERED`, `ACKNOWLEDGED`, `REJECTED`, `EXPIRED` |
| `code_index_snapshots.status` | `BUILDING`, `READY`, `FAILED`, `SUPERSEDED` |
| `code_symbols.symbol_kind` | `module`, `class`, `interface`, `function`, `method`, `variable`, `constant`, `type`, `enum`, `test`, `route`, `other` |
| `code_dependencies.edge_type` | `imports`, `calls`, `inherits`, `implements`, `references`, `tests` |
| `knowledge_chunks.source_type` | `code_file`, `code_symbol`, `documentation_section`, `knowledge_entry`, `architecture_decision` |
| `retrieval_queries.strategy` | `exact`, `symbol`, `graph`, `fts`, `trigram`, `docs`, `hybrid` |
| `indexing_jobs.kind` / `.status` | `full`, `incremental` / `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED` |
| `knowledge_entries.kind` / `.visibility` | `note`, `requirement`, `specification`, `runbook`, `glossary`, `lesson` / `project`, `workspace` |
| `architecture_decisions.status` | `PROPOSED`, `ACCEPTED`, `REJECTED`, `DEPRECATED`, `SUPERSEDED` |
| `technology_versions.support_status` | `supported`, `lts`, `deprecated`, `eol`, `unknown` |
| `documentation_sources.source_type` | `url`, `pdf`, `markdown`, `html`, `git` |
| `documentation_sources.trust_level` | `official`, `vendor`, `community`, `internal`, `untrusted` |
| `documentation_sources.refresh_policy` | `manual`, `daily`, `weekly`, `monthly` |
| `documentation_snapshots.status` | `FETCHING`, `READY`, `FAILED`, `QUARANTINED`, `SUPERSEDED` |
| `quality_gates.gate_type` | `FORMAT`, `LINT`, `TYPECHECK`, `BUILD`, `UNIT`, `INTEGRATION`, `E2E`, `SAST`, `SCA`, `SECRETS`, `IAC`, `POLICY`, `MIGRATION`, `PERF`, `REVIEW`, `COVERAGE`, `SBOM`, `SIGN`, `SCOPE` (синхронизировано с QUALITY_GATES.md §2.1, нормативный источник) |
| `findings.source` | `gate`, `agent_review`, `manual`, `github` |
| `artifacts.kind` | `git_diff`, `cli_raw_log`, `cli_output`, `test_report`, `coverage_report`, `scan_report`, `sbom`, `context_pack`, `checkpoint_state`, `documentation`, `knowledge`, `report`, `archive`, `other` |
| `evidence.kind` | `command_result`, `test_report`, `scan_report`, `diff`, `review_verdict`, `manual_attestation`, `benchmark`, `agent_claim` |
| `evidence.producer` | `verification_engine`, `cli_gateway`, `agent`, `user`, `github`, `system` |
| `evidence_links.target_type` | `task`, `gate_execution`, `finding`, `compliance_result`, `test_result`, `agent_message`, `approval`, `pull_request`, `architecture_decision`, `agent_decision` |
| `evidence_links.relation` | `supports`, `refutes`, `produced_by`, `verifies`, `references` |
| `standard_controls.verification_method` | `automated`, `manual`, `hybrid` |
| `project_controls.applicability` | `applicable`, `not_applicable`, `tailored` |
| `policies.policy_type` | `routing`, `approval`, `sandbox`, `network`, `documentation`, `review`, `acp_acl`, `provider_settings`, `retention` |
| `approvals.requested_by_type` | `user`, `agent`, `system` |
| `checkpoints.reason` | `plan_approved`, `directive`, `pause`, `pre_external_action`, `periodic`, `recovery` |
| `external_actions.action_type` | `git_push`, `branch_delete`, `pr_create`, `pr_update`, `pr_merge`, `pr_comment`, `migration_apply`, `docs_download`, `production_deploy` |
| `external_actions.status` | `PLANNED`, `EXECUTING`, `CONFIRMED`, `FAILED`, `COMPENSATED` (бриф/ADR-0012) |
| `worker_leases.resource_type` | `task`, `run`, `cli_execution`, `indexing_job`, `external_action`, `outbox_partition`, `recovery` |
| `github_installations.status` | `ACTIVE`, `SUSPENDED`, `REVOKED` |
| `github_webhook_deliveries.status` | `RECEIVED`, `REJECTED`, `PROCESSED`, `IGNORED`, `FAILED` |
| `pull_requests.state` | `OPEN`, `CLOSED`, `MERGED` |
| `audit_events.outcome` | `success`, `denied`, `failure` |

---

## 4. Общие функции и триггеры

```sql
-- Контекст RLS (§19). missing_ok=true + NULLIF: без контекста функция возвращает NULL,
-- а сравнение с NULL никогда не даёт TRUE → строки невидимы (fail-closed).
CREATE FUNCTION app_current_workspace() RETURNS uuid
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT NULLIF(current_setting('app.workspace_id', true), '')::uuid $$;
CREATE FUNCTION app_current_user() RETURNS uuid
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;

-- updated_at
CREATE FUNCTION trg_fn_set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- optimistic locking: каждое UPDATE обязано увеличить version ровно на 1
CREATE FUNCTION trg_fn_enforce_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
    RAISE EXCEPTION 'version must be incremented by 1 (old=%, new=%)', OLD.version, NEW.version
      USING ERRCODE = '40001';  -- serialization_failure → приложение маппит в VersionConflict
  END IF;
  RETURN NEW;
END $$;

-- запрет UPDATE/DELETE для append-only таблиц (вторая линия поверх REVOKE)
CREATE FUNCTION trg_fn_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = '42501';
END $$;

-- запрет изменения workspace_id у существующей строки (общий для всех tenant-таблиц)
CREATE FUNCTION trg_fn_immutable_workspace() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id THEN
    RAISE EXCEPTION 'workspace_id is immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
```

Каждая tenant-таблица получает `trg_<t>_immutable_ws BEFORE UPDATE OF workspace_id`; таблицы с `updated_at` — `trg_<t>_updated_at`; агрегаты с `version` — `trg_<t>_version`. В листингах ниже эти стандартные триггеры не повторяются; специфичные триггеры перечислены явно. Триггеры перехода состояний и прочие инварианты — §18.

---

## 5. IAM и tenancy

### 5.1 `users` (G, identity)
Учётная запись человека. Глобальная: один пользователь может состоять в нескольких workspace.

```sql
CREATE TABLE users (
  id               uuid7       NOT NULL,
  email            text        NOT NULL CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 320),
  display_name     text        NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
  status           text        NOT NULL DEFAULT 'ACTIVE'
                               CONSTRAINT ck_users_status CHECK (status IN ('INVITED','ACTIVE','DISABLED','DELETED')),
  locale           text        NOT NULL DEFAULT 'ru',
  last_login_at    timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_users PRIMARY KEY (id),
  CONSTRAINT uq_users_email UNIQUE (email)
);
```
Email хранится нормализованным (lower) — уникальность без `citext`. Удаление пользователя — логическое (`DELETED` + псевдонимизация `email`/`display_name`), т.к. `users.id` используется в истории и аудите (`RESTRICT`).

### 5.2 `user_credentials` (G, identity) — **дополнение к ТЗ**
Локальные учётные данные (ADR-0009). Отдельно от `users`, чтобы `aicd_app` мог выдать `SELECT` на `users` без доступа к хэшам и чтобы будущий OIDC не требовал nullable-колонок в `users`.

```sql
CREATE TABLE user_credentials (
  user_id              uuid        NOT NULL,
  password_hash        text        NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
  password_changed_at  timestamptz NOT NULL DEFAULT now(),
  failed_attempts      int         NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
  locked_until         timestamptz,
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_user_credentials PRIMARY KEY (user_id),
  CONSTRAINT fk_user_credentials_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
```
Хранится только argon2id-хэш (не секрет в смысле ТЗ §7.1). Доступ: только функции модуля `identity` (`aicd_app`), столбец не попадает ни в одно представление/лог.

### 5.3 `user_sessions` (G, identity) — **дополнение к ТЗ**
Серверные сессии web (ADR-0009): cookie содержит случайный токен, в БД — только его SHA-256.

```sql
CREATE TABLE user_sessions (
  id               uuid7       NOT NULL,
  user_id          uuid        NOT NULL,
  token_hash       sha256_hex  NOT NULL,
  csrf_token_hash  sha256_hex  NOT NULL,
  ip               inet,
  user_agent       text        CHECK (length(user_agent) <= 512),
  created_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at     timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  revoked_at       timestamptz,
  revoke_reason    text        CHECK (revoke_reason IN ('logout','expired','password_changed','admin_revoked','user_disabled')),
  CONSTRAINT pk_user_sessions PRIMARY KEY (id),
  CONSTRAINT uq_user_sessions_token_hash UNIQUE (token_hash),
  CONSTRAINT fk_user_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_user_sessions_expiry CHECK (expires_at > created_at),
  CONSTRAINT ck_user_sessions_revoke CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL))
);
CREATE INDEX ix_user_sessions_user_active ON user_sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_user_sessions_expires ON user_sessions (expires_at) WHERE revoked_at IS NULL;
```

### 5.4 `workspaces` (T*, projects)
Корень tenancy. RLS сравнивает `id` с `app.workspace_id`.

```sql
CREATE TABLE workspaces (
  id          uuid7       NOT NULL,
  name        text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  slug        text        NOT NULL CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'),
  status      text        NOT NULL DEFAULT 'ACTIVE'
                          CONSTRAINT ck_workspaces_status CHECK (status IN ('ACTIVE','ARCHIVED','DELETION_PENDING')),
  settings    jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(settings) = 'object'),
  created_by  uuid        NOT NULL,
  version     bigint      NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_workspaces PRIMARY KEY (id),
  CONSTRAINT uq_workspaces_slug UNIQUE (slug),
  CONSTRAINT fk_workspaces_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT
);
```
`settings` — нефункциональные предпочтения (UI, i18n). Всё, что влияет на безопасность/маршрутизацию, хранится в `policies` (версионируется и попадает в `policy_snapshot`).

### 5.5 `workspace_members` (T, identity)
Членство и RBAC-роль (ТЗ §15).

```sql
CREATE TABLE workspace_members (
  workspace_id  uuid             NOT NULL,
  user_id       uuid             NOT NULL,
  role          workspace_role_t NOT NULL,
  invited_by    uuid,
  joined_at     timestamptz      NOT NULL DEFAULT now(),
  updated_at    timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT pk_workspace_members PRIMARY KEY (workspace_id, user_id),   -- = UNIQUE(workspace_id,user_id) ТЗ
  CONSTRAINT fk_workspace_members_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_workspace_members_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_workspace_members_invited_by FOREIGN KEY (invited_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX ix_workspace_members_user ON workspace_members (user_id);
CREATE INDEX ix_workspace_members_owners ON workspace_members (workspace_id) WHERE role = 'owner';
-- Инвариант «в workspace всегда ≥1 owner»: CONSTRAINT TRIGGER trg_workspace_members_last_owner
-- AFTER UPDATE OR DELETE … DEFERRABLE INITIALLY DEFERRED (§18.10).
```

### 5.6 `project_member_overrides` (T, identity) — **дополнение к ТЗ**
ТЗ §15 требует «workspace/project-scoped permissions»; бриф разрешает на уровне проекта только **понижение** прав. Таблица хранит понижающие переопределения; отсутствие строки = роль workspace.

```sql
CREATE TABLE project_member_overrides (
  workspace_id  uuid             NOT NULL,
  project_id    uuid             NOT NULL,
  user_id       uuid             NOT NULL,
  role          workspace_role_t NOT NULL CHECK (role IN ('developer','reviewer','viewer')),
  set_by        uuid             NOT NULL,
  created_at    timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT pk_project_member_overrides PRIMARY KEY (project_id, user_id),
  CONSTRAINT fk_pmo_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_pmo_member  FOREIGN KEY (workspace_id, user_id) REFERENCES workspace_members(workspace_id, user_id) ON DELETE CASCADE,
  CONSTRAINT fk_pmo_set_by  FOREIGN KEY (set_by) REFERENCES users(id) ON DELETE RESTRICT
);
-- trg_project_member_overrides_downgrade_only: BEFORE INSERT/UPDATE — rank(role) < rank(workspace_members.role),
-- где rank: owner=5, admin=4, developer=3, reviewer=2, viewer=1 (§18.10).
```

### 5.7 `idempotency_keys` (M, platform) — **дополнение к ТЗ**
Хранение результатов POST-команд с `Idempotency-Key` (ТЗ §8). `workspace_id IS NULL` — только для команд вне workspace (`POST /workspaces`).

```sql
CREATE TABLE idempotency_keys (
  id             uuid7       NOT NULL,
  workspace_id   uuid,
  user_id        uuid        NOT NULL,
  key            text        NOT NULL CHECK (length(key) BETWEEN 8 AND 255),
  method         text        NOT NULL CHECK (method IN ('POST','PUT','PATCH','DELETE')),
  path           text        NOT NULL CHECK (length(path) <= 2048),
  request_hash   sha256_hex  NOT NULL,
  status         text        NOT NULL DEFAULT 'IN_PROGRESS' CHECK (status IN ('IN_PROGRESS','COMPLETED')),
  status_code    smallint    CHECK (status_code BETWEEN 100 AND 599),
  response       jsonb       CHECK (response IS NULL OR octet_length(response::text) <= 65536),
  response_ref   text,       -- ссылка на artifact, если ответ > 64 KiB (на практике не ожидается)
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT now() + interval '24 hours',
  CONSTRAINT pk_idempotency_keys PRIMARY KEY (id),
  CONSTRAINT uq_idempotency_keys_scope UNIQUE NULLS NOT DISTINCT (workspace_id, user_id, key),
  CONSTRAINT fk_idempotency_keys_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
  CONSTRAINT fk_idempotency_keys_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_idempotency_keys_completed CHECK (status = 'IN_PROGRESS' OR status_code IS NOT NULL)
);
CREATE INDEX ix_idempotency_keys_expires ON idempotency_keys (expires_at);
```
Повтор с тем же ключом и другим `request_hash` → 422 (`idempotency_key_reused`); с `IN_PROGRESS` → 409. Запись строки происходит в той же транзакции, что и команда.

---

## 6. Проекты, репозитории, стек

### 6.1 `projects` (T, projects)

```sql
CREATE TABLE projects (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  name          text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  description   text        CHECK (length(description) <= 10000),
  status        text        NOT NULL DEFAULT 'ACTIVE'
                            CONSTRAINT ck_projects_status CHECK (status IN ('ACTIVE','ARCHIVED','DELETION_PENDING')),
  created_by    uuid        NOT NULL,
  version       bigint      NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_projects PRIMARY KEY (id),
  CONSTRAINT uq_projects_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_projects_ws_name UNIQUE (workspace_id, name),
  CONSTRAINT fk_projects_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_projects_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE INDEX ix_projects_ws_status ON projects (workspace_id, status, created_at DESC);
```

### 6.2 `project_settings` (T, projects)
Настройки проекта (ТЗ §7.2, §12 Documentation Policy). Одна строка на проект.

```sql
CREATE TABLE project_settings (
  project_id          uuid                 NOT NULL,
  workspace_id        uuid                 NOT NULL,
  documentation_mode  documentation_mode_t NOT NULL DEFAULT 'SMART',
  routing_policy_id   uuid,
  quality_profile     quality_profile_t    NOT NULL DEFAULT 'strict',
  security_profile    text                 NOT NULL DEFAULT 'standard'
                                           CHECK (security_profile IN ('standard','hardened')),
  approval_policy     text                 NOT NULL DEFAULT 'risk_based'
                                           CHECK (approval_policy IN ('risk_based','always','critical_only')),
  max_parallel_tasks  int                  NOT NULL DEFAULT 4 CHECK (max_parallel_tasks BETWEEN 1 AND 64),
  max_review_loops    int                  NOT NULL DEFAULT 3 CHECK (max_review_loops BETWEEN 0 AND 10),
  settings_version    int                  NOT NULL DEFAULT 1 CHECK (settings_version >= 1),
  version             bigint               NOT NULL DEFAULT 0,
  updated_by          uuid,
  updated_at          timestamptz          NOT NULL DEFAULT now(),
  CONSTRAINT pk_project_settings PRIMARY KEY (project_id),
  CONSTRAINT fk_project_settings_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_project_settings_routing_policy FOREIGN KEY (routing_policy_id) REFERENCES policies(id) ON DELETE RESTRICT,
  CONSTRAINT fk_project_settings_updated_by FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
);
```
`settings_version` (ТЗ) — номер версии **схемы** настроек (для миграции содержимого), `version` — optimistic lock. `routing_policy_id` ссылается на `policies` (M-таблица): триггер `trg_project_settings_policy_scope` проверяет `policy_type = 'routing'` и `workspace_id IS NULL OR workspace_id = NEW.workspace_id` (обычный FK не может проверить tenancy для M-таблицы). `quality_profile` по умолчанию `strict` — предложение (§22).

### 6.3 `repositories` (T, projects)

```sql
CREATE TABLE repositories (
  id                      uuid7       NOT NULL,
  workspace_id            uuid        NOT NULL,
  project_id              uuid        NOT NULL,
  provider                text        NOT NULL CHECK (provider IN ('local','github')),
  remote_url              text        CHECK (remote_url IS NULL OR remote_url ~ '^(https://|ssh://|git@)'),
  default_branch          text        NOT NULL DEFAULT 'main' CHECK (length(default_branch) BETWEEN 1 AND 255),
  local_root_ref          text        NOT NULL,   -- логическое имя корня в allowlist хранилища репозиториев, не абсолютный путь
  github_installation_id  uuid,
  github_repo_id          bigint,
  full_name               text,       -- owner/name для GitHub
  status                  text        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','UNREACHABLE','DETACHED')),
  last_synced_at          timestamptz,
  last_synced_commit_sha  git_sha,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_repositories PRIMARY KEY (id),
  CONSTRAINT uq_repositories_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_repositories_local_root UNIQUE (workspace_id, local_root_ref),
  CONSTRAINT fk_repositories_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_repositories_gh_installation FOREIGN KEY (workspace_id, github_installation_id)
      REFERENCES github_installations(workspace_id, id) ON DELETE SET NULL (github_installation_id),
  CONSTRAINT ck_repositories_github CHECK (provider <> 'github' OR (remote_url IS NOT NULL AND full_name IS NOT NULL)),
  CONSTRAINT ck_repositories_local_root_ref CHECK (local_root_ref !~ '(^/|\.\.)')
);
CREATE INDEX ix_repositories_project ON repositories (workspace_id, project_id);
CREATE UNIQUE INDEX uq_repositories_github_repo ON repositories (workspace_id, github_repo_id) WHERE github_repo_id IS NOT NULL;
```
`local_root_ref` — ссылка, разрешаемая `security.paths.safe_join()` относительно настроенного корня хранилища; абсолютные пути и `..` запрещены CHECK (защита от path traversal, ТЗ §20).

### 6.4 `worktrees` (T, orchestration) — **дополнение к ТЗ**
Учёт изолированных git worktree (ADR-0006, ADR-0010) для recovery (ТЗ §6: reconcile Git worktree) и сборки orphan-ов.

```sql
CREATE TABLE worktrees (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  repository_id    uuid        NOT NULL,
  run_id           uuid        NOT NULL,
  task_id          uuid,
  branch_name      text        NOT NULL CHECK (branch_name ~ '^aicd/[A-Za-z0-9._/-]{1,200}$'),
  base_commit_sha  git_sha     NOT NULL,
  head_commit_sha  git_sha,
  path_ref         text        NOT NULL CHECK (path_ref !~ '(^/|\.\.)'),
  worker_host_id   uuid,
  status           text        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','LOCKED','REMOVED','ORPHANED')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  removed_at       timestamptz,
  CONSTRAINT pk_worktrees PRIMARY KEY (id),
  CONSTRAINT uq_worktrees_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_worktrees_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_worktrees_run  FOREIGN KEY (workspace_id, run_id)  REFERENCES runs(workspace_id, id)  ON DELETE RESTRICT,
  CONSTRAINT fk_worktrees_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE SET NULL (task_id),
  CONSTRAINT fk_worktrees_host FOREIGN KEY (worker_host_id) REFERENCES worker_hosts(id) ON DELETE SET NULL,
  CONSTRAINT ck_worktrees_removed CHECK ((status = 'REMOVED') = (removed_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_worktrees_active_branch ON worktrees (repository_id, branch_name) WHERE status IN ('ACTIVE','LOCKED');
CREATE INDEX ix_worktrees_run ON worktrees (workspace_id, run_id);
CREATE INDEX ix_worktrees_reconcile ON worktrees (worker_host_id, status) WHERE status IN ('ACTIVE','LOCKED','ORPHANED');
```

### 6.5 `technologies` (G, projects)

```sql
CREATE TABLE technologies (
  id          uuid7       NOT NULL,
  name        text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  ecosystem   text        NOT NULL CHECK (ecosystem ~ '^[a-z0-9][a-z0-9_.+-]{0,63}$'),  -- python, npm, php, go, docker, postgresql…
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_technologies PRIMARY KEY (id),
  CONSTRAINT uq_technologies_ecosystem_name UNIQUE (ecosystem, name)
);
```

### 6.6 `technology_versions` (G, projects)

```sql
CREATE TABLE technology_versions (
  id              uuid7 NOT NULL,
  technology_id   uuid  NOT NULL,
  version_label   text  NOT NULL CHECK (length(version_label) BETWEEN 1 AND 100),
  support_status  text  NOT NULL DEFAULT 'unknown'
                        CHECK (support_status IN ('supported','lts','deprecated','eol','unknown')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_technology_versions PRIMARY KEY (id),
  CONSTRAINT uq_technology_versions UNIQUE (technology_id, version_label),
  CONSTRAINT fk_technology_versions_technology FOREIGN KEY (technology_id) REFERENCES technologies(id) ON DELETE RESTRICT
);
```

### 6.7 `project_technologies` (T, projects)
Определённый стек проекта (`GET /projects/{id}/technologies`, AC-02).

```sql
CREATE TABLE project_technologies (
  workspace_id           uuid        NOT NULL,
  project_id             uuid        NOT NULL,
  technology_version_id  uuid        NOT NULL,
  detected_from          text        NOT NULL CHECK (length(detected_from) <= 1024),  -- манифест/файл (относительный путь) или 'manual'
  detected_commit_sha    git_sha,
  detected_at            timestamptz NOT NULL DEFAULT now(),
  confirmed_by           uuid,
  confirmed_at           timestamptz,
  CONSTRAINT pk_project_technologies PRIMARY KEY (project_id, technology_version_id),
  CONSTRAINT fk_project_technologies_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_project_technologies_tv FOREIGN KEY (technology_version_id) REFERENCES technology_versions(id) ON DELETE RESTRICT,
  CONSTRAINT fk_project_technologies_confirmed_by FOREIGN KEY (confirmed_by) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT ck_project_technologies_confirm CHECK ((confirmed_by IS NULL) = (confirmed_at IS NULL))
);
CREATE INDEX ix_project_technologies_ws_project ON project_technologies (workspace_id, project_id);
```

---

## 7. Оркестрация

### 7.1 `runs` (T, orchestration)
Запуск (ТЗ §6 Run FSM, §7.3, §8.1). Расширен по брифу: `workspace_id`, `repository_id`, `quality_profile`, `approval_policy`, `blocked_reason`, `plan_version`, `updated_at`; по ADR-0011 — `last_event_seq`.

```sql
CREATE TABLE runs (
  id                    uuid7                  NOT NULL,
  workspace_id          uuid                   NOT NULL,
  project_id            uuid                   NOT NULL,
  repository_id         uuid                   NOT NULL,
  requested_by          uuid                   NOT NULL,
  mode                  run_mode_t             NOT NULL,
  objective             text                   NOT NULL CHECK (length(objective) BETWEEN 1 AND 20000),
  status                run_status_t           NOT NULL DEFAULT 'CREATED',
  blocked_reason        task_blocked_reason_t,
  resume_status         run_status_t,          -- состояние, в которое вернуться из PAUSED/BLOCKED
  base_commit_sha       git_sha,
  quality_profile       quality_profile_t      NOT NULL,
  quality_profile_id    uuid                   NOT NULL,   -- конкретная версия профиля (snapshot)
  approval_policy       text                   NOT NULL DEFAULT 'risk_based'
                                               CHECK (approval_policy IN ('risk_based','always','critical_only')),
  documentation_mode    documentation_mode_t   NOT NULL,   -- snapshot project_settings на момент создания
  policy_snapshot       jsonb                  NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(policy_snapshot) = 'object'),
  plan_version          int                    NOT NULL DEFAULT 0 CHECK (plan_version >= 0),
  approved_plan_version int                    CHECK (approved_plan_version >= 1),
  last_event_seq        bigint                 NOT NULL DEFAULT 0 CHECK (last_event_seq >= 0),
  failure_category      failure_category_t,
  failure_message       text                   CHECK (length(failure_message) <= 4000),
  cancel_requested_at   timestamptz,
  version               bigint                 NOT NULL DEFAULT 0,
  created_at            timestamptz            NOT NULL DEFAULT now(),
  updated_at            timestamptz            NOT NULL DEFAULT now(),
  started_at            timestamptz,
  finished_at           timestamptz,
  CONSTRAINT pk_runs PRIMARY KEY (id),
  CONSTRAINT uq_runs_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_runs_project    FOREIGN KEY (workspace_id, project_id)    REFERENCES projects(workspace_id, id)     ON DELETE RESTRICT,
  CONSTRAINT fk_runs_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_runs_requested_by FOREIGN KEY (requested_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_runs_quality_profile FOREIGN KEY (quality_profile_id) REFERENCES quality_profiles(id) ON DELETE RESTRICT,
  CONSTRAINT ck_runs_blocked_reason CHECK ((status = 'BLOCKED') = (blocked_reason IS NOT NULL)),
  CONSTRAINT ck_runs_resume_status CHECK (
      (resume_status IS NULL OR status IN ('PAUSED','BLOCKED'))
      AND (resume_status IS NULL OR resume_status IN ('DISCOVERY','PLANNED','AWAITING_APPROVAL','RUNNING','VERIFYING'))),
  CONSTRAINT ck_runs_finished CHECK ((status IN ('COMPLETED','FAILED','CANCELLED')) = (finished_at IS NOT NULL)),
  CONSTRAINT ck_runs_failure CHECK (status = 'FAILED' OR failure_category IS NULL),
  CONSTRAINT ck_runs_plan_version CHECK (approved_plan_version IS NULL OR approved_plan_version <= plan_version)
);
CREATE INDEX ix_runs_project_status_created ON runs (workspace_id, project_id, status, created_at DESC);  -- ТЗ §7.3 + workspace_id
CREATE INDEX ix_runs_active ON runs (workspace_id, status) WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED');
CREATE INDEX ix_runs_requested_by ON runs (workspace_id, requested_by, created_at DESC);
-- Специфичные триггеры: trg_runs_transition (§18.3), trg_runs_completion_guard (§18.4),
-- trg_runs_quality_profile_scope (профиль global или того же workspace, code = quality_profile).
```

`quality_profile` (код) дублирует `quality_profiles.code` намеренно (фильтрация/отчёты без JOIN); согласованность проверяет `trg_runs_quality_profile_scope`. `policy_snapshot` — полная копия действующих политик (routing/approval/sandbox/network/review/acp_acl) на момент создания run для воспроизводимости (ТЗ §5).

### 7.2 `tasks` (T, orchestration)
Узел DAG (ТЗ §6 Task FSM). Расширен по брифу: `workspace_id`, `blocked_reason`, `attempt_count`, `retry_budget_seconds`, `timeout_seconds`, `next_attempt_at`, `review_loop_count`, `plan_version`.

```sql
CREATE TABLE tasks (
  id                    uuid7                 NOT NULL,
  workspace_id          uuid                  NOT NULL,
  run_id                uuid                  NOT NULL,
  plan_version          int                   NOT NULL CHECK (plan_version >= 1),
  task_key              text                  NOT NULL CHECK (task_key ~ '^[A-Za-z0-9._-]{1,64}$'), -- стабильный ключ планировщика в пределах плана
  origin_task_id        uuid,                 -- задача, из которой порождена (fix/review loop, replan)
  agent_role            agent_role_t          NOT NULL,
  title                 text                  NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  instructions          text                  NOT NULL CHECK (length(instructions) <= 100000),
  status                task_status_t         NOT NULL DEFAULT 'PENDING',
  blocked_reason        task_blocked_reason_t,
  complexity            smallint              NOT NULL CHECK (complexity BETWEEN 1 AND 5),     -- L1..L5
  risk_level            risk_level_t          NOT NULL,
  acceptance_criteria   jsonb                 NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(acceptance_criteria) = 'array'),
  max_attempts          int                   NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  attempt_count         int                   NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  timeout_seconds       int                   NOT NULL DEFAULT 1800 CHECK (timeout_seconds BETWEEN 30 AND 86400),
  retry_budget_seconds  int                   NOT NULL DEFAULT 7200 CHECK (retry_budget_seconds BETWEEN 0 AND 604800),
  retry_spent_seconds   int                   NOT NULL DEFAULT 0 CHECK (retry_spent_seconds >= 0),
  next_attempt_at       timestamptz,
  review_loop_count     int                   NOT NULL DEFAULT 0 CHECK (review_loop_count >= 0),
  max_review_loops      int                   NOT NULL DEFAULT 3 CHECK (max_review_loops BETWEEN 0 AND 10),
  lease_fencing_token   bigint,               -- токен актуальной аренды (§15.3); записи результата сверяют его
  superseded_at         timestamptz,          -- задача заменена новой версией плана (replan)
  version               bigint                NOT NULL DEFAULT 0,
  created_at            timestamptz           NOT NULL DEFAULT now(),
  updated_at            timestamptz           NOT NULL DEFAULT now(),
  started_at            timestamptz,
  finished_at           timestamptz,
  CONSTRAINT pk_tasks PRIMARY KEY (id),
  CONSTRAINT uq_tasks_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_tasks_ws_run_id UNIQUE (workspace_id, run_id, id),          -- цель составных FK task_dependencies
  CONSTRAINT uq_tasks_run_plan_key UNIQUE (run_id, plan_version, task_key),
  CONSTRAINT fk_tasks_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_tasks_origin FOREIGN KEY (workspace_id, run_id, origin_task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,          -- origin только из того же run
  CONSTRAINT ck_tasks_blocked_reason CHECK ((status = 'BLOCKED') = (blocked_reason IS NOT NULL)),
  CONSTRAINT ck_tasks_attempts CHECK (attempt_count <= max_attempts),
  CONSTRAINT ck_tasks_retry_wait CHECK (status <> 'RETRY_WAIT' OR next_attempt_at IS NOT NULL),
  CONSTRAINT ck_tasks_leased CHECK (status NOT IN ('LEASED','RUNNING') OR lease_fencing_token IS NOT NULL),
  CONSTRAINT ck_tasks_finished CHECK ((status IN ('SUCCEEDED','FAILED','CANCELLED')) = (finished_at IS NOT NULL)),
  CONSTRAINT ck_tasks_superseded CHECK (superseded_at IS NULL OR status IN ('CANCELLED','SUCCEEDED','FAILED'))
);
CREATE INDEX ix_tasks_run_status ON tasks (workspace_id, run_id, status);                      -- ТЗ §7.3 + workspace_id
CREATE INDEX ix_tasks_ready ON tasks (workspace_id, run_id, created_at) WHERE status = 'READY';   -- выборка планировщиком
CREATE INDEX ix_tasks_retry_due ON tasks (next_attempt_at) WHERE status = 'RETRY_WAIT';
CREATE INDEX ix_tasks_pending ON tasks (run_id) WHERE status = 'PENDING';
CREATE INDEX ix_tasks_blocked ON tasks (workspace_id, blocked_reason) WHERE status = 'BLOCKED';
CREATE INDEX ix_tasks_origin ON tasks (origin_task_id) WHERE origin_task_id IS NOT NULL;
-- Специфичные триггеры: trg_tasks_transition (§18.3), trg_tasks_lock_run_share (§18.4),
-- trg_tasks_success_guard (§18.4).
```

### 7.3 `task_dependencies` (T, orchestration)
Рёбра DAG. Инвариант «зависимость только внутри одного run» (ТЗ §7.3) обеспечен **декларативно**: денормализованный `run_id` входит в оба составных FK на `tasks(workspace_id, run_id, id)` — строка, связывающая задачи разных runs, не может существовать. Отсутствие циклов — §18.2.

```sql
CREATE TABLE task_dependencies (
  workspace_id        uuid        NOT NULL,
  run_id              uuid        NOT NULL,
  task_id             uuid        NOT NULL,
  depends_on_task_id  uuid        NOT NULL,
  dependency_type     text        NOT NULL DEFAULT 'finish' CHECK (dependency_type IN ('finish','review','artifact')),
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_task_dependencies PRIMARY KEY (task_id, depends_on_task_id),
  CONSTRAINT ck_task_dependencies_no_self CHECK (task_id <> depends_on_task_id),
  CONSTRAINT fk_task_dependencies_task FOREIGN KEY (workspace_id, run_id, task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_task_dependencies_depends_on FOREIGN KEY (workspace_id, run_id, depends_on_task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_task_dependencies_parent ON task_dependencies (depends_on_task_id);   -- ТЗ §7.3
CREATE INDEX ix_task_dependencies_run ON task_dependencies (workspace_id, run_id);
-- trg_task_dependencies_same_run_backstop: BEFORE INSERT/UPDATE — защитная проверка (на случай будущей
--   миграции, ослабившей FK) tasks.run_id обоих концов = NEW.run_id; RAISE 'cross-run dependency'.
-- trg_task_dependencies_acyclic: CONSTRAINT TRIGGER AFTER INSERT OR UPDATE DEFERRABLE INITIALLY DEFERRED (§18.2).
```

### 7.4 `task_attempts` (T, orchestration)
Попытка исполнения задачи; основа retry budget и failure category (AC-07).

```sql
CREATE TABLE task_attempts (
  id                   uuid7              NOT NULL,
  workspace_id         uuid               NOT NULL,
  task_id              uuid               NOT NULL,
  attempt_no           int                NOT NULL CHECK (attempt_no BETWEEN 1 AND 10),
  status               text               NOT NULL DEFAULT 'RUNNING'
                                          CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  provider             cli_provider_t,    -- NULL для попыток без CLI (детерминированный L1-инструмент)
  agent_assignment_id  uuid,
  routing_decision_id  uuid,
  fencing_token        bigint             NOT NULL,
  started_at           timestamptz        NOT NULL DEFAULT now(),
  finished_at          timestamptz,
  failure_category     failure_category_t,
  failure_message      text               CHECK (length(failure_message) <= 4000),   -- уже редактированный текст
  CONSTRAINT pk_task_attempts PRIMARY KEY (id),
  CONSTRAINT uq_task_attempts_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_task_attempts_task_attempt_no UNIQUE (task_id, attempt_no),
  CONSTRAINT fk_task_attempts_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_task_attempts_assignment FOREIGN KEY (workspace_id, agent_assignment_id)
      REFERENCES agent_assignments(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_task_attempts_routing FOREIGN KEY (workspace_id, routing_decision_id)
      REFERENCES routing_decisions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_task_attempts_failure CHECK (
      (status <> 'FAILED' OR failure_category IS NOT NULL)
      AND (status NOT IN ('RUNNING','SUCCEEDED') OR failure_category IS NULL)),
  CONSTRAINT ck_task_attempts_finished CHECK ((status = 'RUNNING') = (finished_at IS NULL))
);
CREATE UNIQUE INDEX uq_task_attempts_one_running ON task_attempts (task_id) WHERE status = 'RUNNING';
CREATE INDEX ix_task_attempts_failure ON task_attempts (workspace_id, failure_category, started_at DESC) WHERE status = 'FAILED';
```

### 7.5 `agent_profiles` (G, agent_runtime)
12 логических ролей (ТЗ §4). Глобальный справочник, заполняется seed-миграцией; workspace-специфичные отклонения — через `policies(policy_type='agent'…)` не вводятся в v1 (§22).

```sql
CREATE TABLE agent_profiles (
  id                            uuid7        NOT NULL,
  role                          agent_role_t NOT NULL,
  instruction_template_version  text         NOT NULL CHECK (instruction_template_version ~ '^\d+\.\d+\.\d+$'),
  allowed_tools                 jsonb        NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(allowed_tools) = 'array'),
  required_capabilities         jsonb        NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(required_capabilities) = 'array'),
  policy_profile_id             uuid,
  enabled                       boolean      NOT NULL DEFAULT true,
  created_at                    timestamptz  NOT NULL DEFAULT now(),
  updated_at                    timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_profiles PRIMARY KEY (id),
  CONSTRAINT uq_agent_profiles_role UNIQUE (role),
  CONSTRAINT fk_agent_profiles_policy FOREIGN KEY (policy_profile_id) REFERENCES policies(id) ON DELETE RESTRICT
);
-- trg_agent_profiles_global_policy: policy_profile_id должен ссылаться на policies с workspace_id IS NULL.
```
**Реализация (миграция 0006):** таблица создана без seed — содержимое профилей версионируется файлами и синхронизируется отдельно (P4-02); способ заполнения (seed-миграция или синхронизация `aicd`-командой под `aicd_migrator`) — открытый вопрос. `aicd_app`/`aicd_worker` — только `SELECT`. Владелец в коде — `aicd_orchestration` (модуль `agent_runtime` пока не выделен).

### 7.6 `agent_assignments` (T, orchestration)
Назначение профиля и провайдера задаче. Колонка `review_independence` фиксирует, было ли ревью действительно независимым (ТЗ §4: «нельзя ложно маркировать самопроверку как независимую»).

```sql
CREATE TABLE agent_assignments (
  id                   uuid7          NOT NULL,
  workspace_id         uuid           NOT NULL,
  task_id              uuid           NOT NULL,
  agent_profile_id     uuid           NOT NULL,
  provider             cli_provider_t NOT NULL,
  routing_decision_id  uuid,
  reviewed_attempt_id  uuid,          -- для ролей reviewer/security: попытка, результат которой ревьюируется
  review_independence  text           NOT NULL DEFAULT 'not_applicable'
                                      CHECK (review_independence IN ('not_applicable','independent_provider','same_provider_fallback','manual')),
  reason               text           NOT NULL CHECK (length(reason) <= 2000),
  assigned_at          timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_assignments PRIMARY KEY (id),
  CONSTRAINT uq_agent_assignments_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_agent_assignments_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_assignments_profile FOREIGN KEY (agent_profile_id) REFERENCES agent_profiles(id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_assignments_routing FOREIGN KEY (workspace_id, routing_decision_id)
      REFERENCES routing_decisions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_assignments_reviewed_attempt FOREIGN KEY (workspace_id, reviewed_attempt_id)
      REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_agent_assignments_review CHECK (review_independence = 'not_applicable' OR reviewed_attempt_id IS NOT NULL)
);
CREATE INDEX ix_agent_assignments_task ON agent_assignments (workspace_id, task_id, assigned_at DESC);
-- trg_agent_assignments_independence (§18.8): если review_independence = 'independent_provider',
--   то provider <> task_attempts.provider ревьюируемой попытки; иначе RAISE.
--   'same_provider_fallback' допустим только при наличии APPROVED approvals.action_type='SELF_REVIEW_FALLBACK' для задачи.
```

### 7.7 `routing_decisions` (T, orchestration)
Решение Router (ТЗ §5) с разбором оценки и версией политики для воспроизводимости.

```sql
CREATE TABLE routing_decisions (
  id                 uuid7          NOT NULL,
  workspace_id       uuid           NOT NULL,
  task_id            uuid           NOT NULL,
  outcome            text           NOT NULL CHECK (outcome IN ('SELECTED','FALLBACK','BLOCKED')),
  selected_provider  cli_provider_t,
  complexity_level   smallint       NOT NULL CHECK (complexity_level BETWEEN 1 AND 5),
  score_breakdown    jsonb          NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(score_breakdown) = 'object'),
  reason             text           NOT NULL CHECK (length(reason) <= 2000),
  policy_id          uuid,
  policy_version     int            NOT NULL CHECK (policy_version >= 1),
  created_at         timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_routing_decisions PRIMARY KEY (id),
  CONSTRAINT uq_routing_decisions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_routing_decisions_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_routing_decisions_policy FOREIGN KEY (policy_id) REFERENCES policies(id) ON DELETE RESTRICT,
  CONSTRAINT ck_routing_decisions_outcome CHECK ((outcome = 'BLOCKED') = (selected_provider IS NULL))
);
CREATE INDEX ix_routing_decisions_task ON routing_decisions (workspace_id, task_id, created_at DESC);
```
`task_attempt_id` из исходного каталога ТЗ не требуется: связь попытка→решение хранится в `task_attempts.routing_decision_id` (решение принимается до создания попытки).

### 7.8 `allowed_transitions` (G, orchestration) — **дополнение к ТЗ**
Справочник разрешённых переходов FSM как защитный рубеж БД (см. обоснование в §18.3). Заполняется только миграциями, генерируемыми из `orchestration/transitions.py` (ADR-0007); CI-тест сверяет совпадение.

```sql
CREATE TABLE allowed_transitions (
  entity       text NOT NULL CHECK (entity IN ('run','task','execution','directive','approval','external_action')),
  from_status  text NOT NULL,
  to_status    text NOT NULL,
  CONSTRAINT pk_allowed_transitions PRIMARY KEY (entity, from_status, to_status),
  CONSTRAINT ck_allowed_transitions_not_self CHECK (from_status <> to_status)
);
```
Начальное содержимое — §18.3.

---

## 8. CLI Gateway

### 8.1 `worker_hosts` (G, cli_adapters) — **дополнение к ТЗ**
ТЗ §7.2 ссылается на `cli_installations.host_id`, но не определяет таблицу хостов. Нужна для heartbeat, recovery по хосту (PID/process group/worktree живут на конкретном хосте) и привязки установок CLI.

```sql
CREATE TABLE worker_hosts (
  id                 uuid7       NOT NULL,
  hostname           text        NOT NULL CHECK (hostname ~ '^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$'),
  kind               text        NOT NULL CHECK (kind IN ('api','orchestrator','outbox','exec','verify','index')),
  labels             jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(labels) = 'object'),
  status             text        NOT NULL DEFAULT 'ONLINE' CHECK (status IN ('ONLINE','DRAINING','OFFLINE')),
  last_heartbeat_at  timestamptz NOT NULL DEFAULT now(),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_worker_hosts PRIMARY KEY (id),
  CONSTRAINT uq_worker_hosts_hostname_kind UNIQUE (hostname, kind)
);
CREATE INDEX ix_worker_hosts_heartbeat ON worker_hosts (last_heartbeat_at) WHERE status <> 'OFFLINE';
```

### 8.2 `cli_providers` (G, cli_adapters)

```sql
CREATE TABLE cli_providers (
  id            uuid7          NOT NULL,
  code          cli_provider_t NOT NULL,
  display_name  text           NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
  enabled       boolean        NOT NULL DEFAULT true,
  created_at    timestamptz    NOT NULL DEFAULT now(),
  updated_at    timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_cli_providers PRIMARY KEY (id),
  CONSTRAINT uq_cli_providers_code UNIQUE (code)
);
```
Только две строки (`codex`, `claude`) — seed-миграция; добавление третьего провайдера требует изменения домена `cli_provider_t` и ADR (ADR-0003). Workspace-уровневые «безопасные настройки» провайдера (`PATCH /providers/{id}/settings`) хранятся в `policies(policy_type='provider_settings')`, а не здесь.

### 8.3 `cli_installations` (G, cli_adapters)
Обнаруженная установка CLI на хосте и результат runtime probe (ТЗ §9, ADR-0003).

```sql
CREATE TABLE cli_installations (
  id                     uuid7          NOT NULL,
  provider_id            uuid           NOT NULL,
  host_id                uuid           NOT NULL,
  binary_path            text           NOT NULL CHECK (binary_path ~ '^/[A-Za-z0-9._/+-]+$' AND length(binary_path) <= 1024),
  detected_version       text           CHECK (length(detected_version) <= 100),
  compatibility          text           NOT NULL DEFAULT 'untested'
                                        CHECK (compatibility IN ('supported','untested','unsupported')),  -- по compatibility matrix
  auth_status            auth_status_t  NOT NULL DEFAULT 'UNKNOWN',
  capabilities           jsonb          NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(capabilities) = 'object'),
  credential_volume_ref  text           CHECK (credential_volume_ref ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'),  -- имя docker volume, НЕ содержимое
  max_concurrency        int            NOT NULL DEFAULT 1 CHECK (max_concurrency BETWEEN 1 AND 32),
  enabled                boolean        NOT NULL DEFAULT true,
  checked_at             timestamptz,
  created_at             timestamptz    NOT NULL DEFAULT now(),
  updated_at             timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_cli_installations PRIMARY KEY (id),
  CONSTRAINT uq_cli_installations_host_binary UNIQUE (provider_id, host_id, binary_path),
  CONSTRAINT fk_cli_installations_provider FOREIGN KEY (provider_id) REFERENCES cli_providers(id) ON DELETE RESTRICT,
  CONSTRAINT fk_cli_installations_host FOREIGN KEY (host_id) REFERENCES worker_hosts(id) ON DELETE RESTRICT
);
CREATE INDEX ix_cli_installations_routable ON cli_installations (provider_id)
  WHERE enabled AND auth_status = 'AUTHENTICATED' AND compatibility <> 'unsupported';
```
`credential_volume_ref` — «безопасная ссылка» в смысле ТЗ §7.1: имя тома/хранилища, в котором CLI сам держит свою авторизацию. Содержимое тома в БД не попадает никогда.

### 8.4 `cli_sessions` (G, cli_adapters)
Сессии CLI, если CLI их поддерживает (feature detection). Без учётных данных (ТЗ §7.2).

```sql
CREATE TABLE cli_sessions (
  id               uuid7       NOT NULL,
  installation_id  uuid        NOT NULL,
  session_ref      text        NOT NULL CHECK (length(session_ref) BETWEEN 1 AND 255),  -- непрозрачный ID сессии CLI
  status           text        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','IDLE','CLOSED','INVALID')),
  started_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at     timestamptz NOT NULL DEFAULT now(),
  ended_at         timestamptz,
  CONSTRAINT pk_cli_sessions PRIMARY KEY (id),
  CONSTRAINT uq_cli_sessions_ref UNIQUE (installation_id, session_ref),
  CONSTRAINT fk_cli_sessions_installation FOREIGN KEY (installation_id) REFERENCES cli_installations(id) ON DELETE CASCADE,
  CONSTRAINT ck_cli_sessions_ended CHECK ((status = 'CLOSED') = (ended_at IS NOT NULL))
);
CREATE INDEX ix_cli_sessions_active ON cli_sessions (installation_id) WHERE status IN ('ACTIVE','IDLE');
```
Сессии глобальны (бриф), но **переиспользование** сессии между workspace запрещено на уровне CLI Gateway (контекст одной сессии не должен смешивать данные арендаторов); см. §22 OQ-DB-06.

### 8.5 `cli_executions` (T, cli_adapters)
Один запуск CLI по контракту v1.0 (ТЗ §6 Execution FSM, §9).

```sql
CREATE TABLE cli_executions (
  id                   uuid7              NOT NULL,   -- = execution_id в ExecutionRequest
  workspace_id         uuid               NOT NULL,
  task_attempt_id      uuid               NOT NULL,
  installation_id      uuid               NOT NULL,
  session_id           uuid,
  worktree_id          uuid,
  status               execution_status_t NOT NULL DEFAULT 'QUEUED',
  contract_version     text               NOT NULL DEFAULT '1.0' CHECK (contract_version ~ '^\d+\.\d+$'),
  command_profile      text               NOT NULL CHECK (command_profile ~ '^[a-z0-9_.-]{1,64}$'),  -- allowlisted профиль адаптера
  request_hash         sha256_hex         NOT NULL,
  worktree_ref         text               CHECK (worktree_ref !~ '(^/|\.\.)'),
  container_ref        text               CHECK (length(container_ref) <= 128),
  pid_ref              text               CHECK (pid_ref ~ '^[A-Za-z0-9.-]+:[0-9]+(:[0-9]+)?$'),     -- host:pid[:pgid]
  timeout_seconds      int                NOT NULL CHECK (timeout_seconds BETWEEN 30 AND 86400),
  fencing_token        bigint             NOT NULL,
  exit_code            int,
  failure_category     cli_error_code_t,
  output_artifact_id   uuid,              -- нормализованный результат
  raw_log_artifact_id  uuid,              -- защищённый raw log (ТЗ §9)
  created_at           timestamptz        NOT NULL DEFAULT now(),   -- QUEUED
  started_at           timestamptz,
  exited_at            timestamptz,
  finished_at          timestamptz,       -- COLLECTED или иное терминальное
  updated_at           timestamptz        NOT NULL DEFAULT now(),
  CONSTRAINT pk_cli_executions PRIMARY KEY (id),
  CONSTRAINT uq_cli_executions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_cli_executions_attempt FOREIGN KEY (workspace_id, task_attempt_id) REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_cli_executions_installation FOREIGN KEY (installation_id) REFERENCES cli_installations(id) ON DELETE RESTRICT,
  CONSTRAINT fk_cli_executions_session FOREIGN KEY (session_id) REFERENCES cli_sessions(id) ON DELETE SET NULL,
  CONSTRAINT fk_cli_executions_worktree FOREIGN KEY (workspace_id, worktree_id) REFERENCES worktrees(workspace_id, id) ON DELETE SET NULL (worktree_id),
  CONSTRAINT fk_cli_executions_output FOREIGN KEY (workspace_id, output_artifact_id) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_cli_executions_raw_log FOREIGN KEY (workspace_id, raw_log_artifact_id) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_cli_executions_exit_code CHECK (status NOT IN ('EXITED','COLLECTED') OR exit_code IS NOT NULL),
  CONSTRAINT ck_cli_executions_collected CHECK (status <> 'COLLECTED' OR output_artifact_id IS NOT NULL),
  CONSTRAINT ck_cli_executions_failure CHECK (
      (status NOT IN ('TIMED_OUT','CANCELLED','CRASHED','AUTH_REQUIRED','RATE_LIMITED') OR failure_category IS NOT NULL)
      AND (status <> 'TIMED_OUT'     OR failure_category = 'TIMEOUT')
      AND (status <> 'CANCELLED'     OR failure_category = 'CANCELLED')
      AND (status <> 'AUTH_REQUIRED' OR failure_category = 'AUTH_REQUIRED')
      AND (status <> 'RATE_LIMITED'  OR failure_category = 'RATE_LIMITED')),
  CONSTRAINT ck_cli_executions_finished CHECK (
      (status IN ('COLLECTED','TIMED_OUT','CANCELLED','CRASHED','AUTH_REQUIRED','RATE_LIMITED')) = (finished_at IS NOT NULL))
);
CREATE INDEX ix_cli_executions_attempt ON cli_executions (workspace_id, task_attempt_id);
CREATE INDEX ix_cli_executions_active ON cli_executions (installation_id, created_at)
  WHERE status IN ('QUEUED','STARTING','RUNNING','EXITED');                  -- лимиты параллелизма + recovery
-- trg_cli_executions_transition (§18.3).
```
`exit_code = 0` при `EXITED/COLLECTED` **не** означает verified (ТЗ §9): статус проверки живёт в `evidence`/`gate_executions`, не здесь. `failure_category` у `CRASHED` может быть `PROCESS_FAILED`/`SANDBOX_DENIED`/`OUTPUT_PARSE_ERROR`; у `EXITED` с ненулевым кодом — `PROCESS_FAILED` либо `NULL` (решает адаптер).

### 8.6 `provider_health` (G, cli_adapters)
Временной ряд проверок доступности/авторизации установки (ТЗ §5, §16 alerting). Admin-only (бриф).

```sql
CREATE TABLE provider_health (
  id                 uuid7                   NOT NULL,
  installation_id    uuid                    NOT NULL,
  auth_status        auth_status_t           NOT NULL,
  availability       provider_availability_t NOT NULL,
  error_category     cli_error_code_t,
  detail             text                    CHECK (length(detail) <= 2000),   -- редактированный текст
  probe_duration_ms  int                     CHECK (probe_duration_ms >= 0),
  observed_at        timestamptz             NOT NULL DEFAULT now(),
  CONSTRAINT pk_provider_health PRIMARY KEY (id),
  CONSTRAINT fk_provider_health_installation FOREIGN KEY (installation_id) REFERENCES cli_installations(id) ON DELETE CASCADE,
  CONSTRAINT ck_provider_health_error CHECK (availability IN ('AVAILABLE','DEGRADED') OR error_category IS NOT NULL)
);
CREATE INDEX ix_provider_health_latest ON provider_health (installation_id, observed_at DESC);
```

### 8.7 `usage_metrics` (T, cli_adapters)
Метрики использования по исполнению. Токены/лимиты — только если реально предоставлены CLI (ТЗ §9, §16); иначе `source = 'not_exposed_by_cli'` и `metric_value IS NULL` (UI показывает `unknown`).

```sql
CREATE TABLE usage_metrics (
  id            uuid7          NOT NULL,
  workspace_id  uuid           NOT NULL,
  execution_id  uuid           NOT NULL,
  provider      cli_provider_t NOT NULL,
  metric_name   text           NOT NULL CHECK (metric_name ~ '^[a-z][a-z0-9_.]{0,99}$'),  -- tokens.input, duration.wall, …
  metric_value  numeric,
  unit          text           NOT NULL CHECK (unit IN ('tokens','seconds','milliseconds','bytes','count','percent')),
  source        text           NOT NULL CHECK (source IN ('cli_reported','platform_measured','not_exposed_by_cli')),
  observed_at   timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_usage_metrics PRIMARY KEY (id),
  CONSTRAINT fk_usage_metrics_execution FOREIGN KEY (workspace_id, execution_id) REFERENCES cli_executions(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT ck_usage_metrics_value CHECK ((source = 'not_exposed_by_cli') = (metric_value IS NULL)),
  CONSTRAINT ck_usage_metrics_nonneg CHECK (metric_value IS NULL OR metric_value >= 0),
  CONSTRAINT uq_usage_metrics_exec_metric UNIQUE (execution_id, metric_name, source)
);
CREATE INDEX ix_usage_metrics_ws_time ON usage_metrics (workspace_id, observed_at DESC);
```

---

## 9. ACP, чат, поручения, активность

### 9.1 `agent_messages` (T, collaboration)
Принятые брокером ACP-сообщения (ТЗ §10). Отклонённые брокером (schema/ACL/limit) сюда не пишутся — они фиксируются в `agent_activity_events` (`event_type = 'acp.rejected'`) и `audit_events`. Таблица append-only.

```sql
CREATE TABLE agent_messages (
  id                   uuid7              NOT NULL,    -- = message_id конверта
  workspace_id         uuid               NOT NULL,
  run_id               uuid               NOT NULL,
  task_id              uuid,
  protocol             text               NOT NULL DEFAULT 'acp/1.0' CHECK (protocol ~ '^acp/\d+\.\d+$'),
  sender_role          text               NOT NULL CHECK (sender_role IN
                         ('requirements','architect','planner','developer','reviewer','security','qa','database',
                          'performance','devops','maintenance','documentation','orchestrator')),
  sender_provider      cli_provider_t,
  recipient_role       text               NOT NULL CHECK (recipient_role IN
                         ('requirements','architect','planner','developer','reviewer','security','qa','database',
                          'performance','devops','maintenance','documentation','orchestrator','user')),
  message_type         acp_message_type_t NOT NULL,
  correlation_id       uuid,
  reply_to_id          uuid,
  schema_version       text               NOT NULL DEFAULT '1.0' CHECK (schema_version ~ '^\d+\.\d+$'),
  payload              jsonb              NOT NULL,
  source_execution_id  uuid,              -- исполнение CLI, из вывода которого извлечено сообщение
  created_at           timestamptz        NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_messages PRIMARY KEY (id),
  CONSTRAINT uq_agent_messages_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_agent_messages_run  FOREIGN KEY (workspace_id, run_id)  REFERENCES runs(workspace_id, id)  ON DELETE RESTRICT,
  CONSTRAINT fk_agent_messages_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_messages_reply_to FOREIGN KEY (workspace_id, reply_to_id) REFERENCES agent_messages(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_messages_execution FOREIGN KEY (workspace_id, source_execution_id) REFERENCES cli_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_agent_messages_payload_object CHECK (jsonb_typeof(payload) = 'object'),
  CONSTRAINT ck_agent_messages_payload_size CHECK (octet_length(payload::text) <= 32768),   -- ТЗ §10: ≤ 32 KiB
  CONSTRAINT ck_agent_messages_no_self_reply CHECK (reply_to_id IS NULL OR reply_to_id <> id),
  CONSTRAINT ck_agent_messages_sender_provider CHECK ((sender_role = 'orchestrator') = (sender_provider IS NULL))
);
CREATE INDEX ix_agent_messages_run_time ON agent_messages (workspace_id, run_id, created_at);     -- ТЗ §7.3 + workspace_id
CREATE INDEX ix_agent_messages_correlation ON agent_messages (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX ix_agent_messages_task_time ON agent_messages (workspace_id, task_id, created_at) WHERE task_id IS NOT NULL;
CREATE INDEX ix_agent_messages_reply_to ON agent_messages (reply_to_id) WHERE reply_to_id IS NOT NULL;
-- trg_agent_messages_append_only: BEFORE UPDATE OR DELETE → trg_fn_append_only() (кроме роли aicd_maintenance, §20).
```
**Реализация (миграция 0006) — отличия от листинга:** (1) отправитель `user` (ACP §1, §4.2: `QUESTION`/`ANSWER` из Live Agent Chat) — `sender_role` допускает `'user'`, добавлена `sender_user_id uuid` (FK `users`), вместо `ck_agent_messages_sender_provider` — `ck_agent_messages_sender` (агент → `sender_provider`, `user` → `sender_user_id`, `orchestrator` → ни того, ни другого); (2) поля конверта ACP §2 / дополнения ACP §11: `task_attempt_id` (составной FK `task_attempts`, только при `task_id` и отправителе-агенте, `ck_agent_messages_attempt`) и `recipient_task_id` (`to.task_id`, составной FK `tasks(workspace_id, run_id, id)` — получатель из того же run; индекс `ix_agent_messages_inbox`); (3) `correlation_id NOT NULL` (ACP 1.0 требует значение), индекс по нему без предиката; (4) append-only: `UPDATE` запрещён всем, `DELETE` — только `aicd_maintenance` в сессии обслуживания (архив run, §20.4; функция `trg_fn_append_only_except_maintenance`).

Ограничение `≤ 32768` байт на `payload::text` — защитный рубеж БД; брокер ACP проверяет размер **всего** сериализованного конверта до вставки (ТЗ §10). Крупные данные — только `artifact` refs в payload.

### 9.2 `message_deliveries` (T, collaboration)
Учёт доставки ACP-сообщения каждому потребителю (идемпотентный consumer, at-least-once; ТЗ §10).

```sql
CREATE TABLE message_deliveries (
  workspace_id        uuid        NOT NULL,
  message_id          uuid        NOT NULL,
  consumer_id         text        NOT NULL CHECK (consumer_id ~ '^[a-z0-9_.:-]{1,128}$'),   -- напр. 'task:<uuid>', 'ui-projection'
  delivery_count      int         NOT NULL DEFAULT 0 CHECK (delivery_count >= 0),
  first_delivered_at  timestamptz,
  last_delivered_at   timestamptz,
  acked_at            timestamptz,
  dead_lettered_at    timestamptz,
  last_error          text        CHECK (length(last_error) <= 2000),
  CONSTRAINT pk_message_deliveries PRIMARY KEY (message_id, consumer_id),    -- = UNIQUE(message_id,consumer_id) ТЗ
  CONSTRAINT fk_message_deliveries_message FOREIGN KEY (workspace_id, message_id) REFERENCES agent_messages(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT ck_message_deliveries_ack CHECK (acked_at IS NULL OR delivery_count >= 1),
  CONSTRAINT ck_message_deliveries_terminal CHECK (acked_at IS NULL OR dead_lettered_at IS NULL)
);
CREATE INDEX ix_message_deliveries_pending ON message_deliveries (consumer_id, last_delivered_at)
  WHERE acked_at IS NULL AND dead_lettered_at IS NULL;
```
**Реализация (миграция 0006):** добавлена `next_attempt_at timestamptz` (backoff ретраев, ACP §7/§11); `ix_message_deliveries_pending` — по `(consumer_id, next_attempt_at)`; дополнительно `ix_message_deliveries_ws_message (workspace_id, message_id)`. Права: `aicd_worker` — `SELECT/INSERT/UPDATE`, `aicd_app` — `SELECT`.

### 9.3 `agent_decisions` (T, orchestration)
Зафиксированные решения (`DECISION_RECORDED`, вердикты ревью, принятие риска) с обоснованием и ссылками на evidence (ТЗ §7.2, §10).

```sql
CREATE TABLE agent_decisions (
  id                  uuid7        NOT NULL,
  workspace_id        uuid         NOT NULL,
  run_id              uuid         NOT NULL,
  task_id             uuid,
  decision_type       text         NOT NULL CHECK (decision_type IN
                        ('architecture','plan','routing','review_verdict','risk_acceptance','scope_change','other')),
  rationale           text         NOT NULL CHECK (length(rationale) BETWEEN 1 AND 20000),
  evidence_refs       uuid[]       NOT NULL DEFAULT '{}',          -- id строк evidence; проверяются приложением
  acp_message_id      uuid,
  decided_by_type     text         NOT NULL CHECK (decided_by_type IN ('user','agent','system')),
  decided_by_role     agent_role_t,
  decided_by_user_id  uuid,
  created_at          timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_decisions PRIMARY KEY (id),
  CONSTRAINT uq_agent_decisions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_agent_decisions_run  FOREIGN KEY (workspace_id, run_id)  REFERENCES runs(workspace_id, id)  ON DELETE RESTRICT,
  CONSTRAINT fk_agent_decisions_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_decisions_message FOREIGN KEY (workspace_id, acp_message_id) REFERENCES agent_messages(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_decisions_user FOREIGN KEY (decided_by_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_agent_decisions_actor CHECK (
      (decided_by_type = 'user'  AND decided_by_user_id IS NOT NULL AND decided_by_role IS NULL) OR
      (decided_by_type = 'agent' AND decided_by_role IS NOT NULL AND decided_by_user_id IS NULL) OR
      (decided_by_type = 'system' AND decided_by_user_id IS NULL AND decided_by_role IS NULL)),
  CONSTRAINT ck_agent_decisions_risk_by_user CHECK (decision_type <> 'risk_acceptance' OR decided_by_type = 'user')
);
CREATE INDEX ix_agent_decisions_run ON agent_decisions (workspace_id, run_id, created_at);
```
`ck_agent_decisions_risk_by_user` реализует «no self-authorized critical actions» (ТЗ §10): принять риск может только человек. `evidence_refs` — массив, т.к. PostgreSQL не поддерживает FK на элементы массива; нормализованная альтернатива — `evidence_links(target_type='agent_decision')`, которая и является источником истины для трассировки; массив — денормализованная копия для отображения.

### 9.4 `chat_threads` (T, collaboration)
Потоки Live Agent Chat (ТЗ §11: `All Agents`, `Codex`, `Claude`, `Role`, `Task`, `Approvals`).

```sql
CREATE TABLE chat_threads (
  id               uuid7          NOT NULL,
  workspace_id     uuid           NOT NULL,
  run_id           uuid           NOT NULL,
  scope            text           NOT NULL CHECK (scope IN ('all_agents','provider','role','task','approvals')),
  target_role      agent_role_t,
  target_provider  cli_provider_t,
  target_task_id   uuid,
  created_by       uuid,          -- NULL: создан системой при старте run
  created_at       timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_chat_threads PRIMARY KEY (id),
  CONSTRAINT uq_chat_threads_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_chat_threads_ws_run_id UNIQUE (workspace_id, run_id, id),
  CONSTRAINT uq_chat_threads_scope UNIQUE NULLS NOT DISTINCT (run_id, scope, target_role, target_provider, target_task_id),
  CONSTRAINT fk_chat_threads_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_chat_threads_task FOREIGN KEY (workspace_id, run_id, target_task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_chat_threads_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT ck_chat_threads_target CHECK (
      (scope IN ('all_agents','approvals') AND num_nonnulls(target_role, target_provider, target_task_id) = 0) OR
      (scope = 'provider' AND target_provider IS NOT NULL AND num_nonnulls(target_role, target_task_id) = 0) OR
      (scope = 'role'     AND target_role IS NOT NULL     AND num_nonnulls(target_provider, target_task_id) = 0) OR
      (scope = 'task'     AND target_task_id IS NOT NULL  AND num_nonnulls(target_role, target_provider) = 0))
);
CREATE INDEX ix_chat_threads_run ON chat_threads (workspace_id, run_id);
```

### 9.5 `chat_messages` (T, collaboration)
Сообщения в потоках: пользовательские, проекции ACP/событий, безопасные выдержки CLI.

```sql
CREATE TABLE chat_messages (
  id               uuid7          NOT NULL,
  workspace_id     uuid           NOT NULL,
  run_id           uuid           NOT NULL,     -- денормализовано из thread для выборки по run
  thread_id        uuid           NOT NULL,
  sender_type      text           NOT NULL CHECK (sender_type IN ('user','agent','system')),
  sender_id        uuid,          -- users.id при sender_type='user'
  sender_role      agent_role_t,  -- при sender_type='agent'
  sender_provider  cli_provider_t,
  body             text           NOT NULL CHECK (octet_length(body) <= 32768),   -- уже редактированный текст
  message_kind     text           NOT NULL CHECK (message_kind IN
                     ('text','directive','acp_message','activity','cli_excerpt','approval','system_notice')),
  source_event_id  uuid,          -- event_id из run_event_log / id agent_activity_events (без FK: партиционирование/retention)
  acp_message_id   uuid,
  mentions         agent_role_t[] NOT NULL DEFAULT '{}',   -- упоминания ролей (ТЗ §11)
  search_vector    tsvector       GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED,
  created_at       timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_chat_messages PRIMARY KEY (id),
  CONSTRAINT uq_chat_messages_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_chat_messages_thread FOREIGN KEY (workspace_id, run_id, thread_id) REFERENCES chat_threads(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_chat_messages_sender FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_chat_messages_acp FOREIGN KEY (workspace_id, acp_message_id) REFERENCES agent_messages(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_chat_messages_sender CHECK (
      (sender_type = 'user'   AND sender_id IS NOT NULL AND sender_role IS NULL) OR
      (sender_type = 'agent'  AND sender_id IS NULL AND sender_role IS NOT NULL) OR
      (sender_type = 'system' AND sender_id IS NULL AND sender_role IS NULL)),
  CONSTRAINT ck_chat_messages_acp CHECK (message_kind <> 'acp_message' OR acp_message_id IS NOT NULL)
);
CREATE INDEX ix_chat_messages_thread_time ON chat_messages (workspace_id, thread_id, created_at);
CREATE INDEX ix_chat_messages_run_time ON chat_messages (workspace_id, run_id, created_at);
CREATE INDEX ix_chat_messages_search ON chat_messages USING gin (workspace_id, search_vector);   -- btree_gin
```
Конфигурация FTS `simple` — нейтральна к языку (UI по умолчанию русский, сообщения агентов часто английские); выбор словаря — §22.

### 9.6 `user_directives` (T, orchestration)
Поручения пользователя (ТЗ §8.2, §11; Directive FSM). `target` из API раскладывается в типизированные колонки.

```sql
CREATE TABLE user_directives (
  id                       uuid7                   NOT NULL,
  workspace_id             uuid                    NOT NULL,
  run_id                   uuid                    NOT NULL,
  chat_message_id          uuid                    NOT NULL,
  target_type              text                    NOT NULL CHECK (target_type IN ('run','role','task','provider')),
  target_role              agent_role_t,
  target_task_id           uuid,
  target_provider          cli_provider_t,
  directive_type           directive_type_t        NOT NULL,
  status                   directive_status_t      NOT NULL DEFAULT 'RECEIVED',
  impact_status            directive_impact_type_t,  -- агрегат: максимальный impact по всем directive_impacts; NULL до IMPACT_READY
  requires_impact_preview  boolean                 NOT NULL DEFAULT true,
  expected_run_version     bigint,                   -- из запроса (ТЗ §8.2); сверяется с runs.version
  approval_id              uuid,                     -- CRITICAL_DIRECTIVE, если затронуты критические операции
  checkpoint_id            uuid,
  resulting_plan_version   int,
  submitted_by             uuid                    NOT NULL,
  failure_message          text                    CHECK (length(failure_message) <= 4000),
  version                  bigint                  NOT NULL DEFAULT 0,
  created_at               timestamptz             NOT NULL DEFAULT now(),
  updated_at               timestamptz             NOT NULL DEFAULT now(),
  completed_at             timestamptz,
  CONSTRAINT pk_user_directives PRIMARY KEY (id),
  CONSTRAINT uq_user_directives_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_user_directives_ws_run_id UNIQUE (workspace_id, run_id, id),
  CONSTRAINT uq_user_directives_chat_message UNIQUE (chat_message_id),
  CONSTRAINT fk_user_directives_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_chat_message FOREIGN KEY (workspace_id, chat_message_id) REFERENCES chat_messages(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_target_task FOREIGN KEY (workspace_id, run_id, target_task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_approval FOREIGN KEY (workspace_id, approval_id) REFERENCES approvals(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_checkpoint FOREIGN KEY (workspace_id, checkpoint_id) REFERENCES checkpoints(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_submitted_by FOREIGN KEY (submitted_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_user_directives_target CHECK (
      (target_type = 'run'      AND num_nonnulls(target_role, target_task_id, target_provider) = 0) OR
      (target_type = 'role'     AND target_role IS NOT NULL     AND num_nonnulls(target_task_id, target_provider) = 0) OR
      (target_type = 'task'     AND target_task_id IS NOT NULL  AND num_nonnulls(target_role, target_provider) = 0) OR
      (target_type = 'provider' AND target_provider IS NOT NULL AND num_nonnulls(target_role, target_task_id) = 0)),
  CONSTRAINT ck_user_directives_impact CHECK (
      status IN ('RECEIVED','IMPACT_ANALYSIS_PENDING','REJECTED','CANCELLED','FAILED') OR impact_status IS NOT NULL),
  CONSTRAINT ck_user_directives_awaiting CHECK (status <> 'AWAITING_CONFIRMATION' OR approval_id IS NOT NULL OR requires_impact_preview),
  CONSTRAINT ck_user_directives_completed CHECK ((status IN ('COMPLETED','REJECTED','CANCELLED','FAILED')) = (completed_at IS NOT NULL))
);
CREATE INDEX ix_user_directives_run_active ON user_directives (workspace_id, run_id, created_at)
  WHERE status NOT IN ('COMPLETED','REJECTED','CANCELLED','FAILED');
CREATE INDEX ix_user_directives_run ON user_directives (workspace_id, run_id, created_at DESC);
-- trg_user_directives_transition (§18.3).
```
Колонка `impact_status` сохранена по ТЗ §7.2, но её значения — `directive_impact_type` (обобщённый результат анализа), а ход обработки отражает `status` (`directive_status`). См. §22 OQ-DB-04.

### 9.7 `directive_impacts` (T, orchestration)
Влияние поручения на конкретные задачи (impact preview, ТЗ §8.2). Задача и поручение — из одного run (составные FK по `run_id`).

```sql
CREATE TABLE directive_impacts (
  workspace_id  uuid                    NOT NULL,
  run_id        uuid                    NOT NULL,
  directive_id  uuid                    NOT NULL,
  task_id       uuid                    NOT NULL,
  impact_type   directive_impact_type_t NOT NULL,
  resolution    text                    NOT NULL DEFAULT 'PENDING' CHECK (resolution IN ('PENDING','APPLIED','SKIPPED','SUPERSEDED')),
  details       jsonb                   NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
  created_at    timestamptz             NOT NULL DEFAULT now(),
  resolved_at   timestamptz,
  CONSTRAINT pk_directive_impacts PRIMARY KEY (directive_id, task_id),
  CONSTRAINT fk_directive_impacts_directive FOREIGN KEY (workspace_id, run_id, directive_id)
      REFERENCES user_directives(workspace_id, run_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_directive_impacts_task FOREIGN KEY (workspace_id, run_id, task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_directive_impacts_resolved CHECK ((resolution = 'PENDING') = (resolved_at IS NULL))
);
CREATE INDEX ix_directive_impacts_task ON directive_impacts (task_id);
```
Строки `TASK_ADD` вставляются на шаге `REPLANNING` и ссылаются на вновь созданные задачи. Влияние уровня run (без задачи) выражается только агрегатом `user_directives.impact_status`.

### 9.8 `directive_acknowledgements` (T, orchestration)
Подтверждения получения поручения ролями. Различает «доставлено» и «подтверждено» (ТЗ §11).

```sql
CREATE TABLE directive_acknowledgements (
  id                 uuid7        NOT NULL,
  workspace_id       uuid         NOT NULL,
  directive_id       uuid         NOT NULL,
  recipient_role     agent_role_t NOT NULL,
  recipient_task_id  uuid,
  acp_message_id     uuid,        -- ACP-сообщение, которым поручение доставлено
  status             text         NOT NULL DEFAULT 'PENDING'
                                  CHECK (status IN ('PENDING','DELIVERED','ACKNOWLEDGED','REJECTED','EXPIRED')),
  delivered_at       timestamptz,
  acknowledged_at    timestamptz,
  created_at         timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_directive_acknowledgements PRIMARY KEY (id),
  CONSTRAINT uq_directive_ack_recipient UNIQUE NULLS NOT DISTINCT (directive_id, recipient_role, recipient_task_id),
  CONSTRAINT fk_directive_ack_directive FOREIGN KEY (workspace_id, directive_id) REFERENCES user_directives(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_directive_ack_task FOREIGN KEY (workspace_id, recipient_task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_directive_ack_message FOREIGN KEY (workspace_id, acp_message_id) REFERENCES agent_messages(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_directive_ack_delivered CHECK (status = 'PENDING' OR delivered_at IS NOT NULL OR status = 'EXPIRED'),
  CONSTRAINT ck_directive_ack_acknowledged CHECK ((status = 'ACKNOWLEDGED') = (acknowledged_at IS NOT NULL))
);
CREATE INDEX ix_directive_ack_pending ON directive_acknowledgements (workspace_id, directive_id) WHERE status IN ('PENDING','DELIVERED');
```
Жизненный цикл строки (реализация `aicd_orchestration.directives`): `PENDING` (dispatch) → `DELIVERED` (`delivered_at`; старт попытки задачи-получателя, получатель уровня run при этом привязывается к задаче своей роли) → `ACKNOWLEDGED` (`acknowledged_at`; принятый итог агента) | `REJECTED` (агент `cannot_comply`); `PENDING`/`DELIVERED` → `EXPIRED` при завершении задачи без доставки/подтверждения, отмене/завершении run или `FAILED` поручения. Открытая строка задачи, вытесненной заменой, переносится на замену (снова `PENDING`). `acp_message_id` остаётся `NULL`: доставка идёт через инструкции задачи и раздел `directives` Context Pack, отдельного ACP-сообщения нет.

### 9.9 `agent_activity_events` (T, collaboration) — партиционирована помесячно
Лента активности (CLI normalized events, переходы, отклонённые ACP, recovery) для `GET /runs/{id}/activity` и timeline (ТЗ §7.2, §11). Самая объёмная таблица (вывод CLI) → `PARTITION BY RANGE (occurred_at)`.

```sql
CREATE TABLE agent_activity_events (
  id                uuid7                  NOT NULL,
  workspace_id      uuid                   NOT NULL,
  run_id            uuid                   NOT NULL,
  task_id           uuid,
  execution_id      uuid,
  event_type        text                   NOT NULL CHECK (event_type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),  -- каталог: EVENT_SCHEMAS.md
  cli_event         normalized_cli_event_t,     -- для event_type='cli.*': нормализованный тип (ТЗ §9)
  schema_version    text                   NOT NULL DEFAULT '1.0',
  payload           jsonb                  NOT NULL DEFAULT '{}'::jsonb,
  correlation_id    uuid,
  occurred_at       timestamptz            NOT NULL,
  recorded_at       timestamptz            NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_activity_events PRIMARY KEY (id, occurred_at),
  CONSTRAINT fk_activity_run       FOREIGN KEY (workspace_id, run_id)       REFERENCES runs(workspace_id, id)           ON DELETE RESTRICT,
  CONSTRAINT fk_activity_task      FOREIGN KEY (workspace_id, task_id)      REFERENCES tasks(workspace_id, id)          ON DELETE RESTRICT,
  CONSTRAINT fk_activity_execution FOREIGN KEY (workspace_id, execution_id) REFERENCES cli_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_activity_payload CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 32768),
  CONSTRAINT ck_activity_cli_event CHECK ((event_type LIKE 'cli.%') = (cli_event IS NOT NULL))
) PARTITION BY RANGE (occurred_at);
CREATE INDEX ix_activity_run_time ON agent_activity_events (workspace_id, run_id, occurred_at);
CREATE INDEX ix_activity_task_time ON agent_activity_events (workspace_id, task_id, occurred_at) WHERE task_id IS NOT NULL;
CREATE INDEX ix_activity_execution ON agent_activity_events (execution_id, occurred_at) WHERE execution_id IS NOT NULL;
-- Партиции: agent_activity_events_yYYYYmMM, создаются заранее на 3 месяца вперёд (§20.3).
```
**Реализация (миграция 0006):** DDL без изменений; `trg_agent_activity_events_append_only` (`trg_fn_append_only`) — строки неизменяемы для всех ролей, retention только DETACH + DROP партиции (`maint_drop_partition` с архивом). Права: `aicd_worker` — `SELECT/INSERT`, `aicd_app` — `SELECT`. Отложенные FK 0003 (`fk_agent_assignments_profile`, `fk_agent_decisions_message`, `fk_chat_messages_acp`, `fk_directive_ack_message`) добавлены этой же миграцией вместе с индексами ссылающихся колонок `acp_message_id`.
Выдержки вывода CLI попадают в `payload` только после редактирования секретов и усечения (ТЗ §8.3, §16); полный raw log — защищённый artifact.

---

## 10. Code Intelligence

**Правило для колонок `*_ref` из ТЗ.** Ссылка на целый объект в object storage реализуется как `uuid` FK на `artifacts` (имя колонки из ТЗ сохранено для трассируемости): `knowledge_entries.body_ref`, `architecture_decisions.content_ref`, `documentation_snapshots.storage_ref`, `checkpoints.state_ref`. Ссылка на **фрагмент** (миллионы строк, отдельный artifact на каждую неразумен) — текстовый локатор с CHECK формата: `git:<blob_sha>#L<from>-L<to>` (текст кода берётся из git, не копируется) или `artifact:<uuid>#bytes=<from>-<to>`: `knowledge_chunks.text_ref`, `documentation_sections.body_ref`. Логические пути (`local_root_ref`, `path_ref`, `worktree_ref`) — относительные имена в allowlist-корнях, без `/` в начале и `..`.

### 10.1 `code_index_snapshots` (T, code_intelligence)
Commit-aware снимок индекса (ТЗ §12, ADR-0005).

```sql
CREATE TABLE code_index_snapshots (
  id                  uuid7       NOT NULL,
  workspace_id        uuid        NOT NULL,
  repository_id       uuid        NOT NULL,
  commit_sha          git_sha     NOT NULL,
  index_version       int         NOT NULL CHECK (index_version >= 1),   -- версия формата/алгоритма индексатора
  parent_snapshot_id  uuid,       -- база инкрементального построения
  status              text        NOT NULL DEFAULT 'BUILDING' CHECK (status IN ('BUILDING','READY','FAILED','SUPERSEDED')),
  file_count          int         CHECK (file_count >= 0),
  symbol_count        int         CHECK (symbol_count >= 0),
  error               text        CHECK (length(error) <= 4000),
  created_at          timestamptz NOT NULL DEFAULT now(),
  completed_at        timestamptz,
  CONSTRAINT pk_code_index_snapshots PRIMARY KEY (id),
  CONSTRAINT uq_code_index_snapshots_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_code_index_snapshots_commit UNIQUE (repository_id, commit_sha, index_version),     -- ТЗ
  CONSTRAINT fk_cis_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_cis_parent FOREIGN KEY (workspace_id, parent_snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE SET NULL (parent_snapshot_id),
  CONSTRAINT ck_cis_completed CHECK ((status IN ('READY','FAILED','SUPERSEDED')) = (completed_at IS NOT NULL))
);
CREATE INDEX ix_cis_repo_ready ON code_index_snapshots (workspace_id, repository_id, created_at DESC) WHERE status = 'READY';
```

### 10.2 `code_files` (T, code_intelligence) — **дополнение к ТЗ**
ТЗ §12 требует file tree, incremental hashing и test links, но в каталоге нет таблицы файлов. Без неё инкрементальное переиндексирование (сравнение хэшей файлов между снимками) и дерево файлов Repository Explorer пришлось бы выводить из `code_symbols` (файлы без символов теряются).

```sql
CREATE TABLE code_files (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  snapshot_id   uuid        NOT NULL,
  path          text        NOT NULL CHECK (length(path) BETWEEN 1 AND 4096 AND path !~ '(^/|(^|/)\.\.(/|$))'),
  language      text        CHECK (language ~ '^[a-z0-9_+#-]{1,32}$'),
  blob_sha      git_sha     NOT NULL,          -- git blob id: ключ инкрементальности
  content_hash  sha256_hex  NOT NULL,
  size_bytes    bigint      NOT NULL CHECK (size_bytes >= 0),
  is_test       boolean     NOT NULL DEFAULT false,
  is_generated  boolean     NOT NULL DEFAULT false,
  is_binary     boolean     NOT NULL DEFAULT false,
  CONSTRAINT pk_code_files PRIMARY KEY (id),
  CONSTRAINT uq_code_files_ws_snapshot_id UNIQUE (workspace_id, snapshot_id, id),
  CONSTRAINT uq_code_files_path UNIQUE (snapshot_id, path),
  CONSTRAINT fk_code_files_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_code_files_path_trgm ON code_files USING gin (path gin_trgm_ops);
CREATE INDEX ix_code_files_blob ON code_files (workspace_id, blob_sha);
```

### 10.3 `code_symbols` (T, code_intelligence)
Символы (tree-sitter / LSP).

```sql
CREATE TABLE code_symbols (
  id              uuid7       NOT NULL,
  workspace_id    uuid        NOT NULL,
  snapshot_id     uuid        NOT NULL,
  file_id         uuid        NOT NULL,
  file_path       text        NOT NULL,          -- денормализовано из code_files (ТЗ §7.2)
  symbol_name     text        NOT NULL CHECK (length(symbol_name) BETWEEN 1 AND 1024),
  qualified_name  text        CHECK (length(qualified_name) <= 4096),
  symbol_kind     text        NOT NULL CHECK (symbol_kind IN
                    ('module','class','interface','function','method','variable','constant','type','enum','test','route','other')),
  start_line      int         NOT NULL CHECK (start_line >= 1),
  end_line        int         NOT NULL,
  signature       text        CHECK (length(signature) <= 2000),
  content_hash    sha256_hex  NOT NULL,
  CONSTRAINT pk_code_symbols PRIMARY KEY (id),
  CONSTRAINT uq_code_symbols_ws_snapshot_id UNIQUE (workspace_id, snapshot_id, id),
  CONSTRAINT fk_code_symbols_file FOREIGN KEY (workspace_id, snapshot_id, file_id)
      REFERENCES code_files(workspace_id, snapshot_id, id) ON DELETE CASCADE,       -- символ и файл из одного снимка
  CONSTRAINT ck_code_symbols_lines CHECK (end_line >= start_line)
);
CREATE INDEX ix_code_symbols_snapshot_name ON code_symbols (snapshot_id, symbol_name);
CREATE INDEX ix_code_symbols_name_trgm ON code_symbols USING gin (symbol_name gin_trgm_ops);
CREATE INDEX ix_code_symbols_file ON code_symbols (file_id, start_line);
```

### 10.4 `code_dependencies` (T, code_intelligence)
Граф зависимостей и test links (`edge_type = 'tests'`). Оба конца — из одного снимка (составные FK по `snapshot_id`).

```sql
CREATE TABLE code_dependencies (
  id                uuid7       NOT NULL,
  workspace_id      uuid        NOT NULL,
  snapshot_id       uuid        NOT NULL,
  source_symbol_id  uuid        NOT NULL,
  target_symbol_id  uuid,
  target_ref        text        CHECK (length(target_ref) <= 1024),   -- внешний модуль/пакет, неразрешённый символ
  edge_type         text        NOT NULL CHECK (edge_type IN ('imports','calls','inherits','implements','references','tests')),
  CONSTRAINT pk_code_dependencies PRIMARY KEY (id),
  CONSTRAINT fk_code_dependencies_source FOREIGN KEY (workspace_id, snapshot_id, source_symbol_id)
      REFERENCES code_symbols(workspace_id, snapshot_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_code_dependencies_target FOREIGN KEY (workspace_id, snapshot_id, target_symbol_id)
      REFERENCES code_symbols(workspace_id, snapshot_id, id) ON DELETE CASCADE,
  CONSTRAINT ck_code_dependencies_target CHECK (num_nonnulls(target_symbol_id, target_ref) >= 1),
  CONSTRAINT ck_code_dependencies_no_self CHECK (target_symbol_id IS NULL OR target_symbol_id <> source_symbol_id)
);
CREATE INDEX ix_code_dependencies_source ON code_dependencies (snapshot_id, source_symbol_id, edge_type);
CREATE INDEX ix_code_dependencies_target ON code_dependencies (snapshot_id, target_symbol_id, edge_type) WHERE target_symbol_id IS NOT NULL;
```

### 10.5 `knowledge_chunks` (T, code_intelligence)
Фрагменты для FTS/trigram-поиска (без embeddings в v1).

```sql
CREATE TABLE knowledge_chunks (
  id                         uuid7       NOT NULL,
  workspace_id               uuid        NOT NULL,
  project_id                 uuid,        -- NULL для документации уровня workspace
  source_type                text        NOT NULL CHECK (source_type IN
                               ('code_file','code_symbol','documentation_section','knowledge_entry','architecture_decision')),
  source_id                  uuid        NOT NULL,     -- полиморфная ссылка; целостность — §18.11
  snapshot_id                uuid,        -- code_index_snapshots (для source_type code_*)
  documentation_snapshot_id  uuid,        -- documentation_snapshots (для documentation_section)
  chunk_index                int         NOT NULL CHECK (chunk_index >= 0),
  text_ref                   text        NOT NULL CHECK (text_ref ~ '^(git:[0-9a-f]{40}([0-9a-f]{24})?#L[0-9]+-L[0-9]+|artifact:[0-9a-f-]{36}#bytes=[0-9]+-[0-9]+)$'),
  content_hash               sha256_hex  NOT NULL,
  token_estimate             int         CHECK (token_estimate >= 0),
  search_vector              tsvector    NOT NULL,
  metadata                   jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
  created_at                 timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_knowledge_chunks PRIMARY KEY (id),
  CONSTRAINT uq_knowledge_chunks_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_knowledge_chunks_source UNIQUE NULLS NOT DISTINCT (source_type, source_id, snapshot_id, documentation_snapshot_id, chunk_index),
  CONSTRAINT fk_knowledge_chunks_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_knowledge_chunks_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_knowledge_chunks_doc_snapshot FOREIGN KEY (workspace_id, documentation_snapshot_id)
      REFERENCES documentation_snapshots(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT ck_knowledge_chunks_freshness_anchor CHECK (
      (source_type IN ('code_file','code_symbol') AND snapshot_id IS NOT NULL AND documentation_snapshot_id IS NULL) OR
      (source_type = 'documentation_section' AND documentation_snapshot_id IS NOT NULL AND snapshot_id IS NULL) OR
      (source_type IN ('knowledge_entry','architecture_decision') AND snapshot_id IS NULL AND documentation_snapshot_id IS NULL))
);
CREATE INDEX ix_knowledge_chunks_fts ON knowledge_chunks USING gin (workspace_id, search_vector);   -- btree_gin
CREATE INDEX ix_knowledge_chunks_snapshot ON knowledge_chunks (snapshot_id) WHERE snapshot_id IS NOT NULL;
CREATE INDEX ix_knowledge_chunks_source ON knowledge_chunks (source_type, source_id);
```
`ck_knowledge_chunks_freshness_anchor` гарантирует, что каждый фрагмент кода привязан к commit-снимку — основа правила «устаревший фрагмент блокируется или явно маркируется» (ТЗ §12).

### 10.6 `retrieval_queries` (T, code_intelligence)

```sql
CREATE TABLE retrieval_queries (
  id                  uuid7                NOT NULL,
  workspace_id        uuid                 NOT NULL,
  run_id              uuid                 NOT NULL,
  task_id             uuid                 NOT NULL,
  task_attempt_id     uuid,
  query_text          text                 NOT NULL CHECK (length(query_text) BETWEEN 1 AND 4000),
  strategy            text                 NOT NULL CHECK (strategy IN ('exact','symbol','graph','fts','trigram','docs','hybrid')),
  snapshot_id         uuid,
  documentation_mode  documentation_mode_t NOT NULL,
  created_at          timestamptz          NOT NULL DEFAULT now(),
  CONSTRAINT pk_retrieval_queries PRIMARY KEY (id),
  CONSTRAINT uq_retrieval_queries_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_retrieval_queries_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_retrieval_queries_attempt FOREIGN KEY (workspace_id, task_attempt_id) REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_retrieval_queries_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE SET NULL (snapshot_id),
  CONSTRAINT ck_retrieval_queries_docs_off CHECK (NOT (strategy = 'docs' AND documentation_mode = 'OFF'))   -- AC-10
);
CREATE INDEX ix_retrieval_queries_task ON retrieval_queries (workspace_id, task_id, created_at);
```

### 10.7 `retrieval_results` (T, code_intelligence)

```sql
CREATE TABLE retrieval_results (
  workspace_id  uuid    NOT NULL,
  query_id      uuid    NOT NULL,
  chunk_id      uuid    NOT NULL,
  rank          int     NOT NULL CHECK (rank >= 1),
  score         real    NOT NULL,
  selected      boolean NOT NULL DEFAULT false,
  is_stale      boolean NOT NULL DEFAULT false,   -- фрагмент не из снимка текущего commit: явная маркировка (ТЗ §12)
  CONSTRAINT pk_retrieval_results PRIMARY KEY (query_id, chunk_id),             -- = UNIQUE(query_id,chunk_id) ТЗ
  CONSTRAINT uq_retrieval_results_rank UNIQUE (query_id, rank),
  CONSTRAINT fk_retrieval_results_query FOREIGN KEY (workspace_id, query_id) REFERENCES retrieval_queries(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_retrieval_results_chunk FOREIGN KEY (workspace_id, chunk_id) REFERENCES knowledge_chunks(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_retrieval_results_chunk ON retrieval_results (chunk_id);
```

### 10.8 `context_packs` (T, code_intelligence)
Компактный контекст для попытки (ТЗ §12 «экономия контекста»). Кэш — по `content_hash + source_commit + policy_version`.

```sql
CREATE TABLE context_packs (
  id                  uuid7                NOT NULL,
  workspace_id        uuid                 NOT NULL,
  task_attempt_id     uuid                 NOT NULL,
  snapshot_id         uuid                 NOT NULL,
  source_commit       git_sha              NOT NULL,
  policy_version      int                  NOT NULL CHECK (policy_version >= 1),
  documentation_mode  documentation_mode_t NOT NULL,
  token_budget        int                  NOT NULL CHECK (token_budget > 0),
  size_bytes          bigint               NOT NULL CHECK (size_bytes >= 0),
  manifest            jsonb                NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),  -- состав: chunk ids, hashes, stale-флаги
  content_hash        sha256_hex           NOT NULL,
  artifact_id         uuid                 NOT NULL,
  created_at          timestamptz          NOT NULL DEFAULT now(),
  CONSTRAINT pk_context_packs PRIMARY KEY (id),
  CONSTRAINT uq_context_packs_attempt UNIQUE (task_attempt_id),
  CONSTRAINT fk_context_packs_attempt FOREIGN KEY (workspace_id, task_attempt_id) REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_context_packs_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_context_packs_artifact FOREIGN KEY (workspace_id, artifact_id) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_context_packs_cache ON context_packs (workspace_id, source_commit, policy_version, content_hash);
```
`snapshot_id ... ON DELETE RESTRICT` не даёт удалить снимок, на который ссылается context pack (воспроизводимость); retention снимков учитывает это (§20).

### 10.9 `indexing_jobs` (T, code_intelligence)

```sql
CREATE TABLE indexing_jobs (
  id                uuid7       NOT NULL,
  workspace_id      uuid        NOT NULL,
  repository_id     uuid        NOT NULL,
  commit_sha        git_sha     NOT NULL,
  kind              text        NOT NULL DEFAULT 'incremental' CHECK (kind IN ('full','incremental')),
  status            text        NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  base_snapshot_id  uuid,
  snapshot_id       uuid,
  attempt_count     int         NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  created_at        timestamptz NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  error             text        CHECK (length(error) <= 4000),
  CONSTRAINT pk_indexing_jobs PRIMARY KEY (id),
  CONSTRAINT fk_indexing_jobs_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_indexing_jobs_base FOREIGN KEY (workspace_id, base_snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE SET NULL (base_snapshot_id),
  CONSTRAINT fk_indexing_jobs_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE SET NULL (snapshot_id),
  CONSTRAINT ck_indexing_jobs_finished CHECK ((status IN ('SUCCEEDED','FAILED','CANCELLED')) = (finished_at IS NOT NULL)),
  CONSTRAINT ck_indexing_jobs_kind CHECK (kind = 'full' OR base_snapshot_id IS NOT NULL OR status IN ('QUEUED','FAILED','CANCELLED'))
);
CREATE UNIQUE INDEX uq_indexing_jobs_active ON indexing_jobs (repository_id, commit_sha) WHERE status IN ('QUEUED','RUNNING');
CREATE INDEX ix_indexing_jobs_queue ON indexing_jobs (created_at) WHERE status = 'QUEUED';
```

---

## 11. Документация и база знаний

### 11.1 `knowledge_entries` (T, code_intelligence)

```sql
CREATE TABLE knowledge_entries (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  project_id    uuid        NOT NULL,
  kind          text        NOT NULL CHECK (kind IN ('note','requirement','specification','runbook','glossary','lesson')),
  title         text        NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  body_ref      uuid        NOT NULL,        -- artifacts.id
  content_hash  sha256_hex  NOT NULL,
  visibility    text        NOT NULL DEFAULT 'project' CHECK (visibility IN ('project','workspace')),
  created_by    uuid,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_knowledge_entries PRIMARY KEY (id),
  CONSTRAINT uq_knowledge_entries_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_knowledge_entries_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_knowledge_entries_body FOREIGN KEY (workspace_id, body_ref) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_knowledge_entries_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX ix_knowledge_entries_project ON knowledge_entries (workspace_id, project_id, kind);
```

### 11.2 `architecture_decisions` (T, code_intelligence)
ADR проекта пользователя (не ADR платформы).

```sql
CREATE TABLE architecture_decisions (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  project_id       uuid        NOT NULL,
  adr_number       int         NOT NULL CHECK (adr_number >= 1),
  title            text        NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  status           text        NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED','ACCEPTED','REJECTED','DEPRECATED','SUPERSEDED')),
  content_ref      uuid        NOT NULL,     -- artifacts.id
  run_id           uuid,                     -- run, в котором предложено (роль architect)
  superseded_by_id uuid,
  approved_by      uuid,
  approved_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_architecture_decisions PRIMARY KEY (id),
  CONSTRAINT uq_architecture_decisions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_architecture_decisions_number UNIQUE (project_id, adr_number),
  CONSTRAINT fk_adr_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_adr_content FOREIGN KEY (workspace_id, content_ref) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_adr_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE SET NULL (run_id),
  CONSTRAINT fk_adr_superseded_by FOREIGN KEY (workspace_id, superseded_by_id) REFERENCES architecture_decisions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_adr_approved_by FOREIGN KEY (approved_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_adr_approved CHECK ((status = 'ACCEPTED') <= (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
  CONSTRAINT ck_adr_superseded CHECK ((status = 'SUPERSEDED') = (superseded_by_id IS NOT NULL))
);
```
`ck_adr_approved` использует булево `<=` (импликация): `ACCEPTED` ⇒ указан человек-утверждающий. Агент не может перевести ADR в `ACCEPTED` без человека (ТЗ §4 L5: ADR + approval).

### 11.3 `documentation_sources` (T, code_intelligence)
Источники внешней документации (ТЗ §12; опционально). В v1 — уровня workspace (см. §22 OQ-DB-05 о глобальном кэше).

```sql
CREATE TABLE documentation_sources (
  id                     uuid7       NOT NULL,
  workspace_id           uuid        NOT NULL,
  technology_version_id  uuid,
  source_type            text        NOT NULL CHECK (source_type IN ('url','pdf','markdown','html','git')),
  uri_or_file_ref        text        NOT NULL CHECK (length(uri_or_file_ref) BETWEEN 1 AND 2048),
  trust_level            text        NOT NULL DEFAULT 'untrusted'
                                     CHECK (trust_level IN ('official','vendor','community','internal','untrusted')),
  license_info           text        CHECK (length(license_info) <= 2000),
  refresh_policy         text        NOT NULL DEFAULT 'manual' CHECK (refresh_policy IN ('manual','daily','weekly','monthly')),
  enabled                boolean     NOT NULL DEFAULT true,
  approval_id            uuid,       -- DOCS_DOWNLOAD, если требуется политикой SMART
  created_by             uuid        NOT NULL,
  last_refreshed_at      timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_documentation_sources PRIMARY KEY (id),
  CONSTRAINT uq_documentation_sources_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_documentation_sources_uri UNIQUE (workspace_id, uri_or_file_ref),
  CONSTRAINT fk_doc_sources_tv FOREIGN KEY (technology_version_id) REFERENCES technology_versions(id) ON DELETE RESTRICT,
  CONSTRAINT fk_doc_sources_approval FOREIGN KEY (workspace_id, approval_id) REFERENCES approvals(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_doc_sources_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_doc_sources_uri CHECK (
      (source_type IN ('url','html') AND uri_or_file_ref ~ '^https://') OR
      (source_type = 'git' AND uri_or_file_ref ~ '^https://') OR
      (source_type IN ('pdf','markdown') AND (uri_or_file_ref ~ '^https://' OR uri_or_file_ref ~ '^artifact:[0-9a-f-]{36}$')))
);
```
CHECK разрешает только `https://` (без `http`, `file://`, `gopher://` и т.п.) — первый рубеж SSRF-защиты; основная (private IP denylist, DNS rebinding, размер/MIME) — в `security.ssrf.fetch()` (ТЗ §15).

### 11.4 `documentation_snapshots` (T, code_intelligence)

```sql
CREATE TABLE documentation_snapshots (
  id                  uuid7       NOT NULL,
  workspace_id        uuid        NOT NULL,
  source_id           uuid        NOT NULL,
  content_hash        sha256_hex,              -- NULL пока FETCHING
  status              text        NOT NULL DEFAULT 'FETCHING' CHECK (status IN ('FETCHING','READY','FAILED','QUARANTINED','SUPERSEDED')),
  storage_ref         uuid,                    -- artifacts.id
  size_bytes          bigint      CHECK (size_bytes >= 0),
  mime_type           text        CHECK (mime_type ~ '^[a-z]+/[a-z0-9.+-]+$'),
  external_action_id  uuid,                    -- ledger загрузки (ADR-0012)
  error               text        CHECK (length(error) <= 4000),
  fetched_at          timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_documentation_snapshots PRIMARY KEY (id),
  CONSTRAINT uq_documentation_snapshots_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_documentation_snapshots_hash UNIQUE (source_id, content_hash),
  CONSTRAINT fk_doc_snapshots_source FOREIGN KEY (workspace_id, source_id) REFERENCES documentation_sources(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_doc_snapshots_storage FOREIGN KEY (workspace_id, storage_ref) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_doc_snapshots_action FOREIGN KEY (workspace_id, external_action_id) REFERENCES external_actions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_doc_snapshots_ready CHECK (status <> 'READY' OR (storage_ref IS NOT NULL AND content_hash IS NOT NULL AND fetched_at IS NOT NULL))
);
CREATE INDEX ix_doc_snapshots_source_ready ON documentation_snapshots (source_id, fetched_at DESC) WHERE status = 'READY';
```
`QUARANTINED` — содержимое не прошло проверки (zip bomb, опасный тип, подозрение на prompt injection) и не индексируется.

### 11.5 `documentation_sections` (T, code_intelligence)

```sql
CREATE TABLE documentation_sections (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  snapshot_id   uuid        NOT NULL,
  section_path  text        NOT NULL CHECK (length(section_path) BETWEEN 1 AND 1024),
  heading       text        CHECK (length(heading) <= 1000),
  body_ref      text        NOT NULL CHECK (body_ref ~ '^artifact:[0-9a-f-]{36}#bytes=[0-9]+-[0-9]+$'),
  content_hash  sha256_hex  NOT NULL,
  CONSTRAINT pk_documentation_sections PRIMARY KEY (id),
  CONSTRAINT uq_documentation_sections_path UNIQUE (snapshot_id, section_path),
  CONSTRAINT fk_doc_sections_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES documentation_snapshots(workspace_id, id) ON DELETE CASCADE
);
```

---

## 12. Качество и evidence

### 12.1 `artifacts` (T, platform)
Метаданные объектов в object storage (diff, raw logs, отчёты, context packs, checkpoints). Содержимое неизменяемо (content-addressed по `sha256`).

```sql
CREATE TABLE artifacts (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  project_id       uuid,       -- ТЗ: project_id; NULL только для артефактов уровня workspace (документация)
  run_id           uuid,
  kind             text        NOT NULL CHECK (kind IN
                     ('git_diff','cli_raw_log','cli_output','test_report','coverage_report','scan_report','sbom',
                      'context_pack','checkpoint_state','documentation','knowledge','report','archive','other',
                      'gate_log','applicability_report','signature')),   -- последние три: QUALITY_GATES §9, миграция 0008
  storage_ref      text        NOT NULL CHECK (storage_ref ~ '^(s3|file)://[A-Za-z0-9._/-]+$' AND length(storage_ref) <= 1100),
  sha256           sha256_hex  NOT NULL,
  size_bytes       bigint      NOT NULL CHECK (size_bytes >= 0),
  mime_type        text        NOT NULL CHECK (mime_type ~ '^[a-z]+/[a-z0-9.+-]+$'),
  is_sensitive     boolean     NOT NULL DEFAULT false,   -- raw logs: доступ только admin+, не в чат/отчёты (ТЗ §9)
  redaction_applied boolean    NOT NULL DEFAULT false,
  retention_class  text        NOT NULL DEFAULT 'standard' CHECK (retention_class IN ('ephemeral','standard','evidence','archive')),
  expires_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_artifacts PRIMARY KEY (id),
  CONSTRAINT uq_artifacts_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_artifacts_storage_ref UNIQUE (storage_ref),
  CONSTRAINT fk_artifacts_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_artifacts_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_artifacts_raw_log_sensitive CHECK (kind <> 'cli_raw_log' OR is_sensitive),
  CONSTRAINT ck_artifacts_evidence_no_expiry CHECK (retention_class <> 'evidence' OR expires_at IS NULL)
);
CREATE INDEX ix_artifacts_run ON artifacts (workspace_id, run_id, kind) WHERE run_id IS NOT NULL;
CREATE INDEX ix_artifacts_expiry ON artifacts (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX ix_artifacts_sha256 ON artifacts (workspace_id, sha256);
-- trg_artifacts_immutable: BEFORE UPDATE — менять можно только retention_class/expires_at.
```
`storage_ref` строится как `<scheme>://<bucket>/<workspace_id>/<yyyy>/<mm>/<artifact_id>` — префикс workspace изолирует объекты на уровне хранилища (presigned URL выдаётся только после RLS-проверки строки).

### 12.2 `quality_profiles` (M, verification) — **дополнение к ТЗ**
ТЗ §7.2 ссылается на `quality_gates.profile_id`, но таблицы профилей нет. Профили версионируются; `workspace_id IS NULL` — глобальные встроенные профили `basic/strict/critical`.

```sql
CREATE TABLE quality_profiles (
  id            uuid7             NOT NULL,
  workspace_id  uuid,
  code          quality_profile_t NOT NULL,
  version       int               NOT NULL CHECK (version >= 1),   -- версия содержимого; строка неизменяема
  definition    jsonb             NOT NULL CHECK (jsonb_typeof(definition) = 'object'),  -- пороги: coverage, severity block и т.п.
  active        boolean           NOT NULL DEFAULT true,
  created_by    uuid,
  created_at    timestamptz       NOT NULL DEFAULT now(),
  CONSTRAINT pk_quality_profiles PRIMARY KEY (id),
  CONSTRAINT uq_quality_profiles_version UNIQUE NULLS NOT DISTINCT (workspace_id, code, version),
  CONSTRAINT fk_quality_profiles_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_quality_profiles_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX uq_quality_profiles_active ON quality_profiles (workspace_id, code) NULLS NOT DISTINCT WHERE active;
```

### 12.3 `quality_gates` (M, verification)
Определения gate внутри версии профиля (ТЗ §14).

```sql
CREATE TABLE quality_gates (
  id               uuid7       NOT NULL,
  workspace_id     uuid,       -- копия quality_profiles.workspace_id (для RLS без JOIN)
  profile_id       uuid        NOT NULL,
  name             text        NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),   -- напр. 'ruff', 'pytest-unit', 'semgrep'
  gate_type        text        NOT NULL CHECK (gate_type IN
                     ('FORMAT','LINT','TYPECHECK','BUILD','UNIT','INTEGRATION','E2E','SAST','SCA','SECRETS',
                      'IAC','POLICY','MIGRATION','PERF','REVIEW','COVERAGE','SBOM','SIGN','SCOPE')),
  required         boolean     NOT NULL DEFAULT true,
  config           jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config) = 'object'),  -- tool, args-профиль, пороги
  applicability    jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(applicability) = 'object'), -- языки/пути/технологии
  timeout_seconds  int         NOT NULL DEFAULT 900 CHECK (timeout_seconds BETWEEN 10 AND 86400),
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_quality_gates PRIMARY KEY (id),
  CONSTRAINT uq_quality_gates_profile_name UNIQUE (profile_id, name),
  CONSTRAINT fk_quality_gates_profile FOREIGN KEY (profile_id) REFERENCES quality_profiles(id) ON DELETE RESTRICT
);
CREATE INDEX ix_quality_gates_profile ON quality_gates (profile_id, gate_type);
-- trg_quality_gates_ws_match: BEFORE INSERT/UPDATE — NEW.workspace_id IS NOT DISTINCT FROM quality_profiles.workspace_id.
```

### 12.4 `gate_executions` (T, verification)
Запуск gate для run/задачи в Verification Engine. `required` — snapshot из определения на момент планирования (изменение профиля не меняет вердикт уже идущего run).

```sql
CREATE TABLE gate_executions (
  id                     uuid7                   NOT NULL,
  workspace_id           uuid                    NOT NULL,
  run_id                 uuid                    NOT NULL,
  task_id                uuid,
  gate_id                uuid                    NOT NULL,
  gate_type              text                    NOT NULL,    -- денормализовано из quality_gates
  required               boolean                 NOT NULL,
  status                 gate_execution_status_t NOT NULL DEFAULT 'PENDING',
  attempt_no             int                     NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
  commit_sha             git_sha                 NOT NULL,
  evidence_id            uuid,
  not_applicable_reason  text                    CHECK (length(not_applicable_reason) <= 2000),
  error_message          text                    CHECK (length(error_message) <= 4000),
  error_code             text                    CHECK (error_code IN       -- QUALITY_GATES §9, миграция 0008
                           ('TOOL_UNAVAILABLE','TIMEOUT','INFRA','SHA_MISMATCH','REPORT_UNPARSABLE','EMPTY_SUITE',
                            'WORKTREE_DIRTY','NO_BASE_COMMIT','UNSUPPORTED_GATE')),
  superseded_at          timestamptz,            -- заменён повторным запуском (после fix)
  created_at             timestamptz             NOT NULL DEFAULT now(),
  started_at             timestamptz,
  finished_at            timestamptz,
  updated_at             timestamptz             NOT NULL DEFAULT now(),
  CONSTRAINT pk_gate_executions PRIMARY KEY (id),
  CONSTRAINT uq_gate_executions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_gate_executions_id_gate UNIQUE (id, gate_id),                  -- цель FK findings(gate_execution_id, source_gate_id)
  CONSTRAINT fk_gate_executions_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_gate_executions_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_gate_executions_gate FOREIGN KEY (gate_id) REFERENCES quality_gates(id) ON DELETE RESTRICT,
  CONSTRAINT fk_gate_executions_evidence FOREIGN KEY (workspace_id, evidence_id) REFERENCES evidence(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_gate_executions_pass_evidence CHECK (status NOT IN ('PASS','FAIL') OR evidence_id IS NOT NULL),   -- ТЗ §14
  CONSTRAINT ck_gate_executions_na_reason CHECK (status NOT IN ('NOT_APPLICABLE','SKIPPED') OR not_applicable_reason IS NOT NULL),
  CONSTRAINT ck_gate_executions_finished CHECK ((status IN ('PASS','FAIL','ERROR','SKIPPED','NOT_APPLICABLE')) = (finished_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_gate_executions_current ON gate_executions (run_id, task_id, gate_id) NULLS NOT DISTINCT
  WHERE superseded_at IS NULL;
CREATE INDEX ix_gate_executions_run ON gate_executions (workspace_id, run_id, status);
CREATE INDEX ix_gate_executions_active ON gate_executions (created_at) WHERE status IN ('PENDING','RUNNING');
-- trg_gate_executions_lock_run_share (§18.4); trg_gate_executions_evidence_verified: PASS требует evidence.verification_status='VERIFIED'.
```

### 12.5 `test_results` (T, verification)

```sql
CREATE TABLE test_results (
  id                  uuid7        NOT NULL,
  workspace_id        uuid         NOT NULL,
  gate_execution_id   uuid         NOT NULL,
  suite               text         NOT NULL CHECK (length(suite) BETWEEN 1 AND 500),
  passed              int          NOT NULL CHECK (passed >= 0),
  failed              int          NOT NULL CHECK (failed >= 0),
  skipped             int          NOT NULL CHECK (skipped >= 0),
  errors              int          NOT NULL DEFAULT 0 CHECK (errors >= 0),
  duration_ms         bigint       CHECK (duration_ms >= 0),
  coverage_percent    numeric(5,2) CHECK (coverage_percent BETWEEN 0 AND 100),
  report_artifact_id  uuid         NOT NULL,
  created_at          timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_test_results PRIMARY KEY (id),
  CONSTRAINT uq_test_results_suite UNIQUE (gate_execution_id, suite),
  CONSTRAINT fk_test_results_gate_execution FOREIGN KEY (workspace_id, gate_execution_id) REFERENCES gate_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_test_results_report FOREIGN KEY (workspace_id, report_artifact_id) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT
);
-- trg_test_results_gate_consistency: если failed+errors > 0, то gate_execution не может быть PASS (AC-11).
```

### 12.6 `findings` (T, verification)
Находки gate, ревью-агентов, ручные и из GitHub. По брифу добавлен `gate_execution_id`; семантика `source_gate_id` уточнена ниже.

```sql
CREATE TABLE findings (
  id                   uuid7              NOT NULL,
  workspace_id         uuid               NOT NULL,
  run_id               uuid               NOT NULL,
  task_id              uuid,
  source               text               NOT NULL CHECK (source IN ('gate','agent_review','manual','github')),
  source_gate_id       uuid,              -- определение gate (quality_gates.id)
  gate_execution_id    uuid,              -- конкретный запуск gate
  severity             finding_severity_t NOT NULL,
  rule_id              text               NOT NULL CHECK (length(rule_id) BETWEEN 1 AND 255),
  cwe_id               text               CHECK (cwe_id ~ '^CWE-[0-9]{1,5}$'),
  title                text               NOT NULL CHECK (length(title) BETWEEN 1 AND 1000),
  file_path            text               CHECK (file_path !~ '(^/|(^|/)\.\.(/|$))'),
  line                 int                CHECK (line >= 1),
  status               finding_status_t   NOT NULL DEFAULT 'OPEN',
  fingerprint          sha256_hex         NOT NULL,     -- дедупликация: rule_id + путь + нормализованный фрагмент
  evidence_id          uuid,
  fixed_in_commit_sha  git_sha,
  resolved_by          uuid,
  resolution_reason    text               CHECK (length(resolution_reason) <= 4000),
  created_at           timestamptz        NOT NULL DEFAULT now(),
  updated_at           timestamptz        NOT NULL DEFAULT now(),
  CONSTRAINT pk_findings PRIMARY KEY (id),
  CONSTRAINT uq_findings_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_findings_run_fingerprint UNIQUE (run_id, fingerprint),
  CONSTRAINT fk_findings_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_gate FOREIGN KEY (source_gate_id) REFERENCES quality_gates(id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_gate_execution FOREIGN KEY (workspace_id, gate_execution_id) REFERENCES gate_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_gate_execution_gate FOREIGN KEY (gate_execution_id, source_gate_id)
      REFERENCES gate_executions(id, gate_id) ON DELETE RESTRICT,     -- согласованность: запуск принадлежит именно этому gate
  CONSTRAINT fk_findings_evidence FOREIGN KEY (workspace_id, evidence_id) REFERENCES evidence(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_findings_resolved_by FOREIGN KEY (resolved_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_findings_gate_source CHECK ((source = 'gate') = (gate_execution_id IS NOT NULL AND source_gate_id IS NOT NULL)),
  CONSTRAINT ck_findings_line CHECK (line IS NULL OR file_path IS NOT NULL),
  CONSTRAINT ck_findings_verified_fixed CHECK (status <> 'VERIFIED_FIXED' OR (evidence_id IS NOT NULL AND fixed_in_commit_sha IS NOT NULL)),
  CONSTRAINT ck_findings_human_resolution CHECK (
      status NOT IN ('FALSE_POSITIVE','ACCEPTED_RISK') OR (resolved_by IS NOT NULL AND resolution_reason IS NOT NULL))
);
CREATE INDEX ix_findings_run_open ON findings (workspace_id, run_id, severity) WHERE status IN ('OPEN','ACKNOWLEDGED','FIXED');
CREATE INDEX ix_findings_task ON findings (workspace_id, task_id) WHERE task_id IS NOT NULL;
CREATE INDEX ix_findings_gate_execution ON findings (gate_execution_id) WHERE gate_execution_id IS NOT NULL;
```
**Миграция 0010 (OQ-43 (c), (d); QUALITY_GATES §9):** `control_refs text[] NOT NULL DEFAULT '{}'` (коды `standard_controls.control_code`, ≤ 50, `ck_findings_control_refs`; GIN-индекс `ix_findings_control_refs`) — связь находки с контролями, Compliance Engine считает такую находку относящейся к контролю; `version bigint NOT NULL DEFAULT 1` — сильный ETag для `POST /findings/{id}/transitions` (If-Match), увеличивается триггером `trg_findings_bump_version` при каждом `UPDATE` (находки меняют и worker, и api).

Решение по `source_gate_id`: это **определение** gate (`quality_gates.id`), а конкретный запуск — `gate_execution_id`; составной FK `(gate_execution_id, source_gate_id) → gate_executions(id, gate_id)` гарантирует их согласованность. `FALSE_POSITIVE` / `ACCEPTED_RISK` — только человек (`resolved_by` — users), агент не может закрыть находку сам (ТЗ §10 «no self-authorized critical actions»).

### 12.7 `evidence` (T, verification)
Доказательство: сохранённый результат процесса + метаданные воспроизводимости (ТЗ §14, AC-14). По брифу добавлены `commit_sha`, `command`, `exit_code`, `environment`, `started_at`, `finished_at`.

```sql
CREATE TABLE evidence (
  id                   uuid7                          NOT NULL,
  workspace_id         uuid                           NOT NULL,
  run_id               uuid                           NOT NULL,
  task_id              uuid,
  kind                 text                           NOT NULL CHECK (kind IN
                         ('command_result','test_report','scan_report','diff','review_verdict','manual_attestation','benchmark','agent_claim')),
  artifact_id          uuid                           NOT NULL,
  producer             text                           NOT NULL CHECK (producer IN
                         ('verification_engine','cli_gateway','agent','user','github','system')),
  producer_user_id     uuid,
  verification_status  evidence_verification_status_t NOT NULL DEFAULT 'PENDING',
  commit_sha           git_sha,
  command              text                           CHECK (length(command) <= 8192),   -- редактированная командная строка
  exit_code            int,
  environment          jsonb                          NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(environment) = 'object'), -- образ, версии инструментов
  started_at           timestamptz,
  finished_at          timestamptz,
  verified_at          timestamptz,
  created_at           timestamptz                    NOT NULL DEFAULT now(),
  CONSTRAINT pk_evidence PRIMARY KEY (id),
  CONSTRAINT uq_evidence_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_evidence_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_evidence_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_evidence_artifact FOREIGN KEY (workspace_id, artifact_id) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_evidence_producer_user FOREIGN KEY (producer_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  -- AC-14: для процессных доказательств обязательны commit, команда, exit code, время
  CONSTRAINT ck_evidence_process_fields CHECK (
      kind NOT IN ('command_result','test_report','scan_report','benchmark')
      OR (commit_sha IS NOT NULL AND command IS NOT NULL AND exit_code IS NOT NULL
          AND started_at IS NOT NULL AND finished_at IS NOT NULL)),
  CONSTRAINT ck_evidence_time_order CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
  -- Evidence-first: слова агента и сырой вывод CLI не могут быть VERIFIED
  CONSTRAINT ck_evidence_verified_producer CHECK (
      verification_status <> 'VERIFIED' OR producer IN ('verification_engine','user','github')),
  CONSTRAINT ck_evidence_agent_claim CHECK (kind <> 'agent_claim' OR (producer = 'agent' AND verification_status <> 'VERIFIED')),
  CONSTRAINT ck_evidence_user CHECK ((producer = 'user') = (producer_user_id IS NOT NULL)),
  CONSTRAINT ck_evidence_verified_at CHECK ((verification_status IN ('VERIFIED','REJECTED')) = (verified_at IS NOT NULL))
);
CREATE INDEX ix_evidence_run ON evidence (workspace_id, run_id, created_at);
CREATE INDEX ix_evidence_task ON evidence (workspace_id, task_id) WHERE task_id IS NOT NULL;
-- trg_evidence_immutable: BEFORE UPDATE — изменяемы только verification_status/verified_at и только из PENDING/UNVERIFIED.
```
Контрольная сумма артефакта (AC-14) — `artifacts.sha256` по `artifact_id`; Verification Engine перепроверяет её при чтении.

### 12.8 `evidence_links` (T, verification)
Трассировка evidence ↔ цели (ТЗ §13: «… → check → evidence → verdict»). Полиморфная связь; целостность — §18.11.

```sql
CREATE TABLE evidence_links (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  evidence_id   uuid        NOT NULL,
  target_type   text        NOT NULL CHECK (target_type IN
                  ('task','gate_execution','finding','compliance_result','test_result','agent_message','approval',
                   'pull_request','architecture_decision','agent_decision')),
  target_id     uuid        NOT NULL,
  relation      text        NOT NULL CHECK (relation IN ('supports','refutes','produced_by','verifies','references')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_evidence_links PRIMARY KEY (id),
  CONSTRAINT uq_evidence_links UNIQUE (evidence_id, target_type, target_id, relation),
  CONSTRAINT fk_evidence_links_evidence FOREIGN KEY (workspace_id, evidence_id) REFERENCES evidence(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_evidence_links_target ON evidence_links (workspace_id, target_type, target_id);
```

---

## 13. Стандарты и compliance

### 13.1 `standards` (G, standards)
Реестр стандартов (ТЗ §13). Только метаданные и ссылки; полные тексты не хранятся (лицензии).

```sql
CREATE TABLE standards (
  id             uuid7       NOT NULL,
  code           text        NOT NULL CHECK (code ~ '^[A-Za-z0-9/ ._:-]{2,100}$'),   -- 'ISO/IEC 25010', 'OWASP ASVS', 'NIST SP 800-218'
  edition        text        NOT NULL CHECK (length(edition) BETWEEN 1 AND 50),       -- '2023', '4.0.3', '1.1'
  title          text        NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  reference_uri  text        CHECK (reference_uri ~ '^https://'),
  license_note   text        CHECK (length(license_note) <= 2000),
  active         boolean     NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_standards PRIMARY KEY (id),
  CONSTRAINT uq_standards_code_edition UNIQUE (code, edition)
);
```

### 13.2 `standard_controls` (G, standards)
Контроли — **собственные формулировки** платформы со ссылкой на пункт (ТЗ §13: не выдавать за официальный текст).

```sql
CREATE TABLE standard_controls (
  id                   uuid7        NOT NULL,
  standard_id          uuid         NOT NULL,
  clause_ref           text         NOT NULL CHECK (length(clause_ref) BETWEEN 1 AND 100),
  control_code         text         NOT NULL CHECK (control_code ~ '^[A-Za-z0-9._-]{1,64}$'),
  description          text         NOT NULL CHECK (length(description) BETWEEN 1 AND 4000),
  verification_method  text         NOT NULL CHECK (verification_method IN ('automated_gate','agent_review','manual','document_check')),
  default_agent_role   agent_role_t,
  default_gate_type    text         CHECK (default_gate_type IN
                         ('FORMAT','LINT','TYPECHECK','BUILD','UNIT','INTEGRATION','E2E','SAST','SCA','SECRETS',
                          'IAC','POLICY','MIGRATION','PERF','REVIEW','COVERAGE','SBOM','SIGN','SCOPE')),
  active               boolean      NOT NULL DEFAULT true,
  created_at           timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_standard_controls PRIMARY KEY (id),
  CONSTRAINT uq_standard_controls_code UNIQUE (standard_id, control_code),
  CONSTRAINT fk_standard_controls_standard FOREIGN KEY (standard_id) REFERENCES standards(id) ON DELETE RESTRICT,
  CONSTRAINT ck_standard_controls_automated CHECK (verification_method = 'manual' OR default_gate_type IS NOT NULL)
);
```

### 13.3 `project_controls` (T, standards)
Применимость контроля к проекту, утверждённая человеком (ТЗ §13 «применимость утверждается для проекта»).

```sql
CREATE TABLE project_controls (
  workspace_id   uuid         NOT NULL,
  project_id     uuid         NOT NULL,
  control_id     uuid         NOT NULL,
  applicability  text         NOT NULL CHECK (applicability IN ('applicable','not_applicable','tailored')),
  rationale      text         CHECK (length(rationale) <= 4000),
  agent_role     agent_role_t,             -- ответственная роль (трассировка ТЗ §13)
  approved_by    uuid,
  approved_at    timestamptz,
  created_at     timestamptz  NOT NULL DEFAULT now(),
  updated_at     timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_project_controls PRIMARY KEY (project_id, control_id),
  CONSTRAINT fk_project_controls_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_project_controls_control FOREIGN KEY (control_id) REFERENCES standard_controls(id) ON DELETE RESTRICT,
  CONSTRAINT fk_project_controls_approved_by FOREIGN KEY (approved_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_project_controls_rationale CHECK (applicability = 'applicable' OR rationale IS NOT NULL),
  CONSTRAINT ck_project_controls_approval CHECK ((approved_by IS NULL) = (approved_at IS NULL))
);
CREATE INDEX ix_project_controls_ws_project ON project_controls (workspace_id, project_id) WHERE approved_by IS NOT NULL;
```
Неутверждённая строка (`approved_by IS NULL`) — черновик; Compliance Engine учитывает только утверждённые.

**Миграция 0010 (QUALITY_GATES §5.3 п.6, §9):** `blocking boolean` — контроль блокирует завершение run при актуальном `compliance_results.status = 'FAIL'`; `NULL` — умолчание платформы `control_default_blocking(quality_profile, agent_role)` (контроли роли `security` блокируют в профилях `strict`/`critical`). Явное значение утверждает человек: триггер `trg_project_controls_approval` охраняет и `blocking`. Guard завершения (`aicd_verification.verdict.run_completion_verdict`) читает `compliance_results`/`project_controls` напрямую (модуль standards — соседний слой).

### 13.4 `compliance_results` (T, standards)

```sql
CREATE TABLE compliance_results (
  id                 uuid7               NOT NULL,
  workspace_id       uuid                NOT NULL,
  run_id             uuid                NOT NULL,
  control_id         uuid                NOT NULL,
  status             compliance_status_t NOT NULL,
  evidence_id        uuid,
  gate_execution_id  uuid,
  reviewed_by        uuid,
  notes              text                CHECK (length(notes) <= 4000),
  checked_at         timestamptz         NOT NULL DEFAULT now(),
  superseded_at      timestamptz,
  CONSTRAINT pk_compliance_results PRIMARY KEY (id),
  CONSTRAINT uq_compliance_results_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_compliance_results_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_compliance_results_control FOREIGN KEY (control_id) REFERENCES standard_controls(id) ON DELETE RESTRICT,
  CONSTRAINT fk_compliance_results_evidence FOREIGN KEY (workspace_id, evidence_id) REFERENCES evidence(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_compliance_results_gate_execution FOREIGN KEY (workspace_id, gate_execution_id) REFERENCES gate_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_compliance_results_reviewed_by FOREIGN KEY (reviewed_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_compliance_results_pass_evidence CHECK (status NOT IN ('PASS','FAIL') OR evidence_id IS NOT NULL),
  CONSTRAINT ck_compliance_results_na CHECK (status <> 'NOT_APPLICABLE' OR notes IS NOT NULL)
);
CREATE UNIQUE INDEX uq_compliance_results_current ON compliance_results (run_id, control_id) WHERE superseded_at IS NULL;
CREATE INDEX ix_compliance_results_run ON compliance_results (workspace_id, run_id, status);
```
«Автоматические проверки не равны официальной сертификации» (ТЗ §13) отражается в отчёте; в БД `PASS` всегда опирается на evidence.

---

## 14. Политики и approvals

### 14.1 `policies` (M, orchestration)
Версионируемые политики (routing, approval, sandbox, network, documentation, review, ACP ACL, provider settings, retention). `workspace_id IS NULL` — глобальные значения по умолчанию; `project_id` — переопределение уровня проекта. Строки неизменяемы, кроме флага `active`: изменение = новая строка с `version + 1`.

```sql
CREATE TABLE policies (
  id            uuid7       NOT NULL,
  workspace_id  uuid,
  project_id    uuid,
  policy_type   text        NOT NULL CHECK (policy_type IN
                  ('routing','approval','sandbox','network','documentation','review','acp_acl','provider_settings','retention')),
  name          text        NOT NULL DEFAULT 'default' CHECK (name ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
  version       int         NOT NULL CHECK (version >= 1),
  definition    jsonb       NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
  active        boolean     NOT NULL DEFAULT true,
  created_by    uuid,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_policies PRIMARY KEY (id),
  CONSTRAINT uq_policies_version UNIQUE NULLS NOT DISTINCT (workspace_id, project_id, policy_type, name, version),
  CONSTRAINT fk_policies_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_policies_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_policies_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT ck_policies_scope CHECK (project_id IS NULL OR workspace_id IS NOT NULL)
);
CREATE UNIQUE INDEX uq_policies_active ON policies (workspace_id, project_id, policy_type, name) NULLS NOT DISTINCT WHERE active;
-- trg_policies_immutable: BEFORE UPDATE — разрешено изменять только active.
```
Optimistic locking для `policies` и `quality_profiles` реализуется уникальностью `(…, version)`: две конкурентные правки создают одинаковую следующую версию → нарушение UNIQUE → HTTP 409. Поэтому `trg_fn_enforce_version` к ним не применяется (уточнение ADR-0008 п.5).

### 14.2 `approvals` (T, orchestration)
Явные решения человека по опасным действиям (ТЗ §15, AC-13). Approval привязан к **конкретному предмету** через `subject_hash` (например, хэш плана версии N, SHA коммита для merge, хэш SQL миграции), чтобы одобрение нельзя было «перенести» на изменённое действие.

```sql
CREATE TABLE approvals (
  id                   uuid7                 NOT NULL,
  workspace_id         uuid                  NOT NULL,
  project_id           uuid                  NOT NULL,
  run_id               uuid,                 -- ТЗ: run_id; NULL для DOCS_DOWNLOAD вне run
  task_id              uuid,
  action_type          approval_action_type_t NOT NULL,
  status               approval_status_t     NOT NULL DEFAULT 'PENDING',
  risk_level           risk_level_t          NOT NULL,
  required_role        workspace_role_t      NOT NULL,     -- минимальная роль утверждающего по матрице брифа
  requested_by_type    text                  NOT NULL CHECK (requested_by_type IN ('user','agent','system')),
  requested_by         uuid,                 -- users.id при requested_by_type='user'
  requested_by_role    agent_role_t,         -- при requested_by_type='agent'
  subject_hash         sha256_hex            NOT NULL,
  subject              jsonb                 NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(subject) = 'object'),
  allow_self_approval  boolean               NOT NULL DEFAULT false,  -- только single-member workspace + явная политика (аудируется)
  decided_by           uuid,
  decision_reason      text                  CHECK (length(decision_reason) <= 4000),
  requested_at         timestamptz           NOT NULL DEFAULT now(),
  decided_at           timestamptz,
  expires_at           timestamptz,
  version              bigint                NOT NULL DEFAULT 0,
  updated_at           timestamptz           NOT NULL DEFAULT now(),
  CONSTRAINT pk_approvals PRIMARY KEY (id),
  CONSTRAINT uq_approvals_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_approvals_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_approvals_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_approvals_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_approvals_requested_by FOREIGN KEY (requested_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_approvals_decided_by FOREIGN KEY (decided_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_approvals_requester CHECK (
      (requested_by_type = 'user'   AND requested_by IS NOT NULL AND requested_by_role IS NULL) OR
      (requested_by_type = 'agent'  AND requested_by IS NULL AND requested_by_role IS NOT NULL) OR
      (requested_by_type = 'system' AND requested_by IS NULL AND requested_by_role IS NULL)),
  CONSTRAINT ck_approvals_decision CHECK (
      (status IN ('APPROVED','REJECTED')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
  CONSTRAINT ck_approvals_reject_reason CHECK (status <> 'REJECTED' OR decision_reason IS NOT NULL),
  CONSTRAINT ck_approvals_four_eyes CHECK (
      status <> 'APPROVED' OR requested_by IS NULL OR decided_by <> requested_by OR allow_self_approval),
  CONSTRAINT ck_approvals_run_bound CHECK (
      action_type NOT IN ('PLAN_APPROVAL','MERGE','CRITICAL_DIRECTIVE','MANUAL_REVIEW','SELF_REVIEW_FALLBACK','LOCAL_EXECUTION_FALLBACK')
      OR run_id IS NOT NULL),
  CONSTRAINT ck_approvals_expiry CHECK (expires_at IS NULL OR expires_at > requested_at)
);
CREATE INDEX ix_approvals_pending ON approvals (workspace_id, requested_at) WHERE status = 'PENDING';
CREATE INDEX ix_approvals_pending_expiry ON approvals (expires_at) WHERE status = 'PENDING' AND expires_at IS NOT NULL;
CREATE INDEX ix_approvals_run ON approvals (workspace_id, run_id, action_type) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX uq_approvals_pending_subject ON approvals (workspace_id, action_type, subject_hash) WHERE status = 'PENDING';
-- trg_approvals_transition (§18.3); trg_approvals_lock_run_share (§18.4);
-- trg_approvals_decider_role (§18.7): decided_by — член workspace с ролью ≥ required_role (с учётом project_member_overrides).
```

---

## 15. Надёжность

### 15.1 `checkpoints` (T, orchestration)
Безопасная точка восстановления run/задачи (ТЗ §6, §8.2: «safe checkpoint» перед replan; recovery).

```sql
CREATE TABLE checkpoints (
  id              uuid7       NOT NULL,
  workspace_id    uuid        NOT NULL,
  run_id          uuid        NOT NULL,
  task_id         uuid,
  state_version   bigint      NOT NULL CHECK (state_version >= 0),   -- runs.version (или tasks.version) на момент снимка
  plan_version    int         NOT NULL CHECK (plan_version >= 0),
  reason          text        NOT NULL CHECK (reason IN ('plan_approved','directive','pause','pre_external_action','periodic','recovery')),
  state_ref       uuid        NOT NULL,        -- artifacts.id (kind='checkpoint_state'): сериализованное состояние FSM/DAG
  worktree_id     uuid,
  git_commit_sha  git_sha,                     -- WIP-коммит в изолированной ветке worktree, если был
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_checkpoints PRIMARY KEY (id),
  CONSTRAINT uq_checkpoints_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_checkpoints_state UNIQUE NULLS NOT DISTINCT (run_id, task_id, state_version),
  CONSTRAINT fk_checkpoints_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_checkpoints_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_checkpoints_state FOREIGN KEY (workspace_id, state_ref) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_checkpoints_worktree FOREIGN KEY (workspace_id, worktree_id) REFERENCES worktrees(workspace_id, id) ON DELETE SET NULL (worktree_id)
);
CREATE INDEX ix_checkpoints_run_latest ON checkpoints (workspace_id, run_id, created_at DESC);
```

### 15.2 `outbox_events` (S, platform) — партиционирована помесячно
Transactional outbox (ТЗ §10, §16, ADR-0002): событие вставляется в той же транзакции, что и переход состояния.

```sql
CREATE TABLE outbox_events (
  id                uuid7       NOT NULL,
  workspace_id      uuid,       -- NULL для системных событий (provider.health_changed)
  run_id            uuid,       -- для маршрутизации в run_event_log
  aggregate_type    text        NOT NULL CHECK (aggregate_type ~ '^[a-z][a-z0-9_]{0,63}$'),
  aggregate_id      uuid        NOT NULL,
  event_type        text        NOT NULL CHECK (event_type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
  schema_version    text        NOT NULL DEFAULT '1.0' CHECK (schema_version ~ '^\d+\.\d+$'),
  payload           jsonb       NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 65536),
  correlation_id    uuid,
  causation_id      uuid,
  traceparent       text        CHECK (traceparent ~ '^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$'),  -- W3C
  created_at        timestamptz NOT NULL DEFAULT now(),
  available_at      timestamptz NOT NULL DEFAULT now(),   -- backoff при ошибке публикации
  published_at      timestamptz,
  publish_attempts  int         NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
  last_error        text        CHECK (length(last_error) <= 2000),
  CONSTRAINT pk_outbox_events PRIMARY KEY (id, created_at),
  CONSTRAINT ck_outbox_events_run_ws CHECK (run_id IS NULL OR workspace_id IS NOT NULL)
) PARTITION BY RANGE (created_at);
CREATE INDEX ix_outbox_events_unpublished ON outbox_events (available_at, id) WHERE published_at IS NULL;   -- партиальный
CREATE INDEX ix_outbox_events_aggregate ON outbox_events (aggregate_type, aggregate_id, created_at);
-- trg_outbox_events_guard: BEFORE UPDATE — изменяемы только published_at, publish_attempts, last_error, available_at.
```
Диспетчер (`worker-outbox`): `SELECT … WHERE published_at IS NULL AND available_at <= now() ORDER BY available_at, id LIMIT :n FOR UPDATE SKIP LOCKED` → для событий с `run_id` вставка в `run_event_log` (§15.6) → публикация в Redis/Dramatiq и pub/sub → `published_at = now()`; всё, кроме вызова Redis, — в одной транзакции. Сбой после публикации и до commit даёт повторную доставку (at-least-once) — потребители идемпотентны через `processed_events`. FK намеренно отсутствуют (высокий оборот, партиционирование, системная таблица).

### 15.3 `processed_events` (G, platform) — **дополнение к ТЗ**
Дедупликация идемпотентных потребителей outbox-событий (ТЗ §10, §16). Для ACP используется `message_deliveries`.

```sql
CREATE TABLE processed_events (
  consumer      text        NOT NULL CHECK (consumer ~ '^[a-z0-9_.:-]{1,128}$'),   -- имя Dramatiq-актора/обработчика
  event_id      uuid        NOT NULL,
  processed_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_processed_events PRIMARY KEY (consumer, event_id)
);
CREATE INDEX ix_processed_events_retention ON processed_events (processed_at);
```
Потребитель в одной транзакции: `INSERT … ON CONFLICT DO NOTHING` → 0 строк = уже обработано, выход; иначе — побочные эффекты в БД + commit. Таблица глобальная (без `workspace_id`): содержит только UUID событий без tenant-данных; доступ — только `aicd_worker`.

### 15.4 `worker_leases` (S, platform)
Аренды ресурсов с fencing token (ТЗ §6, §16, ADR-0007).

```sql
CREATE SEQUENCE worker_lease_fencing_seq AS bigint START 1 INCREMENT 1 NO CYCLE;

CREATE TABLE worker_leases (
  id              uuid7       NOT NULL,
  resource_type   text        NOT NULL CHECK (resource_type IN
                    ('task','run','cli_execution','indexing_job','external_action','outbox_partition','recovery')),
  resource_id     uuid        NOT NULL,
  workspace_id    uuid,       -- трассировка/контекст; NULL для системных ресурсов
  worker_id       text        NOT NULL CHECK (worker_id ~ '^[A-Za-z0-9._:-]{1,200}$'),   -- host:process:uuid
  worker_host_id  uuid,
  fencing_token   bigint      NOT NULL,
  acquired_at     timestamptz NOT NULL DEFAULT now(),
  heartbeat_at    timestamptz NOT NULL DEFAULT now(),
  lease_until     timestamptz NOT NULL,
  released_at     timestamptz,
  CONSTRAINT pk_worker_leases PRIMARY KEY (id),
  CONSTRAINT uq_worker_leases_resource UNIQUE (resource_type, resource_id),
  CONSTRAINT fk_worker_leases_host FOREIGN KEY (worker_host_id) REFERENCES worker_hosts(id) ON DELETE SET NULL,
  CONSTRAINT ck_worker_leases_until CHECK (lease_until > acquired_at),
  CONSTRAINT ck_worker_leases_fencing CHECK (fencing_token > 0)
);
CREATE INDEX ix_worker_leases_active_expiry ON worker_leases (lease_until) WHERE released_at IS NULL;   -- reaper
CREATE INDEX ix_worker_leases_worker ON worker_leases (worker_id) WHERE released_at IS NULL;
-- trg_worker_leases_fencing_monotonic (§18.6).
```
Захват/перезахват (атомарно, единственный писатель токена — sequence):

```sql
INSERT INTO worker_leases (id, resource_type, resource_id, workspace_id, worker_id, worker_host_id,
                           fencing_token, acquired_at, heartbeat_at, lease_until)
VALUES (:id, 'task', :task_id, :ws, :worker_id, :host_id,
        nextval('worker_lease_fencing_seq'), now(), now(), now() + make_interval(secs => :ttl))
ON CONFLICT (resource_type, resource_id) DO UPDATE
   SET worker_id = EXCLUDED.worker_id, worker_host_id = EXCLUDED.worker_host_id,
       fencing_token = EXCLUDED.fencing_token, acquired_at = now(), heartbeat_at = now(),
       lease_until = EXCLUDED.lease_until, released_at = NULL
 WHERE worker_leases.released_at IS NOT NULL OR worker_leases.lease_until < now()
RETURNING fencing_token;   -- 0 строк: ресурс арендован другим воркером
```
Heartbeat: `UPDATE worker_leases SET heartbeat_at = now(), lease_until = now() + :ttl WHERE resource_type = :t AND resource_id = :r AND fencing_token = :token AND released_at IS NULL` — 0 строк = аренда потеряна (`LEASE_LOST`), воркер обязан прекратить запись результатов. Запись результатов защищается токеном: `UPDATE tasks … WHERE id = :id AND version = :v AND lease_fencing_token = :token`; `cli_executions.fencing_token`, `task_attempts.fencing_token`, `external_actions.fencing_token` фиксируют, под каким токеном выполнялась работа (recovery различает «старого» и «нового» владельца).

### 15.5 `external_actions` (T, orchestration) — **дополнение к ТЗ**
Ledger побочных эффектов (ADR-0012; ТЗ §6 «повтор не должен повторять подтверждённые внешние действия», AC-09).

```sql
CREATE TABLE external_actions (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  run_id           uuid,
  task_id          uuid,
  action_type      text        NOT NULL CHECK (action_type IN
                     ('git_push','branch_delete','pr_create','pr_update','pr_merge','pr_comment','migration_apply','docs_download','production_deploy')),
  idempotency_key  sha256_hex  NOT NULL,     -- sha256(run_id, task_id, action_type, target, plan_version) — ADR-0012
  target           text        NOT NULL CHECK (length(target) BETWEEN 1 AND 1024),   -- 'owner/repo#branch', URL источника и т.п.
  is_destructive   boolean     NOT NULL DEFAULT false,
  status           text        NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED','EXECUTING','CONFIRMED','FAILED','COMPENSATED')),
  approval_id      uuid,
  request          jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(request) = 'object'),
  request_hash     sha256_hex  NOT NULL,     -- сверяется с approvals.subject_hash
  result           jsonb       CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
  external_ref     text        CHECK (length(external_ref) <= 1024),   -- PR URL/номер, SHA push, id snapshot
  fencing_token    bigint,
  attempt_count    int         NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  last_error       text        CHECK (length(last_error) <= 4000),
  planned_at       timestamptz NOT NULL DEFAULT now(),
  executing_at     timestamptz,
  confirmed_at     timestamptz,
  failed_at        timestamptz,
  compensated_at   timestamptz,
  version          bigint      NOT NULL DEFAULT 0,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_external_actions PRIMARY KEY (id),
  CONSTRAINT uq_external_actions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_external_actions_idempotency_key UNIQUE (idempotency_key),
  CONSTRAINT fk_external_actions_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_external_actions_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_external_actions_approval FOREIGN KEY (workspace_id, approval_id) REFERENCES approvals(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_external_actions_approval_required CHECK (
      (action_type NOT IN ('pr_merge','production_deploy') AND NOT (action_type = 'migration_apply' AND is_destructive))
      OR approval_id IS NOT NULL),
  CONSTRAINT ck_external_actions_executing CHECK (
      ((status IN ('EXECUTING','CONFIRMED','COMPENSATED')) <= (executing_at IS NOT NULL AND fencing_token IS NOT NULL))
      AND (status <> 'PLANNED' OR executing_at IS NULL)),     -- FAILED возможен и до исполнения (отказ в approval)
  CONSTRAINT ck_external_actions_confirmed CHECK ((status IN ('CONFIRMED','COMPENSATED')) <= (confirmed_at IS NOT NULL)),
  CONSTRAINT ck_external_actions_failed CHECK ((status = 'FAILED') = (failed_at IS NOT NULL))
);
CREATE INDEX ix_external_actions_reconcile ON external_actions (status, updated_at) WHERE status IN ('PLANNED','EXECUTING');
CREATE INDEX ix_external_actions_run ON external_actions (workspace_id, run_id) WHERE run_id IS NOT NULL;
-- trg_external_actions_transition (§18.3); trg_external_actions_approval_gate (§18.7):
--   переход в EXECUTING при approval_id IS NOT NULL требует approvals.status='APPROVED',
--   approvals.subject_hash = NEW.request_hash и (expires_at IS NULL OR expires_at > now()).
```

### 15.6 `run_event_log` (T, platform) — **дополнение к ТЗ**
Долговременный журнал SSE-событий run (ADR-0011; AC-08: переподключение по `Last-Event-ID` без дублей). Пишется только outbox-диспетчером.

```sql
CREATE TABLE run_event_log (
  workspace_id    uuid                       NOT NULL,
  run_id          uuid                       NOT NULL,
  seq             bigint                     NOT NULL CHECK (seq >= 1),     -- SSE id
  event_id        uuid                       NOT NULL,                      -- = outbox_events.id
  event_type      sse_event_type_persisted_t NOT NULL,
  schema_version  text                       NOT NULL DEFAULT '1.0' CHECK (schema_version ~ '^\d+\.\d+$'),
  task_id         uuid,
  payload         jsonb                      NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 32768),
  occurred_at     timestamptz                NOT NULL,                      -- время доменного события
  created_at      timestamptz                NOT NULL DEFAULT now(),
  CONSTRAINT pk_run_event_log PRIMARY KEY (run_id, seq),
  CONSTRAINT uq_run_event_log_event_id UNIQUE (event_id),
  CONSTRAINT fk_run_event_log_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_run_event_log_ws_run ON run_event_log (workspace_id, run_id, seq);
-- trg_run_event_log_append_only: BEFORE UPDATE OR DELETE → trg_fn_append_only() (retention — только aicd_maintenance, §20).
```
Выдача `seq` (ADR-0011): в транзакции диспетчера `UPDATE runs SET last_event_seq = last_event_seq + 1 WHERE id = :run_id RETURNING last_event_seq` (строчная блокировка run сериализует выдачу); `INSERT … ON CONFLICT (event_id) DO NOTHING` делает повторную обработку того же outbox-события безопасной. Обновление `last_event_seq` не считается изменением агрегата: триггер `trg_runs_version` объявлен как `BEFORE UPDATE OF <все колонки, кроме last_event_seq и updated_at>`, поэтому `version`/ETag run не меняется от потока событий. `heartbeat` не персистится (домен `sse_event_type_persisted_t`).

---

## 16. GitHub

### 16.1 `github_installations` (T, integrations_github) — **дополнение к ТЗ**
Установка GitHub App, привязанная к workspace. Приватный ключ App и webhook secret в БД не хранятся (секрет-хранилище окружения, ТЗ §7.1).

```sql
CREATE TABLE github_installations (
  id                    uuid7       NOT NULL,
  workspace_id          uuid        NOT NULL,
  installation_id       bigint      NOT NULL CHECK (installation_id > 0),
  account_login         text        NOT NULL CHECK (account_login ~ '^[A-Za-z0-9-]{1,39}$'),
  account_type          text        NOT NULL CHECK (account_type IN ('user','organization')),
  repository_selection  text        NOT NULL DEFAULT 'selected' CHECK (repository_selection IN ('all','selected')),
  permissions           jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(permissions) = 'object'),
  status                text        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED','REVOKED')),
  installed_by          uuid,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_github_installations PRIMARY KEY (id),
  CONSTRAINT uq_github_installations_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_github_installations_installation_id UNIQUE (installation_id),
  CONSTRAINT fk_github_installations_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_github_installations_installed_by FOREIGN KEY (installed_by) REFERENCES users(id) ON DELETE SET NULL
);
```
`UNIQUE (installation_id)` глобален намеренно: одна установка GitHub App не может обслуживать два workspace (иначе вебхук нельзя однозначно отнести к арендатору).

### 16.2 `github_webhook_deliveries` (G, integrations_github) — **дополнение к ТЗ**
Приём вебхуков: проверка подписи и дедупликация по `X-GitHub-Delivery` (ТЗ §15). Глобальная, т.к. workspace определяется только после проверки подписи и разрешения `installation_id` (`sys_resolve_installation_workspace`, §19.5).

```sql
CREATE TABLE github_webhook_deliveries (
  id                   uuid7       NOT NULL,
  delivery_guid        uuid        NOT NULL,       -- X-GitHub-Delivery
  event                text        NOT NULL CHECK (event ~ '^[a-z_]{1,64}$'),
  action               text        CHECK (action ~ '^[a-z_]{1,64}$'),
  installation_id      bigint,
  signature_valid      boolean     NOT NULL,
  payload_sha256       sha256_hex  NOT NULL,
  payload_storage_key  text        CHECK (payload_storage_key ~ '^(s3|file)://[A-Za-z0-9._/-]{1,1024}$'),  -- глобальный префикс webhooks/
  status               text        NOT NULL DEFAULT 'RECEIVED' CHECK (status IN ('RECEIVED','REJECTED','PROCESSED','IGNORED','FAILED')),
  error                text        CHECK (length(error) <= 2000),
  received_at          timestamptz NOT NULL DEFAULT now(),
  processed_at         timestamptz,
  CONSTRAINT pk_github_webhook_deliveries PRIMARY KEY (id),
  CONSTRAINT uq_github_webhook_deliveries_guid UNIQUE (delivery_guid),
  CONSTRAINT ck_gwd_signature CHECK (signature_valid OR (status = 'REJECTED' AND payload_storage_key IS NULL)),
  CONSTRAINT ck_gwd_processed CHECK ((status IN ('PROCESSED','IGNORED','FAILED')) = (processed_at IS NOT NULL))
);
CREATE INDEX ix_gwd_pending ON github_webhook_deliveries (received_at) WHERE status = 'RECEIVED';
```
Невалидная подпись → строка `REJECTED` без сохранения payload (защита от заполнения хранилища); глобальной FK на `github_installations` нет (запрет global → tenant ссылок).
Реализация (миграция 0012): payload не сохраняется и для подлинных доставок (`payload_storage_key` остаётся `NULL`) — payload лишь подсказка (SC-GH-03), в outbox уходит ограниченная выжимка (`hint`: id репозитория, номер PR, head), состояние перечитывается через GitHub API; хранится `payload_sha256`. Запись приёма — `sys_github_webhook_receive` (§19.5), статус обработки — `aicd_worker`.

### 16.3 `pull_requests` (T, integrations_github) — **дополнение к ТЗ**

```sql
CREATE TABLE pull_requests (
  id                        uuid7       NOT NULL,
  workspace_id              uuid        NOT NULL,
  repository_id             uuid        NOT NULL,
  run_id                    uuid,
  create_action_id          uuid,       -- external_actions (pr_create)
  merge_action_id           uuid,       -- external_actions (pr_merge)
  pr_number                 int         NOT NULL CHECK (pr_number > 0),
  head_branch               text        NOT NULL CHECK (length(head_branch) BETWEEN 1 AND 255),
  base_branch               text        NOT NULL CHECK (length(base_branch) BETWEEN 1 AND 255),
  head_sha                  git_sha     NOT NULL,
  state                     text        NOT NULL CHECK (state IN ('OPEN','CLOSED','MERGED')),
  url                       text        NOT NULL CHECK (url ~ '^https://'),
  merged_commit_sha         git_sha,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  merged_at                 timestamptz,
  closed_at                 timestamptz,
  CONSTRAINT pk_pull_requests PRIMARY KEY (id),
  CONSTRAINT uq_pull_requests_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_pull_requests_number UNIQUE (repository_id, pr_number),
  CONSTRAINT fk_pull_requests_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_pull_requests_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE SET NULL (run_id),
  CONSTRAINT fk_pull_requests_create_action FOREIGN KEY (workspace_id, create_action_id) REFERENCES external_actions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_pull_requests_merge_action FOREIGN KEY (workspace_id, merge_action_id) REFERENCES external_actions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_pull_requests_merged CHECK ((state = 'MERGED') = (merged_at IS NOT NULL AND merged_commit_sha IS NOT NULL)),
  CONSTRAINT ck_pull_requests_closed CHECK (state <> 'CLOSED' OR closed_at IS NOT NULL)
);
CREATE INDEX ix_pull_requests_run ON pull_requests (workspace_id, run_id) WHERE run_id IS NOT NULL;
CREATE INDEX ix_pull_requests_open ON pull_requests (workspace_id, repository_id) WHERE state = 'OPEN';
```
`state = 'MERGED'` может появиться и из вебхука (merge человеком в GitHub) — тогда `merge_action_id IS NULL`; merge **платформой** возможен только через `external_actions(pr_merge)` с approval (AC-13).
Реализация (миграция 0012): `trg_pull_requests_state` — `MERGED` терминально, `merge_action_id` ссылается только на `pr_merge`, `create_action_id` — только на `pr_create`. `trg_fn_lock_run_share` допускает в `COMPLETED` run approvals `MERGE`/`MANUAL_REVIEW` с `subject.scope = 'github'` (merge PR и пуш CI-конфигурации — шаги после завершения run; `ck_approvals_run_bound` требует `run_id` для `MERGE`).

---

## 17. Наблюдаемость и аудит

### 17.1 `audit_events` (M, platform) — партиционирована помесячно, append-only
Журнал аудита (ТЗ §15, §23: каждое поручение, решение, approval, запуск CLI и gate имеет traceable event). `workspace_id IS NULL` — события вне workspace (вход, смена пароля, операции оператора).

```sql
CREATE TABLE audit_events (
  id             uuid7       NOT NULL,
  workspace_id   uuid,
  actor_type     text        NOT NULL CHECK (actor_type IN ('user','agent','system','github')),
  actor_id       uuid,        -- users.id для user; NULL для system/github
  actor_role     text,        -- workspace_role пользователя или agent_role агента на момент действия
  action         text        NOT NULL CHECK (action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),   -- 'approval.decided', 'cli.execution.started'
  resource_type  text        NOT NULL CHECK (resource_type ~ '^[a-z][a-z0-9_]{0,63}$'),
  resource_id    uuid,
  outcome        text        NOT NULL DEFAULT 'success' CHECK (outcome IN ('success','denied','failure')),
  request_id     uuid,
  correlation_id uuid,
  traceparent    text,
  ip             inet,
  metadata       jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 16384),
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_audit_events PRIMARY KEY (id, created_at),
  CONSTRAINT ck_audit_events_actor CHECK (actor_type <> 'user' OR actor_id IS NOT NULL)
) PARTITION BY RANGE (created_at);
CREATE INDEX ix_audit_events_ws_time ON audit_events (workspace_id, created_at DESC);
CREATE INDEX ix_audit_events_resource ON audit_events (resource_type, resource_id, created_at DESC);
CREATE INDEX ix_audit_events_actor ON audit_events (actor_id, created_at DESC) WHERE actor_id IS NOT NULL;
-- REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM PUBLIC, aicd_app, aicd_worker;
-- trg_audit_events_append_only: BEFORE UPDATE OR DELETE → trg_fn_append_only();  (§18.5)
```
FK отсутствуют намеренно: аудит должен переживать удаление/псевдонимизацию субъектов и не блокировать retention других таблиц. `metadata` проходит redaction до записи (ТЗ §16).

### 17.2 Метрики и представления
Метрики ТЗ §16 (run success rate, verified completion rate, gate failures, retry count, provider availability, task latency, context pack size, index freshness, agent message count, queue lag, approval wait time) вычисляются из таблиц-источников через read-only представления `v_*` модулей-владельцев (MODULE_BOUNDARIES §2 п.6), например `v_run_outcomes`, `v_task_latency`, `v_approval_wait`, `v_index_freshness`. Отдельных таблиц агрегатов в v1 нет; при необходимости — материализованные представления с `REFRESH … CONCURRENTLY` (решение после измерений, ТЗ §23). `usage_metrics` (§8.7) — единственная таблица «сырых» метрик исполнения.

---

## 18. Инварианты, обеспечиваемые БД

Принцип: доменная логика (FSM, guard-условия, RBAC) живёт в коде модулей (ADR-0007); БД — **второй, независимый рубеж**, который не даёт сохранить состояние, нарушающее ключевые инварианты, даже при ошибке в коде, гонке воркеров или ручном вмешательстве. Нарушение → исключение с SQLSTATE, который `platform` маппит в доменную ошибку.

### 18.1 Зависимости задач только внутри одного run (ТЗ §7.3)

1. **Декларативно:** `task_dependencies.run_id` входит в оба составных FK `(workspace_id, run_id, task_id|depends_on_task_id) → tasks(workspace_id, run_id, id)` (§7.3). Межrun-ссылка не проходит FK.
2. **Защитный триггер** (требование ТЗ «отдельная проверка/триггер»; страхует от будущей миграции, ослабившей FK):

```sql
CREATE FUNCTION trg_fn_task_dependencies_same_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- self-dependency отклоняет ck_task_dependencies_no_self (тогда в IN одна задача)
  IF (SELECT count(*) FROM tasks
       WHERE id IN (NEW.task_id, NEW.depends_on_task_id)
         AND run_id = NEW.run_id AND workspace_id = NEW.workspace_id)
     <> 2 - (NEW.task_id = NEW.depends_on_task_id)::int THEN
    RAISE EXCEPTION 'task dependency must link tasks of the same run %', NEW.run_id USING ERRCODE = '23514';
  END IF;
  -- сериализация изменений плана run (см. 18.2)
  PERFORM 1 FROM runs WHERE id = NEW.run_id FOR NO KEY UPDATE;
  -- утверждённый план неизменяем: replan создаёт новую plan_version
  IF EXISTS (SELECT 1 FROM tasks t JOIN runs r ON r.id = t.run_id
              WHERE t.id = NEW.task_id AND r.approved_plan_version IS NOT NULL
                AND t.plan_version <= r.approved_plan_version) THEN
    RAISE EXCEPTION 'dependencies of an approved plan version are immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_task_dependencies_same_run BEFORE INSERT OR UPDATE ON task_dependencies
  FOR EACH ROW EXECUTE FUNCTION trg_fn_task_dependencies_same_run();
```

### 18.2 DAG без циклов (ТЗ §6, §7.3, AC-02)

**Транзакционная процедура утверждения плана** (`orchestration.plans.approve(run_id, plan_version, expected_run_version)`), уровень изоляции `READ COMMITTED`:

1. `SELECT … FROM runs WHERE id = :run_id FOR UPDATE` — блокировка строки run. Все мутации плана (вставка задач/рёбер планировщиком, replan) берут ту же блокировку (`FOR NO KEY UPDATE` в триггере 18.1), поэтому граф во время проверки не меняется. Это дешевле и детерминированнее, чем `SERIALIZABLE` (не нужны повторы на `40001`, которые к тому же защищают только от других `SERIALIZABLE`-транзакций).
2. Проверить `runs.version = :expected_run_version`, `status IN ('PLANNED','AWAITING_APPROVAL')`, `plan_version = :plan_version`.
3. **Приложение:** загрузить вершины/рёбра версии плана и выполнить алгоритм Кана; при цикле вернуть `422 dag_cycle_detected` с путём цикла (понятная ошибка для UI).
4. **БД (контрольная проверка тем же запросом, что и в constraint trigger):**

```sql
WITH RECURSIVE walk(start_id, cur_id) AS (
    SELECT d.task_id, d.depends_on_task_id
      FROM task_dependencies d WHERE d.run_id = :run_id
  UNION ALL
    SELECT w.start_id, d.depends_on_task_id
      FROM walk w JOIN task_dependencies d ON d.task_id = w.cur_id AND d.run_id = :run_id
) CYCLE cur_id SET is_cycle USING path
SELECT EXISTS (SELECT 1 FROM walk WHERE start_id = cur_id OR is_cycle) AS has_cycle;
```

5. `UPDATE runs SET approved_plan_version = :plan_version, status = …, version = version + 1`; задачи без незавершённых зависимостей → `READY`; `outbox_events` (`run.state_changed`, `task.state_changed`); commit.

**Constraint trigger как постоянная гарантия:** `trg_task_dependencies_acyclic` — `CONSTRAINT TRIGGER AFTER INSERT OR UPDATE ON task_dependencies DEFERRABLE INITIALLY DEFERRED FOR EACH ROW`; функция выполняет запрос п.4 для `NEW.run_id` один раз на run в транзакции (отметка через `set_config('aicd.dag_checked.' || NEW.run_id, '1', true)`), при цикле — `RAISE … ERRCODE '23514'` и откат всей транзакции. Стоимость O(V·E) на план приемлема для планов в десятки–сотни задач; измерить в P3.

### 18.3 Валидация переходов состояний

**Выбор:** гибрид — **основная** валидация в коде (`orchestration/transitions.py` + guard-функции, переход как `UPDATE … WHERE status = :old AND version = :expected`, ADR-0007) **плюс** защитный триггер в БД по справочнику `allowed_transitions`.
**Обоснование:** (1) переходы выполняют несколько процессов (`api`, `worker-orchestrator`, `worker-exec`, `worker-verify`, reconciler) — ошибка в любом из них не должна оставить запись в невозможном состоянии; (2) справочник — данные, генерируемые из того же `transitions.py`, поэтому двойного ручного сопровождения нет (CI-тест сравнивает множества); (3) стоимость — один индексный lookup на `UPDATE OF status`. Альтернатива «только CHECK» не выражает переходы (CHECK видит лишь новую строку), альтернатива «только код» не даёт defence-in-depth, требуемый ТЗ §6 «запрещать невалидные переходы».

```sql
CREATE FUNCTION trg_fn_check_transition() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_entity text := TG_ARGV[0];
        v_from   text := CASE WHEN TG_OP = 'INSERT' THEN '__new__' ELSE OLD.status END;
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
  IF NOT EXISTS (SELECT 1 FROM allowed_transitions
                  WHERE entity = v_entity AND from_status = v_from AND to_status = NEW.status) THEN
    RAISE EXCEPTION 'invalid % transition: % -> %', v_entity, v_from, NEW.status USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_runs_transition BEFORE INSERT OR UPDATE OF status ON runs
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('run');
-- аналогично: tasks('task'), cli_executions('execution'), user_directives('directive'),
-- approvals('approval'), external_actions('external_action'), indexing_jobs('indexing_job'), findings('finding').
```

Начальное содержимое `allowed_transitions` (из ТЗ §6 и Directive/Approval/Ledger FSM; сверено с DATA_FLOW.md §3 — **эта таблица является консолидированным нормативным списком переходов**; в коде источник — `transitions.py`, CI сравнивает множества). `__new__` — допустимое начальное состояние при `INSERT`.

| entity | from | разрешённые to |
|---|---|---|
| run | `__new__` | `CREATED` |
| run | `CREATED` | `DISCOVERY`, `CANCELLED`, `FAILED` |
| run | `DISCOVERY` | `PLANNED`, `PAUSED`, `BLOCKED`, `FAILED`, `CANCELLED` |
| run | `PLANNED` | `AWAITING_APPROVAL`, `RUNNING`, `DISCOVERY`, `PAUSED`, `BLOCKED`, `FAILED`, `CANCELLED` |
| run | `AWAITING_APPROVAL` | `RUNNING`, `PLANNED`, `DISCOVERY` (план отклонён с перепланированием), `PAUSED`, `BLOCKED`, `FAILED`, `CANCELLED` |
| run | `RUNNING` | `VERIFYING`, `PLANNED` (replan по поручению), `PAUSED`, `BLOCKED`, `FAILED`, `CANCELLED` |
| run | `VERIFYING` | `COMPLETED`, `RUNNING` (fix после FAIL gate), `PAUSED`, `BLOCKED`, `FAILED`, `CANCELLED` |
| run | `PAUSED` | `DISCOVERY`, `PLANNED`, `AWAITING_APPROVAL`, `RUNNING`, `VERIFYING`, `FAILED`, `CANCELLED` |
| run | `BLOCKED` | `DISCOVERY`, `PLANNED`, `AWAITING_APPROVAL`, `RUNNING`, `VERIFYING`, `PAUSED`, `FAILED`, `CANCELLED` |
| task | `__new__` | `PENDING` |
| task | `PENDING` | `READY`, `BLOCKED`, `CANCELLED` |
| task | `READY` | `LEASED`, `PENDING` (новая зависимость при replan), `BLOCKED`, `CANCELLED` |
| task | `LEASED` | `RUNNING`, `READY` (аренда потеряна до старта), `RETRY_WAIT`, `BLOCKED`, `FAILED`, `CANCELLED` |
| task | `RUNNING` | `VERIFYING`, `RETRY_WAIT`, `BLOCKED`, `FAILED`, `CANCELLED` |
| task | `VERIFYING` | `SUCCEEDED`, `RETRY_WAIT`, `BLOCKED`, `FAILED`, `CANCELLED` |
| task | `RETRY_WAIT` | `READY`, `BLOCKED`, `FAILED`, `CANCELLED` |
| task | `BLOCKED` | `PENDING`, `READY`, `SUCCEEDED` (только `blocked_reason=MANUAL_REVIEW` + approval APPROVED с evidence `producer=human`; guard в коде), `FAILED`, `CANCELLED` |
| execution | `__new__` | `QUEUED` |
| execution | `QUEUED` | `STARTING`, `CANCELLED`, `CRASHED` |
| execution | `STARTING` | `RUNNING`, `TIMED_OUT`, `CANCELLED`, `CRASHED`, `AUTH_REQUIRED`, `RATE_LIMITED` |
| execution | `RUNNING` | `EXITED`, `TIMED_OUT`, `CANCELLED`, `CRASHED`, `AUTH_REQUIRED`, `RATE_LIMITED` |
| execution | `EXITED` | `COLLECTED`, `CRASHED` (результат не собран/не разобран), `AUTH_REQUIRED`, `RATE_LIMITED` (классификация после выхода, CLI_ADAPTER_CONTRACT §9) |
| directive | `__new__` | `RECEIVED` |
| directive | `RECEIVED` | `IMPACT_ANALYSIS_PENDING`, `DISPATCHED` (только `ASK`), `REJECTED`, `CANCELLED`, `FAILED` |
| directive | `IMPACT_ANALYSIS_PENDING` | `IMPACT_READY`, `CANCELLED`, `FAILED` |
| directive | `IMPACT_READY` | `AWAITING_CONFIRMATION`, `CONFIRMED`, `CHECKPOINTING` (auto-confirm некритичного влияния), `REJECTED`, `CANCELLED` |
| directive | `AWAITING_CONFIRMATION` | `CONFIRMED`, `REJECTED`, `CANCELLED` |
| directive | `CONFIRMED` | `CHECKPOINTING`, `REPLANNING`, `DISPATCHED`, `CANCELLED`, `FAILED` |
| directive | `CHECKPOINTING` | `REPLANNING`, `DISPATCHED`, `FAILED` |
| directive | `REPLANNING` | `DISPATCHED`, `FAILED` |
| directive | `DISPATCHED` | `ACKNOWLEDGED`, `FAILED` |
| directive | `ACKNOWLEDGED` | `COMPLETED`, `FAILED` |
| approval | `__new__` | `PENDING` |
| approval | `PENDING` | `APPROVED`, `REJECTED`, `EXPIRED`, `CANCELLED` |
| external_action | `__new__` | `PLANNED` |
| external_action | `PLANNED` | `EXECUTING`, `FAILED` |
| external_action | `EXECUTING` | `CONFIRMED`, `FAILED` (только после сверки с внешней системой) |
| external_action | `FAILED` | `EXECUTING` (повтор после reconciliation, `attempt_count + 1`) |
| external_action | `CONFIRMED` | `COMPENSATED` |
| indexing_job | `__new__` | `QUEUED` |
| indexing_job | `QUEUED` | `RUNNING`, `CANCELLED`, `FAILED` |
| indexing_job | `RUNNING` | `SUCCEEDED`, `FAILED`, `CANCELLED`, `QUEUED` (повтор после потери аренды, `attempt_count + 1`) |
| finding | `__new__` | `OPEN` |
| finding | `OPEN` | `ACKNOWLEDGED`, `FIXED`, `VERIFIED_FIXED` (повторный прогон gate не воспроизвёл), `FALSE_POSITIVE`, `ACCEPTED_RISK` |
| finding | `ACKNOWLEDGED` | `OPEN`, `FIXED`, `VERIFIED_FIXED`, `FALSE_POSITIVE`, `ACCEPTED_RISK` |
| finding | `FIXED` | `VERIFIED_FIXED`, `OPEN` (повторный прогон воспроизвёл находку) |
| finding | `VERIFIED_FIXED` | `OPEN` (регрессия на более позднем commit run) |
| finding | `FALSE_POSITIVE` | `OPEN` (решение человека отозвано) |
| finding | `ACCEPTED_RISK` | `OPEN` (принятие риска отозвано/истекло) |

Терминальные состояния (нет исходящих строк): run `COMPLETED/FAILED/CANCELLED`; task `SUCCEEDED/FAILED/CANCELLED`; execution `COLLECTED/TIMED_OUT/CANCELLED/CRASHED/AUTH_REQUIRED/RATE_LIMITED`; directive `COMPLETED/REJECTED/CANCELLED/FAILED`; approval — все, кроме `PENDING`; external_action `COMPENSATED`; indexing_job `SUCCEEDED/FAILED/CANCELLED` (строки indexing_job — миграция 0007, P5-01; в §10.9 FSM не задан). У finding терминальных состояний нет: любое закрытие может быть отозвано (`→ OPEN`); строки finding — миграция 0008 (P6-01; в §12.6 FSM не задан). Дополнительно `trg_findings_guard`: `FALSE_POSITIVE`/`ACCEPTED_RISK` — только `resolved_by = app_current_user()` (человек сессии, ТЗ §10), `VERIFIED_FIXED` — только с `VERIFIED` evidence. Дополнительно `trg_runs_resume_guard`: выход из `PAUSED`/`BLOCKED` в нетерминальное состояние допустим только в `resume_status` (или `PAUSED` из `BLOCKED`).

### 18.4 `COMPLETED` только при успешных обязательных gates и выполненной approval policy (ТЗ §6, §14, AC-11)

```sql
CREATE FUNCTION trg_fn_runs_completion_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'COMPLETED' AND OLD.status <> 'COMPLETED' THEN
    -- 1) все задачи актуального плана успешны (заменённые replan'ом — CANCELLED + superseded_at)
    IF EXISTS (SELECT 1 FROM tasks t WHERE t.run_id = NEW.id AND t.superseded_at IS NULL
                AND t.status <> 'SUCCEEDED') THEN
      RAISE EXCEPTION 'run %: not all tasks SUCCEEDED', NEW.id USING ERRCODE = '23514';
    END IF;
    -- 2) каждый актуальный обязательный gate: PASS или обоснованный NOT_APPLICABLE (SKIPPED ≠ PASS)
    IF EXISTS (SELECT 1 FROM gate_executions g WHERE g.run_id = NEW.id AND g.superseded_at IS NULL
                AND g.required AND g.status NOT IN ('PASS','NOT_APPLICABLE')) THEN
      RAISE EXCEPTION 'run %: required gates not passed', NEW.id USING ERRCODE = '23514';
    END IF;
    -- 3) хотя бы одна проверка реально выполнена (нельзя завершить run «пустой» верификацией)
    IF NOT EXISTS (SELECT 1 FROM gate_executions g WHERE g.run_id = NEW.id AND g.superseded_at IS NULL
                    AND g.status = 'PASS') THEN
      RAISE EXCEPTION 'run %: no PASS gate execution', NEW.id USING ERRCODE = '23514';
    END IF;
    -- 4) нет незакрытых approvals; утверждённый план существует
    IF EXISTS (SELECT 1 FROM approvals a WHERE a.run_id = NEW.id AND a.status = 'PENDING')
       OR NEW.approved_plan_version IS NULL THEN
      RAISE EXCEPTION 'run %: approval policy not satisfied', NEW.id USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_runs_completion_guard BEFORE UPDATE OF status ON runs
  FOR EACH ROW EXECUTE FUNCTION trg_fn_runs_completion_guard();
```

Дополнительно `trg_tasks_success_guard`: задача → `SUCCEEDED` только если все её актуальные обязательные `gate_executions` в `PASS/NOT_APPLICABLE` и нет ни одного `FAIL/ERROR/PENDING/RUNNING`; `trg_gate_executions_evidence_verified`: `PASS` требует `evidence.verification_status = 'VERIFIED'` (exit code 0 / слова агента не дают PASS, ТЗ §9, §14). Блокирующие пороги находок (например, «нет OPEN critical») — из `quality_profiles.definition`, проверяются Verification Engine и выражаются как `FAIL` соответствующего gate, а не отдельным триггером.

**Гонки.** Писатели `tasks.status`, `gate_executions` и `approvals` в начале транзакции берут `SELECT 1 FROM runs WHERE id = :run_id FOR SHARE` (триггеры `trg_*_lock_run_share`); переход run в `COMPLETED` выполняет `UPDATE runs` (блокировка `FOR NO KEY UPDATE`), которая конфликтует с `FOR SHARE`. Поэтому новый провальный gate не может «проскочить» между проверкой и commit: либо он закоммичен до проверки (и виден её запросам — в `READ COMMITTED` каждый запрос PL/pgSQL берёт свежий снимок), либо ждёт и после commit run видит его терминальным — и отклоняется: `trg_*_lock_run_share` после получения `FOR SHARE` проверяет, что run не находится в `COMPLETED/FAILED/CANCELLED` (запись результатов в завершённый run запрещена).

### 18.5 Append-only `audit_events` (ТЗ §15)

1. `REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM PUBLIC, aicd_app, aicd_worker;` — `GRANT INSERT, SELECT` только.
2. `trg_audit_events_append_only BEFORE UPDATE OR DELETE … EXECUTE FUNCTION trg_fn_append_only()` — страхует и от владельца (кроме `DETACH PARTITION`, см. 3).
3. Удаление возможно только целыми партициями ролью `aicd_maintenance` после экспорта в архив (§20.2), с записью события `audit.partition_archived` (sha256 архива) в **новую** партицию.
4. Предложение (§22): hash-chain (`prev_hash`, `row_hash`) для tamper-evidence.

Та же схема (REVOKE + триггер) — для `run_event_log`, `agent_messages`, `agent_activity_events`.

### 18.6 Монотонность fencing token

- Единственный источник токенов — `worker_lease_fencing_seq` (`nextval` не откатывается и строго возрастает в пределах кластера).
- Триггер на `worker_leases`:

```sql
CREATE FUNCTION trg_fn_worker_leases_fencing() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.fencing_token < OLD.fencing_token THEN
    RAISE EXCEPTION 'fencing token must not decrease' USING ERRCODE = '23514';
  END IF;
  IF (NEW.worker_id IS DISTINCT FROM OLD.worker_id OR (OLD.released_at IS NOT NULL AND NEW.released_at IS NULL))
     AND NEW.fencing_token <= OLD.fencing_token THEN
    RAISE EXCEPTION 'lease re-acquisition requires a new fencing token' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_worker_leases_fencing_monotonic BEFORE UPDATE ON worker_leases
  FOR EACH ROW EXECUTE FUNCTION trg_fn_worker_leases_fencing();
```
- Аналогичный триггер на `tasks.lease_fencing_token` (не убывает). Восстановление из бэкапа: после PITR sequence продвигается `setval(… , max(fencing_token) + 1_000_000)` (runbook) — иначе возможен повтор токенов.

### 18.7 Approvals и опасные действия (ТЗ §15, AC-13)

- `ck_approvals_four_eyes` — запрашивающий человек не может утвердить сам (кроме явно разрешённого single-member режима, аудируется).
- `trg_approvals_decider_role`: `decided_by` должен быть членом workspace проекта с рангом роли (с учётом `project_member_overrides`) ≥ `required_role`. Матрица «action_type → required_role» — из брифа (reviewer: `MANUAL_REVIEW`, `SELF_REVIEW_FALLBACK`; developer: `PLAN_APPROVAL`, `DOCS_DOWNLOAD`; admin: `MERGE`, `DESTRUCTIVE_MIGRATION`, `DATA_DELETION`, `CRITICAL_DIRECTIVE`, `LOCAL_EXECUTION_FALLBACK`; owner: `PRODUCTION_DEPLOY`, `SECRET_CHANGE`, `UNRESTRICTED_NETWORK`); `required_role` заполняет приложение, CHECK-функция `approval_min_role(action_type)` в триггере запрещает занижение.
- `ck_external_actions_approval_required` + `trg_external_actions_approval_gate` — `pr_merge`, `production_deploy`, деструктивный `migration_apply` не могут перейти в `EXECUTING` без `APPROVED`, не истёкшего approval с `subject_hash = request_hash`.

### 18.8 Независимость ревью (ТЗ §4)

```sql
CREATE FUNCTION trg_fn_agent_assignments_independence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_reviewed_provider text;
BEGIN
  IF NEW.review_independence = 'independent_provider' THEN
    SELECT provider INTO v_reviewed_provider FROM task_attempts WHERE id = NEW.reviewed_attempt_id;
    IF v_reviewed_provider IS NULL OR v_reviewed_provider = NEW.provider THEN
      RAISE EXCEPTION 'review is not independent: same provider %', NEW.provider USING ERRCODE = '23514';
    END IF;
  ELSIF NEW.review_independence = 'same_provider_fallback' AND NOT EXISTS (
      SELECT 1 FROM approvals a JOIN task_attempts ta ON ta.id = NEW.reviewed_attempt_id
       WHERE a.task_id = ta.task_id AND a.action_type = 'SELF_REVIEW_FALLBACK' AND a.status = 'APPROVED') THEN
    RAISE EXCEPTION 'same-provider review requires SELF_REVIEW_FALLBACK approval' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
```

### 18.9 Отсутствие секретов (ТЗ §7.1, §15)

- Ни одна таблица не содержит колонок для секретов; допустимы только хэши (`password_hash`, `token_hash`, `csrf_token_hash`) и ссылки (`credential_volume_ref`, `*_ref`).
- **Schema-lint в CI** (§21.4): запрещены имена колонок по шаблону `(secret|password|passwd|api_?key|private_?key|access_?token|refresh_?token|cookie|credential)` кроме allowlist (`password_hash`, `password_changed_at`, `token_hash`, `csrf_token_hash`, `credential_volume_ref`, `fencing_token`, `lease_fencing_token`, `token_budget`, `token_estimate`).
- Текстовые поля из внешних источников (`failure_message`, `last_error`, `detail`, `command`, `body`, `payload`, `metadata`) проходят `security.redact()` до записи; тест «secret canary» (ТЗ §20 Security) вставляет известный фиктивный токен в вывод CLI и проверяет его отсутствие во всех таблицах и логах.
- Настройки PostgreSQL: `log_statement = 'ddl'`, `log_parameter_max_length = 0`, `log_parameter_max_length_on_error = 0` — значения параметров запросов не попадают в журнал СУБД.

### 18.10 Членство

- `trg_workspace_members_last_owner` (deferred constraint trigger): после UPDATE/DELETE в workspace со статусом `ACTIVE` остаётся ≥ 1 `owner`.
- `trg_project_member_overrides_downgrade_only`: роль переопределения строго ниже роли в workspace; при понижении роли в workspace переопределения, ставшие «не ниже», удаляются тем же триггером.

### 18.11 Полиморфные ссылки

`knowledge_chunks(source_type, source_id)`, `evidence_links(target_type, target_id)`, `audit_events(resource_type, resource_id)` не имеют FK. Для первых двух — триггер `BEFORE INSERT`, проверяющий существование цели **в том же workspace** динамическим запросом по `target_type` (таблица соответствий `target_type → таблица` в коде функции); для `audit_events` проверки нет намеренно (аудит переживает субъект). Ночная задача `maint_check_polymorphic_refs` сообщает о «висячих» ссылках после retention.

### 18.12 Лимиты размеров jsonb/текста

| Колонка | Лимит | Основание |
|---|---|---|
| `agent_messages.payload` | `octet_length(payload::text) <= 32768` | ТЗ §10 (≤ 32 KiB) |
| `agent_activity_events.payload`, `run_event_log.payload` | ≤ 32768 | ТЗ §8.3 «ограничивать по размеру» |
| `chat_messages.body` | ≤ 32768 байт | то же |
| `outbox_events.payload` | ≤ 65536 | крупные данные — только refs |
| `audit_events.metadata` | ≤ 16384 | — |
| `idempotency_keys.response` | ≤ 65536 | — |

### 18.13 Tenancy

- Составные FK `(workspace_id, …)` для всех tenant → tenant ссылок (§1.3); глобальные таблицы не ссылаются на tenant.
- `trg_<t>_immutable_ws` запрещает менять `workspace_id`.
- M-таблицы (`policies`, `quality_profiles`, `quality_gates`) проверяют scope триггерами: tenant-строка может ссылаться на global или на строку своего workspace, global — только на global.

---

## 19. Row Level Security

### 19.1 Роли БД

| Роль | LOGIN | BYPASSRLS | Назначение | Права |
|---|---|---|---|---|
| `aicd_migrator` | да | нет | владелец схемы `aicd`; только Alembic (DDL, seed-данные справочников) | owner объектов; RLS на него действует (`FORCE`) |
| `aicd_app` | да | **нет** | процесс `api` | DML на tenant/M-таблицы по GRANT; `SELECT` на справочники; `INSERT` в `outbox_events`, `audit_events`, `idempotency_keys`; нет доступа к `worker_leases`, `processed_events`, `cli_*` (кроме `SELECT` для `/providers`) |
| `aicd_worker` | да | **нет** | `worker-*` процессы | DML на tenant-таблицы своего домена; полный доступ к S-таблицам (`outbox_events`, `worker_leases`) и `processed_events`; запись в `cli_installations`, `cli_sessions`, `provider_health`, `worker_hosts` |
| `aicd_maintenance` | да | **да** | только задачи retention/архивирования/purge/backup через `aicd` admin CLI | каждое использование аудируется (§19.6) |
| `aicd_sysfn` | **нет** | да | владелец `SECURITY DEFINER` функций `sys_*` (§19.5) — **дополнение к брифу** | `SELECT` только на колонки, нужные функциям |

Схема приложения — `aicd`; `search_path = aicd` задаётся на ролях; `PUBLIC` лишён `CREATE` на схеме и всех прав на таблицы.

### 19.2 Установка контекста

`packages/platform` — `db.transaction(ctx)` в начале **каждой** транзакции:

```sql
SELECT set_config('app.workspace_id', :workspace_id, true),   -- true = SET LOCAL: действует до конца транзакции
       set_config('app.user_id',      :user_id,      true),   -- '' для системных акторов
       set_config('app.actor_type',   :actor_type,   true);   -- 'user' | 'agent' | 'system'
```

- `SET LOCAL` совместим с пулингом соединений в режиме transaction (PgBouncer): контекст не «протекает» в чужую транзакцию.
- API устанавливает `app.workspace_id` **только после** проверки членства и RBAC (`identity.api.authorize`); идентификатор workspace берётся из ресурса запроса (`project_id` → `workspace_id`) через `SECURITY DEFINER`-функцию `sys_resolve_project_workspace(project_id)`, которая возвращает workspace только если `app.user_id` — его член.
- Воркеры: каждое сообщение Dramatiq/outbox несёт `workspace_id`; воркер открывает транзакцию с этим контекстом. Межарендаторные выборки (планировщик, reaper) — только через `sys_*` функции, возвращающие идентификаторы, после чего работа идёт в контексте конкретного workspace.
- Без контекста `app_current_workspace()` = `NULL` → tenant-строки невидимы и не вставляются (fail-closed).

### 19.3 Политики

```sql
-- Шаблон для каждой T-таблицы (пример: runs)
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs FORCE ROW LEVEL SECURITY;
CREATE POLICY p_runs_tenant ON runs
  FOR ALL TO aicd_app, aicd_worker
  USING (workspace_id = app_current_workspace())
  WITH CHECK (workspace_id = app_current_workspace());

-- Корень tenancy
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY p_workspaces_select ON workspaces FOR SELECT TO aicd_app, aicd_worker
  USING (id = app_current_workspace());
CREATE POLICY p_workspaces_update ON workspaces FOR UPDATE TO aicd_app
  USING (id = app_current_workspace()) WITH CHECK (id = app_current_workspace());
CREATE POLICY p_workspaces_create ON workspaces
  FOR INSERT TO aicd_app
  WITH CHECK (id = app_current_workspace() AND created_by = app_current_user());
-- API при создании генерирует UUIDv7, устанавливает его как контекст и вставляет workspace + owner-членство в одной транзакции.

-- M-таблица (global + tenant): чтение global-строк разрешено, запись — только своих
ALTER TABLE policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE policies FORCE ROW LEVEL SECURITY;
CREATE POLICY p_policies_read ON policies FOR SELECT TO aicd_app, aicd_worker
  USING (workspace_id IS NULL OR workspace_id = app_current_workspace());
CREATE POLICY p_policies_write ON policies FOR INSERT TO aicd_app
  WITH CHECK (workspace_id = app_current_workspace());
CREATE POLICY p_policies_update ON policies FOR UPDATE TO aicd_app
  USING (workspace_id = app_current_workspace()) WITH CHECK (workspace_id = app_current_workspace());
-- То же для quality_profiles, quality_gates.

-- idempotency_keys: строки workspace или «без workspace» самого пользователя
CREATE POLICY p_idempotency_keys ON idempotency_keys FOR ALL TO aicd_app
  USING (workspace_id = app_current_workspace()
         OR (workspace_id IS NULL AND user_id = app_current_user()))
  WITH CHECK (workspace_id = app_current_workspace()
         OR (workspace_id IS NULL AND user_id = app_current_user()));

-- audit_events: читать — только свой workspace; писать — свой workspace или глобальные события
CREATE POLICY p_audit_events_read ON audit_events FOR SELECT TO aicd_app
  USING (workspace_id = app_current_workspace());
CREATE POLICY p_audit_events_insert ON audit_events FOR INSERT TO aicd_app, aicd_worker
  WITH CHECK (workspace_id IS NULL OR workspace_id = app_current_workspace());

-- S-таблицы: api только вставляет события своего workspace; воркеры видят все строки
ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY p_outbox_events_app_insert ON outbox_events FOR INSERT TO aicd_app
  WITH CHECK (workspace_id = app_current_workspace());
CREATE POLICY p_outbox_events_worker ON outbox_events FOR ALL TO aicd_worker
  USING (true) WITH CHECK (workspace_id IS NULL OR workspace_id = app_current_workspace()
                           OR current_setting('app.actor_type', true) = 'system');
CREATE POLICY p_worker_leases_worker ON worker_leases FOR ALL TO aicd_worker USING (true) WITH CHECK (true);
```

Партиционированные таблицы: политики задаются на родителе; `GRANT` — только на родителя (прямой доступ к партициям отозван), на каждой партиции также `ENABLE`+`FORCE ROW LEVEL SECURITY` с той же политикой (создаётся процедурой создания партиций).

### 19.4 Классификация таблиц

| Класс | Таблицы | RLS |
|---|---|---|
| **T** tenant | `workspace_members`, `project_member_overrides`, `projects`, `project_settings`, `repositories`, `worktrees`, `project_technologies`, `runs`, `tasks`, `task_dependencies`, `task_attempts`, `agent_assignments`, `routing_decisions`, `cli_executions`, `usage_metrics`, `agent_messages`, `message_deliveries`, `agent_decisions`, `chat_threads`, `chat_messages`, `user_directives`, `directive_impacts`, `directive_acknowledgements`, `agent_activity_events`, `code_index_snapshots`, `code_files`, `code_symbols`, `code_dependencies`, `knowledge_chunks`, `retrieval_queries`, `retrieval_results`, `context_packs`, `indexing_jobs`, `knowledge_entries`, `architecture_decisions`, `documentation_sources`, `documentation_snapshots`, `documentation_sections`, `artifacts`, `gate_executions`, `test_results`, `findings`, `evidence`, `evidence_links`, `project_controls`, `compliance_results`, `approvals`, `checkpoints`, `external_actions`, `run_event_log`, `github_installations`, `pull_requests` + корень `workspaces` | `workspace_id = app_current_workspace()` (для `workspaces` — `id`) |
| **M** mixed | `policies`, `quality_profiles`, `quality_gates`, `idempotency_keys`, `audit_events` | чтение global + своё, запись своё (§19.3) |
| **S** system | `outbox_events`, `worker_leases` | `aicd_worker` — все строки; `aicd_app` — только INSERT своего workspace в outbox, к leases доступа нет |
| **G** global | `users`, `user_credentials`, `user_sessions`, `cli_providers`, `agent_profiles`, `allowed_transitions`, `standards`, `standard_controls`, `technologies`, `technology_versions`, `worker_hosts`, `cli_installations`, `cli_sessions`, `provider_health`, `github_webhook_deliveries`, `processed_events` | RLS не включается; доступ ограничен GRANT: справочники — `SELECT` для app/worker, запись — миграции/`aicd` CLI; `user_credentials`/`user_sessions` — только `aicd_app` (модуль identity); `provider_health`/`cli_*` — запись `aicd_worker`, чтение `aicd_app` (UI провайдеров: admin) |

Тест схемы (§21.4) проверяет: каждая таблица с колонкой `workspace_id` имеет `relrowsecurity AND relforcerowsecurity` и ≥ 1 политику; ни одна таблица не попала одновременно в G и не имеет `workspace_id`-колонки.

### 19.5 `SECURITY DEFINER` функции межарендаторного доступа

Владелец — `aicd_sysfn` (NOLOGIN, BYPASSRLS); `SET search_path = pg_catalog, aicd`; `REVOKE EXECUTE … FROM PUBLIC`; `GRANT EXECUTE` точечно. Возвращают только идентификаторы.

| Функция | Кому | Возвращает |
|---|---|---|
| `sys_list_user_workspaces()` | `aicd_app` | `(workspace_id, role)` для `app_current_user()` — список «мои workspace» (бриф) |
| `sys_resolve_project_workspace(project_id uuid)` | `aicd_app` | `workspace_id`, если `app_current_user()` — член; иначе `NULL` |
| `sys_resolve_installation_workspace(installation_id bigint)` | `aicd_worker` | `workspace_id` установки GitHub App (после проверки подписи вебхука) |
| `sys_github_webhook_receive(id, event_id, delivery_guid, event, action, installation_id, signature_valid, payload_sha256, status, error, hint jsonb)` | `aicd_app` | `'accepted'`/`'ignored'`/`'rejected'`/`'duplicate'`: строка `github_webhook_deliveries` (дедупликация по GUID; `REJECTED` подделка с тем же GUID заменяется подлинной доставкой) + для `RECEIVED` событие outbox `github.webhook.received` без workspace — у `aicd_app` нет прямых прав на G-таблицу и INSERT в outbox вне своего workspace (миграция 0012) |
| `sys_schedulable_workspaces(limit int)` | `aicd_worker` | workspace с `READY`-задачами, наступившими `RETRY_WAIT`, истекающими `PENDING` approvals, активными directives |
| `sys_login_lookup(email text)` | `aicd_app` | `user_id` и признак активности для аутентификации (глобальная таблица, но функция централизует rate-limited lookup) |
| `sys_resolve_run_workspace(run_id uuid)` / `sys_resolve_task_workspace(task_id uuid)` | `aicd_app` | `workspace_id` run/задачи, если `app_current_user()` — член; иначе `NULL` (миграция 0003) |
| `sys_resolve_approval_workspace(approval_id uuid)` | `aicd_app` | `workspace_id` approval, если `app_current_user()` — член; иначе `NULL` (миграция 0005; `GET/POST /approvals/{id}…`) |
| `sys_resolve_finding_workspace(finding_id uuid)` | `aicd_app` | `workspace_id` находки, если `app_current_user()` — член; иначе `NULL` (миграция 0010; `GET /findings/{id}`, `POST /findings/{id}/transitions`) |
| `sys_record_compliance_review(run_id, control_id, status, notes, evidence_ids uuid[])` | `aicd_app` | id новой строки `compliance_results` — вердикт человека `PASS`/`FAIL` (QUALITY_GATES §8.2): только `app_current_workspace()`, эффективная роль проекта ≥ `approval_min_role('MANUAL_REVIEW')` (иначе `42501`), применимость утверждена и не `not_applicable`, run не терминален, текущий вердикт `UNVERIFIED`/`MANUAL_REVIEW` (не `automated_gate`)/человека (иначе `55000`), evidence этого run (иначе `23514`); прежняя строка — `superseded_at`, evidence → `evidence_links` (`supports`/`refutes`); `NULL` — не найдено/не член (миграция 0010; у `aicd_app` нет `INSERT` на `compliance_results`) |
| `sys_recovery_workspaces(limit int)` | `aicd_worker` | workspace с `cli_executions` в `QUEUED/STARTING/RUNNING/EXITED`, `worktrees` в `ACTIVE/LOCKED`, `external_actions` в `EXECUTING`, нетерминальными `runs` — для recovery reconciler (миграция 0005) |

### 19.6 Обход RLS для обслуживания

- `aicd_maintenance` используется **только** командами `aicd maintenance …` (retention, archive, purge workspace, проверки целостности) и `pg_dump` для бэкапа; подключение разрешено `pg_hba.conf` только с хоста оператора/джоба.
- Каждая сессия начинается с `SELECT maint_begin(:job, :reason)` — функция пишет `audit_events(actor_type='system', action='maintenance.session_started')`; без этого вызова триггеры `trg_*_append_only` и retention-функции отклоняют операции (`current_setting('aicd.maint_session', true)` проверяется в функциях обслуживания).
- `log_statement = 'all'` для роли `aicd_maintenance` (`ALTER ROLE … SET log_statement = 'all'`), параметры не логируются (§18.9).
- `aicd_migrator` не обходит RLS: data-миграции, затрагивающие tenant-данные, выполняются пакетами по workspace с установкой контекста либо через `aicd_maintenance` с явной пометкой в миграции.

### 19.7 Тесты RLS

Для каждой T/M/S-таблицы автоматически (параметризованный тест по `information_schema`): два workspace A/B; под `aicd_app` и `aicd_worker` с контекстом A — `SELECT/UPDATE/DELETE` строк B возвращают 0 строк, `INSERT` с `workspace_id = B` падает с `42501`; без контекста — 0 строк; составной FK на родителя из B падает. Отдельно — `sys_*` функции не возвращают данных чужих пользователей. Это реализация AC-12 на уровне БД.

---

## 20. Retention, архивирование, партиционирование

### 20.1 Партиционирование

| Таблица | Схема | Обоснование |
|---|---|---|
| `agent_activity_events` | `RANGE (occurred_at)`, месяц | наибольший объём (вывод CLI), нет входящих FK, retention удалением партиций без `VACUUM`-нагрузки |
| `audit_events` | `RANGE (created_at)`, месяц | append-only, долгое хранение, архив целыми партициями |
| `outbox_events` | `RANGE (created_at)`, месяц | высокий оборот, опубликованные строки удаляются партициями; без FK |
| `agent_messages` | **не** партиционируется в v1 | входящие FK (`message_deliveries`, `reply_to_id`, `chat_messages`, `agent_decisions`, `directive_acknowledgements`) потребовали бы ключ партиции во всех уникальных ключах; объём ограничен (≤ 32 KiB, на run); retention — вместе с архивом run |
| `run_event_log` | **не** партиционируется в v1 | PK `(run_id, seq)` и `UNIQUE (event_id)` пришлось бы расширить ключом партиции; удаление — пакетами по run (§20.2). Пересмотреть после замеров (§22) |

Партиции создаёт задача `maint_ensure_partitions` (ежедневно) на 3 месяца вперёд; `DEFAULT`-партиция не создаётся (иначе `ATTACH` требует её сканирования); алерт, если впереди < 2 партиций. Имена: `<table>_y2027m01`.

### 20.2 Механика

- **Архив перед удалением:** данные экспортируются в NDJSON (gzip) в object storage (`archive/<workspace_id>/<table>/<period>.ndjson.gz`), sha256 фиксируется в `artifacts(kind='archive', retention_class='archive')` и `audit_events`. Для глобальных партиций `audit_events` — архив по месяцу целиком (несколько workspace в одном файле, доступ только оператору).
- **Партиционированные таблицы:** `ALTER TABLE … DETACH PARTITION … CONCURRENTLY` → экспорт → `DROP TABLE`.
- **Непартиционированные:** удаление пакетами (`DELETE … WHERE ctid = ANY(…) LIMIT`-эквивалент по 5–10 тыс. строк) ролью `aicd_maintenance` с паузами; `RESTRICT`-FK не позволяют удалить то, на что ещё ссылаются evidence/approvals.
- **Архив run:** терминальный run старше порога → экспорт пакета (`agent_messages`, `message_deliveries`, `chat_*`, `run_event_log`, `retrieval_*`, `directive_*`) в один artifact → удаление детальных строк. **Не удаляются:** `runs`, `tasks`, `task_attempts`, `gate_executions`, `test_results`, `findings`, `evidence`, `evidence_links`, `compliance_results`, `approvals`, `external_actions`, `pull_requests`, `agent_decisions` (трассировка ТЗ §13, §23 «аудит»).

### 20.3 Сроки хранения по умолчанию (**предложение**, настраивается `policies(policy_type='retention')`, утверждает владелец)

| Данные | Хранение online | Действие по истечении |
|---|---|---|
| `agent_activity_events` | 90 дней | архив партиции → drop |
| `run_event_log` | 30 дней после терминального статуса run | удаление (SSE-replay не нужен); копия — в архиве run |
| `agent_messages`, `message_deliveries`, `chat_threads`, `chat_messages`, `user_directives`/`directive_*` | 365 дней после терминального статуса run | архив run → удаление (кроме `user_directives`, которые остаются как трасса, но `chat_messages.body` заменяется ссылкой на архив) |
| `audit_events` | 400 дней | архив партиции (рекомендуется object lock/WORM bucket) → drop; без архива удаление запрещено |
| `outbox_events` | опубликованные — 2 полных месяца | drop партиции, только если в ней 0 неопубликованных; алерт на неопубликованные старше 15 мин |
| `processed_events` | 30 дней | удаление (окно > максимального окна повторной доставки Dramatiq) |
| `idempotency_keys` | до `expires_at` (24 ч) | удаление ежечасно |
| `user_sessions` | 30 дней после `expires_at`/`revoked_at` | удаление |
| `provider_health` | 30 дней | удаление (агрегаты — в Prometheus) |
| `github_webhook_deliveries` | 30 дней (+ payload-объекты) | удаление |
| `code_index_snapshots` (+ `code_files`/`code_symbols`/`code_dependencies`/`knowledge_chunks` каскадом) | последние 5 `READY` на репозиторий + все, на которые ссылаются `context_packs` неархивированных run; `FAILED`/`SUPERSEDED` — 7 дней | удаление |
| `documentation_snapshots` | `SUPERSEDED` — 90 дней, если не используются | удаление |
| `artifacts` по `retention_class` | `ephemeral` 7 дн.; `cli_raw_log` (sensitive) 30 дн.; `standard` 180 дн.; `evidence`/`archive` — без автоудаления | удаление объекта и строки, если нет FK-ссылок |
| `usage_metrics` | 365 дней | удаление |


### 20.4 Реализация (P3-18, миграция 0005, `aicd_platform.retention`)

- **Запуск:** `aicd maintenance retention [--reason …]` — подключение `aicd_maintenance` из `AICD_MAINTENANCE_DATABASE_URL(_FILE)`; одно соединение на весь проход. Расписание (ежедневно) задаёт оператор (cron/systemd timer). Код выхода 3 — есть предупреждения (алерт).
- **Сессия (§19.6):** `maint_begin(session_id, job, reason)` (только `current_user = aicd_maintenance`) пишет `maintenance.session_started` и выставляет `aicd.maint_session`; `maint_end` — `maintenance.session_finished` с итогами. Без сессии `maint_drop_partition` и `DELETE FROM run_event_log` отклоняются (`42501`); каждый drop партиции (`maintenance.partition_dropped`: партиция, ссылка на архив, sha256, число строк) и каждый удалённый пакет (`maintenance.rows_deleted`) аудируются в той же транзакции.
- **Партиции:** `maint_ensure_monthly_partitions` — `SECURITY DEFINER` (владелец таблиц `aicd_migrator`, `EXECUTE` только `aicd_maintenance`); job создаёт `retention_partitions_ahead_months` (3) месяцев вперёд и предупреждает, если впереди < 2. `maint_drop_partition(table, partition, cutoff, archive_ref, sha256)` — `SECURITY DEFINER`: имя по шаблону, партиция целиком до `cutoff`, `cutoff` ≤ начала текущего месяца; для `audit_events`/`agent_activity_events` без ссылки на архив и sha256 — отказ; для `outbox_events` — только при 0 неопубликованных (иначе `skipped_unpublished`). **Отличие от §20.2:** `DETACH` без `CONCURRENTLY` (невозможен в функции/транзакции; партиция старая, блокировка кратковременная, `lock_timeout = 5s`).
- **Архив:** NDJSON.gz (детерминированный, `mtime=0`) партиции через родителя по диапазону ключа → `FileSystemArchiveSink` (`retention_archive_root`, по умолчанию `<artifact_fs_root>/archive`): `archive/global/<table>/<YYYY-MM>.ndjson.gz`, атомарная запись и сверка sha256; только затем drop. **Отличие:** sha256 фиксируется в `audit_events`, строка `artifacts(kind='archive')` не создаётся (у `artifacts` обязателен `workspace_id`, а глобальная партиция аудита содержит несколько workspace); S3-приёмник — открытый вопрос.
- **Пакетное удаление** (`DELETE … WHERE ctid = ANY(ARRAY(SELECT ctid … LIMIT n))`, `retention_batch_size` = 5000, пауза `retention_batch_pause_seconds`): `run_event_log` терминальных run с `finished_at` старше срока (триггер append-only пропускает только `DELETE` от `aicd_maintenance` в сессии обслуживания; `audit_events` построчно неизменяем всегда), `processed_events`, `idempotency_keys` (истёкшие), `user_sessions`, `provider_health`. Дополнительно job считает неопубликованные outbox-события старше 15 мин (предупреждение).
- **Сроки:** `Settings.retention_*` (значения §20.3); активная **глобальная** политика `policies(policy_type='retention', workspace_id IS NULL)` переопределяет их ключами `run_event_log_days`, `agent_activity_days`, `agent_messages_days`, `audit_days` (≥ 30), `outbox_published_months`, `processed_events_days`, `user_sessions_days`, `provider_health_days`; некорректные значения пропускаются с предупреждением. Политики уровня workspace в v1 не применяются (партиции глобальные).
- **`agent_activity_events` (миграция 0006):** партиции создаются вперёд, истёкшие — архив NDJSON.gz (`archive/global/agent_activity_events/<YYYY-MM>.ndjson.gz`) → `maint_drop_partition`.
- **Архив run (миграция 0006):** терминальный run с `finished_at` старше `agent_messages_days` без строки `artifacts(kind='archive', run_id)` (она же маркер идемпотентности) → один NDJSON.gz (`archive/<workspace_id>/runs/<run_id>.ndjson.gz`, строка = `{"table", "row"}`: `chat_threads`, `chat_messages` без `search_vector`, `agent_messages`, `message_deliveries`, `user_directives`, `directive_impacts`, `directive_acknowledgements`) → в одной транзакции: `chat_messages` поручений — `body` = `[архив run: <ref>]`, прочие `chat_messages` удаляются, пустые `chat_threads` удаляются, `agent_messages` удаляются (`message_deliveries` — каскадом), кроме сообщений, на которые ссылаются сохраняемые строки (`agent_decisions`, `directive_acknowledgements`, оставшиеся `chat_messages`, ответы из других run) и их цепочек `reply_to`; строка `artifacts(kind='archive', retention_class='archive')`; аудит `maintenance.run_archived` (sha256, число строк). **Отличия от §20.2–20.3:** `user_directives`/`directive_*` только архивируются, не удаляются; удаление — одним оператором на run, а не пакетами по 5–10 тыс. (объём run ограничен); `retrieval_*` — Phase 5. Права `aicd_maintenance`: `DELETE` на `agent_messages`, `chat_messages`, `chat_threads`, `UPDATE (body)` на `chat_messages`, `INSERT` на `artifacts`.
- **Не реализовано:** `code_index_snapshots`, `documentation_snapshots`, `artifacts` по `retention_class`, `usage_metrics`, `github_webhook_deliveries` — в фазах, где появляются их потребители.

---

## 21. Стратегия миграций

### 21.1 Инструмент и организация
- Alembic, каталог `infrastructure/migrations/` (ТЗ §21); миграции пишутся явно (`op.execute`/операции Alembic). `autogenerate` используется только как **проверка дрейфа** моделей и схемы в CI (`alembic check`), не как источник миграций.
- Линейная история: CI проверяет `alembic heads` = 1. Имя ревизии: `YYYYMMDD_HHMM_<slug>`.
- Таблицы создаются в порядке зависимостей; FK на таблицу из более поздней фазы (например, `repositories → github_installations`, `project_settings → policies`, `tasks/approvals → …`) добавляется `ALTER TABLE … ADD CONSTRAINT … NOT VALID` + `VALIDATE CONSTRAINT` в миграции, создающей целевую таблицу. Распределение по фазам: P1 — IAM, projects, platform-таблицы (`outbox_events`, `audit_events`, `idempotency_keys`, `artifacts`, `processed_events`, `allowed_transitions`); P2 — CLI, `worker_hosts`, `worktrees`; P3 — orchestration, approvals, policies, checkpoints, leases, ledger, `run_event_log`; P4 — ACP/chat/directives; P5 — code intelligence, документация; P6 — quality/evidence/standards; P8 — GitHub.
- Seed-данные (`cli_providers`, `agent_profiles`, `allowed_transitions`, глобальные `quality_profiles`/`quality_gates`/`policies`, каталог `standards`/`standard_controls`) — отдельные data-миграции с идемпотентным `INSERT … ON CONFLICT DO UPDATE`.

### 21.2 Expand / contract
Несовместимые изменения выполняются в несколько релизов: **expand** (добавить колонку/таблицу nullable или с default, двойная запись, backfill пакетами) → переключение чтения в коде → **contract** (удаление старого) не ранее следующего релиза. Пример переименования колонки: `ADD new` → триггер/код двойной записи → backfill → чтение из `new` → `DROP old`. Правила безопасности онлайн-миграций:
- `SET lock_timeout = '5s'`, `statement_timeout` на миграцию; повтор при таймауте блокировки.
- Индексы — `CREATE INDEX CONCURRENTLY` (в autocommit-блоке Alembic); на партиционированных — по партициям + `ATTACH`.
- Ограничения — `ADD CONSTRAINT … NOT VALID` + `VALIDATE CONSTRAINT` отдельным шагом.
- Расширение перечисления — `ALTER DOMAIN … ADD CONSTRAINT … NOT VALID` → `VALIDATE` → `DROP` старого ограничения (§1.4); сужение — только contract-фазой после проверки отсутствия значений.
- Никаких `ADD COLUMN … DEFAULT <volatile>` и прочих перезаписей больших таблиц в одной транзакции.

### 21.3 Обратимость
Каждая миграция реализует `downgrade()`. Если откат невозможен без потери данных (удаление колонки с данными, необратимое преобразование), миграция помечается `irreversible = True` и в docstring содержит обоснование и план восстановления (из бэкапа / из архива); CI отклоняет миграцию с `downgrade()`, бросающим исключение, без такой пометки (ТЗ §7.1).

### 21.4 Тесты миграций (CI, PostgreSQL 17 в контейнере)
1. `upgrade head` на пустой БД; `downgrade base`; «лестница»: для каждой ревизии `upgrade → downgrade → upgrade`.
2. Seeded-БД: фикстура с двумя workspace и представительными данными на ревизии N−1 → `upgrade` → проверки сохранности/backfill.
3. Schema-lint: именование (`pk_/fk_/uq_/ck_/ix_`), все FK-колонки покрыты индексом (с ведущими колонками), каждая таблица с `workspace_id` — `ENABLE`+`FORCE RLS` и политика (§19.4), запрет секретных имён колонок (§18.9), значения доменов перечислений совпадают с `packages/contracts` enums (генерируются из README §3), каждая таблица принадлежит ровно одному модулю (`OWNED_TABLES`, MODULE_BOUNDARIES §5), `allowed_transitions` = `transitions.py`.
4. RLS-тесты (§19.7) и тесты инвариантов (§18: цикл в DAG, межrun-зависимость, COMPLETED без PASS, самоутверждение, независимость ревью, убывание fencing token, UPDATE `audit_events`) — negative tests.
5. `alembic check` — отсутствие дрейфа.

### 21.5 Dry-run и выкатка (ТЗ §16)
- `aicd migrate --dry-run`: генерация SQL (`alembic upgrade --sql`) для ревью + прогон на восстановленной копии последнего бэкапа (staging) с замером длительности и блокировок; отчёт прикладывается к релизу.
- Перед `upgrade` в production — обязательный свежий бэкап и проверка его восстановимости (runbook P9).
- Миграции выполняет отдельный job под `aicd_migrator` до запуска новых версий процессов; совместимость N/N−1 обеспечивается expand/contract.

---

## 22. Предлагаемые дополнения / открытые вопросы

| ID | Вопрос / предложение | Решение в этом документе | Требует |
|---|---|---|---|
| OQ-DB-01 | Локальные перечисления §3.2 отсутствуют в README §3 | Значения предложены здесь | Внести в README §3 (или признать локальными) |
| OQ-DB-02 | ADR-0007 описывает FSM только в коде | Добавлен защитный слой `allowed_transitions` + триггер (§18.3); переходы **сверить с DATA_FLOW.md** | Утверждение архитектора; синхронизация списков |
| OQ-DB-03 | Семантика `findings.source_gate_id` в ТЗ | = `quality_gates.id` (определение); запуск — `gate_execution_id`; согласованность составным FK | Подтверждение |
| OQ-DB-04 | `user_directives.impact_status` (ТЗ) дублирует `status`? | Хранит агрегат `directive_impact_type`; ход обработки — `status` | Подтверждение или переименование в `impact_type` |
| OQ-DB-05 | Документационный кэш: глобальный (ТЗ §12 «централизованно») или per-workspace | В v1 per-workspace (изоляция, разные решения DOCS_DOWNLOAD, защита от отравления кэша); дедупликация объектов по sha256 на уровне хранилища | Решение владельца |
| OQ-DB-06 | `cli_sessions` глобальные (бриф) — риск смешения контекста арендаторов | Запрет переиспользования сессии между workspace в CLI Gateway; альтернатива — сделать таблицу tenant | Решение архитектора |
| OQ-DB-07 | Права уровня проекта (ТЗ §15) | `project_member_overrides`, только понижение (бриф) | Подтверждение |
| OQ-DB-08 | `COMPLETED` требует ≥ 1 `PASS` gate — как быть с run `AUDIT`/`DOCUMENT` без кода | Профили обязаны содержать всегда применимый gate (например, `REVIEW`) | Подтверждение (QUALITY_GATES.md) |
| OQ-DB-09 | Модель ролей брифа не покрывает межарендаторные выборки воркеров | NOLOGIN `aicd_sysfn` + `sys_*` функции; S-класс (`outbox_events`, `worker_leases`) с политикой «все строки» для `aicd_worker`. Вариант усиления — отдельная роль `aicd_dispatcher` только для outbox | Утверждение (SECURITY_ARCHITECTURE.md) |
| OQ-DB-10 | Горячая строка `runs` (`last_event_seq` по ADR-0011 + `FOR SHARE` из §18.4) | Оставлено по ADR-0011; при измеренной конкуренции — вынести счётчик в `run_event_counters(run_id, last_seq)` | Замер в P3 |
| OQ-DB-11 | Партиционирование `agent_messages`, `run_event_log` | Не в v1 (FK/уникальность); retention пакетами | Пересмотр после нагрузочного теста P9 |
| OQ-DB-12 | Значения `approval_policy` (в ТЗ только пример `risk_based`), default `quality_profile` | `risk_based`/`always`/`critical_only` (как в openapi.yaml); default профиль `strict` | Решение владельца |
| OQ-DB-13 | Сроки retention §20.3 | Предложения | Утверждение владельцем |
| OQ-DB-14 | Tamper-evidence аудита | Опционально: hash-chain `prev_hash`/`row_hash` (sha256 канонического JSON строки) в P9 | Решение |
| OQ-DB-15 | Кто управляет глобальными справочниками (`standards`, `technologies`, `agent_profiles`, `cli_installations`) — роли RBAC workspace-scoped | v1: seed-миграции + `aicd` admin CLI; API — только чтение; «platform admin» не вводится | Решение владельца |
| OQ-DB-16 | Конфигурация FTS | `simple` (смешанный ru/en); альтернатива — две колонки `russian`/`english` | Решение после пилотных данных |
| OQ-DB-17 | Значения `gate_type` | Предложены по ТЗ §14 в формате `QG-<TYPE>` | Синхронизировать с QUALITY_GATES.md |
| OQ-DB-18 | `sender_role = 'orchestrator'`, `recipient_role = 'user'` расширяют `agent_role` в ACP | Добавлены CHECK-ом таблицы | Синхронизировать с AGENT_COMMUNICATION_PROTOCOL.md |
| OQ-DB-19 | Отступления от полей каталога ТЗ | `approvals.run_id` nullable + `project_id`; `artifacts.project_id` nullable; `routing_decisions` без `task_attempt_id` (связь через `task_attempts.routing_decision_id`); `agent_decisions.decided_by` → `decided_by_type/role/user_id`; `user_directives.target` → типизированные колонки; `chat_messages.sender_id` + `sender_role`; `agent_activity_events` PK `(id, occurred_at)` из-за партиционирования | Подтверждение |
| OQ-DB-20 | Добавленные таблицы вне брифа | `project_member_overrides`, `allowed_transitions`, `code_files` | Подтверждение |
