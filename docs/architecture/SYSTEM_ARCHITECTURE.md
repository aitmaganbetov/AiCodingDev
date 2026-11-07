# SYSTEM_ARCHITECTURE — AiCodingDev v1.0

**Статус:** Proposed (Phase 0). **Основание:** ТЗ §1–3, §16–17, §22; ADR-0001…0014.
Нотация: C4 (Context → Container → Component) в Mermaid.

## 1. Назначение и границы системы

AiCodingDev — self-hosted / local-first платформа управляемой AI-разработки. Пользователь формулирует задачу (run), платформа декомпозирует её в DAG задач, назначает 12 логических ролей, исполняет их **только** через официальные Codex CLI и Claude Code CLI в изолированных worktree, независимо проверяет результат (quality gates + evidence), показывает межагентное общение в Live Agent Chat и запрашивает human approval для опасных действий.

**Не входит в v1.0** (ТЗ §1): собственная LLM, AI API, автономный production deploy, безусловный auto-merge, обязательная векторная БД, обязательный Kubernetes, официальная ISO-сертификация.

## 2. Контекст (C4 Level 1)

```mermaid
flowchart LR
  user([Разработчик / Reviewer / Admin]) -->|HTTPS, браузер| AICD[AiCodingDev]
  operator([Оператор хоста]) -->|"штатный login CLI (однократно)"| CRED[(Учётные данные CLI\nпо подпискам)]
  AICD -->|запуск процессов| CODEX[OpenAI Codex CLI]
  AICD -->|запуск процессов| CLAUDE[Anthropic Claude Code CLI]
  CODEX -->|HTTPS через egress proxy| OAI[(Сервис OpenAI\nпо подписке)]
  CLAUDE -->|HTTPS через egress proxy| ANT[(Сервис Anthropic\nпо подписке)]
  CODEX -.-> CRED
  CLAUDE -.-> CRED
  AICD <-->|Git CLI| LREPO[(Локальные Git-репозитории)]
  AICD <-->|GitHub App, webhooks| GH[(GitHub)]
  AICD -->|"опционально, SMART/STRICT + approval"| DOCS[(Внешняя документация)]
```

Платформа сама **не вызывает** AI API: сетевой трафик к провайдерам генерирует только официальный CLI внутри runner-контейнера.

## 3. Контейнеры (C4 Level 2)

```mermaid
flowchart TB
  subgraph Edge
    RP[Reverse proxy\nCaddy/nginx, TLS]
    WEB[Web SPA\nReact+TS+Vite+Tailwind]
  end
  subgraph Backend["Backend (единый образ, разные entrypoints)"]
    API[api\nFastAPI REST + SSE]
    ORCH[worker-orchestrator\nFSM, DAG, Router, ACP Broker,\nDirectives, Recovery]
    OUT[worker-outbox\nOutbox dispatcher]
    EXEC[worker-exec\nCLI Gateway]
    VER[worker-verify\nVerification Engine]
    IDX[worker-index\nCode Intelligence, Docs importer]
  end
  subgraph Data
    PG[(PostgreSQL 17\nsource of truth)]
    RD[(Redis 7\nDramatiq broker, pub/sub)]
    OBJ[(MinIO / FS adapter\nартефакты)]
  end
  subgraph Isolation
    DSP[docker-socket-proxy]
    RUN[runner-контейнеры\ncodex / claude]
    VCT[verification-контейнеры]
    EGR[egress-proxy\nallowlist]
  end
  subgraph Observability["Observability (P1)"]
    OTEL[OTel Collector] --> PROM[Prometheus] --> GRAF[Grafana]
  end
  RP --> WEB
  RP --> API
  API --> PG
  API --> RD
  API --> OBJ
  ORCH --> PG
  OUT --> PG
  OUT --> RD
  RD --> ORCH & EXEC & VER & IDX
  EXEC --> DSP --> RUN
  VER --> DSP --> VCT
  RUN --> EGR
  IDX --> EGR
  EXEC & VER & IDX --> OBJ
  EXEC & VER & IDX --> PG
```

