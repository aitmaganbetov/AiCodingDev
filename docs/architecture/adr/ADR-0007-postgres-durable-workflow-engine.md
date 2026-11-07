# ADR-0007: Собственный durable workflow engine на PostgreSQL

- **Статус:** Proposed
- **Дата:** 2026-11-07

## Контекст
Мастер-промпт требует «Durable Workflow Engine»; ТЗ §6, §16 описывает его свойства (FSM, DAG, leases с fencing token, checkpoints, outbox, recovery), но не называет продукт. ТЗ §3 фиксирует стек PostgreSQL + Redis + Dramatiq.

## Решение
Реализовать движок в модуле `packages/orchestration` поверх PostgreSQL:
1. **FSM** Run/Task/Execution/Directive/Approval — декларативные таблицы переходов в коде (`transitions.py`) + guard-функции; переход = `UPDATE … SET status=:new, version=version+1 WHERE id=:id AND version=:expected AND status=:old` + `outbox_events` в одной транзакции. 0 строк → `InvalidStateTransition`/`VersionConflict`.
2. **DAG Scheduler:** при утверждении плана — транзакционная проверка ацикличности (алгоритм Кана в приложении под `SELECT … FOR UPDATE` строки run + повторная проверка триггером зависимостей в пределах одного run). Готовность задачи: все зависимости `SUCCEEDED` → `READY`.
3. **Leases:** `worker_leases` c `lease_until`, `heartbeat_at`, монотонным `fencing_token` (sequence). Каждая запись результата проверяет актуальность токена; истёкшая аренда отбирается reaper'ом.
4. **Checkpoints:** снимок состояния run (версии задач, plan_version, указатели worktree/commit) в `checkpoints` + artifact; создаются на переходах фаз и перед replan по directive.
5. **Recovery reconciler:** при старте и периодически сверяет `cli_executions` (PID/process group/контейнер), `worktrees`, `worker_leases`, `external_actions`, артефакты.
6. **Таймеры** (retry backoff, approval expiry, lease expiry) — колонки `next_attempt_at`/`expires_at` + периодический tick-актор; без отдельного scheduler-сервиса.

## Альтернативы
- **Temporal** — сильные гарантии, но тяжёлая отдельная инфраструктура (свой кластер/БД), противоречит стеку ТЗ и self-hosted простоте; отвергнуто для v1.0 (может быть пересмотрено).
- **Prefect/Airflow** — ориентированы на data pipelines, слабая поддержка интерактивных директив и approvals.
- **Чистый Dramatiq pipelines** — нет durable состояния DAG.

## Последствия
+ Полный контроль над семантикой approvals/directives/replan, один источник истины.
− Ответственность за корректность движка на команде → обязательные property-based тесты FSM, chaos-тесты (kill worker, lost lease, duplicate delivery).

## Связанные требования
FR-ORCH-01..12, NFR-REL-01..05, AC-06..09.
