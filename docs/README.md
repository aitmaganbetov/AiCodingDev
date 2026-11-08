# AiCodingDev v1.0 — документация Phase 0

**Статус:** Phase 0 утверждена 2026-11-07; Phase 1 (Project Foundation) — реализация, см. `planning/IMPLEMENTATION_BACKLOG.md`.
**Source of Truth требований:** [`/AiCodingDev_v1_TZ.md`](../AiCodingDev_v1_TZ.md) (далее — ТЗ). При расхождении документа Phase 0 с ТЗ приоритет у ТЗ, кроме пунктов, явно помеченных `DEVIATION` и вынесенных в [`planning/OPEN_QUESTIONS.md`](planning/OPEN_QUESTIONS.md) на утверждение.

> Статус реализации ведётся только в `planning/IMPLEMENTATION_BACKLOG.md` (с evidence). Проектные документы описывают целевое состояние.

## 1. Карта документов

| Каталог | Документ | Назначение |
|---|---|---|
| `architecture/` | [SYSTEM_ARCHITECTURE.md](architecture/SYSTEM_ARCHITECTURE.md) | Контекст, контейнеры, компоненты, deployment, ключевые решения |
| | [MODULE_BOUNDARIES.md](architecture/MODULE_BOUNDARIES.md) | Модули монолита, их публичные интерфейсы, владение таблицами, правила зависимостей |
| | [DATA_FLOW.md](architecture/DATA_FLOW.md) | Потоки данных, state machines Run/Task/Execution/Directive/Approval, DAG Scheduler, retry/checkpoint/recovery/idempotency |
| | [SECURITY_ARCHITECTURE.md](architecture/SECURITY_ARCHITECTURE.md) | Зоны доверия, AuthN/AuthZ, RBAC, sandbox, секреты, RLS |
| | [adr/](architecture/adr/) | Architecture Decision Records (ADR-0001…) |
| | [data-model.md](architecture/data-model.md), [state-machines.md](architecture/state-machines.md), [permissions-matrix.md](architecture/permissions-matrix.md) | Указатели путей ТЗ §21 на нормативные разделы |
| `runbooks/` | [README.md](runbooks/README.md) | Эксплуатационные инструкции: установка, восстановление, бэкапы, CLI auth, инциденты, GitHub App, ротация секретов, обновления, релиз (P9-06) |
| `release/` | [RELEASE_CHECKLIST.md](release/RELEASE_CHECKLIST.md) | Release checklist v1.0 (ТЗ §23, RG-01…RG-15) со ссылками на доказательства (P9-09, черновик) |
| `database/` | [DATABASE_DESIGN.md](database/DATABASE_DESIGN.md) | Полная логическая схема PostgreSQL: таблицы, ограничения, индексы, RLS, retention |
| | [ER_DIAGRAM.md](database/ER_DIAGRAM.md) | ER-диаграммы по доменам (Mermaid), кардинальности, инварианты |
| `api/` | [API_DESIGN.md](api/API_DESIGN.md) | Правила REST API v1: ошибки, пагинация, idempotency, ETag, SSE, RBAC |
| | [openapi.yaml](api/openapi.yaml) | Символьная ссылка на единственный источник `packages/contracts/openapi.yaml` (ADR-0013) |
| `protocols/` | [CLI_ADAPTER_CONTRACT.md](protocols/CLI_ADAPTER_CONTRACT.md) | CLI Execution Contract v1.0, probe, нормализованные события и ошибки |
| | [AGENT_COMMUNICATION_PROTOCOL.md](protocols/AGENT_COMMUNICATION_PROTOCOL.md) | ACP v1.0: конверт, типы, payload-схемы, ACL, доставка |
| | [EVENT_SCHEMAS.md](protocols/EVENT_SCHEMAS.md) | Доменные/outbox/SSE события, контракты Live Agent Chat |
| `agents/` | [AGENT_ROLES.md](agents/AGENT_ROLES.md) | 12 логических ролей: вход/выход, права, инструменты, ACL ACP |
| | [ROUTING_POLICY.md](agents/ROUTING_POLICY.md) | Intelligent Router, уровни L1–L5, scoring, fallback |
| | [QUALITY_GATES.md](agents/QUALITY_GATES.md) | Профили basic/strict/critical, evidence, Standards Registry |
| `security/` | [THREAT_MODEL.md](security/THREAT_MODEL.md) | STRIDE по зонам доверия, abuse cases |
| | [SECURITY_CONTROLS.md](security/SECURITY_CONTROLS.md) | Каталог контролей с привязкой к ASVS/SSDF/угрозам и тестам |
| `planning/` | [REQUIREMENTS_REGISTRY.md](planning/REQUIREMENTS_REGISTRY.md) | Реестр FR/NFR с трассировкой на ТЗ (дополнительный документ) |
| | [DEVELOPMENT_ROADMAP.md](planning/DEVELOPMENT_ROADMAP.md) | Фазы 0–9: задачи, зависимости, результаты, критерии завершения |
| | [IMPLEMENTATION_BACKLOG.md](planning/IMPLEMENTATION_BACKLOG.md) | Backlog задач (ID, требование, статус, зависимости, DoD) |
| | [DEPENDENCY_GRAPH.md](planning/DEPENDENCY_GRAPH.md) | Граф зависимостей модулей и фаз |
| | [ACCEPTANCE_CRITERIA.md](planning/ACCEPTANCE_CRITERIA.md) | Критерии приёмки: AC-01…AC-16 из ТЗ §19 + критерии фаз |
| | [OPEN_QUESTIONS.md](planning/OPEN_QUESTIONS.md) | Противоречия, риски, вопросы на утверждение |