| Контейнер | Ответственность | Масштабирование | Доступы |
|---|---|---|---|
| `reverse-proxy` | TLS, статика SPA, маршрутизация `/api`, SSE буферизация off | 1 | — |
| `api` | REST v1, SSE, AuthN/AuthZ, Idempotency, rate limit, приём директив/approvals | N реплик (stateless) | PG (`aicd_app`), Redis, MinIO (presign) |
| `worker-orchestrator` | FSM-переходы, DAG scheduler, Router, ACP Broker, impact analysis, таймеры, recovery reconciler | N (конкуренция через leases/SKIP LOCKED) | PG (`aicd_worker`), Redis |
| `worker-outbox` | Публикация outbox → Dramatiq + `run_event_log` + pub/sub | 1–2 | PG, Redis |
| `worker-exec` | CLI Gateway: probe, auth check, запуск runner, стрим событий, сбор результата | по хостам с CLI; лимит параллелизма на провайдер | PG, Redis, MinIO, docker-socket-proxy |
| `worker-verify` | Quality gates в verification-контейнерах, evidence | N | PG, MinIO, docker-socket-proxy |
| `worker-index` | Индексация, Context Packs, импорт документации | N | PG, MinIO, egress-proxy (только docs) |
| `postgres` | Состояние, FTS, pg_trgm, RLS | 1 (+ реплика/бэкап) | — |
| `redis` | Брокер Dramatiq, pub/sub, rate-limit | 1 | — |
| `minio` | Артефакты (diff, логи, отчёты, context packs, evidence) | 1 | — |
| `egress-proxy` | Allowlist исходящих соединений runner/docs | 1 | Интернет |
| `docker-socket-proxy` | Ограниченный Docker API для exec/verify | 1 на хост | Docker daemon |

## 4. Компоненты backend (C4 Level 3)

Соответствуют каталогу `packages/` (ТЗ §17); детали и владение таблицами — [MODULE_BOUNDARIES.md](MODULE_BOUNDARIES.md).

```mermaid
flowchart LR
  subgraph apps/api
    R[Routers v1] --> SVC[Application services]
    SSE[SSE endpoint]
  end
  SVC --> IAM[identity\nusers, sessions, RBAC]
  SVC --> PRJ[projects\nworkspaces, repos, settings]
  SVC --> ORC[orchestration\nruns, tasks, DAG, FSM, router,\napprovals, directives, recovery]
  SVC --> CHAT[collaboration\nACP broker, chat, directives intake]
  ORC --> AR[agent_runtime\n12 профилей, шаблоны инструкций]
  ORC --> CLIA[cli_adapters\ncodex, claude, fake]
  ORC --> CI[code_intelligence\nindex, retrieval, context packs, docs]
  ORC --> VER[verification\ngates, evidence]
  VER --> STD[standards\nregistry, compliance]
  CLIA --> SEC[security\nsandbox policy, redaction, egress]
  VER --> SEC
  ALL[platform\ndb, outbox, artifacts, audit, telemetry, contracts]
```

## 5. Ключевые сквозные решения

| Тема | Решение | ADR |
|---|---|---|
| Стиль | Modular monolith + workers | 0001 |
| Состояние | PostgreSQL source of truth; outbox; Dramatiq/Redis at-least-once | 0002 |
| Исполнители | Только официальные CLI, runtime probe, compat matrix | 0003 |
| Коммуникация агентов | ACP через брокер, evidence-first, approvals | 0004 |
| Знания | Обязательный локальный индекс, docs OFF/SMART/STRICT | 0005 |
| Изоляция | Worktree на задачу, runner-контейнеры, egress allowlist | 0006, 0010 |
| Workflow | Собственный durable engine на PG (FSM+DAG+leases+checkpoints) | 0007 |
| Данные/тенанты | UUIDv7, text+CHECK, workspace_id везде, RLS | 0008 |
| Вход | Локальные аккаунты, server-side сессии, CSRF | 0009 |
| Realtime | SSE из `run_event_log` с Last-Event-ID | 0011 |
| Побочные эффекты | `external_actions` ledger | 0012 |
| Контракты | `packages/contracts` единый источник, codegen, oasdiff | 0013 |
| Frontend | React+TS+Vite+Tailwind, TanStack Query, React Flow, Monaco | 0014 |

## 6. Сквозной сценарий (ТЗ §19, AC-01…AC-05)

