"""P9-05 / OQ-51 (a), (b): права aicd_maintenance для pg_dump и approval VULN_DB_UPDATE.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-08

(a) Логический бэкап (`pg_dump -Fc`) под ролью `aicd_maintenance` (DATABASE_DESIGN §19.6: роль используется для
backup). `pg_dump` читает каждую партицию и последовательность напрямую, а `SELECT` у роли был только на таблицы,
существовавшие в 0001 (DR drill P9-05: «permission denied for table outbox_events_y2026m10»). Выдаётся:
* `SELECT` на существующие партиции `outbox_events_*` и `agent_activity_events_*` (партиции `audit_events`,
  созданные в 0001, право уже имеют);
* `SELECT` на `worker_lease_fencing_seq` (`pg_dump` читает `last_value`; `USAGE`/`UPDATE` не выдаются);
* будущие партиции: `maint_ensure_monthly_partitions` выдаёт `SELECT` роли `aicd_maintenance` на каждую
  создаваемую партицию (`audit_events`, `outbox_events`, `agent_activity_events`) — точечно, без
  `ALTER DEFAULT PRIVILEGES` (новые таблицы миграций получают права явно в своих миграциях).

(b) Тип approval `VULN_DB_UPDATE` (SECURITY_ARCHITECTURE §11): обновление офлайн-БД уязвимостей SCA-gates — единственный
шаг с egress к реестрам БД. Мин. роль утверждающего — admin, four-eyes и step-up (критический набор
`FOUR_EYES_ACTIONS`), срок — 24 ч, привязка — sha256 канонического JSON предмета (виды БД, экосистемы OSV, реестр БД
Trivy, egress-хосты). Запрос и использование — только роль `aicd_maintenance` (утилита `scripts.vulndb.update` на
хосте оператора) через SECURITY DEFINER-функции (владелец `aicd_sysfn`):
* `sys_vulndb_request_approval` — `PENDING` от имени пользователя-инициатора (член workspace; e-mail), outbox
  `approval.requested`, аудит `approval.request`; повтор того же предмета возвращает существующий `PENDING`;
* `sys_vulndb_authorize` — проверка `APPROVED`, не истёк, предмет совпадает (hash и jsonb), однократность (аудит
  `vulndb.update_authorized` по approval) — и запись этого аудита;
* `sys_vulndb_report` — итог обновления (аудит `vulndb.update_finished`, outcome success|failure), только после
  авторизации.

Миграция — только SQL через op.execute (offline `alembic upgrade --sql`). Обратимость: downgrade возвращает домен,
`approval_min_role`, тело `maint_ensure_monthly_partitions` (0005), удаляет функции и отзывает выданные права;
при наличии строк `approvals.action_type = 'VULN_DB_UPDATE'` downgrade прерывается (данные аудита не удаляются).
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

ACTIONS_V1 = (
    "'PLAN_APPROVAL','MERGE','PRODUCTION_DEPLOY','DESTRUCTIVE_MIGRATION','DATA_DELETION','SECRET_CHANGE',"
    "'UNRESTRICTED_NETWORK','DOCS_DOWNLOAD','CRITICAL_DIRECTIVE','MANUAL_REVIEW','SELF_REVIEW_FALLBACK',"
    "'LOCAL_EXECUTION_FALLBACK'"
)


def _partitions_function(grant_maintenance: bool) -> str:
    grant = (
        "\n      EXECUTE format('GRANT SELECT ON aicd.%I TO aicd_maintenance', part);   -- pg_dump (0016)"
        if grant_maintenance
        else ""
    )
    return f"""
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
      EXECUTE format('REVOKE ALL ON aicd.%I FROM PUBLIC, aicd_app, aicd_worker', part);{grant}
      created := created + 1;
    END IF;
    m := (m + interval '1 month')::date;
  END LOOP;
  RETURN created;
END $$;
"""  # noqa: S608 — DDL из констант миграции


GRANT_PARTITIONS = r"""
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname FROM pg_inherits i
             JOIN pg_class c ON c.oid = i.inhrelid
             JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relnamespace = 'aicd'::regnamespace AND p.relname IN ('outbox_events', 'agent_activity_events', 'audit_events')
  LOOP
    EXECUTE format('GRANT SELECT ON aicd.%I TO aicd_maintenance', r.relname);
  END LOOP;
END $$;
"""

REVOKE_PARTITIONS = r"""
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname FROM pg_inherits i
             JOIN pg_class c ON c.oid = i.inhrelid
             JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relnamespace = 'aicd'::regnamespace AND p.relname IN ('outbox_events', 'agent_activity_events')
  LOOP
    EXECUTE format('REVOKE SELECT ON aicd.%I FROM aicd_maintenance', r.relname);
  END LOOP;