## 2. Соглашения по идентификаторам

| Префикс | Объект | Пример |
|---|---|---|
| `FR-<DOMAIN>-NN` | Функциональное требование | `FR-ORCH-03` |
| `NFR-<AREA>-NN` | Нефункциональное требование | `NFR-SEC-05` |
| `AC-NN` | Приёмочный сценарий ТЗ §19 (1…16) | `AC-06` |
| `ADR-NNNN` | Architecture Decision Record | `ADR-0007` |
| `P<phase>-<NN>` | Задача backlog | `P3-04` |
| `OQ-NN` | Открытый вопрос | `OQ-05` |
| `RISK-NN` | Технический риск | `RISK-02` |
| `TH-NN` | Угроза (threat model) | `TH-12` |
| `SC-<AREA>-NN` | Security control | `SC-SBX-03` |
| `QG-<TYPE>` | Тип quality gate | `QG-SAST` |

Домены FR: `IAM` (identity/RBAC/tenancy), `PRJ` (projects/repos), `ORCH` (runs/tasks/DAG/FSM), `ROUTE` (router), `AGENT` (12 ролей), `ACP`, `CLI` (gateway/adapters), `SBX` (sandbox/worktrees), `CHAT` (Mission Control/Live Chat/directives), `CI` (code intelligence), `DOC` (documentation RAG), `QG` (quality gates/evidence), `STD` (standards/compliance), `APR` (approvals), `GH` (GitHub), `OBS` (observability/analytics), `AUD` (audit), `API`.

## 3. Канонические перечисления (обязательны для всех документов, схем и кода)

Значения хранятся в БД как `text` + `CHECK` (не PostgreSQL `ENUM`, чтобы расширять без блокирующих миграций — см. ADR-0008).