```mermaid
sequenceDiagram
  autonumber
  actor U as Пользователь
  participant API
  participant ORCH as Orchestrator
  participant IDX as Code Intelligence
  participant EX as CLI Gateway
  participant RUN as Runner (Codex)
  participant RUN2 as Runner (Claude)
  participant VER as Verification
  U->>API: POST /runs (Idempotency-Key)
  API->>ORCH: Run CREATED (+outbox)
  ORCH->>IDX: индекс snapshot(commit), стек
  ORCH->>EX: planner task (L по сложности)
  EX->>RUN2: planner CLI → план (ACP HANDOFF)
  ORCH->>ORCH: DAG валидация (ацикличность) → PLANNED
  ORCH-->>U: approval PLAN_APPROVAL (если policy)
  U->>API: approve
  ORCH->>EX: developer task → Codex (Router)
  EX->>RUN: CLI в worktree (Context Pack ro)
  RUN-->>EX: diff + claims (UNVERIFIED)
  ORCH->>VER: gates на commit SHA
  VER-->>ORCH: evidence PASS/FAIL
  ORCH->>EX: reviewer task → Claude (другой провайдер)
  RUN2-->>ORCH: ACP FINDING / FIX_REQUEST или approve
  ORCH-->>U: SSE: agent.message, task.state_changed, gate.completed
  ORCH->>ORCH: все обязательные gates PASS + approvals → COMPLETED
```

## 7. Развёртывание

**Профиль `local` (по умолчанию):** `docker compose` на одном хосте (Linux; macOS/Windows через Docker Desktop — с ограничениями OQ-03). Все сервисы из §3; бэкенд-процессы — один образ.

**Профиль `server`:** тот же compose / systemd на выделенном Linux-хосте; PostgreSQL может быть внешним; несколько `worker-exec` хостов регистрируются в `worker_hosts`.

**Kubernetes** — не требуется (ТЗ §1); Helm chart — вне v1.0.

```mermaid
flowchart LR
  subgraph Host["Linux host (docker compose)"]
    direction TB
    rp[reverse-proxy :443] --> api
    api --- pg[(postgres)] & rd[(redis)] & mn[(minio)]
    wo[worker-orchestrator] & wx[worker-exec] & wv[worker-verify] & wi[worker-index] & wout[worker-outbox]
    wx & wv --> dsp[docker-socket-proxy] --> dockerd[(dockerd / rootless)]
    dockerd --> runners[runner/verification containers]
    runners --> egr[egress-proxy]
    vol[(volume aicd-cred-codex\nvolume aicd-cred-claude)] -.only runner.-> runners
  end
```

Сети compose: `edge` (proxy↔api), `backend` (api/workers↔data), `sandbox` (runners↔egress-proxy, `internal: true` для прямого выхода).

## 8. Качественные атрибуты и тактики

| Атрибут | Тактики | Требования |
|---|---|---|
| Надёжность | outbox, идемпотентные consumers, leases+fencing, checkpoints, ledger внешних действий, reconciler | NFR-REL-* |
| Безопасность | RBAC+RLS, sandbox, egress allowlist, redaction, approvals, four-eyes, SSRF-защита | NFR-SEC-* |
| Наблюдаемость | OTel traces с `run_id/task_id/execution_id`, структурные JSON-логи, метрики ТЗ §16 | NFR-OBS-* |
| Поддерживаемость | границы модулей + import-linter, контракты-first, ADR, миграции с downgrade | NFR-MAINT-* |
| Производительность | SKIP LOCKED очереди, partial indexes, инкрементальная индексация, SSE fan-out через pub/sub | NFR-PERF-* (SLO после baseline) |
| Переносимость | docker compose, отсутствие облачных зависимостей, режим docs OFF офлайн | NFR-PORT-* |
| Доступность UI | клавиатура, ARIA, пустые/ошибочные состояния, responsive | NFR-UX-* |

## 9. Ограничения и допущения

1. Возможности CLI (машинный вывод, usage, resume) определяются probe; архитектура не предполагает конкретных флагов.
2. Параллелизм ограничен лимитами подписок; Router учитывает наблюдаемые `RATE_LIMITED` и конфигурируемый `max_concurrent_sessions` на установку.
3. Целевая ОС хостов исполнения v1.0 — Linux x86_64/arm64 (OQ-03 для macOS/Windows).
4. SLO производительности не заявляются до baseline-измерений (ТЗ §23).