END $$;
"""

VULNDB = r"""
ALTER DOMAIN approval_action_type_t DROP CONSTRAINT approval_action_type_t_check;
ALTER DOMAIN approval_action_type_t ADD CONSTRAINT approval_action_type_t_check CHECK (VALUE IN
  (__ACTIONS_V1__,'VULN_DB_UPDATE'));

CREATE OR REPLACE FUNCTION approval_min_role(p_action_type text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN p_action_type IN ('MANUAL_REVIEW','SELF_REVIEW_FALLBACK') THEN 'reviewer'
    WHEN p_action_type IN ('PLAN_APPROVAL','DOCS_DOWNLOAD') THEN 'developer'
    WHEN p_action_type IN ('MERGE','DESTRUCTIVE_MIGRATION','DATA_DELETION','CRITICAL_DIRECTIVE','LOCAL_EXECUTION_FALLBACK',
                           'VULN_DB_UPDATE') THEN 'admin'
    WHEN p_action_type IN ('PRODUCTION_DEPLOY','SECRET_CHANGE','UNRESTRICTED_NETWORK') THEN 'owner'
  END
$$;

-- Запрос approval VULN_DB_UPDATE (утилита scripts.vulndb.update, роль aicd_maintenance). Идентификаторы (UUIDv7)
-- генерирует утилита. Инициатор — пользователь-член workspace (по e-mail): он не сможет утвердить сам (four-eyes).
CREATE FUNCTION sys_vulndb_request_approval(p_approval_id uuid, p_event_id uuid, p_audit_id uuid, p_workspace_id uuid,
                                            p_project_id uuid, p_requested_by_email text, p_subject jsonb,
                                            p_subject_hash text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
DECLARE
  v_user uuid;
  v_id   uuid;
BEGIN
  IF p_approval_id IS NULL OR p_event_id IS NULL OR p_audit_id IS NULL OR p_workspace_id IS NULL OR p_project_id IS NULL
     OR p_subject IS NULL OR jsonb_typeof(p_subject) <> 'object' OR octet_length(p_subject::text) > 16384
     OR p_subject_hash IS NULL OR p_subject_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'sys_vulndb_request_approval: invalid arguments' USING ERRCODE = '22023';
  END IF;
  SELECT u.id INTO v_user FROM aicd.users u
    JOIN aicd.workspace_members m ON m.user_id = u.id AND m.workspace_id = p_workspace_id
   WHERE u.email = lower(p_requested_by_email) AND u.status = 'ACTIVE';
  IF v_user IS NULL THEN
    RAISE EXCEPTION 'requester % is not an active member of workspace %', p_requested_by_email, p_workspace_id
      USING ERRCODE = '42501';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM aicd.projects p WHERE p.id = p_project_id AND p.workspace_id = p_workspace_id) THEN
    RAISE EXCEPTION 'project % is not in workspace %', p_project_id, p_workspace_id USING ERRCODE = '22023';
  END IF;
  INSERT INTO aicd.approvals (id, workspace_id, project_id, action_type, risk_level, required_role, requested_by_type,
                              requested_by, subject_hash, subject, expires_at)
  VALUES (p_approval_id, p_workspace_id, p_project_id, 'VULN_DB_UPDATE', 'high', aicd.approval_min_role('VULN_DB_UPDATE'),
          'user', v_user, p_subject_hash, p_subject, now() + interval '24 hours')
  ON CONFLICT (workspace_id, action_type, subject_hash) WHERE status = 'PENDING' DO NOTHING
  RETURNING id INTO v_id;
  IF v_id IS NULL THEN
    SELECT a.id INTO v_id FROM aicd.approvals a
     WHERE a.workspace_id = p_workspace_id AND a.action_type = 'VULN_DB_UPDATE' AND a.subject_hash = p_subject_hash
       AND a.status = 'PENDING';
    RETURN v_id;
  END IF;
  INSERT INTO aicd.outbox_events (id, workspace_id, run_id, aggregate_type, aggregate_id, event_type, payload)
  VALUES (p_event_id, p_workspace_id, NULL, 'approval', v_id, 'approval.requested',
          jsonb_build_object('approval_id', v_id, 'action_type', 'VULN_DB_UPDATE', 'risk_level', 'high',
                             'required_role', aicd.approval_min_role('VULN_DB_UPDATE'), 'task_id', NULL));
  INSERT INTO aicd.audit_events (id, workspace_id, actor_type, actor_id, action, resource_type, resource_id, metadata)
  VALUES (p_audit_id, p_workspace_id, 'user', v_user, 'approval.request', 'approval', v_id,
          jsonb_build_object('action_type', 'VULN_DB_UPDATE', 'risk_level', 'high', 'subject_hash', p_subject_hash,
                             'via', 'aicd_maintenance', 'session_user', session_user::text));
  RETURN v_id;
END $$;

-- Однократное использование утверждённого approval перед загрузкой БД. Ошибка 42501 — нет действующего
-- одобрения (не APPROVED, истёк, уже использован); 22023 — предмет не совпадает или неверные аргументы.
CREATE FUNCTION sys_vulndb_authorize(p_approval_id uuid, p_audit_id uuid, p_subject jsonb, p_subject_hash text,
                                     p_operator text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
DECLARE a record;
BEGIN
  IF p_approval_id IS NULL OR p_audit_id IS NULL OR p_subject IS NULL OR p_subject_hash IS NULL
     OR p_operator IS NULL OR length(p_operator) NOT BETWEEN 1 AND 200 THEN
    RAISE EXCEPTION 'sys_vulndb_authorize: invalid arguments' USING ERRCODE = '22023';
  END IF;
  -- однократность при конкурентных вызовах: сериализация по approval (без права UPDATE на approvals)
  PERFORM pg_advisory_xact_lock(hashtextextended('vulndb:' || p_approval_id::text, 0));
  SELECT x.id, x.workspace_id, x.action_type, x.status, x.subject, x.subject_hash, x.requested_by, x.decided_by,
         x.decided_at, x.expires_at
    INTO a FROM aicd.approvals x WHERE x.id = p_approval_id;
  IF NOT FOUND OR a.action_type <> 'VULN_DB_UPDATE' THEN
    RAISE EXCEPTION 'approval % is not a VULN_DB_UPDATE approval', p_approval_id USING ERRCODE = '42501';
  END IF;
  IF a.status <> 'APPROVED' THEN
    RAISE EXCEPTION 'approval % is %, not APPROVED', p_approval_id, a.status USING ERRCODE = '42501';
  END IF;
  IF a.expires_at IS NOT NULL AND a.expires_at <= now() THEN
    RAISE EXCEPTION 'approval % expired at %', p_approval_id, a.expires_at USING ERRCODE = '42501';
  END IF;
  IF a.subject_hash <> p_subject_hash OR a.subject <> p_subject THEN
    RAISE EXCEPTION 'approval % covers a different update (subject mismatch)', p_approval_id USING ERRCODE = '22023';
  END IF;
  IF EXISTS (SELECT 1 FROM aicd.audit_events e WHERE e.action = 'vulndb.update_authorized'
                AND e.resource_type = 'approval' AND e.resource_id = p_approval_id) THEN
    RAISE EXCEPTION 'approval % has already been used', p_approval_id USING ERRCODE = '42501';
  END IF;
  INSERT INTO aicd.audit_events (id, workspace_id, actor_type, action, resource_type, resource_id, metadata)
  VALUES (p_audit_id, a.workspace_id, 'system', 'vulndb.update_authorized', 'approval', p_approval_id,
          jsonb_build_object('operator', p_operator, 'session_user', session_user::text, 'subject_hash', p_subject_hash,
                             'requested_by', a.requested_by, 'decided_by', a.decided_by));
  RETURN jsonb_build_object('workspace_id', a.workspace_id, 'requested_by', a.requested_by,
                            'decided_by', a.decided_by, 'decided_at', a.decided_at, 'expires_at', a.expires_at);
END $$;

CREATE FUNCTION sys_vulndb_report(p_approval_id uuid, p_audit_id uuid, p_outcome text, p_metadata jsonb)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
DECLARE v_ws uuid;
BEGIN
  IF p_outcome NOT IN ('success', 'failure') OR p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
     OR octet_length(p_metadata::text) > 16384 THEN
    RAISE EXCEPTION 'sys_vulndb_report: invalid arguments' USING ERRCODE = '22023';
  END IF;
  SELECT e.workspace_id INTO v_ws FROM aicd.audit_events e
   WHERE e.action = 'vulndb.update_authorized' AND e.resource_type = 'approval' AND e.resource_id = p_approval_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'approval % was not authorized for an update', p_approval_id USING ERRCODE = '42501';
  END IF;
  INSERT INTO aicd.audit_events (id, workspace_id, actor_type, action, resource_type, resource_id, outcome, metadata)
  VALUES (p_audit_id, v_ws, 'system', 'vulndb.update_finished', 'approval', p_approval_id, p_outcome,
          p_metadata || jsonb_build_object('session_user', session_user::text));
END $$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_vulndb_request_approval(uuid, uuid, uuid, uuid, uuid, text, jsonb, text) OWNER TO aicd_sysfn;
ALTER FUNCTION sys_vulndb_authorize(uuid, uuid, jsonb, text, text) OWNER TO aicd_sysfn;
ALTER FUNCTION sys_vulndb_report(uuid, uuid, text, jsonb) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_vulndb_request_approval(uuid, uuid, uuid, uuid, uuid, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sys_vulndb_authorize(uuid, uuid, jsonb, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sys_vulndb_report(uuid, uuid, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_vulndb_request_approval(uuid, uuid, uuid, uuid, uuid, text, jsonb, text) TO aicd_maintenance;
GRANT EXECUTE ON FUNCTION sys_vulndb_authorize(uuid, uuid, jsonb, text, text) TO aicd_maintenance;
GRANT EXECUTE ON FUNCTION sys_vulndb_report(uuid, uuid, text, jsonb) TO aicd_maintenance;
-- Права владельца функций (BYPASSRLS, без LOGIN): ровно нужные колонки.
GRANT SELECT (id, email, status) ON users TO aicd_sysfn;
GRANT SELECT (id, workspace_id, action_type, status, subject, subject_hash, requested_by, decided_by, decided_at,
              expires_at) ON approvals TO aicd_sysfn;
GRANT INSERT (id, workspace_id, project_id, action_type, risk_level, required_role, requested_by_type, requested_by,
              subject_hash, subject, expires_at) ON approvals TO aicd_sysfn;
GRANT SELECT (workspace_id, action, resource_type, resource_id) ON audit_events TO aicd_sysfn;
GRANT INSERT (id, workspace_id, actor_type, actor_id, action, resource_type, resource_id, outcome, metadata)
  ON audit_events TO aicd_sysfn;
""".replace("__ACTIONS_V1__", ACTIONS_V1)

DOWNGRADE_VULNDB = r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM aicd.approvals WHERE action_type = 'VULN_DB_UPDATE') THEN
    RAISE EXCEPTION 'approvals VULN_DB_UPDATE exist: downgrade 0016 would lose approval history' USING ERRCODE = '55000';
  END IF;
END $$;
DROP FUNCTION IF EXISTS sys_vulndb_report(uuid, uuid, text, jsonb);
DROP FUNCTION IF EXISTS sys_vulndb_authorize(uuid, uuid, jsonb, text, text);
DROP FUNCTION IF EXISTS sys_vulndb_request_approval(uuid, uuid, uuid, uuid, uuid, text, jsonb, text);
REVOKE INSERT (id, workspace_id, actor_type, actor_id, action, resource_type, resource_id, outcome, metadata)
  ON audit_events FROM aicd_sysfn;
REVOKE SELECT (workspace_id, action, resource_type, resource_id) ON audit_events FROM aicd_sysfn;
REVOKE INSERT (id, workspace_id, project_id, action_type, risk_level, required_role, requested_by_type, requested_by,
               subject_hash, subject, expires_at) ON approvals FROM aicd_sysfn;
REVOKE SELECT (action_type, subject, subject_hash, requested_by, decided_by, decided_at) ON approvals FROM aicd_sysfn;
REVOKE SELECT (id, email, status) ON users FROM aicd_sysfn;
CREATE OR REPLACE FUNCTION approval_min_role(p_action_type text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN p_action_type IN ('MANUAL_REVIEW','SELF_REVIEW_FALLBACK') THEN 'reviewer'
    WHEN p_action_type IN ('PLAN_APPROVAL','DOCS_DOWNLOAD') THEN 'developer'
    WHEN p_action_type IN ('MERGE','DESTRUCTIVE_MIGRATION','DATA_DELETION','CRITICAL_DIRECTIVE','LOCAL_EXECUTION_FALLBACK') THEN 'admin'
    WHEN p_action_type IN ('PRODUCTION_DEPLOY','SECRET_CHANGE','UNRESTRICTED_NETWORK') THEN 'owner'
  END
$$;
ALTER DOMAIN approval_action_type_t DROP CONSTRAINT approval_action_type_t_check;
ALTER DOMAIN approval_action_type_t ADD CONSTRAINT approval_action_type_t_check CHECK (VALUE IN (__ACTIONS_V1__));
""".replace("__ACTIONS_V1__", ACTIONS_V1)


def upgrade() -> None:
    op.execute(GRANT_PARTITIONS)
    op.execute("GRANT SELECT ON SEQUENCE worker_lease_fencing_seq TO aicd_maintenance;")
    op.execute(_partitions_function(grant_maintenance=True))
    op.execute(VULNDB)


def downgrade() -> None:
    op.execute(DOWNGRADE_VULNDB)
    op.execute(_partitions_function(grant_maintenance=False))
    op.execute("REVOKE SELECT ON SEQUENCE worker_lease_fencing_seq FROM aicd_maintenance;")
    op.execute(REVOKE_PARTITIONS)
