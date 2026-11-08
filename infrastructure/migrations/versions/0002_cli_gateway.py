"""CLI Gateway: хосты воркеров, провайдеры, установки CLI, сессии, worktrees, исполнения, health, usage.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08

Источник DDL: docs/database/DATABASE_DESIGN.md §6.4, §7.8, §8.1–8.7, §18.3, §18.6.
Отличия, обусловленные порядком фаз (FK добавляются миграцией Phase 3 вместе с таблицами):
  * worktrees.run_id / worktrees.task_id — FK на runs/tasks;
  * cli_executions.task_attempt_id — FK на task_attempts.
`allowed_transitions` создаётся здесь (первый потребитель — Execution FSM); строки run/task/
directive/approval/external_action добавит Phase 3.
`worker_lease_fencing_seq` создаётся здесь: токен нужен cli_executions уже в Phase 2.

Обратимость: downgrade удаляет созданные объекты (данные исполнений теряются).
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = r"""
CREATE SEQUENCE worker_lease_fencing_seq AS bigint START 1 NO CYCLE;

-- §7.8 allowed_transitions (G) — защитный рубеж FSM (§18.3)
CREATE TABLE allowed_transitions (
  entity       text NOT NULL CHECK (entity IN ('run','task','execution','directive','approval','external_action')),
  from_status  text NOT NULL,
  to_status    text NOT NULL,
  CONSTRAINT pk_allowed_transitions PRIMARY KEY (entity, from_status, to_status),
  CONSTRAINT ck_allowed_transitions_not_self CHECK (from_status <> to_status)
);

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

INSERT INTO allowed_transitions (entity, from_status, to_status) VALUES
  ('execution','__new__','QUEUED'),
  ('execution','QUEUED','STARTING'), ('execution','QUEUED','CANCELLED'), ('execution','QUEUED','CRASHED'),
  ('execution','STARTING','RUNNING'), ('execution','STARTING','TIMED_OUT'), ('execution','STARTING','CANCELLED'),
  ('execution','STARTING','CRASHED'), ('execution','STARTING','AUTH_REQUIRED'), ('execution','STARTING','RATE_LIMITED'),
  ('execution','RUNNING','EXITED'), ('execution','RUNNING','TIMED_OUT'), ('execution','RUNNING','CANCELLED'),
  ('execution','RUNNING','CRASHED'), ('execution','RUNNING','AUTH_REQUIRED'), ('execution','RUNNING','RATE_LIMITED'),
  ('execution','EXITED','COLLECTED'), ('execution','EXITED','CRASHED'),
  ('execution','EXITED','AUTH_REQUIRED'), ('execution','EXITED','RATE_LIMITED');  -- классификация после выхода (CLI_ADAPTER_CONTRACT §9)

-- §8.1 worker_hosts (G) — дополнение к ТЗ
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
CREATE TRIGGER trg_worker_hosts_updated_at BEFORE UPDATE ON worker_hosts FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();

-- §8.2 cli_providers (G) + seed (ADR-0003: только два провайдера)
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
CREATE TRIGGER trg_cli_providers_updated_at BEFORE UPDATE ON cli_providers FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
INSERT INTO cli_providers (id, code, display_name) VALUES
  ('01a1b000-0000-7000-8000-000000000001', 'codex', 'OpenAI Codex CLI'),
  ('01a1b000-0000-7000-8000-000000000002', 'claude', 'Anthropic Claude Code CLI');