| Перечисление | Значения |
|---|---|
| `run_mode` | `CREATE`, `DEVELOP`, `DEBUG`, `AUDIT`, `MODERNIZE`, `SECURITY`, `OPTIMIZE`, `TEST`, `MAINTAIN`, `DOCUMENT`, `REVIEW` |
| `run_status` | `CREATED`, `DISCOVERY`, `PLANNED`, `AWAITING_APPROVAL`, `RUNNING`, `VERIFYING`, `COMPLETED`, `PAUSED`, `BLOCKED`, `FAILED`, `CANCELLED` |
| `task_status` | `PENDING`, `READY`, `LEASED`, `RUNNING`, `VERIFYING`, `SUCCEEDED`, `BLOCKED`, `RETRY_WAIT`, `FAILED`, `CANCELLED` |
| `task_blocked_reason` | `APPROVAL_REQUIRED`, `PROVIDER_UNAVAILABLE`, `AUTH_REQUIRED`, `DIRECTIVE_REPLAN`, `RUN_PAUSED`, `DOCS_REQUIRED`, `MANUAL_REVIEW`, `REVIEW_LOOP_EXHAUSTED`, `DEPENDENCY_FAILED` |
| `execution_status` | `QUEUED`, `STARTING`, `RUNNING`, `EXITED`, `COLLECTED`, `TIMED_OUT`, `CANCELLED`, `CRASHED`, `AUTH_REQUIRED`, `RATE_LIMITED` |
| `cli_error_code` | `CLI_NOT_INSTALLED`, `AUTH_REQUIRED`, `RATE_LIMITED`, `TIMEOUT`, `SANDBOX_DENIED`, `PROCESS_FAILED`, `OUTPUT_PARSE_ERROR`, `UNSUPPORTED_VERSION`, `CANCELLED` |
| `failure_category` (task_attempts) | `cli_error_code` ∪ `GATE_FAILED`, `REVIEW_REJECTED`, `LEASE_LOST`, `WORKER_CRASH`, `POLICY_DENIED`, `INTERNAL_ERROR` |
| `cli_provider` | `codex`, `claude` |
| `agent_role` | `requirements`, `architect`, `planner`, `developer`, `reviewer`, `security`, `qa`, `database`, `performance`, `devops`, `maintenance`, `documentation` |
| `complexity_level` | `L1`…`L5` (в БД `tasks.complexity` smallint 1..5) |
| `risk_level` | `low`, `medium`, `high`, `critical` |
| `workspace_role` (RBAC) | `owner`, `admin`, `developer`, `reviewer`, `viewer` |
| `acp_message_type` | `TASK_ASSIGN`, `HANDOFF`, `QUESTION`, `ANSWER`, `REVIEW_REQUEST`, `FINDING`, `FIX_REQUEST`, `EVIDENCE_SUBMITTED`, `DECISION_PROPOSAL`, `DECISION_RECORDED`, `BLOCKED`, `TASK_COMPLETED` |
| `directive_type` (чат-команды) | `ASK`, `NEW_TASK`, `CHANGE_REQUEST`, `STOP`, `PAUSE`, `RESUME`, `REVIEW`, `APPROVE`, `REJECT` |
| `directive_status` | `RECEIVED`, `IMPACT_ANALYSIS_PENDING`, `IMPACT_READY`, `AWAITING_CONFIRMATION`, `CONFIRMED`, `CHECKPOINTING`, `REPLANNING`, `DISPATCHED`, `ACKNOWLEDGED`, `COMPLETED`, `REJECTED`, `CANCELLED`, `FAILED` |
| `directive_impact_type` | `NONE`, `CONTEXT_ONLY`, `REPLAN_REQUIRED`, `TASK_CANCEL`, `TASK_ADD`, `RESTART_REQUIRED` |
| `approval_action_type` | `PLAN_APPROVAL`, `MERGE`, `PRODUCTION_DEPLOY`, `DESTRUCTIVE_MIGRATION`, `DATA_DELETION`, `SECRET_CHANGE`, `UNRESTRICTED_NETWORK`, `DOCS_DOWNLOAD`, `CRITICAL_DIRECTIVE`, `MANUAL_REVIEW`, `SELF_REVIEW_FALLBACK`, `LOCAL_EXECUTION_FALLBACK`, `VULN_DB_UPDATE` (0016) |
| `approval_status` | `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `CANCELLED` |
| `documentation_mode` | `OFF`, `SMART` (default), `STRICT` |
| `quality_profile` | `basic`, `strict`, `critical` |
| `gate_execution_status` | `PENDING`, `RUNNING`, `PASS`, `FAIL`, `ERROR`, `SKIPPED`, `NOT_APPLICABLE` |
| `compliance_status` | `PASS`, `FAIL`, `NOT_APPLICABLE`, `UNVERIFIED`, `MANUAL_REVIEW` |
| `finding_severity` | `info`, `low`, `medium`, `high`, `critical` |
| `finding_status` | `OPEN`, `ACKNOWLEDGED`, `FIXED`, `VERIFIED_FIXED`, `FALSE_POSITIVE`, `ACCEPTED_RISK` |
| `evidence_verification_status` | `PENDING`, `VERIFIED`, `REJECTED`, `UNVERIFIED` |
| `auth_status` (CLI) | `UNKNOWN`, `AUTHENTICATED`, `AUTH_REQUIRED`, `EXPIRED`, `ERROR` |
| `provider_availability` | `AVAILABLE`, `DEGRADED`, `RATE_LIMITED`, `UNAVAILABLE` |
| `normalized_cli_event` | `started`, `output`, `tool_event`, `artifact`, `warning`, `finished` |
| `sse_event_type` | `run.state_changed`, `task.state_changed`, `agent.message`, `cli.output`, `directive.impact_ready`, `directive.state_changed`, `approval.requested`, `approval.decided`, `gate.completed`, `run.completed`, `chat.message`, `heartbeat` |

## 4. Общие проектные соглашения

- **Язык документации:** русский; идентификаторы, перечисления, имена таблиц/полей/эндпоинтов — английские.
- **Время:** `timestamptz`, UTC, ISO 8601 с `Z` в API.
- **ID:** UUID v7 (сортируемые по времени) генерируются приложением; внешние ID (GitHub, PID, session_ref) — отдельные колонки.
- **Версии контрактов:** HTTP API — `/api/v1`; ACP — `acp/1.0`; CLI — `contract_version: "1.0"`; события — `schema_version: "1.0"`. SemVer: minor — только аддитивные изменения.
- **Канонические пути контрактов (решение Phase 0, см. OQ-02):** на Phase 0 спецификации лежат в `docs/`; в Phase 1 (задача P1-03) машиночитаемые контракты переносятся в `packages/contracts/` как единственный источник, а `docs/api/openapi.yaml` заменяется ссылкой — дублирование запрещено.
- **Статусы задач backlog:** `TODO`, `IN_PROGRESS`, `IN_REVIEW`, `BLOCKED`, `DONE` (только с evidence: commit SHA + команда проверки + результат).
