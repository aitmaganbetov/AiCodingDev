"""Retention/архивирование (P3-18) и SECURITY DEFINER резолверы approval/recovery.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08

Источник: docs/database/DATABASE_DESIGN.md §19.5, §19.6, §20.
  * maint_begin / maint_end / maint_require_session — сессия обслуживания под aicd_maintenance (§19.6):
    без maint_begin (audit `maintenance.session_started`) retention-функции и удаление из run_event_log
    отклоняются;
  * maint_ensure_monthly_partitions становится SECURITY DEFINER (владелец — aicd_migrator, владелец
    таблиц): создавать партиции может aicd_maintenance; список таблиц дополнен agent_activity_events
    (таблица появится в Phase 4; до этого функция возвращает 0);
  * maint_drop_partition — DETACH + DROP истёкшей помесячной партиции (§20.2). Для audit_events и
    agent_activity_events требует ссылку на архив и sha256 (без архива удаление запрещено, §20.3);
    партиция outbox_events удаляется, только если в ней 0 неопубликованных событий. Отличие от §20.2:
    DETACH без CONCURRENTLY (CONCURRENTLY невозможен внутри функции/транзакции; партиция старая, блокировка
    кратковременная);
  * run_event_log: триггер append-only разрешает DELETE только роли aicd_maintenance в сессии
    обслуживания (удаление пакетами, §20.2); audit_events по-прежнему неизменяем построчно;
  * sys_resolve_approval_workspace(uuid) — как sys_resolve_run_workspace (только для членов, иначе NULL);
  * sys_recovery_workspaces(int) — workspace с незавершёнными исполнениями/worktrees/внешними
    действиями/run для recovery reconciler (P3-12), как sys_schedulable_workspaces.
Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).

Обратимость: downgrade удаляет функции, возвращает триггер run_event_log, функцию партиций (0003) и права.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

MAINTENANCE = r"""
-- §19.6: сессия обслуживания. Идентификатор (UUIDv7) генерирует приложение (PostgreSQL 17 не умеет uuidv7()).
CREATE FUNCTION maint_begin(p_session_id uuid, p_job text, p_reason text) RETURNS uuid
LANGUAGE plpgsql SET search_path = pg_catalog, aicd AS $$
BEGIN
  IF current_user <> 'aicd_maintenance' THEN
    RAISE EXCEPTION 'maintenance session requires role aicd_maintenance' USING ERRCODE = '42501';
  END IF;
  IF p_job IS NULL OR p_job !~ '^[a-z][a-z0-9_]{0,63}$' THEN
    RAISE EXCEPTION 'invalid maintenance job name' USING ERRCODE = '22023';
  END IF;
  IF p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 500 THEN
    RAISE EXCEPTION 'maintenance reason is required (1..500 chars)' USING ERRCODE = '22023';
  END IF;
  IF COALESCE(current_setting('aicd.maint_session', true), '') <> '' THEN
    RAISE EXCEPTION 'maintenance session already started' USING ERRCODE = '55000';
  END IF;
  INSERT INTO aicd.audit_events (id, workspace_id, actor_type, action, resource_type, resource_id, metadata)
  VALUES (p_session_id, NULL, 'system', 'maintenance.session_started', 'maintenance', p_session_id,
          jsonb_build_object('job', p_job, 'reason', p_reason, 'role', session_user::text,
                             'backend_pid', pg_backend_pid()));
  PERFORM set_config('aicd.maint_session', p_session_id::text, false);   -- до конца соединения
  RETURN p_session_id;
END $$;

-- Идентификатор текущей сессии обслуживания; без maint_begin — отказ (42501).
CREATE FUNCTION maint_require_session() RETURNS uuid
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, aicd AS $$
DECLARE v text := COALESCE(current_setting('aicd.maint_session', true), '');
BEGIN
  IF v = '' OR session_user <> 'aicd_maintenance' THEN
    RAISE EXCEPTION 'maintenance session is not started (call maint_begin)' USING ERRCODE = '42501';
  END IF;
  RETURN v::uuid;
END $$;

