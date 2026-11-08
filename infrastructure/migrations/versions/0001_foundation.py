"""Foundation: домены, общие функции, IAM, workspaces, проекты, репозитории, idempotency, artifacts, audit.

Revision ID: 0001
Revises:
Create Date: 2026-11-07

Источник DDL: docs/database/DATABASE_DESIGN.md §3, §4, §5, §6.1–6.3, §12.1, §17.1, §18.5, §18.10, §19.
Отличия от документа, обусловленные порядком фаз (зависимые таблицы появляются в следующих миграциях):
  * project_settings.routing_policy_id — FK на policies добавляется миграцией Phase 3;
  * repositories.github_installation_id — FK на github_installations добавляется в Phase 8;
  * artifacts.run_id — FK на runs добавляется в Phase 3.
Дополнение к документу: workspace_members.version (API отдаёт ETag члена, API_DESIGN §9).

Обратимость: downgrade полностью удаляет созданные объекты (данные теряются — применять
только на пустой БД или после резервной копии).
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DOMAINS = r"""
CREATE DOMAIN uuid7       AS uuid CHECK (uuid_extract_version(VALUE) = 7);
CREATE DOMAIN git_sha     AS text CHECK (VALUE ~ '^[0-9a-f]{40}([0-9a-f]{24})?$');
CREATE DOMAIN sha256_hex  AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE DOMAIN run_mode_t AS text CHECK (VALUE IN
  ('CREATE','DEVELOP','DEBUG','AUDIT','MODERNIZE','SECURITY','OPTIMIZE','TEST','MAINTAIN','DOCUMENT','REVIEW'));
CREATE DOMAIN run_status_t AS text CHECK (VALUE IN
  ('CREATED','DISCOVERY','PLANNED','AWAITING_APPROVAL','RUNNING','VERIFYING','COMPLETED','PAUSED','BLOCKED','FAILED','CANCELLED'));
CREATE DOMAIN task_status_t AS text CHECK (VALUE IN
  ('PENDING','READY','LEASED','RUNNING','VERIFYING','SUCCEEDED','BLOCKED','RETRY_WAIT','FAILED','CANCELLED'));
CREATE DOMAIN task_blocked_reason_t AS text CHECK (VALUE IN
  ('APPROVAL_REQUIRED','PROVIDER_UNAVAILABLE','AUTH_REQUIRED','DIRECTIVE_REPLAN','RUN_PAUSED','DOCS_REQUIRED','MANUAL_REVIEW','REVIEW_LOOP_EXHAUSTED','DEPENDENCY_FAILED'));
CREATE DOMAIN execution_status_t AS text CHECK (VALUE IN
  ('QUEUED','STARTING','RUNNING','EXITED','COLLECTED','TIMED_OUT','CANCELLED','CRASHED','AUTH_REQUIRED','RATE_LIMITED'));
CREATE DOMAIN cli_error_code_t AS text CHECK (VALUE IN
  ('CLI_NOT_INSTALLED','AUTH_REQUIRED','RATE_LIMITED','TIMEOUT','SANDBOX_DENIED','PROCESS_FAILED','OUTPUT_PARSE_ERROR','UNSUPPORTED_VERSION','CANCELLED'));
CREATE DOMAIN failure_category_t AS text CHECK (VALUE IN
  ('CLI_NOT_INSTALLED','AUTH_REQUIRED','RATE_LIMITED','TIMEOUT','SANDBOX_DENIED','PROCESS_FAILED','OUTPUT_PARSE_ERROR','UNSUPPORTED_VERSION','CANCELLED',
   'GATE_FAILED','REVIEW_REJECTED','LEASE_LOST','WORKER_CRASH','POLICY_DENIED','INTERNAL_ERROR'));
CREATE DOMAIN cli_provider_t AS text CHECK (VALUE IN ('codex','claude'));
CREATE DOMAIN agent_role_t AS text CHECK (VALUE IN
  ('requirements','architect','planner','developer','reviewer','security','qa','database','performance','devops','maintenance','documentation'));
CREATE DOMAIN risk_level_t AS text CHECK (VALUE IN ('low','medium','high','critical'));
CREATE DOMAIN workspace_role_t AS text CHECK (VALUE IN ('owner','admin','developer','reviewer','viewer'));
CREATE DOMAIN acp_message_type_t AS text CHECK (VALUE IN
  ('TASK_ASSIGN','HANDOFF','QUESTION','ANSWER','REVIEW_REQUEST','FINDING','FIX_REQUEST','EVIDENCE_SUBMITTED','DECISION_PROPOSAL','DECISION_RECORDED','BLOCKED','TASK_COMPLETED'));