-- §8.3 cli_installations (G)
CREATE TABLE cli_installations (
  id                     uuid7          NOT NULL,
  provider_id            uuid           NOT NULL,
  host_id                uuid           NOT NULL,
  binary_path            text           NOT NULL CHECK (binary_path ~ '^/[A-Za-z0-9._/+-]+$' AND length(binary_path) <= 1024),
  detected_version       text           CHECK (length(detected_version) <= 100),
  compatibility          text           NOT NULL DEFAULT 'untested'
                                        CHECK (compatibility IN ('supported','untested','unsupported')),
  auth_status            auth_status_t  NOT NULL DEFAULT 'UNKNOWN',
  capabilities           jsonb          NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(capabilities) = 'object'),
  credential_volume_ref  text           CHECK (credential_volume_ref ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'),
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
CREATE TRIGGER trg_cli_installations_updated_at BEFORE UPDATE ON cli_installations FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();

-- §8.4 cli_sessions (G)
CREATE TABLE cli_sessions (
  id               uuid7       NOT NULL,
  installation_id  uuid        NOT NULL,
  session_ref      text        NOT NULL CHECK (length(session_ref) BETWEEN 1 AND 255),
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

-- §6.4 worktrees (T) — FK run_id/task_id добавляются в Phase 3
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
  CONSTRAINT fk_worktrees_host FOREIGN KEY (worker_host_id) REFERENCES worker_hosts(id) ON DELETE SET NULL,
  CONSTRAINT ck_worktrees_removed CHECK ((status = 'REMOVED') = (removed_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_worktrees_active_branch ON worktrees (repository_id, branch_name) WHERE status IN ('ACTIVE','LOCKED');
CREATE INDEX ix_worktrees_run ON worktrees (workspace_id, run_id);
CREATE INDEX ix_worktrees_reconcile ON worktrees (worker_host_id, status) WHERE status IN ('ACTIVE','LOCKED','ORPHANED');
CREATE TRIGGER trg_worktrees_updated_at BEFORE UPDATE ON worktrees FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_worktrees_immutable_ws BEFORE UPDATE OF workspace_id ON worktrees FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- §8.5 cli_executions (T) — FK task_attempt_id добавляется в Phase 3
CREATE TABLE cli_executions (
  id                   uuid7              NOT NULL,
  workspace_id         uuid               NOT NULL,
  task_attempt_id      uuid               NOT NULL,
  installation_id      uuid               NOT NULL,
  session_id           uuid,
  worktree_id          uuid,
  status               execution_status_t NOT NULL DEFAULT 'QUEUED',
  contract_version     text               NOT NULL DEFAULT '1.0' CHECK (contract_version ~ '^\d+\.\d+$'),
  command_profile      text               NOT NULL CHECK (command_profile ~ '^[a-z0-9_.-]{1,64}$'),
  request_hash         sha256_hex         NOT NULL,
  worktree_ref         text               CHECK (worktree_ref !~ '(^/|\.\.)'),
  container_ref        text               CHECK (length(container_ref) <= 128),
  pid_ref              text               CHECK (pid_ref ~ '^[A-Za-z0-9.-]+:[0-9]+(:[0-9]+)?$'),
  timeout_seconds      int                NOT NULL CHECK (timeout_seconds BETWEEN 30 AND 86400),
  fencing_token        bigint             NOT NULL,
  exit_code            int,
  failure_category     cli_error_code_t,
  output_artifact_id   uuid,
  raw_log_artifact_id  uuid,
  created_at           timestamptz        NOT NULL DEFAULT now(),
  started_at           timestamptz,
  exited_at            timestamptz,
  finished_at          timestamptz,
  updated_at           timestamptz        NOT NULL DEFAULT now(),
  CONSTRAINT pk_cli_executions PRIMARY KEY (id),
  CONSTRAINT uq_cli_executions_ws_id UNIQUE (workspace_id, id),
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
  WHERE status IN ('QUEUED','STARTING','RUNNING','EXITED');
CREATE TRIGGER trg_cli_executions_updated_at BEFORE UPDATE ON cli_executions FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_cli_executions_immutable_ws BEFORE UPDATE OF workspace_id ON cli_executions FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_cli_executions_transition BEFORE INSERT OR UPDATE OF status ON cli_executions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('execution');

-- §8.6 provider_health (G)
CREATE TABLE provider_health (
  id                 uuid7                   NOT NULL,
  installation_id    uuid                    NOT NULL,
  auth_status        auth_status_t           NOT NULL,
  availability       provider_availability_t NOT NULL,
  error_category     cli_error_code_t,
  detail             text                    CHECK (length(detail) <= 2000),
  probe_duration_ms  int                     CHECK (probe_duration_ms >= 0),
  observed_at        timestamptz             NOT NULL DEFAULT now(),
  CONSTRAINT pk_provider_health PRIMARY KEY (id),
  CONSTRAINT fk_provider_health_installation FOREIGN KEY (installation_id) REFERENCES cli_installations(id) ON DELETE CASCADE,
  CONSTRAINT ck_provider_health_error CHECK (availability IN ('AVAILABLE','DEGRADED') OR error_category IS NOT NULL)
);
CREATE INDEX ix_provider_health_latest ON provider_health (installation_id, observed_at DESC);

-- §8.7 usage_metrics (T)
CREATE TABLE usage_metrics (
  id            uuid7          NOT NULL,
  workspace_id  uuid           NOT NULL,
  execution_id  uuid           NOT NULL,
  provider      cli_provider_t NOT NULL,
  metric_name   text           NOT NULL CHECK (metric_name ~ '^[a-z][a-z0-9_.]{0,99}$'),
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
"""

RLS = r"""
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['worktrees','cli_executions','usage_metrics']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO aicd_app, aicd_worker '
                   'USING (workspace_id = app_current_workspace()) '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_tenant', t);
  END LOOP;
END $$;
"""

SYSFN = r"""
-- Лимит параллелизма — свойство установки CLI (все workspace), а RLS скрывает чужие строки:
-- функция возвращает только число активных исполнений, без данных арендаторов (§19.5).
CREATE FUNCTION sys_active_executions(p_installation_id uuid)
RETURNS int
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT count(*)::int FROM aicd.cli_executions
   WHERE installation_id = p_installation_id AND status IN ('QUEUED','STARTING','RUNNING','EXITED')
$$;
GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_active_executions(uuid) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_active_executions(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_active_executions(uuid) TO aicd_worker;
GRANT SELECT (installation_id, status) ON cli_executions TO aicd_sysfn;
"""

GRANTS = r"""
REVOKE ALL ON allowed_transitions, worker_hosts, cli_providers, cli_installations, cli_sessions,
              worktrees, cli_executions, provider_health, usage_metrics FROM PUBLIC;
-- Справочники и установки: чтение для API (/providers), запись — воркеры (probe, heartbeat).
GRANT SELECT ON allowed_transitions, cli_providers TO aicd_app, aicd_worker;
GRANT SELECT ON worker_hosts, cli_installations, provider_health TO aicd_app;
GRANT SELECT, INSERT, UPDATE ON worker_hosts, cli_installations, cli_sessions TO aicd_worker;
GRANT SELECT, INSERT ON provider_health TO aicd_worker;
GRANT UPDATE (enabled) ON cli_providers TO aicd_worker;
-- Tenant-таблицы исполнения: API читает, воркеры пишут.
GRANT SELECT ON worktrees, cli_executions, usage_metrics TO aicd_app;
GRANT SELECT, INSERT, UPDATE ON worktrees, cli_executions TO aicd_worker;
GRANT SELECT, INSERT ON usage_metrics TO aicd_worker;
GRANT USAGE ON SEQUENCE worker_lease_fencing_seq TO aicd_worker;
GRANT SELECT ON allowed_transitions, worker_hosts, cli_providers, cli_installations, cli_sessions, worktrees,
               cli_executions, provider_health, usage_metrics TO aicd_maintenance;
GRANT DELETE ON provider_health TO aicd_maintenance;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_active_executions(uuid);
DROP TABLE IF EXISTS usage_metrics, provider_health, cli_executions, worktrees, cli_sessions,
                     cli_installations, cli_providers, worker_hosts, allowed_transitions CASCADE;
DROP FUNCTION IF EXISTS trg_fn_check_transition();
DROP SEQUENCE IF EXISTS worker_lease_fencing_seq;
"""


def upgrade() -> None:
    for block in (TABLES, RLS, SYSFN, GRANTS):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
