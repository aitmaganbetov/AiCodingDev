"""UI redesign B1/B2 (UI_REDESIGN_PLAN §3.10 G-01, G-03, G-04, G-05): индексы read-моделей workspace и backlog outbox.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-09

Только чтение существующих таблиц, без изменения архитектуры (§7.2):

* `ix_run_event_log_ws_created (workspace_id, created_at, event_id)` — лента `GET /activity` (keyset по
  `(created_at, event_id)` в пределах workspace; OQ-45 (c): индекса не было) и повтор SSE workspace
  (`GET /workspaces/{id}/events`, тот же keyset);
* `ix_cli_executions_ws_created (workspace_id, created_at, id)` — история исполнений CLI
  `GET /providers/{id}/executions` (keyset по `(created_at, id)` desc);
* `ix_tasks_ws_created (workspace_id, created_at, id) WHERE superseded_at IS NULL` — список задач workspace
  `GET /tasks` (только актуальные задачи плана);
* `sys_outbox_backlog()` — узкая SECURITY DEFINER функция (§19.5) для `GET /system/health`: у `aicd_app` нет
  `SELECT` на `outbox_events` (только INSERT, RLS), функция возвращает лишь агрегат (число неопубликованных
  событий и время самого старого) — без строк, payload и workspace. Владелец `aicd_sysfn` (BYPASSRLS), фиксированный
  search_path, `EXECUTE` — только `aicd_app`; `aicd_sysfn` получает `SELECT` лишь на колонки
  `(created_at, published_at)`.

Миграция — только SQL через op.execute (offline `alembic upgrade --sql`). Обратимость: downgrade удаляет функцию,
отзывает выданные права и удаляет индексы.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

INDEXES = r"""
CREATE INDEX ix_run_event_log_ws_created ON run_event_log (workspace_id, created_at, event_id);
CREATE INDEX ix_cli_executions_ws_created ON cli_executions (workspace_id, created_at, id);
CREATE INDEX ix_tasks_ws_created ON tasks (workspace_id, created_at, id) WHERE superseded_at IS NULL;
"""

SYSFN = r"""
-- §19.5: backlog доставки outbox для экрана System Health (admin). Только агрегат, без данных арендаторов.
CREATE FUNCTION sys_outbox_backlog()
RETURNS TABLE (pending bigint, oldest_created_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT count(*)::bigint, min(o.created_at) FROM aicd.outbox_events o WHERE o.published_at IS NULL
$$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_outbox_backlog() OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_outbox_backlog() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_outbox_backlog() TO aicd_app;
GRANT SELECT (created_at, published_at) ON outbox_events TO aicd_sysfn;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_outbox_backlog();
REVOKE SELECT (created_at, published_at) ON outbox_events FROM aicd_sysfn;
DROP INDEX IF EXISTS ix_tasks_ws_created;
DROP INDEX IF EXISTS ix_cli_executions_ws_created;
DROP INDEX IF EXISTS ix_run_event_log_ws_created;
"""


def upgrade() -> None:
    op.execute(INDEXES)
    op.execute(SYSFN)


def downgrade() -> None:
    op.execute(DOWNGRADE)