CREATE DOMAIN directive_type_t AS text CHECK (VALUE IN
  ('ASK','NEW_TASK','CHANGE_REQUEST','STOP','PAUSE','RESUME','REVIEW','APPROVE','REJECT'));
CREATE DOMAIN directive_status_t AS text CHECK (VALUE IN
  ('RECEIVED','IMPACT_ANALYSIS_PENDING','IMPACT_READY','AWAITING_CONFIRMATION','CONFIRMED','CHECKPOINTING','REPLANNING','DISPATCHED','ACKNOWLEDGED','COMPLETED','REJECTED','CANCELLED','FAILED'));
CREATE DOMAIN directive_impact_type_t AS text CHECK (VALUE IN
  ('NONE','CONTEXT_ONLY','REPLAN_REQUIRED','TASK_CANCEL','TASK_ADD','RESTART_REQUIRED'));
CREATE DOMAIN approval_action_type_t AS text CHECK (VALUE IN
  ('PLAN_APPROVAL','MERGE','PRODUCTION_DEPLOY','DESTRUCTIVE_MIGRATION','DATA_DELETION','SECRET_CHANGE','UNRESTRICTED_NETWORK','DOCS_DOWNLOAD','CRITICAL_DIRECTIVE','MANUAL_REVIEW','SELF_REVIEW_FALLBACK','LOCAL_EXECUTION_FALLBACK'));
CREATE DOMAIN approval_status_t AS text CHECK (VALUE IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED'));
CREATE DOMAIN documentation_mode_t AS text CHECK (VALUE IN ('OFF','SMART','STRICT'));
CREATE DOMAIN quality_profile_t AS text CHECK (VALUE IN ('basic','strict','critical'));
CREATE DOMAIN gate_execution_status_t AS text CHECK (VALUE IN
  ('PENDING','RUNNING','PASS','FAIL','ERROR','SKIPPED','NOT_APPLICABLE'));
CREATE DOMAIN compliance_status_t AS text CHECK (VALUE IN ('PASS','FAIL','NOT_APPLICABLE','UNVERIFIED','MANUAL_REVIEW'));
CREATE DOMAIN finding_severity_t AS text CHECK (VALUE IN ('info','low','medium','high','critical'));
CREATE DOMAIN finding_status_t AS text CHECK (VALUE IN
  ('OPEN','ACKNOWLEDGED','FIXED','VERIFIED_FIXED','FALSE_POSITIVE','ACCEPTED_RISK'));
CREATE DOMAIN evidence_verification_status_t AS text CHECK (VALUE IN ('PENDING','VERIFIED','REJECTED','UNVERIFIED'));
CREATE DOMAIN auth_status_t AS text CHECK (VALUE IN ('UNKNOWN','AUTHENTICATED','AUTH_REQUIRED','EXPIRED','ERROR'));
CREATE DOMAIN provider_availability_t AS text CHECK (VALUE IN ('AVAILABLE','DEGRADED','RATE_LIMITED','UNAVAILABLE'));
CREATE DOMAIN normalized_cli_event_t AS text CHECK (VALUE IN ('started','output','tool_event','artifact','warning','finished'));
CREATE DOMAIN sse_event_type_persisted_t AS text CHECK (VALUE IN
  ('run.state_changed','task.state_changed','agent.message','cli.output','directive.impact_ready','directive.state_changed',
   'approval.requested','approval.decided','gate.completed','run.completed','chat.message'));
"""

FUNCTIONS = r"""
CREATE FUNCTION app_current_workspace() RETURNS uuid
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT NULLIF(current_setting('app.workspace_id', true), '')::uuid $$;
CREATE FUNCTION app_current_user() RETURNS uuid
  LANGUAGE sql STABLE PARALLEL SAFE
  AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;

CREATE FUNCTION trg_fn_set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

CREATE FUNCTION trg_fn_enforce_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
    RAISE EXCEPTION 'version must be incremented by 1 (old=%, new=%)', OLD.version, NEW.version
      USING ERRCODE = '40001';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION trg_fn_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = '42501';
END $$;

CREATE FUNCTION trg_fn_immutable_workspace() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id THEN
    RAISE EXCEPTION 'workspace_id is immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

-- Ранг роли для правил «только понижение» (§18.10).
CREATE FUNCTION workspace_role_rank(r text) RETURNS int LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE r WHEN 'owner' THEN 5 WHEN 'admin' THEN 4 WHEN 'developer' THEN 3
                WHEN 'reviewer' THEN 2 WHEN 'viewer' THEN 1 END
$$;

-- Помесячные партиции (§20.1): создаёт партиции от месяца p_from на p_months вперёд.
-- Каждая партиция получает ENABLE/FORCE RLS (политики родителя применяются к запросам через родителя,
-- прямой доступ к партициям отозван).
CREATE FUNCTION maint_ensure_monthly_partitions(p_table text, p_from date, p_months int)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE
  m date := date_trunc('month', p_from)::date;
  created int := 0;
  part text;
BEGIN
  IF p_table NOT IN ('audit_events') THEN
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
"""

TABLES = r"""
-- §5.1 users (G)
CREATE TABLE users (
  id               uuid7       NOT NULL,
  email            text        NOT NULL CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 320),
  display_name     text        NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
  status           text        NOT NULL DEFAULT 'ACTIVE'
                               CONSTRAINT ck_users_status CHECK (status IN ('INVITED','ACTIVE','DISABLED','DELETED')),
  locale           text        NOT NULL DEFAULT 'ru',
  last_login_at    timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_users PRIMARY KEY (id),
  CONSTRAINT uq_users_email UNIQUE (email)
);
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();

