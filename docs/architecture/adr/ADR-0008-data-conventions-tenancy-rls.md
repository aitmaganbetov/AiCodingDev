# ADR-0008: Соглашения данных — UUIDv7, text+CHECK, денормализованный workspace_id, RLS

- **Статус:** Proposed
- **Дата:** 2026-11-07

## Решение
1. PK — UUIDv7, генерируются приложением (сортировка по времени, локальность индексов).
2. Перечисления — `text` + `CHECK (col IN (…))`, значения из `docs/README.md §3`; изменение множества — аддитивная миграция без `ALTER TYPE`.
3. **Каждая tenant-scoped таблица содержит `workspace_id NOT NULL`** (ТЗ §7.1 допускает наследуемую ссылку, но RLS-политика без JOIN проще, быстрее и надёжнее). Родительские таблицы имеют `UNIQUE (workspace_id, id)`, дочерние ссылаются составным FK `(workspace_id, parent_id)` → межтенантная ссылка физически невозможна.
4. **RLS** `ENABLE` + `FORCE` на всех tenant-таблицах; контекст через `SET LOCAL app.workspace_id`; роли `aicd_app`/`aicd_worker` без `BYPASSRLS`; `aicd_maintenance` с `BYPASSRLS` только для retention/backup, каждое использование аудируется.
5. Optimistic locking: `version bigint` на изменяемых агрегатах (runs, tasks, directives, approvals, project_settings, policies).
6. `jsonb` только для конфигураций, snapshot, payload; критические атрибуты — типизированные колонки.

## Последствия
+ Изоляция арендаторов на уровне БД (AC-12), defense-in-depth поверх RBAC.
− Избыточная колонка и составные FK; миграции сложнее; тесты RLS обязательны для каждой таблицы.