CREATE FUNCTION maint_end(p_event_id uuid, p_outcome text, p_metadata jsonb) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, aicd AS $$
DECLARE v_session uuid := aicd.maint_require_session();
BEGIN
  INSERT INTO aicd.audit_events (id, workspace_id, actor_type, action, resource_type, resource_id, outcome, metadata)
  VALUES (p_event_id, NULL, 'system', 'maintenance.session_finished', 'maintenance', v_session, p_outcome,
          COALESCE(p_metadata, '{}'::jsonb));
  PERFORM set_config('aicd.maint_session', '', false);
END $$;

-- run_event_log: append-only для всех, кроме удаления ролью aicd_maintenance в сессии обслуживания (§20.2).
CREATE FUNCTION trg_fn_run_event_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_user = 'aicd_maintenance'
     AND COALESCE(current_setting('aicd.maint_session', true), '') <> '' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = '42501';
END $$;
DROP TRIGGER trg_run_event_log_append_only ON run_event_log;
CREATE TRIGGER trg_run_event_log_append_only BEFORE UPDATE OR DELETE ON run_event_log
  FOR EACH ROW EXECUTE FUNCTION trg_fn_run_event_log_append_only();

-- Помесячные партиции (§20.1): SECURITY DEFINER от владельца таблиц, чтобы job под aicd_maintenance
-- мог создавать партиции вперёд (CREATE TABLE … PARTITION OF требует владения родителем).
CREATE OR REPLACE FUNCTION maint_ensure_monthly_partitions(p_table text, p_from date, p_months int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
DECLARE
  m date := date_trunc('month', p_from)::date;
  created int := 0;
  part text;
BEGIN
  IF p_table NOT IN ('audit_events', 'outbox_events', 'agent_activity_events') THEN
    RAISE EXCEPTION 'partitioning is not configured for %', p_table;
  END IF;
  IF to_regclass('aicd.' || p_table) IS NULL THEN
    RETURN 0;   -- таблица следующей фазы ещё не создана
  END IF;
  IF p_months NOT BETWEEN 0 AND 36 THEN
    RAISE EXCEPTION 'p_months must be within 0..36' USING ERRCODE = '22023';
  END IF;
  FOR i IN 0 .. p_months LOOP
    part := format('%s_y%sm%s', p_table, to_char(m, 'YYYY'), to_char(m, 'MM'));
    IF to_regclass('aicd.' || part) IS NULL THEN
      EXECUTE format('CREATE TABLE aicd.%I PARTITION OF aicd.%I FOR VALUES FROM (%L) TO (%L)',
                     part, p_table, m, (m + interval '1 month')::date);
      EXECUTE format('ALTER TABLE aicd.%I ENABLE ROW LEVEL SECURITY', part);
      EXECUTE format('ALTER TABLE aicd.%I FORCE ROW LEVEL SECURITY', part);
      EXECUTE format('REVOKE ALL ON aicd.%I FROM PUBLIC, aicd_app, aicd_worker', part);
      created := created + 1;
    END IF;
    m := (m + interval '1 month')::date;
  END LOOP;
  RETURN created;
END $$;
REVOKE ALL ON FUNCTION maint_ensure_monthly_partitions(text, date, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION maint_ensure_monthly_partitions(text, date, int) TO aicd_maintenance;

-- §20.2: DETACH + DROP истёкшей партиции. Возвращает 'dropped' | 'missing' | 'skipped_unpublished'.
-- Партиция должна целиком лежать до p_cutoff, p_cutoff — не позже начала текущего месяца.
CREATE FUNCTION maint_drop_partition(p_table text, p_partition text, p_cutoff date,
                                     p_archive_ref text, p_archive_sha256 text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
DECLARE
  v_month date;
  v_unpublished bigint;
BEGIN
  PERFORM aicd.maint_require_session();
  IF p_table NOT IN ('audit_events', 'outbox_events', 'agent_activity_events') THEN
    RAISE EXCEPTION 'partition retention is not configured for %', p_table USING ERRCODE = '22023';
  END IF;
  IF p_partition IS NULL OR p_partition !~ ('^' || p_table || '_y[0-9]{4}m(0[1-9]|1[0-2])$') THEN
    RAISE EXCEPTION 'invalid partition name %', p_partition USING ERRCODE = '22023';
  END IF;
  IF p_cutoff IS NULL OR p_cutoff > date_trunc('month', now())::date THEN
    RAISE EXCEPTION 'cutoff must not be later than the start of the current month' USING ERRCODE = '22023';
  END IF;
  v_month := to_date(right(p_partition, 7), 'YYYY"m"MM');
  IF (v_month + interval '1 month')::date > p_cutoff THEN
    RAISE EXCEPTION 'partition % is not expired (cutoff %)', p_partition, p_cutoff USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i
                   JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid
                   JOIN pg_catalog.pg_class p ON p.oid = i.inhparent
                   JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                  WHERE n.nspname = 'aicd' AND c.relname = p_partition AND p.relname = p_table) THEN
    RETURN 'missing';
  END IF;
  IF p_table IN ('audit_events', 'agent_activity_events')
     AND (p_archive_ref IS NULL OR p_archive_ref !~ '^(s3|file)://[A-Za-z0-9._/-]+$'
          OR p_archive_sha256 IS NULL OR p_archive_sha256 !~ '^[0-9a-f]{64}$') THEN
    RAISE EXCEPTION 'partition % must be archived before drop', p_partition USING ERRCODE = '42501';
  END IF;
  IF p_table = 'outbox_events' THEN
    -- Владелец видит строки только без FORCE; в той же транзакции FORCE возвращается или партиция удаляется.
    EXECUTE format('ALTER TABLE aicd.%I NO FORCE ROW LEVEL SECURITY', p_partition);
    EXECUTE format('SELECT count(*) FROM aicd.%I WHERE published_at IS NULL', p_partition) INTO v_unpublished;
    IF v_unpublished > 0 THEN
      EXECUTE format('ALTER TABLE aicd.%I FORCE ROW LEVEL SECURITY', p_partition);
      RETURN 'skipped_unpublished';
    END IF;
  END IF;
  EXECUTE format('ALTER TABLE aicd.%I DETACH PARTITION aicd.%I', p_table, p_partition);
  EXECUTE format('DROP TABLE aicd.%I', p_partition);
  RETURN 'dropped';
END $$;

REVOKE ALL ON FUNCTION maint_begin(uuid, text, text), maint_require_session(), maint_end(uuid, text, jsonb),
                       maint_drop_partition(text, text, date, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION maint_begin(uuid, text, text), maint_require_session(), maint_end(uuid, text, jsonb),
                          maint_drop_partition(text, text, date, text, text) TO aicd_maintenance;

-- Права aicd_maintenance: аудит своих действий, пакетное удаление журнала SSE (§20.2, §20.3).
GRANT INSERT ON audit_events TO aicd_maintenance;
GRANT DELETE ON run_event_log TO aicd_maintenance;
"""

SYSFN = r"""
-- §19.5: workspace approval, только если текущий пользователь — член этого workspace.
CREATE FUNCTION sys_resolve_approval_workspace(p_approval_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT a.workspace_id
    FROM aicd.approvals a
    JOIN aicd.workspace_members m ON m.workspace_id = a.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE a.id = p_approval_id
$$;

-- Recovery reconciler (P3-12): workspace с незавершённой работой, по давности самой старой единицы.
CREATE FUNCTION sys_recovery_workspaces(p_limit int)
RETURNS TABLE (workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT s.ws
    FROM (SELECT e.workspace_id AS ws, e.created_at AS since FROM aicd.cli_executions e
           WHERE e.status IN ('QUEUED','STARTING','RUNNING','EXITED')
          UNION ALL
          SELECT w.workspace_id, w.created_at FROM aicd.worktrees w WHERE w.status IN ('ACTIVE','LOCKED')
          UNION ALL
          SELECT x.workspace_id, x.executing_at FROM aicd.external_actions x WHERE x.status = 'EXECUTING'
          UNION ALL
          SELECT r.workspace_id, r.created_at FROM aicd.runs r
           WHERE r.status NOT IN ('COMPLETED','FAILED','CANCELLED')) s
   GROUP BY s.ws
   ORDER BY min(s.since), s.ws
   LIMIT greatest(p_limit, 0)
$$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_resolve_approval_workspace(uuid) OWNER TO aicd_sysfn;
ALTER FUNCTION sys_recovery_workspaces(int) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_resolve_approval_workspace(uuid), sys_recovery_workspaces(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_resolve_approval_workspace(uuid) TO aicd_app;
GRANT EXECUTE ON FUNCTION sys_recovery_workspaces(int) TO aicd_worker;
-- Только колонки, которых у aicd_sysfn ещё не было (downgrade отзывает ровно их).
GRANT SELECT (id) ON approvals TO aicd_sysfn;
GRANT SELECT (workspace_id, created_at) ON cli_executions TO aicd_sysfn;
GRANT SELECT (workspace_id, status, created_at) ON worktrees TO aicd_sysfn;
GRANT SELECT (workspace_id, status, executing_at) ON external_actions TO aicd_sysfn;
GRANT SELECT (status, created_at) ON runs TO aicd_sysfn;
"""

DOWNGRADE = r"""
REVOKE SELECT (status, created_at) ON runs FROM aicd_sysfn;
REVOKE SELECT (workspace_id, status, executing_at) ON external_actions FROM aicd_sysfn;
REVOKE SELECT (workspace_id, status, created_at) ON worktrees FROM aicd_sysfn;
REVOKE SELECT (workspace_id, created_at) ON cli_executions FROM aicd_sysfn;
REVOKE SELECT (id) ON approvals FROM aicd_sysfn;
DROP FUNCTION IF EXISTS sys_recovery_workspaces(int);
DROP FUNCTION IF EXISTS sys_resolve_approval_workspace(uuid);

REVOKE DELETE ON run_event_log FROM aicd_maintenance;
REVOKE INSERT ON audit_events FROM aicd_maintenance;

DROP TRIGGER IF EXISTS trg_run_event_log_append_only ON run_event_log;
CREATE TRIGGER trg_run_event_log_append_only BEFORE UPDATE OR DELETE ON run_event_log
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
DROP FUNCTION IF EXISTS trg_fn_run_event_log_append_only();

DROP FUNCTION IF EXISTS maint_drop_partition(text, text, date, text, text);
DROP FUNCTION IF EXISTS maint_end(uuid, text, jsonb);
DROP FUNCTION IF EXISTS maint_require_session();
DROP FUNCTION IF EXISTS maint_begin(uuid, text, text);

-- Функция партиций — как в 0003 (SECURITY INVOKER, EXECUTE для PUBLIC по умолчанию).
CREATE OR REPLACE FUNCTION maint_ensure_monthly_partitions(p_table text, p_from date, p_months int)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE
  m date := date_trunc('month', p_from)::date;
  created int := 0;
  part text;
BEGIN
  IF p_table NOT IN ('audit_events', 'outbox_events') THEN
    RAISE EXCEPTION 'partitioning is not configured for %', p_table;
  END IF;
  FOR i IN 0 .. p_months LOOP
    part := format('%s_y%sm%s', p_table, to_char(m, 'YYYY'), to_char(m, 'MM'));
    IF to_regclass(part) IS NULL THEN
      EXECUTE format('CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                     part, p_table, m, (m + interval '1 month')::date);
      EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', part);
      EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', part);
      EXECUTE format('REVOKE ALL ON %I FROM PUBLIC, aicd_app, aicd_worker', part);
      created := created + 1;
    END IF;
    m := (m + interval '1 month')::date;
  END LOOP;
  RETURN created;
END $$;
REVOKE ALL ON FUNCTION maint_ensure_monthly_partitions(text, date, int) FROM aicd_maintenance;
GRANT EXECUTE ON FUNCTION maint_ensure_monthly_partitions(text, date, int) TO PUBLIC;
"""


def upgrade() -> None:
    for block in (MAINTENANCE, SYSFN):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