-- §5.2 user_credentials (G) — дополнение к ТЗ
CREATE TABLE user_credentials (
  user_id              uuid        NOT NULL,
  password_hash        text        NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
  password_changed_at  timestamptz NOT NULL DEFAULT now(),
  failed_attempts      int         NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
  locked_until         timestamptz,
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_user_credentials PRIMARY KEY (user_id),
  CONSTRAINT fk_user_credentials_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TRIGGER trg_user_credentials_updated_at BEFORE UPDATE ON user_credentials FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();

-- §5.3 user_sessions (G) — дополнение к ТЗ
CREATE TABLE user_sessions (
  id               uuid7       NOT NULL,
  user_id          uuid        NOT NULL,
  token_hash       sha256_hex  NOT NULL,
  csrf_token_hash  sha256_hex  NOT NULL,
  ip               inet,
  user_agent       text        CHECK (length(user_agent) <= 512),
  created_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at     timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  revoked_at       timestamptz,
  revoke_reason    text        CHECK (revoke_reason IN ('logout','expired','password_changed','admin_revoked','user_disabled')),
  CONSTRAINT pk_user_sessions PRIMARY KEY (id),
  CONSTRAINT uq_user_sessions_token_hash UNIQUE (token_hash),
  CONSTRAINT fk_user_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_user_sessions_expiry CHECK (expires_at > created_at),
  CONSTRAINT ck_user_sessions_revoke CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL))
);
CREATE INDEX ix_user_sessions_user_active ON user_sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_user_sessions_expires ON user_sessions (expires_at) WHERE revoked_at IS NULL;

-- §5.4 workspaces (T*)
CREATE TABLE workspaces (
  id          uuid7       NOT NULL,
  name        text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  slug        text        NOT NULL CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'),
  status      text        NOT NULL DEFAULT 'ACTIVE'
                          CONSTRAINT ck_workspaces_status CHECK (status IN ('ACTIVE','ARCHIVED','DELETION_PENDING')),
  settings    jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(settings) = 'object'),
  created_by  uuid        NOT NULL,
  version     bigint      NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_workspaces PRIMARY KEY (id),
  CONSTRAINT uq_workspaces_slug UNIQUE (slug),
  CONSTRAINT fk_workspaces_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE TRIGGER trg_workspaces_updated_at BEFORE UPDATE ON workspaces FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_workspaces_version BEFORE UPDATE ON workspaces FOR EACH ROW EXECUTE FUNCTION trg_fn_enforce_version();

