# Architecture Decision Records

Формат: Michael Nygard ADR (Контекст → Решение → Альтернативы → Последствия → Связанные требования).
Статусы: `Proposed` → `Accepted` (после утверждения владельцем продукта) → `Superseded by ADR-XXXX` / `Deprecated`.
ADR-0001…0006 зафиксированы в ТЗ §22 как утверждённые направления; в Phase 0 они детализированы. ADR-0007…0013 — новые решения Phase 0, ожидают утверждения (см. `planning/OPEN_QUESTIONS.md`).

| ADR | Название | Статус |
|---|---|---|
| [0001](ADR-0001-modular-monolith-workers.md) | Modular Monolith + Workers | Accepted (ТЗ §22) |
| [0002](ADR-0002-postgresql-redis-outbox.md) | PostgreSQL source of truth, Redis/Dramatiq, transactional outbox | Accepted (ТЗ §22) |
| [0003](ADR-0003-official-cli-only.md) | Только официальные Codex CLI и Claude Code CLI | Accepted (ТЗ §22) |
| [0004](ADR-0004-centralized-acp-evidence-approvals.md) | Централизованный ACP, evidence-first, human approvals | Accepted (ТЗ §22) |
| [0005](ADR-0005-code-intelligence-optional-docs.md) | Обязательный индекс кода, документация OFF/SMART/STRICT | Accepted (ТЗ §22) |
| [0006](ADR-0006-worktrees-sandbox-protected-github.md) | Git worktrees, sandbox, защищённые GitHub-действия | Accepted (ТЗ §22) |
| [0007](ADR-0007-postgres-durable-workflow-engine.md) | Собственный durable workflow engine на PostgreSQL | Accepted |
| [0008](ADR-0008-data-conventions-tenancy-rls.md) | Соглашения данных: UUIDv7, text+CHECK, workspace_id + RLS | Accepted |
| [0009](ADR-0009-web-authentication.md) | Аутентификация веб-интерфейса | Accepted |
| [0010](ADR-0010-runner-container-sandbox.md) | Runner-контейнеры для CLI и egress proxy | Accepted |
| [0011](ADR-0011-sse-durable-event-log.md) | SSE из durable `run_event_log` | Accepted |
| [0012](ADR-0012-external-actions-ledger.md) | Журнал внешних действий (side-effect ledger) | Accepted |
| [0013](ADR-0013-contracts-location-and-codegen.md) | Размещение контрактов и генерация кода | Accepted |
| [0014](ADR-0014-frontend-architecture.md) | Архитектура frontend | Accepted |
