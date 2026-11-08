# data-model (ТЗ §21)

Нормативная модель данных — [`docs/database/DATABASE_DESIGN.md`](../database/DATABASE_DESIGN.md) (таблицы, ограничения, RLS) и [`docs/database/ER_DIAGRAM.md`](../database/ER_DIAGRAM.md) (ERD, кардинальности, инварианты). Файл-указатель создан, чтобы путь из ТЗ §21 вёл к единственному источнику (ADR-0013, дублирование запрещено).

Реализовано миграциями: `infrastructure/migrations/versions/0001_foundation.py` (Phase 1 — IAM, workspaces, проекты, репозитории, idempotency, artifacts, audit).