-- §5.5 workspace_members (T); version — дополнение (ETag члена, API_DESIGN §9)
CREATE TABLE workspace_members (
  workspace_id  uuid             NOT NULL,
  user_id       uuid             NOT NULL,
  role          workspace_role_t NOT NULL,
  invited_by    uuid,
  version       bigint           NOT NULL DEFAULT 0,
  joined_at     timestamptz      NOT NULL DEFAULT now(),
  updated_at    timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT pk_workspace_members PRIMARY KEY (workspace_id, user_id),
  CONSTRAINT fk_workspace_members_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_workspace_members_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_workspace_members_invited_by FOREIGN KEY (invited_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX ix_workspace_members_user ON workspace_members (user_id);
CREATE INDEX ix_workspace_members_owners ON workspace_members (workspace_id) WHERE role = 'owner';
CREATE TRIGGER trg_workspace_members_updated_at BEFORE UPDATE ON workspace_members FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_workspace_members_version BEFORE UPDATE ON workspace_members FOR EACH ROW EXECUTE FUNCTION trg_fn_enforce_version();
CREATE TRIGGER trg_workspace_members_immutable_ws BEFORE UPDATE OF workspace_id ON workspace_members FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- §18.10: в ACTIVE workspace всегда ≥ 1 owner (deferred — проверяется в конце транзакции).
CREATE FUNCTION trg_fn_workspace_last_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM workspaces w WHERE w.id = OLD.workspace_id AND w.status = 'ACTIVE')
     AND NOT EXISTS (SELECT 1 FROM workspace_members m WHERE m.workspace_id = OLD.workspace_id AND m.role = 'owner') THEN
    RAISE EXCEPTION 'workspace % must keep at least one owner', OLD.workspace_id USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_workspace_members_last_owner
  AFTER UPDATE OR DELETE ON workspace_members DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION trg_fn_workspace_last_owner();

-- §6.1 projects (T)
CREATE TABLE projects (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  name          text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  description   text        CHECK (length(description) <= 10000),
  status        text        NOT NULL DEFAULT 'ACTIVE'
                            CONSTRAINT ck_projects_status CHECK (status IN ('ACTIVE','ARCHIVED','DELETION_PENDING')),
  created_by    uuid        NOT NULL,
  version       bigint      NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_projects PRIMARY KEY (id),
  CONSTRAINT uq_projects_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_projects_ws_name UNIQUE (workspace_id, name),
  CONSTRAINT fk_projects_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_projects_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE INDEX ix_projects_ws_status ON projects (workspace_id, status, created_at DESC);
CREATE TRIGGER trg_projects_updated_at BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_projects_version BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION trg_fn_enforce_version();
CREATE TRIGGER trg_projects_immutable_ws BEFORE UPDATE OF workspace_id ON projects FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- §6.2 project_settings (T); FK routing_policy_id → policies добавляется в Phase 3
CREATE TABLE project_settings (
  project_id          uuid                 NOT NULL,
  workspace_id        uuid                 NOT NULL,
  documentation_mode  documentation_mode_t NOT NULL DEFAULT 'SMART',
  routing_policy_id   uuid,
  quality_profile     quality_profile_t    NOT NULL DEFAULT 'strict',
  security_profile    text                 NOT NULL DEFAULT 'standard'
                                           CHECK (security_profile IN ('standard','hardened')),
  approval_policy     text                 NOT NULL DEFAULT 'risk_based'
                                           CHECK (approval_policy IN ('risk_based','always','critical_only')),
  max_parallel_tasks  int                  NOT NULL DEFAULT 4 CHECK (max_parallel_tasks BETWEEN 1 AND 64),
  max_review_loops    int                  NOT NULL DEFAULT 3 CHECK (max_review_loops BETWEEN 0 AND 10),
  settings_version    int                  NOT NULL DEFAULT 1 CHECK (settings_version >= 1),
  version             bigint               NOT NULL DEFAULT 0,
  updated_by          uuid,
  updated_at          timestamptz          NOT NULL DEFAULT now(),
  CONSTRAINT pk_project_settings PRIMARY KEY (project_id),
  CONSTRAINT fk_project_settings_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_project_settings_updated_by FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TRIGGER trg_project_settings_updated_at BEFORE UPDATE ON project_settings FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_project_settings_version BEFORE UPDATE ON project_settings FOR EACH ROW EXECUTE FUNCTION trg_fn_enforce_version();
CREATE TRIGGER trg_project_settings_immutable_ws BEFORE UPDATE OF workspace_id ON project_settings FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- §5.6 project_member_overrides (T) — дополнение к ТЗ
CREATE TABLE project_member_overrides (
  workspace_id  uuid             NOT NULL,
  project_id    uuid             NOT NULL,
  user_id       uuid             NOT NULL,
  role          workspace_role_t NOT NULL CHECK (role IN ('developer','reviewer','viewer')),
  set_by        uuid             NOT NULL,
  created_at    timestamptz      NOT NULL DEFAULT now(),
  CONSTRAINT pk_project_member_overrides PRIMARY KEY (project_id, user_id),
  CONSTRAINT fk_pmo_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_pmo_member  FOREIGN KEY (workspace_id, user_id) REFERENCES workspace_members(workspace_id, user_id) ON DELETE CASCADE,
  CONSTRAINT fk_pmo_set_by  FOREIGN KEY (set_by) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE TRIGGER trg_project_member_overrides_immutable_ws BEFORE UPDATE OF workspace_id ON project_member_overrides FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

CREATE FUNCTION trg_fn_pmo_downgrade_only() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ws_role text;
BEGIN
  SELECT role INTO ws_role FROM workspace_members WHERE workspace_id = NEW.workspace_id AND user_id = NEW.user_id;
  IF ws_role IS NULL OR workspace_role_rank(NEW.role) >= workspace_role_rank(ws_role) THEN
    RAISE EXCEPTION 'project override must be lower than workspace role' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_project_member_overrides_downgrade_only BEFORE INSERT OR UPDATE ON project_member_overrides
  FOR EACH ROW EXECUTE FUNCTION trg_fn_pmo_downgrade_only();

-- При понижении роли в workspace удаляются переопределения, ставшие «не ниже» (§18.10).
CREATE FUNCTION trg_fn_member_role_prune_overrides() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM project_member_overrides o
   WHERE o.workspace_id = NEW.workspace_id AND o.user_id = NEW.user_id
     AND workspace_role_rank(o.role) >= workspace_role_rank(NEW.role);
  RETURN NULL;
END $$;
CREATE TRIGGER trg_workspace_members_prune_overrides AFTER UPDATE OF role ON workspace_members
  FOR EACH ROW EXECUTE FUNCTION trg_fn_member_role_prune_overrides();

-- §6.3 repositories (T); FK github_installation_id → github_installations добавляется в Phase 8
CREATE TABLE repositories (
  id                      uuid7       NOT NULL,
  workspace_id            uuid        NOT NULL,
  project_id              uuid        NOT NULL,
  provider                text        NOT NULL CHECK (provider IN ('local','github')),
  remote_url              text        CHECK (remote_url IS NULL OR remote_url ~ '^(https://|ssh://|git@)'),
  default_branch          text        NOT NULL DEFAULT 'main' CHECK (length(default_branch) BETWEEN 1 AND 255),
  local_root_ref          text        NOT NULL,
  github_installation_id  uuid,
  github_repo_id          bigint,
  full_name               text,
  status                  text        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','UNREACHABLE','DETACHED')),
  last_synced_at          timestamptz,
  last_synced_commit_sha  git_sha,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_repositories PRIMARY KEY (id),
  CONSTRAINT uq_repositories_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_repositories_local_root UNIQUE (workspace_id, local_root_ref),
  CONSTRAINT fk_repositories_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_repositories_github CHECK (provider <> 'github' OR (remote_url IS NOT NULL AND full_name IS NOT NULL)),
  CONSTRAINT ck_repositories_local_root_ref CHECK (local_root_ref !~ '(^/|\.\.)')
);
CREATE INDEX ix_repositories_project ON repositories (workspace_id, project_id);
CREATE UNIQUE INDEX uq_repositories_github_repo ON repositories (workspace_id, github_repo_id) WHERE github_repo_id IS NOT NULL;
CREATE TRIGGER trg_repositories_updated_at BEFORE UPDATE ON repositories FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_repositories_immutable_ws BEFORE UPDATE OF workspace_id ON repositories FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- §5.7 idempotency_keys (M) — дополнение к ТЗ
CREATE TABLE idempotency_keys (
  id             uuid7       NOT NULL,
  workspace_id   uuid,
  user_id        uuid        NOT NULL,
  key            text        NOT NULL CHECK (length(key) BETWEEN 8 AND 255),
  method         text        NOT NULL CHECK (method IN ('POST','PUT','PATCH','DELETE')),
  path           text        NOT NULL CHECK (length(path) <= 2048),
  request_hash   sha256_hex  NOT NULL,
  status         text        NOT NULL DEFAULT 'IN_PROGRESS' CHECK (status IN ('IN_PROGRESS','COMPLETED')),
  status_code    smallint    CHECK (status_code BETWEEN 100 AND 599),
  response       jsonb       CHECK (response IS NULL OR octet_length(response::text) <= 65536),
  response_ref   text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL DEFAULT now() + interval '24 hours',
  CONSTRAINT pk_idempotency_keys PRIMARY KEY (id),
  CONSTRAINT uq_idempotency_keys_scope UNIQUE NULLS NOT DISTINCT (workspace_id, user_id, key),
  CONSTRAINT fk_idempotency_keys_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
  CONSTRAINT fk_idempotency_keys_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_idempotency_keys_completed CHECK (status = 'IN_PROGRESS' OR status_code IS NOT NULL)
);
CREATE INDEX ix_idempotency_keys_expires ON idempotency_keys (expires_at);

-- §12.1 artifacts (T); FK run_id → runs добавляется в Phase 3
CREATE TABLE artifacts (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  project_id       uuid,
  run_id           uuid,
  kind             text        NOT NULL CHECK (kind IN
                     ('git_diff','cli_raw_log','cli_output','test_report','coverage_report','scan_report','sbom',
                      'context_pack','checkpoint_state','documentation','knowledge','report','archive','other')),
  storage_ref      text        NOT NULL CHECK (storage_ref ~ '^(s3|file)://[A-Za-z0-9._/-]+$' AND length(storage_ref) <= 1100),
  sha256           sha256_hex  NOT NULL,
  size_bytes       bigint      NOT NULL CHECK (size_bytes >= 0),
  mime_type        text        NOT NULL CHECK (mime_type ~ '^[a-z]+/[a-z0-9.+-]+$'),
  is_sensitive     boolean     NOT NULL DEFAULT false,
  redaction_applied boolean    NOT NULL DEFAULT false,
  retention_class  text        NOT NULL DEFAULT 'standard' CHECK (retention_class IN ('ephemeral','standard','evidence','archive')),
  expires_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_artifacts PRIMARY KEY (id),
  CONSTRAINT uq_artifacts_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_artifacts_storage_ref UNIQUE (storage_ref),
  CONSTRAINT fk_artifacts_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_artifacts_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT ck_artifacts_raw_log_sensitive CHECK (kind <> 'cli_raw_log' OR is_sensitive),
  CONSTRAINT ck_artifacts_evidence_no_expiry CHECK (retention_class <> 'evidence' OR expires_at IS NULL)
);
CREATE INDEX ix_artifacts_run ON artifacts (workspace_id, run_id, kind) WHERE run_id IS NOT NULL;
CREATE INDEX ix_artifacts_expiry ON artifacts (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX ix_artifacts_sha256 ON artifacts (workspace_id, sha256);
CREATE FUNCTION trg_fn_artifacts_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (to_jsonb(NEW) - 'retention_class' - 'expires_at') IS DISTINCT FROM (to_jsonb(OLD) - 'retention_class' - 'expires_at') THEN
    RAISE EXCEPTION 'artifacts are immutable except retention_class/expires_at' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_artifacts_immutable BEFORE UPDATE ON artifacts FOR EACH ROW EXECUTE FUNCTION trg_fn_artifacts_immutable();

-- §17.1 audit_events (M), помесячные партиции, append-only (§18.5)
CREATE TABLE audit_events (
  id             uuid7       NOT NULL,
  workspace_id   uuid,
  actor_type     text        NOT NULL CHECK (actor_type IN ('user','agent','system','github')),
  actor_id       uuid,
  actor_role     text,
  action         text        NOT NULL CHECK (action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
  resource_type  text        NOT NULL CHECK (resource_type ~ '^[a-z][a-z0-9_]{0,63}$'),
  resource_id    uuid,
  outcome        text        NOT NULL DEFAULT 'success' CHECK (outcome IN ('success','denied','failure')),
  request_id     uuid,
  correlation_id uuid,
  traceparent    text,
  ip             inet,
  metadata       jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 16384),
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_audit_events PRIMARY KEY (id, created_at),
  CONSTRAINT ck_audit_events_actor CHECK (actor_type <> 'user' OR actor_id IS NOT NULL)
) PARTITION BY RANGE (created_at);
CREATE INDEX ix_audit_events_ws_time ON audit_events (workspace_id, created_at DESC);
CREATE INDEX ix_audit_events_resource ON audit_events (resource_type, resource_id, created_at DESC);
CREATE INDEX ix_audit_events_actor ON audit_events (actor_id, created_at DESC) WHERE actor_id IS NOT NULL;
CREATE TRIGGER trg_audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
SELECT maint_ensure_monthly_partitions('audit_events', (now() - interval '1 month')::date, 4);
"""

RLS = r"""
-- Корень tenancy (§19.3)
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY p_workspaces_select ON workspaces FOR SELECT TO aicd_app, aicd_worker
  USING (id = app_current_workspace());
CREATE POLICY p_workspaces_update ON workspaces FOR UPDATE TO aicd_app
  USING (id = app_current_workspace()) WITH CHECK (id = app_current_workspace());
CREATE POLICY p_workspaces_create ON workspaces FOR INSERT TO aicd_app
  WITH CHECK (id = app_current_workspace() AND created_by = app_current_user());

-- T-таблицы
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['workspace_members','projects','project_settings','project_member_overrides',
                           'repositories','artifacts']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO aicd_app, aicd_worker '
                   'USING (workspace_id = app_current_workspace()) '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_tenant', t);
  END LOOP;
END $$;

-- M-таблицы
ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY p_idempotency_keys ON idempotency_keys FOR ALL TO aicd_app
  USING (workspace_id = app_current_workspace()
         OR (workspace_id IS NULL AND user_id = app_current_user()))
  WITH CHECK (workspace_id = app_current_workspace()
         OR (workspace_id IS NULL AND user_id = app_current_user()));

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY p_audit_events_read ON audit_events FOR SELECT TO aicd_app
  USING (workspace_id = app_current_workspace());
CREATE POLICY p_audit_events_insert ON audit_events FOR INSERT TO aicd_app, aicd_worker
  WITH CHECK (workspace_id IS NULL OR workspace_id = app_current_workspace());
"""

SYSFN = r"""
-- §19.5: SECURITY DEFINER функции межарендаторного доступа. Владелец aicd_sysfn (BYPASSRLS),
-- фиксированный search_path, возвращают только данные текущего пользователя (app_current_user()).

CREATE FUNCTION sys_list_user_workspaces()
RETURNS TABLE (workspace_id uuid, slug text, name text, role text, settings jsonb, created_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT w.id, w.slug, w.name, m.role::text, w.settings, w.created_at
    FROM aicd.workspace_members m
    JOIN aicd.workspaces w ON w.id = m.workspace_id
   WHERE m.user_id = aicd.app_current_user() AND w.status = 'ACTIVE'
   ORDER BY w.created_at DESC, w.id DESC
$$;

-- Роль текущего пользователя в workspace (NULL — не член; API отвечает 404, API_DESIGN §4).
CREATE FUNCTION sys_membership_role(p_workspace_id uuid)
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT m.role::text
    FROM aicd.workspace_members m
    JOIN aicd.workspaces w ON w.id = m.workspace_id
   WHERE m.workspace_id = p_workspace_id AND m.user_id = aicd.app_current_user() AND w.status = 'ACTIVE'
$$;

-- Workspace проекта, только если текущий пользователь — член этого workspace.
CREATE FUNCTION sys_resolve_project_workspace(p_project_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT p.workspace_id
    FROM aicd.projects p
    JOIN aicd.workspace_members m ON m.workspace_id = p.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE p.id = p_project_id
$$;

-- Эффективная роль в проекте: min(роль workspace, понижающее переопределение) (API_DESIGN §5).
CREATE FUNCTION sys_project_role(p_project_id uuid)
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT COALESCE(o.role, m.role)::text
    FROM aicd.projects p
    JOIN aicd.workspace_members m ON m.workspace_id = p.workspace_id AND m.user_id = aicd.app_current_user()
    LEFT JOIN aicd.project_member_overrides o ON o.project_id = p.id AND o.user_id = m.user_id
   WHERE p.id = p_project_id
$$;

-- Передача владения требует CREATE на схеме у нового владельца — выдаём только на время передачи.
GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY['sys_list_user_workspaces()', 'sys_membership_role(uuid)',
                           'sys_resolve_project_workspace(uuid)', 'sys_project_role(uuid)']
  LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO aicd_sysfn', f);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO aicd_app', f);
  END LOOP;
END $$;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
GRANT SELECT (id, slug, name, status, settings, created_at) ON workspaces TO aicd_sysfn;
GRANT SELECT (workspace_id, user_id, role) ON workspace_members TO aicd_sysfn;
GRANT SELECT (id, workspace_id) ON projects TO aicd_sysfn;
GRANT SELECT (project_id, user_id, role) ON project_member_overrides TO aicd_sysfn;
"""

GRANTS = r"""
REVOKE ALL ON ALL TABLES IN SCHEMA aicd FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_current_workspace(), app_current_user(), workspace_role_rank(text) TO aicd_app, aicd_worker, aicd_sysfn;

-- api (identity, projects, platform)
GRANT SELECT, INSERT, UPDATE ON users TO aicd_app;
GRANT SELECT, INSERT, UPDATE ON user_credentials, user_sessions TO aicd_app;
GRANT SELECT, INSERT, UPDATE ON workspaces TO aicd_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON workspace_members, project_member_overrides TO aicd_app;
GRANT SELECT, INSERT, UPDATE ON projects, project_settings, repositories TO aicd_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_keys TO aicd_app;
GRANT SELECT, INSERT ON artifacts TO aicd_app;
GRANT UPDATE (retention_class, expires_at) ON artifacts TO aicd_app;
GRANT SELECT, INSERT ON audit_events TO aicd_app;
-- /health/ready сверяет ревизию схемы (alembic_version создаётся Alembic до первой миграции).
GRANT SELECT ON alembic_version TO aicd_app, aicd_worker;

-- workers: чтение контекста проектов, артефакты и аудит своих действий
GRANT SELECT ON users TO aicd_worker;
GRANT SELECT ON workspaces, workspace_members, projects, project_settings, project_member_overrides, repositories TO aicd_worker;
GRANT UPDATE (status, last_synced_at, last_synced_commit_sha) ON repositories TO aicd_worker;
GRANT SELECT, INSERT ON artifacts TO aicd_worker;
GRANT INSERT ON audit_events TO aicd_worker;

-- maintenance: retention/архив (§19.6); UPDATE/DELETE аудита запрещены триггером
GRANT SELECT ON ALL TABLES IN SCHEMA aicd TO aicd_maintenance;
GRANT DELETE ON idempotency_keys, user_sessions TO aicd_maintenance;
GRANT DELETE ON artifacts TO aicd_maintenance;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_project_role(uuid);
DROP FUNCTION IF EXISTS sys_resolve_project_workspace(uuid);
DROP FUNCTION IF EXISTS sys_membership_role(uuid);
DROP FUNCTION IF EXISTS sys_list_user_workspaces();
DROP TABLE IF EXISTS audit_events CASCADE;
DROP TABLE IF EXISTS artifacts CASCADE;
DROP TABLE IF EXISTS idempotency_keys CASCADE;
DROP TABLE IF EXISTS repositories CASCADE;
DROP TABLE IF EXISTS project_member_overrides CASCADE;
DROP TABLE IF EXISTS project_settings CASCADE;
DROP TABLE IF EXISTS projects CASCADE;
DROP TABLE IF EXISTS workspace_members CASCADE;
DROP TABLE IF EXISTS workspaces CASCADE;
DROP TABLE IF EXISTS user_sessions CASCADE;
DROP TABLE IF EXISTS user_credentials CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP FUNCTION IF EXISTS trg_fn_artifacts_immutable();
DROP FUNCTION IF EXISTS trg_fn_member_role_prune_overrides();
DROP FUNCTION IF EXISTS trg_fn_pmo_downgrade_only();
DROP FUNCTION IF EXISTS trg_fn_workspace_last_owner();
DROP FUNCTION IF EXISTS maint_ensure_monthly_partitions(text, date, int);
DROP FUNCTION IF EXISTS workspace_role_rank(text);
DROP FUNCTION IF EXISTS trg_fn_immutable_workspace();
DROP FUNCTION IF EXISTS trg_fn_append_only();
DROP FUNCTION IF EXISTS trg_fn_enforce_version();
DROP FUNCTION IF EXISTS trg_fn_set_updated_at();
DROP FUNCTION IF EXISTS app_current_user();
DROP FUNCTION IF EXISTS app_current_workspace();
DROP DOMAIN IF EXISTS sse_event_type_persisted_t, normalized_cli_event_t, provider_availability_t, auth_status_t,
  evidence_verification_status_t, finding_status_t, finding_severity_t, compliance_status_t, gate_execution_status_t,
  quality_profile_t, documentation_mode_t, approval_status_t, approval_action_type_t, directive_impact_type_t,
  directive_status_t, directive_type_t, acp_message_type_t, workspace_role_t, risk_level_t, agent_role_t,
  cli_provider_t, failure_category_t, cli_error_code_t, execution_status_t, task_blocked_reason_t, task_status_t,
  run_status_t, run_mode_t, sha256_hex, git_sha, uuid7;
"""


def upgrade() -> None:
    for block in (DOMAINS, FUNCTIONS, TABLES, RLS, SYSFN, GRANTS):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
