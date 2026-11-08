"""Core Orchestrator: runs/tasks/DAG, approvals, directives, чат, качество, outbox, leases, ledger, журнал SSE.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08

Источник DDL: docs/database/DATABASE_DESIGN.md §7.1–7.4, §7.6–7.7, §9.3–9.8, §12.2–12.4, §12.7, §14,
§15, §18.1–18.8, §19.3–19.5, §20.1. CREATE TABLE перенесены из документа без изменений, кроме:
  * FK на таблицы следующих фаз опущены (добавит миграция Phase 4 вместе с таблицами):
      - agent_assignments.agent_profile_id → agent_profiles (fk_agent_assignments_profile);
      - agent_decisions.acp_message_id → agent_messages (fk_agent_decisions_message);
      - chat_messages.acp_message_id → agent_messages (fk_chat_messages_acp);
      - directive_acknowledgements.acp_message_id → agent_messages (fk_directive_ack_message);
  * fk_task_attempts_assignment добавляется после agent_assignments (циклическая зависимость);
  * evidence создаётся раньше gate_executions (FK gate_executions.evidence_id);
  * worker_lease_fencing_seq уже создана миграцией 0002;
  * trg_runs_version: вместо «BEFORE UPDATE OF <все колонки, кроме last_event_seq/updated_at>» (§15.6)
    — WHEN-условие по значениям: проверка версии пропускается, если изменились только
    last_event_seq/updated_at (устойчиво к добавлению колонок, OQ-DB-10);
  * trg_fn_lock_run_share: в FAILED/CANCELLED run разрешены закрывающие записи (задачи → CANCELLED/
    FAILED, approvals → CANCELLED/EXPIRED, gate → ERROR/SKIPPED), в COMPLETED — никакие;
  * trg_tasks_success_guard: обязательный gate задачи должен быть PASS/NOT_APPLICABLE (как §18.4 для run);
  * trg_project_settings_policy_scope дополнительно требует policies.project_id IS NULL или = project_id.
Добавлены FK, отложенные миграциями 0001/0002: worktrees.run_id/task_id, cli_executions.task_attempt_id,
artifacts.run_id, project_settings.routing_policy_id (+ trg_project_settings_policy_scope, §6.2).
maint_ensure_monthly_partitions расширена таблицей outbox_events (§20.1).
Seed: глобальные quality_profiles basic/strict/critical v1 с минимальным definition (пороги блокировки
находок и покрытия по QUALITY_GATES.md); полный набор gates и порогов добавит Phase 6 новой версией профиля.

Обратимость: downgrade удаляет созданные объекты и FK, возвращает maint_ensure_monthly_partitions и
allowed_transitions к состоянию 0002 (данные оркестрации теряются — только на пустой БД или после бэкапа).
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = r"""
-- §12.2 quality_profiles
CREATE TABLE quality_profiles (
  id            uuid7             NOT NULL,
  workspace_id  uuid,
  code          quality_profile_t NOT NULL,
  version       int               NOT NULL CHECK (version >= 1),   -- версия содержимого; строка неизменяема
  definition    jsonb             NOT NULL CHECK (jsonb_typeof(definition) = 'object'),  -- пороги: coverage, severity block и т.п.
  active        boolean           NOT NULL DEFAULT true,
  created_by    uuid,
  created_at    timestamptz       NOT NULL DEFAULT now(),
  CONSTRAINT pk_quality_profiles PRIMARY KEY (id),
  CONSTRAINT uq_quality_profiles_version UNIQUE NULLS NOT DISTINCT (workspace_id, code, version),
  CONSTRAINT fk_quality_profiles_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_quality_profiles_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX uq_quality_profiles_active ON quality_profiles (workspace_id, code) NULLS NOT DISTINCT WHERE active;

-- §12.3 quality_gates
CREATE TABLE quality_gates (
  id               uuid7       NOT NULL,
  workspace_id     uuid,       -- копия quality_profiles.workspace_id (для RLS без JOIN)
  profile_id       uuid        NOT NULL,
  name             text        NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),   -- напр. 'ruff', 'pytest-unit', 'semgrep'
  gate_type        text        NOT NULL CHECK (gate_type IN
                     ('FORMAT','LINT','TYPECHECK','BUILD','UNIT','INTEGRATION','E2E','SAST','SCA','SECRETS',
                      'IAC','POLICY','MIGRATION','PERF','REVIEW','COVERAGE','SBOM','SIGN','SCOPE')),
  required         boolean     NOT NULL DEFAULT true,
  config           jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config) = 'object'),  -- tool, args-профиль, пороги
  applicability    jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(applicability) = 'object'), -- языки/пути/технологии
  timeout_seconds  int         NOT NULL DEFAULT 900 CHECK (timeout_seconds BETWEEN 10 AND 86400),
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_quality_gates PRIMARY KEY (id),
  CONSTRAINT uq_quality_gates_profile_name UNIQUE (profile_id, name),
  CONSTRAINT fk_quality_gates_profile FOREIGN KEY (profile_id) REFERENCES quality_profiles(id) ON DELETE RESTRICT
);
CREATE INDEX ix_quality_gates_profile ON quality_gates (profile_id, gate_type);
-- trg_quality_gates_ws_match: BEFORE INSERT/UPDATE — NEW.workspace_id IS NOT DISTINCT FROM quality_profiles.workspace_id.

-- §14.1 policies
CREATE TABLE policies (
  id            uuid7       NOT NULL,
  workspace_id  uuid,
  project_id    uuid,
  policy_type   text        NOT NULL CHECK (policy_type IN
                  ('routing','approval','sandbox','network','documentation','review','acp_acl','provider_settings','retention')),
  name          text        NOT NULL DEFAULT 'default' CHECK (name ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
  version       int         NOT NULL CHECK (version >= 1),
  definition    jsonb       NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
  active        boolean     NOT NULL DEFAULT true,
  created_by    uuid,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_policies PRIMARY KEY (id),
  CONSTRAINT uq_policies_version UNIQUE NULLS NOT DISTINCT (workspace_id, project_id, policy_type, name, version),
  CONSTRAINT fk_policies_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_policies_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_policies_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT ck_policies_scope CHECK (project_id IS NULL OR workspace_id IS NOT NULL)
);
CREATE UNIQUE INDEX uq_policies_active ON policies (workspace_id, project_id, policy_type, name) NULLS NOT DISTINCT WHERE active;
-- trg_policies_immutable: BEFORE UPDATE — разрешено изменять только active.

-- §7.1 runs
CREATE TABLE runs (
  id                    uuid7                  NOT NULL,
  workspace_id          uuid                   NOT NULL,
  project_id            uuid                   NOT NULL,
  repository_id         uuid                   NOT NULL,
  requested_by          uuid                   NOT NULL,
  mode                  run_mode_t             NOT NULL,
  objective             text                   NOT NULL CHECK (length(objective) BETWEEN 1 AND 20000),
  status                run_status_t           NOT NULL DEFAULT 'CREATED',
  blocked_reason        task_blocked_reason_t,
  resume_status         run_status_t,          -- состояние, в которое вернуться из PAUSED/BLOCKED
  base_commit_sha       git_sha,
  quality_profile       quality_profile_t      NOT NULL,
  quality_profile_id    uuid                   NOT NULL,   -- конкретная версия профиля (snapshot)
  approval_policy       text                   NOT NULL DEFAULT 'risk_based'
                                               CHECK (approval_policy IN ('risk_based','always','critical_only')),
  documentation_mode    documentation_mode_t   NOT NULL,   -- snapshot project_settings на момент создания
  policy_snapshot       jsonb                  NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(policy_snapshot) = 'object'),
  plan_version          int                    NOT NULL DEFAULT 0 CHECK (plan_version >= 0),
  approved_plan_version int                    CHECK (approved_plan_version >= 1),
  last_event_seq        bigint                 NOT NULL DEFAULT 0 CHECK (last_event_seq >= 0),
  failure_category      failure_category_t,
  failure_message       text                   CHECK (length(failure_message) <= 4000),
  cancel_requested_at   timestamptz,
  version               bigint                 NOT NULL DEFAULT 0,
  created_at            timestamptz            NOT NULL DEFAULT now(),
  updated_at            timestamptz            NOT NULL DEFAULT now(),
  started_at            timestamptz,
  finished_at           timestamptz,
  CONSTRAINT pk_runs PRIMARY KEY (id),
  CONSTRAINT uq_runs_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_runs_project    FOREIGN KEY (workspace_id, project_id)    REFERENCES projects(workspace_id, id)     ON DELETE RESTRICT,
  CONSTRAINT fk_runs_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_runs_requested_by FOREIGN KEY (requested_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_runs_quality_profile FOREIGN KEY (quality_profile_id) REFERENCES quality_profiles(id) ON DELETE RESTRICT,
  CONSTRAINT ck_runs_blocked_reason CHECK ((status = 'BLOCKED') = (blocked_reason IS NOT NULL)),
  CONSTRAINT ck_runs_resume_status CHECK (
      (resume_status IS NULL OR status IN ('PAUSED','BLOCKED'))
      AND (resume_status IS NULL OR resume_status IN ('DISCOVERY','PLANNED','AWAITING_APPROVAL','RUNNING','VERIFYING'))),
  CONSTRAINT ck_runs_finished CHECK ((status IN ('COMPLETED','FAILED','CANCELLED')) = (finished_at IS NOT NULL)),
  CONSTRAINT ck_runs_failure CHECK (status = 'FAILED' OR failure_category IS NULL),
  CONSTRAINT ck_runs_plan_version CHECK (approved_plan_version IS NULL OR approved_plan_version <= plan_version)
);
CREATE INDEX ix_runs_project_status_created ON runs (workspace_id, project_id, status, created_at DESC);  -- ТЗ §7.3 + workspace_id
CREATE INDEX ix_runs_active ON runs (workspace_id, status) WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED');
CREATE INDEX ix_runs_requested_by ON runs (workspace_id, requested_by, created_at DESC);
-- Специфичные триггеры: trg_runs_transition (§18.3), trg_runs_completion_guard (§18.4),
-- trg_runs_quality_profile_scope (профиль global или того же workspace, code = quality_profile).

-- §7.2 tasks
CREATE TABLE tasks (
  id                    uuid7                 NOT NULL,
  workspace_id          uuid                  NOT NULL,
  run_id                uuid                  NOT NULL,
  plan_version          int                   NOT NULL CHECK (plan_version >= 1),
  task_key              text                  NOT NULL CHECK (task_key ~ '^[A-Za-z0-9._-]{1,64}$'), -- стабильный ключ планировщика в пределах плана
  origin_task_id        uuid,                 -- задача, из которой порождена (fix/review loop, replan)
  agent_role            agent_role_t          NOT NULL,
  title                 text                  NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  instructions          text                  NOT NULL CHECK (length(instructions) <= 100000),
  status                task_status_t         NOT NULL DEFAULT 'PENDING',
  blocked_reason        task_blocked_reason_t,
  complexity            smallint              NOT NULL CHECK (complexity BETWEEN 1 AND 5),     -- L1..L5
  risk_level            risk_level_t          NOT NULL,
  acceptance_criteria   jsonb                 NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(acceptance_criteria) = 'array'),
  max_attempts          int                   NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  attempt_count         int                   NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  timeout_seconds       int                   NOT NULL DEFAULT 1800 CHECK (timeout_seconds BETWEEN 30 AND 86400),
  retry_budget_seconds  int                   NOT NULL DEFAULT 7200 CHECK (retry_budget_seconds BETWEEN 0 AND 604800),
  retry_spent_seconds   int                   NOT NULL DEFAULT 0 CHECK (retry_spent_seconds >= 0),
  next_attempt_at       timestamptz,
  review_loop_count     int                   NOT NULL DEFAULT 0 CHECK (review_loop_count >= 0),
  max_review_loops      int                   NOT NULL DEFAULT 3 CHECK (max_review_loops BETWEEN 0 AND 10),
  lease_fencing_token   bigint,               -- токен актуальной аренды (§15.3); записи результата сверяют его
  superseded_at         timestamptz,          -- задача заменена новой версией плана (replan)
  version               bigint                NOT NULL DEFAULT 0,
  created_at            timestamptz           NOT NULL DEFAULT now(),
  updated_at            timestamptz           NOT NULL DEFAULT now(),
  started_at            timestamptz,
  finished_at           timestamptz,
  CONSTRAINT pk_tasks PRIMARY KEY (id),
  CONSTRAINT uq_tasks_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_tasks_ws_run_id UNIQUE (workspace_id, run_id, id),          -- цель составных FK task_dependencies
  CONSTRAINT uq_tasks_run_plan_key UNIQUE (run_id, plan_version, task_key),
  CONSTRAINT fk_tasks_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_tasks_origin FOREIGN KEY (workspace_id, run_id, origin_task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,          -- origin только из того же run
  CONSTRAINT ck_tasks_blocked_reason CHECK ((status = 'BLOCKED') = (blocked_reason IS NOT NULL)),
  CONSTRAINT ck_tasks_attempts CHECK (attempt_count <= max_attempts),
  CONSTRAINT ck_tasks_retry_wait CHECK (status <> 'RETRY_WAIT' OR next_attempt_at IS NOT NULL),
  CONSTRAINT ck_tasks_leased CHECK (status NOT IN ('LEASED','RUNNING') OR lease_fencing_token IS NOT NULL),
  CONSTRAINT ck_tasks_finished CHECK ((status IN ('SUCCEEDED','FAILED','CANCELLED')) = (finished_at IS NOT NULL)),
  CONSTRAINT ck_tasks_superseded CHECK (superseded_at IS NULL OR status IN ('CANCELLED','SUCCEEDED','FAILED'))
);
CREATE INDEX ix_tasks_run_status ON tasks (workspace_id, run_id, status);                      -- ТЗ §7.3 + workspace_id
CREATE INDEX ix_tasks_ready ON tasks (workspace_id, run_id, created_at) WHERE status = 'READY';   -- выборка планировщиком
CREATE INDEX ix_tasks_retry_due ON tasks (next_attempt_at) WHERE status = 'RETRY_WAIT';
CREATE INDEX ix_tasks_pending ON tasks (run_id) WHERE status = 'PENDING';
CREATE INDEX ix_tasks_blocked ON tasks (workspace_id, blocked_reason) WHERE status = 'BLOCKED';
CREATE INDEX ix_tasks_origin ON tasks (origin_task_id) WHERE origin_task_id IS NOT NULL;
-- Специфичные триггеры: trg_tasks_transition (§18.3), trg_tasks_lock_run_share (§18.4),
-- trg_tasks_success_guard (§18.4).

-- §7.3 task_dependencies
CREATE TABLE task_dependencies (
  workspace_id        uuid        NOT NULL,
  run_id              uuid        NOT NULL,
  task_id             uuid        NOT NULL,
  depends_on_task_id  uuid        NOT NULL,
  dependency_type     text        NOT NULL DEFAULT 'finish' CHECK (dependency_type IN ('finish','review','artifact')),
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_task_dependencies PRIMARY KEY (task_id, depends_on_task_id),
  CONSTRAINT ck_task_dependencies_no_self CHECK (task_id <> depends_on_task_id),
  CONSTRAINT fk_task_dependencies_task FOREIGN KEY (workspace_id, run_id, task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_task_dependencies_depends_on FOREIGN KEY (workspace_id, run_id, depends_on_task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_task_dependencies_parent ON task_dependencies (depends_on_task_id);   -- ТЗ §7.3
CREATE INDEX ix_task_dependencies_run ON task_dependencies (workspace_id, run_id);
-- trg_task_dependencies_same_run_backstop: BEFORE INSERT/UPDATE — защитная проверка (на случай будущей
--   миграции, ослабившей FK) tasks.run_id обоих концов = NEW.run_id; RAISE 'cross-run dependency'.
-- trg_task_dependencies_acyclic: CONSTRAINT TRIGGER AFTER INSERT OR UPDATE DEFERRABLE INITIALLY DEFERRED (§18.2).

-- §7.7 routing_decisions
CREATE TABLE routing_decisions (
  id                 uuid7          NOT NULL,
  workspace_id       uuid           NOT NULL,
  task_id            uuid           NOT NULL,
  outcome            text           NOT NULL CHECK (outcome IN ('SELECTED','FALLBACK','BLOCKED')),
  selected_provider  cli_provider_t,
  complexity_level   smallint       NOT NULL CHECK (complexity_level BETWEEN 1 AND 5),
  score_breakdown    jsonb          NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(score_breakdown) = 'object'),
  reason             text           NOT NULL CHECK (length(reason) <= 2000),
  policy_id          uuid,
  policy_version     int            NOT NULL CHECK (policy_version >= 1),
  created_at         timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_routing_decisions PRIMARY KEY (id),
  CONSTRAINT uq_routing_decisions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_routing_decisions_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_routing_decisions_policy FOREIGN KEY (policy_id) REFERENCES policies(id) ON DELETE RESTRICT,
  CONSTRAINT ck_routing_decisions_outcome CHECK ((outcome = 'BLOCKED') = (selected_provider IS NULL))
);
CREATE INDEX ix_routing_decisions_task ON routing_decisions (workspace_id, task_id, created_at DESC);

-- §7.4 task_attempts
CREATE TABLE task_attempts (
  id                   uuid7              NOT NULL,
  workspace_id         uuid               NOT NULL,
  task_id              uuid               NOT NULL,
  attempt_no           int                NOT NULL CHECK (attempt_no BETWEEN 1 AND 10),
  status               text               NOT NULL DEFAULT 'RUNNING'
                                          CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  provider             cli_provider_t,    -- NULL для попыток без CLI (детерминированный L1-инструмент)
  agent_assignment_id  uuid,
  routing_decision_id  uuid,
  fencing_token        bigint             NOT NULL,
  started_at           timestamptz        NOT NULL DEFAULT now(),
  finished_at          timestamptz,
  failure_category     failure_category_t,
  failure_message      text               CHECK (length(failure_message) <= 4000),   -- уже редактированный текст
  CONSTRAINT pk_task_attempts PRIMARY KEY (id),
  CONSTRAINT uq_task_attempts_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_task_attempts_task_attempt_no UNIQUE (task_id, attempt_no),
  CONSTRAINT fk_task_attempts_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  -- fk_task_attempts_assignment добавляется ниже (циклическая зависимость с agent_assignments)
  CONSTRAINT fk_task_attempts_routing FOREIGN KEY (workspace_id, routing_decision_id)
      REFERENCES routing_decisions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_task_attempts_failure CHECK (
      (status <> 'FAILED' OR failure_category IS NOT NULL)
      AND (status NOT IN ('RUNNING','SUCCEEDED') OR failure_category IS NULL)),
  CONSTRAINT ck_task_attempts_finished CHECK ((status = 'RUNNING') = (finished_at IS NULL))
);
CREATE UNIQUE INDEX uq_task_attempts_one_running ON task_attempts (task_id) WHERE status = 'RUNNING';
CREATE INDEX ix_task_attempts_failure ON task_attempts (workspace_id, failure_category, started_at DESC) WHERE status = 'FAILED';

-- §7.6 agent_assignments
CREATE TABLE agent_assignments (
  id                   uuid7          NOT NULL,
  workspace_id         uuid           NOT NULL,
  task_id              uuid           NOT NULL,
  agent_profile_id     uuid           NOT NULL,
  provider             cli_provider_t NOT NULL,
  routing_decision_id  uuid,
  reviewed_attempt_id  uuid,          -- для ролей reviewer/security: попытка, результат которой ревьюируется
  review_independence  text           NOT NULL DEFAULT 'not_applicable'
                                      CHECK (review_independence IN ('not_applicable','independent_provider','same_provider_fallback','manual')),
  reason               text           NOT NULL CHECK (length(reason) <= 2000),
  assigned_at          timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_assignments PRIMARY KEY (id),
  CONSTRAINT uq_agent_assignments_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_agent_assignments_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  -- fk_agent_assignments_profile → agent_profiles: таблица появится в Phase 4 (agent_runtime)
  CONSTRAINT fk_agent_assignments_routing FOREIGN KEY (workspace_id, routing_decision_id)
      REFERENCES routing_decisions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_assignments_reviewed_attempt FOREIGN KEY (workspace_id, reviewed_attempt_id)
      REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_agent_assignments_review CHECK (review_independence = 'not_applicable' OR reviewed_attempt_id IS NOT NULL)
);
CREATE INDEX ix_agent_assignments_task ON agent_assignments (workspace_id, task_id, assigned_at DESC);
-- trg_agent_assignments_independence (§18.8): если review_independence = 'independent_provider',
--   то provider <> task_attempts.provider ревьюируемой попытки; иначе RAISE.
--   'same_provider_fallback' допустим только при наличии APPROVED approvals.action_type='SELF_REVIEW_FALLBACK' для задачи.

-- §9.3 agent_decisions
CREATE TABLE agent_decisions (
  id                  uuid7        NOT NULL,
  workspace_id        uuid         NOT NULL,
  run_id              uuid         NOT NULL,
  task_id             uuid,
  decision_type       text         NOT NULL CHECK (decision_type IN
                        ('architecture','plan','routing','review_verdict','risk_acceptance','scope_change','other')),
  rationale           text         NOT NULL CHECK (length(rationale) BETWEEN 1 AND 20000),
  evidence_refs       uuid[]       NOT NULL DEFAULT '{}',          -- id строк evidence; проверяются приложением
  acp_message_id      uuid,
  decided_by_type     text         NOT NULL CHECK (decided_by_type IN ('user','agent','system')),
  decided_by_role     agent_role_t,
  decided_by_user_id  uuid,
  created_at          timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_decisions PRIMARY KEY (id),
  CONSTRAINT uq_agent_decisions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_agent_decisions_run  FOREIGN KEY (workspace_id, run_id)  REFERENCES runs(workspace_id, id)  ON DELETE RESTRICT,
  CONSTRAINT fk_agent_decisions_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  -- fk_agent_decisions_message → agent_messages: добавит миграция Phase 4
  CONSTRAINT fk_agent_decisions_user FOREIGN KEY (decided_by_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_agent_decisions_actor CHECK (
      (decided_by_type = 'user'  AND decided_by_user_id IS NOT NULL AND decided_by_role IS NULL) OR
      (decided_by_type = 'agent' AND decided_by_role IS NOT NULL AND decided_by_user_id IS NULL) OR
      (decided_by_type = 'system' AND decided_by_user_id IS NULL AND decided_by_role IS NULL)),
  CONSTRAINT ck_agent_decisions_risk_by_user CHECK (decision_type <> 'risk_acceptance' OR decided_by_type = 'user')
);
CREATE INDEX ix_agent_decisions_run ON agent_decisions (workspace_id, run_id, created_at);

-- §9.4 chat_threads
CREATE TABLE chat_threads (
  id               uuid7          NOT NULL,
  workspace_id     uuid           NOT NULL,
  run_id           uuid           NOT NULL,
  scope            text           NOT NULL CHECK (scope IN ('all_agents','provider','role','task','approvals')),
  target_role      agent_role_t,
  target_provider  cli_provider_t,
  target_task_id   uuid,
  created_by       uuid,          -- NULL: создан системой при старте run
  created_at       timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_chat_threads PRIMARY KEY (id),
  CONSTRAINT uq_chat_threads_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_chat_threads_ws_run_id UNIQUE (workspace_id, run_id, id),
  CONSTRAINT uq_chat_threads_scope UNIQUE NULLS NOT DISTINCT (run_id, scope, target_role, target_provider, target_task_id),
  CONSTRAINT fk_chat_threads_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_chat_threads_task FOREIGN KEY (workspace_id, run_id, target_task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_chat_threads_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT ck_chat_threads_target CHECK (
      (scope IN ('all_agents','approvals') AND num_nonnulls(target_role, target_provider, target_task_id) = 0) OR
      (scope = 'provider' AND target_provider IS NOT NULL AND num_nonnulls(target_role, target_task_id) = 0) OR
      (scope = 'role'     AND target_role IS NOT NULL     AND num_nonnulls(target_provider, target_task_id) = 0) OR
      (scope = 'task'     AND target_task_id IS NOT NULL  AND num_nonnulls(target_role, target_provider) = 0))
);
CREATE INDEX ix_chat_threads_run ON chat_threads (workspace_id, run_id);

-- §9.5 chat_messages
CREATE TABLE chat_messages (
  id               uuid7          NOT NULL,
  workspace_id     uuid           NOT NULL,
  run_id           uuid           NOT NULL,     -- денормализовано из thread для выборки по run
  thread_id        uuid           NOT NULL,
  sender_type      text           NOT NULL CHECK (sender_type IN ('user','agent','system')),
  sender_id        uuid,          -- users.id при sender_type='user'
  sender_role      agent_role_t,  -- при sender_type='agent'
  sender_provider  cli_provider_t,
  body             text           NOT NULL CHECK (octet_length(body) <= 32768),   -- уже редактированный текст
  message_kind     text           NOT NULL CHECK (message_kind IN
                     ('text','directive','acp_message','activity','cli_excerpt','approval','system_notice')),
  source_event_id  uuid,          -- event_id из run_event_log / id agent_activity_events (без FK: партиционирование/retention)
  acp_message_id   uuid,
  mentions         agent_role_t[] NOT NULL DEFAULT '{}',   -- упоминания ролей (ТЗ §11)
  search_vector    tsvector       GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED,
  created_at       timestamptz    NOT NULL DEFAULT now(),
  CONSTRAINT pk_chat_messages PRIMARY KEY (id),
  CONSTRAINT uq_chat_messages_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_chat_messages_thread FOREIGN KEY (workspace_id, run_id, thread_id) REFERENCES chat_threads(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_chat_messages_sender FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE RESTRICT,
  -- fk_chat_messages_acp → agent_messages: добавит миграция Phase 4
  CONSTRAINT ck_chat_messages_sender CHECK (
      (sender_type = 'user'   AND sender_id IS NOT NULL AND sender_role IS NULL) OR
      (sender_type = 'agent'  AND sender_id IS NULL AND sender_role IS NOT NULL) OR
      (sender_type = 'system' AND sender_id IS NULL AND sender_role IS NULL)),
  CONSTRAINT ck_chat_messages_acp CHECK (message_kind <> 'acp_message' OR acp_message_id IS NOT NULL)
);
CREATE INDEX ix_chat_messages_thread_time ON chat_messages (workspace_id, thread_id, created_at);
CREATE INDEX ix_chat_messages_run_time ON chat_messages (workspace_id, run_id, created_at);
CREATE INDEX ix_chat_messages_search ON chat_messages USING gin (workspace_id, search_vector);   -- btree_gin

-- §14.2 approvals
CREATE TABLE approvals (
  id                   uuid7                 NOT NULL,
  workspace_id         uuid                  NOT NULL,
  project_id           uuid                  NOT NULL,
  run_id               uuid,                 -- ТЗ: run_id; NULL для DOCS_DOWNLOAD вне run
  task_id              uuid,
  action_type          approval_action_type_t NOT NULL,
  status               approval_status_t     NOT NULL DEFAULT 'PENDING',
  risk_level           risk_level_t          NOT NULL,
  required_role        workspace_role_t      NOT NULL,     -- минимальная роль утверждающего по матрице брифа
  requested_by_type    text                  NOT NULL CHECK (requested_by_type IN ('user','agent','system')),
  requested_by         uuid,                 -- users.id при requested_by_type='user'
  requested_by_role    agent_role_t,         -- при requested_by_type='agent'
  subject_hash         sha256_hex            NOT NULL,
  subject              jsonb                 NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(subject) = 'object'),
  allow_self_approval  boolean               NOT NULL DEFAULT false,  -- только single-member workspace + явная политика (аудируется)
  decided_by           uuid,
  decision_reason      text                  CHECK (length(decision_reason) <= 4000),
  requested_at         timestamptz           NOT NULL DEFAULT now(),
  decided_at           timestamptz,
  expires_at           timestamptz,
  version              bigint                NOT NULL DEFAULT 0,
  updated_at           timestamptz           NOT NULL DEFAULT now(),
  CONSTRAINT pk_approvals PRIMARY KEY (id),
  CONSTRAINT uq_approvals_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_approvals_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_approvals_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_approvals_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_approvals_requested_by FOREIGN KEY (requested_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_approvals_decided_by FOREIGN KEY (decided_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_approvals_requester CHECK (
      (requested_by_type = 'user'   AND requested_by IS NOT NULL AND requested_by_role IS NULL) OR
      (requested_by_type = 'agent'  AND requested_by IS NULL AND requested_by_role IS NOT NULL) OR
      (requested_by_type = 'system' AND requested_by IS NULL AND requested_by_role IS NULL)),
  CONSTRAINT ck_approvals_decision CHECK (
      (status IN ('APPROVED','REJECTED')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
  CONSTRAINT ck_approvals_reject_reason CHECK (status <> 'REJECTED' OR decision_reason IS NOT NULL),
  CONSTRAINT ck_approvals_four_eyes CHECK (
      status <> 'APPROVED' OR requested_by IS NULL OR decided_by <> requested_by OR allow_self_approval),
  CONSTRAINT ck_approvals_run_bound CHECK (
      action_type NOT IN ('PLAN_APPROVAL','MERGE','CRITICAL_DIRECTIVE','MANUAL_REVIEW','SELF_REVIEW_FALLBACK','LOCAL_EXECUTION_FALLBACK')
      OR run_id IS NOT NULL),
  CONSTRAINT ck_approvals_expiry CHECK (expires_at IS NULL OR expires_at > requested_at)
);
CREATE INDEX ix_approvals_pending ON approvals (workspace_id, requested_at) WHERE status = 'PENDING';
CREATE INDEX ix_approvals_pending_expiry ON approvals (expires_at) WHERE status = 'PENDING' AND expires_at IS NOT NULL;
CREATE INDEX ix_approvals_run ON approvals (workspace_id, run_id, action_type) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX uq_approvals_pending_subject ON approvals (workspace_id, action_type, subject_hash) WHERE status = 'PENDING';
-- trg_approvals_transition (§18.3); trg_approvals_lock_run_share (§18.4);
-- trg_approvals_decider_role (§18.7): decided_by — член workspace с ролью ≥ required_role (с учётом project_member_overrides).

-- §15.1 checkpoints
CREATE TABLE checkpoints (
  id              uuid7       NOT NULL,
  workspace_id    uuid        NOT NULL,
  run_id          uuid        NOT NULL,
  task_id         uuid,
  state_version   bigint      NOT NULL CHECK (state_version >= 0),   -- runs.version (или tasks.version) на момент снимка
  plan_version    int         NOT NULL CHECK (plan_version >= 0),
  reason          text        NOT NULL CHECK (reason IN ('plan_approved','directive','pause','pre_external_action','periodic','recovery')),
  state_ref       uuid        NOT NULL,        -- artifacts.id (kind='checkpoint_state'): сериализованное состояние FSM/DAG
  worktree_id     uuid,
  git_commit_sha  git_sha,                     -- WIP-коммит в изолированной ветке worktree, если был
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_checkpoints PRIMARY KEY (id),
  CONSTRAINT uq_checkpoints_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_checkpoints_state UNIQUE NULLS NOT DISTINCT (run_id, task_id, state_version),
  CONSTRAINT fk_checkpoints_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_checkpoints_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_checkpoints_state FOREIGN KEY (workspace_id, state_ref) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_checkpoints_worktree FOREIGN KEY (workspace_id, worktree_id) REFERENCES worktrees(workspace_id, id) ON DELETE SET NULL (worktree_id)
);
CREATE INDEX ix_checkpoints_run_latest ON checkpoints (workspace_id, run_id, created_at DESC);

-- §9.6 user_directives
CREATE TABLE user_directives (
  id                       uuid7                   NOT NULL,
  workspace_id             uuid                    NOT NULL,
  run_id                   uuid                    NOT NULL,
  chat_message_id          uuid                    NOT NULL,
  target_type              text                    NOT NULL CHECK (target_type IN ('run','role','task','provider')),
  target_role              agent_role_t,
  target_task_id           uuid,
  target_provider          cli_provider_t,
  directive_type           directive_type_t        NOT NULL,
  status                   directive_status_t      NOT NULL DEFAULT 'RECEIVED',
  impact_status            directive_impact_type_t,  -- агрегат: максимальный impact по всем directive_impacts; NULL до IMPACT_READY
  requires_impact_preview  boolean                 NOT NULL DEFAULT true,
  expected_run_version     bigint,                   -- из запроса (ТЗ §8.2); сверяется с runs.version
  approval_id              uuid,                     -- CRITICAL_DIRECTIVE, если затронуты критические операции
  checkpoint_id            uuid,
  resulting_plan_version   int,
  submitted_by             uuid                    NOT NULL,
  failure_message          text                    CHECK (length(failure_message) <= 4000),
  version                  bigint                  NOT NULL DEFAULT 0,
  created_at               timestamptz             NOT NULL DEFAULT now(),
  updated_at               timestamptz             NOT NULL DEFAULT now(),
  completed_at             timestamptz,
  CONSTRAINT pk_user_directives PRIMARY KEY (id),
  CONSTRAINT uq_user_directives_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_user_directives_ws_run_id UNIQUE (workspace_id, run_id, id),
  CONSTRAINT uq_user_directives_chat_message UNIQUE (chat_message_id),
  CONSTRAINT fk_user_directives_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_chat_message FOREIGN KEY (workspace_id, chat_message_id) REFERENCES chat_messages(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_target_task FOREIGN KEY (workspace_id, run_id, target_task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_approval FOREIGN KEY (workspace_id, approval_id) REFERENCES approvals(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_checkpoint FOREIGN KEY (workspace_id, checkpoint_id) REFERENCES checkpoints(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_user_directives_submitted_by FOREIGN KEY (submitted_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_user_directives_target CHECK (
      (target_type = 'run'      AND num_nonnulls(target_role, target_task_id, target_provider) = 0) OR
      (target_type = 'role'     AND target_role IS NOT NULL     AND num_nonnulls(target_task_id, target_provider) = 0) OR
      (target_type = 'task'     AND target_task_id IS NOT NULL  AND num_nonnulls(target_role, target_provider) = 0) OR
      (target_type = 'provider' AND target_provider IS NOT NULL AND num_nonnulls(target_role, target_task_id) = 0)),
  CONSTRAINT ck_user_directives_impact CHECK (
      status IN ('RECEIVED','IMPACT_ANALYSIS_PENDING','REJECTED','CANCELLED','FAILED') OR impact_status IS NOT NULL),
  CONSTRAINT ck_user_directives_awaiting CHECK (status <> 'AWAITING_CONFIRMATION' OR approval_id IS NOT NULL OR requires_impact_preview),
  CONSTRAINT ck_user_directives_completed CHECK ((status IN ('COMPLETED','REJECTED','CANCELLED','FAILED')) = (completed_at IS NOT NULL))
);
CREATE INDEX ix_user_directives_run_active ON user_directives (workspace_id, run_id, created_at)
  WHERE status NOT IN ('COMPLETED','REJECTED','CANCELLED','FAILED');
CREATE INDEX ix_user_directives_run ON user_directives (workspace_id, run_id, created_at DESC);
-- trg_user_directives_transition (§18.3).

-- §9.7 directive_impacts
CREATE TABLE directive_impacts (
  workspace_id  uuid                    NOT NULL,
  run_id        uuid                    NOT NULL,
  directive_id  uuid                    NOT NULL,
  task_id       uuid                    NOT NULL,
  impact_type   directive_impact_type_t NOT NULL,
  resolution    text                    NOT NULL DEFAULT 'PENDING' CHECK (resolution IN ('PENDING','APPLIED','SKIPPED','SUPERSEDED')),
  details       jsonb                   NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
  created_at    timestamptz             NOT NULL DEFAULT now(),
  resolved_at   timestamptz,
  CONSTRAINT pk_directive_impacts PRIMARY KEY (directive_id, task_id),
  CONSTRAINT fk_directive_impacts_directive FOREIGN KEY (workspace_id, run_id, directive_id)
      REFERENCES user_directives(workspace_id, run_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_directive_impacts_task FOREIGN KEY (workspace_id, run_id, task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_directive_impacts_resolved CHECK ((resolution = 'PENDING') = (resolved_at IS NULL))
);
CREATE INDEX ix_directive_impacts_task ON directive_impacts (task_id);

-- §9.8 directive_acknowledgements
CREATE TABLE directive_acknowledgements (
  id                 uuid7        NOT NULL,
  workspace_id       uuid         NOT NULL,
  directive_id       uuid         NOT NULL,
  recipient_role     agent_role_t NOT NULL,
  recipient_task_id  uuid,
  acp_message_id     uuid,        -- ACP-сообщение, которым поручение доставлено
  status             text         NOT NULL DEFAULT 'PENDING'
                                  CHECK (status IN ('PENDING','DELIVERED','ACKNOWLEDGED','REJECTED','EXPIRED')),
  delivered_at       timestamptz,
  acknowledged_at    timestamptz,
  created_at         timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_directive_acknowledgements PRIMARY KEY (id),
  CONSTRAINT uq_directive_ack_recipient UNIQUE NULLS NOT DISTINCT (directive_id, recipient_role, recipient_task_id),
  CONSTRAINT fk_directive_ack_directive FOREIGN KEY (workspace_id, directive_id) REFERENCES user_directives(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_directive_ack_task FOREIGN KEY (workspace_id, recipient_task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  -- fk_directive_ack_message → agent_messages: добавит миграция Phase 4
  CONSTRAINT ck_directive_ack_delivered CHECK (status = 'PENDING' OR delivered_at IS NOT NULL OR status = 'EXPIRED'),
  CONSTRAINT ck_directive_ack_acknowledged CHECK ((status = 'ACKNOWLEDGED') = (acknowledged_at IS NOT NULL))
);
CREATE INDEX ix_directive_ack_pending ON directive_acknowledgements (workspace_id, directive_id) WHERE status IN ('PENDING','DELIVERED');

-- §12.7 evidence
CREATE TABLE evidence (
  id                   uuid7                          NOT NULL,
  workspace_id         uuid                           NOT NULL,
  run_id               uuid                           NOT NULL,
  task_id              uuid,
  kind                 text                           NOT NULL CHECK (kind IN
                         ('command_result','test_report','scan_report','diff','review_verdict','manual_attestation','benchmark','agent_claim')),
  artifact_id          uuid                           NOT NULL,
  producer             text                           NOT NULL CHECK (producer IN
                         ('verification_engine','cli_gateway','agent','user','github','system')),
  producer_user_id     uuid,
  verification_status  evidence_verification_status_t NOT NULL DEFAULT 'PENDING',
  commit_sha           git_sha,
  command              text                           CHECK (length(command) <= 8192),   -- редактированная командная строка
  exit_code            int,
  environment          jsonb                          NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(environment) = 'object'), -- образ, версии инструментов
  started_at           timestamptz,
  finished_at          timestamptz,
  verified_at          timestamptz,
  created_at           timestamptz                    NOT NULL DEFAULT now(),
  CONSTRAINT pk_evidence PRIMARY KEY (id),
  CONSTRAINT uq_evidence_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_evidence_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_evidence_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_evidence_artifact FOREIGN KEY (workspace_id, artifact_id) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_evidence_producer_user FOREIGN KEY (producer_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  -- AC-14: для процессных доказательств обязательны commit, команда, exit code, время
  CONSTRAINT ck_evidence_process_fields CHECK (
      kind NOT IN ('command_result','test_report','scan_report','benchmark')
      OR (commit_sha IS NOT NULL AND command IS NOT NULL AND exit_code IS NOT NULL
          AND started_at IS NOT NULL AND finished_at IS NOT NULL)),
  CONSTRAINT ck_evidence_time_order CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
  -- Evidence-first: слова агента и сырой вывод CLI не могут быть VERIFIED
  CONSTRAINT ck_evidence_verified_producer CHECK (
      verification_status <> 'VERIFIED' OR producer IN ('verification_engine','user','github')),
  CONSTRAINT ck_evidence_agent_claim CHECK (kind <> 'agent_claim' OR (producer = 'agent' AND verification_status <> 'VERIFIED')),
  CONSTRAINT ck_evidence_user CHECK ((producer = 'user') = (producer_user_id IS NOT NULL)),
  CONSTRAINT ck_evidence_verified_at CHECK ((verification_status IN ('VERIFIED','REJECTED')) = (verified_at IS NOT NULL))
);
CREATE INDEX ix_evidence_run ON evidence (workspace_id, run_id, created_at);
CREATE INDEX ix_evidence_task ON evidence (workspace_id, task_id) WHERE task_id IS NOT NULL;
-- trg_evidence_immutable: BEFORE UPDATE — изменяемы только verification_status/verified_at и только из PENDING/UNVERIFIED.

-- §12.4 gate_executions
CREATE TABLE gate_executions (
  id                     uuid7                   NOT NULL,
  workspace_id           uuid                    NOT NULL,
  run_id                 uuid                    NOT NULL,
  task_id                uuid,
  gate_id                uuid                    NOT NULL,
  gate_type              text                    NOT NULL,    -- денормализовано из quality_gates
  required               boolean                 NOT NULL,
  status                 gate_execution_status_t NOT NULL DEFAULT 'PENDING',
  attempt_no             int                     NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
  commit_sha             git_sha                 NOT NULL,
  evidence_id            uuid,
  not_applicable_reason  text                    CHECK (length(not_applicable_reason) <= 2000),
  error_message          text                    CHECK (length(error_message) <= 4000),
  superseded_at          timestamptz,            -- заменён повторным запуском (после fix)
  created_at             timestamptz             NOT NULL DEFAULT now(),
  started_at             timestamptz,
  finished_at            timestamptz,
  updated_at             timestamptz             NOT NULL DEFAULT now(),
  CONSTRAINT pk_gate_executions PRIMARY KEY (id),
  CONSTRAINT uq_gate_executions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_gate_executions_id_gate UNIQUE (id, gate_id),                  -- цель FK findings(gate_execution_id, source_gate_id)
  CONSTRAINT fk_gate_executions_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_gate_executions_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_gate_executions_gate FOREIGN KEY (gate_id) REFERENCES quality_gates(id) ON DELETE RESTRICT,
  CONSTRAINT fk_gate_executions_evidence FOREIGN KEY (workspace_id, evidence_id) REFERENCES evidence(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_gate_executions_pass_evidence CHECK (status NOT IN ('PASS','FAIL') OR evidence_id IS NOT NULL),   -- ТЗ §14
  CONSTRAINT ck_gate_executions_na_reason CHECK (status NOT IN ('NOT_APPLICABLE','SKIPPED') OR not_applicable_reason IS NOT NULL),
  CONSTRAINT ck_gate_executions_finished CHECK ((status IN ('PASS','FAIL','ERROR','SKIPPED','NOT_APPLICABLE')) = (finished_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_gate_executions_current ON gate_executions (run_id, task_id, gate_id) NULLS NOT DISTINCT
  WHERE superseded_at IS NULL;
CREATE INDEX ix_gate_executions_run ON gate_executions (workspace_id, run_id, status);
CREATE INDEX ix_gate_executions_active ON gate_executions (created_at) WHERE status IN ('PENDING','RUNNING');
-- trg_gate_executions_lock_run_share (§18.4); trg_gate_executions_evidence_verified: PASS требует evidence.verification_status='VERIFIED'.

-- §15.2 outbox_events
CREATE TABLE outbox_events (
  id                uuid7       NOT NULL,
  workspace_id      uuid,       -- NULL для системных событий (provider.health_changed)
  run_id            uuid,       -- для маршрутизации в run_event_log
  aggregate_type    text        NOT NULL CHECK (aggregate_type ~ '^[a-z][a-z0-9_]{0,63}$'),
  aggregate_id      uuid        NOT NULL,
  event_type        text        NOT NULL CHECK (event_type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
  schema_version    text        NOT NULL DEFAULT '1.0' CHECK (schema_version ~ '^\d+\.\d+$'),
  payload           jsonb       NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 65536),
  correlation_id    uuid,
  causation_id      uuid,
  traceparent       text        CHECK (traceparent ~ '^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$'),  -- W3C
  created_at        timestamptz NOT NULL DEFAULT now(),
  available_at      timestamptz NOT NULL DEFAULT now(),   -- backoff при ошибке публикации
  published_at      timestamptz,
  publish_attempts  int         NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
  last_error        text        CHECK (length(last_error) <= 2000),
  CONSTRAINT pk_outbox_events PRIMARY KEY (id, created_at),
  CONSTRAINT ck_outbox_events_run_ws CHECK (run_id IS NULL OR workspace_id IS NOT NULL)
) PARTITION BY RANGE (created_at);
CREATE INDEX ix_outbox_events_unpublished ON outbox_events (available_at, id) WHERE published_at IS NULL;   -- партиальный
CREATE INDEX ix_outbox_events_aggregate ON outbox_events (aggregate_type, aggregate_id, created_at);
-- trg_outbox_events_guard: BEFORE UPDATE — изменяемы только published_at, publish_attempts, last_error, available_at.

-- §15.3 processed_events
CREATE TABLE processed_events (
  consumer      text        NOT NULL CHECK (consumer ~ '^[a-z0-9_.:-]{1,128}$'),   -- имя Dramatiq-актора/обработчика
  event_id      uuid        NOT NULL,
  processed_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_processed_events PRIMARY KEY (consumer, event_id)
);
CREATE INDEX ix_processed_events_retention ON processed_events (processed_at);

-- §15.4 worker_leases
-- worker_lease_fencing_seq создана миграцией 0002

CREATE TABLE worker_leases (
  id              uuid7       NOT NULL,
  resource_type   text        NOT NULL CHECK (resource_type IN
                    ('task','run','cli_execution','indexing_job','external_action','outbox_partition','recovery')),
  resource_id     uuid        NOT NULL,
  workspace_id    uuid,       -- трассировка/контекст; NULL для системных ресурсов
  worker_id       text        NOT NULL CHECK (worker_id ~ '^[A-Za-z0-9._:-]{1,200}$'),   -- host:process:uuid
  worker_host_id  uuid,
  fencing_token   bigint      NOT NULL,
  acquired_at     timestamptz NOT NULL DEFAULT now(),
  heartbeat_at    timestamptz NOT NULL DEFAULT now(),
  lease_until     timestamptz NOT NULL,
  released_at     timestamptz,
  CONSTRAINT pk_worker_leases PRIMARY KEY (id),
  CONSTRAINT uq_worker_leases_resource UNIQUE (resource_type, resource_id),
  CONSTRAINT fk_worker_leases_host FOREIGN KEY (worker_host_id) REFERENCES worker_hosts(id) ON DELETE SET NULL,
  CONSTRAINT ck_worker_leases_until CHECK (lease_until > acquired_at),
  CONSTRAINT ck_worker_leases_fencing CHECK (fencing_token > 0)
);
CREATE INDEX ix_worker_leases_active_expiry ON worker_leases (lease_until) WHERE released_at IS NULL;   -- reaper
CREATE INDEX ix_worker_leases_worker ON worker_leases (worker_id) WHERE released_at IS NULL;
-- trg_worker_leases_fencing_monotonic (§18.6).

-- §15.5 external_actions
CREATE TABLE external_actions (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  run_id           uuid,
  task_id          uuid,
  action_type      text        NOT NULL CHECK (action_type IN
                     ('git_push','branch_delete','pr_create','pr_update','pr_merge','pr_comment','migration_apply','docs_download','production_deploy')),
  idempotency_key  sha256_hex  NOT NULL,     -- sha256(run_id, task_id, action_type, target, plan_version) — ADR-0012
  target           text        NOT NULL CHECK (length(target) BETWEEN 1 AND 1024),   -- 'owner/repo#branch', URL источника и т.п.
  is_destructive   boolean     NOT NULL DEFAULT false,
  status           text        NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED','EXECUTING','CONFIRMED','FAILED','COMPENSATED')),
  approval_id      uuid,
  request          jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(request) = 'object'),
  request_hash     sha256_hex  NOT NULL,     -- сверяется с approvals.subject_hash
  result           jsonb       CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
  external_ref     text        CHECK (length(external_ref) <= 1024),   -- PR URL/номер, SHA push, id snapshot
  fencing_token    bigint,
  attempt_count    int         NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  last_error       text        CHECK (length(last_error) <= 4000),
  planned_at       timestamptz NOT NULL DEFAULT now(),
  executing_at     timestamptz,
  confirmed_at     timestamptz,
  failed_at        timestamptz,
  compensated_at   timestamptz,
  version          bigint      NOT NULL DEFAULT 0,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_external_actions PRIMARY KEY (id),
  CONSTRAINT uq_external_actions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_external_actions_idempotency_key UNIQUE (idempotency_key),
  CONSTRAINT fk_external_actions_run  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_external_actions_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_external_actions_approval FOREIGN KEY (workspace_id, approval_id) REFERENCES approvals(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_external_actions_approval_required CHECK (
      (action_type NOT IN ('pr_merge','production_deploy') AND NOT (action_type = 'migration_apply' AND is_destructive))
      OR approval_id IS NOT NULL),
  CONSTRAINT ck_external_actions_executing CHECK (
      ((status IN ('EXECUTING','CONFIRMED','COMPENSATED')) <= (executing_at IS NOT NULL AND fencing_token IS NOT NULL))
      AND (status <> 'PLANNED' OR executing_at IS NULL)),     -- FAILED возможен и до исполнения (отказ в approval)
  CONSTRAINT ck_external_actions_confirmed CHECK ((status IN ('CONFIRMED','COMPENSATED')) <= (confirmed_at IS NOT NULL)),
  CONSTRAINT ck_external_actions_failed CHECK ((status = 'FAILED') = (failed_at IS NOT NULL))
);
CREATE INDEX ix_external_actions_reconcile ON external_actions (status, updated_at) WHERE status IN ('PLANNED','EXECUTING');
CREATE INDEX ix_external_actions_run ON external_actions (workspace_id, run_id) WHERE run_id IS NOT NULL;
-- trg_external_actions_transition (§18.3); trg_external_actions_approval_gate (§18.7):
--   переход в EXECUTING при approval_id IS NOT NULL требует approvals.status='APPROVED',
--   approvals.subject_hash = NEW.request_hash и (expires_at IS NULL OR expires_at > now()).

-- §15.6 run_event_log
CREATE TABLE run_event_log (
  workspace_id    uuid                       NOT NULL,
  run_id          uuid                       NOT NULL,
  seq             bigint                     NOT NULL CHECK (seq >= 1),     -- SSE id
  event_id        uuid                       NOT NULL,                      -- = outbox_events.id
  event_type      sse_event_type_persisted_t NOT NULL,
  schema_version  text                       NOT NULL DEFAULT '1.0' CHECK (schema_version ~ '^\d+\.\d+$'),
  task_id         uuid,
  payload         jsonb                      NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 32768),
  occurred_at     timestamptz                NOT NULL,                      -- время доменного события
  created_at      timestamptz                NOT NULL DEFAULT now(),
  CONSTRAINT pk_run_event_log PRIMARY KEY (run_id, seq),
  CONSTRAINT uq_run_event_log_event_id UNIQUE (event_id),
  CONSTRAINT fk_run_event_log_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_run_event_log_ws_run ON run_event_log (workspace_id, run_id, seq);
-- trg_run_event_log_append_only: BEFORE UPDATE OR DELETE → trg_fn_append_only() (retention — только aicd_maintenance, §20).

-- Seed: глобальные профили качества (§12.2). definition минимален; Phase 6 расширит новой версией.
INSERT INTO quality_profiles (id, workspace_id, code, version, definition) VALUES
  ('01a1c000-0000-7000-8000-000000000001', NULL, 'basic', 1,
   '{"schema_version": "1.0", "finding_block_severity": "critical", "coverage_min_percent": null, "note": "minimal seed; P6 will extend"}'),
  ('01a1c000-0000-7000-8000-000000000002', NULL, 'strict', 1,
   '{"schema_version": "1.0", "finding_block_severity": "high", "coverage_min_percent": 85, "note": "minimal seed; P6 will extend"}'),
  ('01a1c000-0000-7000-8000-000000000003', NULL, 'critical', 1,
   '{"schema_version": "1.0", "finding_block_severity": "medium", "coverage_min_percent": 85, "note": "minimal seed; P6 will extend"}');

-- Партиции outbox_events (§20.1): функция обслуживания знает новую таблицу.
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
SELECT maint_ensure_monthly_partitions('outbox_events', (now() - interval '1 month')::date, 4);

-- fk_task_attempts_assignment (§7.4): agent_assignments создана после task_attempts.
ALTER TABLE task_attempts ADD CONSTRAINT fk_task_attempts_assignment FOREIGN KEY (workspace_id, agent_assignment_id)
    REFERENCES agent_assignments(workspace_id, id) ON DELETE RESTRICT;
"""

TRIGGERS = r"""
-- ---------------------------------------------------------------------------------------------
-- Стандартные триггеры (§1.5, §4)
-- ---------------------------------------------------------------------------------------------
CREATE TRIGGER trg_runs_updated_at BEFORE UPDATE ON runs FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_tasks_updated_at BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_approvals_updated_at BEFORE UPDATE ON approvals FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_user_directives_updated_at BEFORE UPDATE ON user_directives FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_gate_executions_updated_at BEFORE UPDATE ON gate_executions FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_external_actions_updated_at BEFORE UPDATE ON external_actions FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_tasks_version BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION trg_fn_enforce_version();
CREATE TRIGGER trg_approvals_version BEFORE UPDATE ON approvals FOR EACH ROW EXECUTE FUNCTION trg_fn_enforce_version();
CREATE TRIGGER trg_user_directives_version BEFORE UPDATE ON user_directives FOR EACH ROW EXECUTE FUNCTION trg_fn_enforce_version();
CREATE TRIGGER trg_external_actions_version BEFORE UPDATE ON external_actions FOR EACH ROW EXECUTE FUNCTION trg_fn_enforce_version();
CREATE TRIGGER trg_quality_gates_immutable_ws BEFORE UPDATE OF workspace_id ON quality_gates FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_runs_immutable_ws BEFORE UPDATE OF workspace_id ON runs FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_tasks_immutable_ws BEFORE UPDATE OF workspace_id ON tasks FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_task_dependencies_immutable_ws BEFORE UPDATE OF workspace_id ON task_dependencies FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_task_attempts_immutable_ws BEFORE UPDATE OF workspace_id ON task_attempts FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_routing_decisions_immutable_ws BEFORE UPDATE OF workspace_id ON routing_decisions FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_agent_assignments_immutable_ws BEFORE UPDATE OF workspace_id ON agent_assignments FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_agent_decisions_immutable_ws BEFORE UPDATE OF workspace_id ON agent_decisions FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_chat_threads_immutable_ws BEFORE UPDATE OF workspace_id ON chat_threads FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_chat_messages_immutable_ws BEFORE UPDATE OF workspace_id ON chat_messages FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_approvals_immutable_ws BEFORE UPDATE OF workspace_id ON approvals FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_checkpoints_immutable_ws BEFORE UPDATE OF workspace_id ON checkpoints FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_user_directives_immutable_ws BEFORE UPDATE OF workspace_id ON user_directives FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_directive_impacts_immutable_ws BEFORE UPDATE OF workspace_id ON directive_impacts FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_directive_acknowledgements_immutable_ws BEFORE UPDATE OF workspace_id ON directive_acknowledgements FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_evidence_immutable_ws BEFORE UPDATE OF workspace_id ON evidence FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_gate_executions_immutable_ws BEFORE UPDATE OF workspace_id ON gate_executions FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_external_actions_immutable_ws BEFORE UPDATE OF workspace_id ON external_actions FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
-- runs.version (ADR-0011, OQ-DB-10): выдача last_event_seq диспетчером outbox не является изменением
-- агрегата — проверка версии пропускается, если изменились только last_event_seq/updated_at.
CREATE TRIGGER trg_runs_version BEFORE UPDATE ON runs FOR EACH ROW
  WHEN ((to_jsonb(NEW) - 'last_event_seq' - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'last_event_seq' - 'updated_at'))
  EXECUTE FUNCTION trg_fn_enforce_version();

-- ---------------------------------------------------------------------------------------------
-- Переходы состояний (§18.3)
-- ---------------------------------------------------------------------------------------------
INSERT INTO allowed_transitions (entity, from_status, to_status)
SELECT v.entity, v.from_status, unnest(v.to_statuses)
  FROM (VALUES
    ('run', '__new__',           ARRAY['CREATED']),
    ('run', 'CREATED',           ARRAY['DISCOVERY','CANCELLED','FAILED']),
    ('run', 'DISCOVERY',         ARRAY['PLANNED','PAUSED','BLOCKED','FAILED','CANCELLED']),
    ('run', 'PLANNED',           ARRAY['AWAITING_APPROVAL','RUNNING','DISCOVERY','PAUSED','BLOCKED','FAILED','CANCELLED']),
    ('run', 'AWAITING_APPROVAL', ARRAY['RUNNING','PLANNED','DISCOVERY','PAUSED','BLOCKED','FAILED','CANCELLED']),
    ('run', 'RUNNING',           ARRAY['VERIFYING','PLANNED','PAUSED','BLOCKED','FAILED','CANCELLED']),
    ('run', 'VERIFYING',         ARRAY['COMPLETED','RUNNING','PAUSED','BLOCKED','FAILED','CANCELLED']),
    ('run', 'PAUSED',            ARRAY['DISCOVERY','PLANNED','AWAITING_APPROVAL','RUNNING','VERIFYING','FAILED','CANCELLED']),
    ('run', 'BLOCKED',           ARRAY['DISCOVERY','PLANNED','AWAITING_APPROVAL','RUNNING','VERIFYING','PAUSED','FAILED','CANCELLED']),
    ('task', '__new__',          ARRAY['PENDING']),
    ('task', 'PENDING',          ARRAY['READY','BLOCKED','CANCELLED']),
    ('task', 'READY',            ARRAY['LEASED','PENDING','BLOCKED','CANCELLED']),
    ('task', 'LEASED',           ARRAY['RUNNING','READY','RETRY_WAIT','BLOCKED','FAILED','CANCELLED']),
    ('task', 'RUNNING',          ARRAY['VERIFYING','RETRY_WAIT','BLOCKED','FAILED','CANCELLED']),
    ('task', 'VERIFYING',        ARRAY['SUCCEEDED','RETRY_WAIT','BLOCKED','FAILED','CANCELLED']),
    ('task', 'RETRY_WAIT',       ARRAY['READY','BLOCKED','FAILED','CANCELLED']),
    ('task', 'BLOCKED',          ARRAY['PENDING','READY','SUCCEEDED','FAILED','CANCELLED']),
    ('directive', '__new__',                 ARRAY['RECEIVED']),
    ('directive', 'RECEIVED',                ARRAY['IMPACT_ANALYSIS_PENDING','DISPATCHED','REJECTED','CANCELLED','FAILED']),
    ('directive', 'IMPACT_ANALYSIS_PENDING', ARRAY['IMPACT_READY','CANCELLED','FAILED']),
    ('directive', 'IMPACT_READY',            ARRAY['AWAITING_CONFIRMATION','CONFIRMED','CHECKPOINTING','REJECTED','CANCELLED']),
    ('directive', 'AWAITING_CONFIRMATION',   ARRAY['CONFIRMED','REJECTED','CANCELLED']),
    ('directive', 'CONFIRMED',               ARRAY['CHECKPOINTING','REPLANNING','DISPATCHED','CANCELLED','FAILED']),
    ('directive', 'CHECKPOINTING',           ARRAY['REPLANNING','DISPATCHED','FAILED']),
    ('directive', 'REPLANNING',              ARRAY['DISPATCHED','FAILED']),
    ('directive', 'DISPATCHED',              ARRAY['ACKNOWLEDGED','FAILED']),
    ('directive', 'ACKNOWLEDGED',            ARRAY['COMPLETED','FAILED']),
    ('approval', '__new__',  ARRAY['PENDING']),
    ('approval', 'PENDING',  ARRAY['APPROVED','REJECTED','EXPIRED','CANCELLED']),
    ('external_action', '__new__',   ARRAY['PLANNED']),
    ('external_action', 'PLANNED',   ARRAY['EXECUTING','FAILED']),
    ('external_action', 'EXECUTING', ARRAY['CONFIRMED','FAILED']),
    ('external_action', 'FAILED',    ARRAY['EXECUTING']),
    ('external_action', 'CONFIRMED', ARRAY['COMPENSATED'])
  ) AS v(entity, from_status, to_statuses);

CREATE TRIGGER trg_runs_transition BEFORE INSERT OR UPDATE OF status ON runs
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('run');
CREATE TRIGGER trg_tasks_transition BEFORE INSERT OR UPDATE OF status ON tasks
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('task');
CREATE TRIGGER trg_user_directives_transition BEFORE INSERT OR UPDATE OF status ON user_directives
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('directive');
CREATE TRIGGER trg_approvals_transition BEFORE INSERT OR UPDATE OF status ON approvals
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('approval');
CREATE TRIGGER trg_external_actions_transition BEFORE INSERT OR UPDATE OF status ON external_actions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('external_action');

-- §18.3: выход из PAUSED/BLOCKED в нетерминальное состояние — только в resume_status (или PAUSED из BLOCKED).
CREATE FUNCTION trg_fn_runs_resume_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status IN ('PAUSED','BLOCKED') AND NEW.status IS DISTINCT FROM OLD.status
     AND NEW.status NOT IN ('COMPLETED','FAILED','CANCELLED')
     AND NOT (OLD.status = 'BLOCKED' AND NEW.status = 'PAUSED')
     AND NEW.status IS DISTINCT FROM OLD.resume_status THEN
    RAISE EXCEPTION 'run %: resume from % must return to resume_status % (got %)',
      NEW.id, OLD.status, OLD.resume_status, NEW.status USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_runs_resume_guard BEFORE UPDATE OF status ON runs
  FOR EACH ROW EXECUTE FUNCTION trg_fn_runs_resume_guard();

-- ---------------------------------------------------------------------------------------------
-- M-таблицы: неизменяемость версий и scope ссылок (§12.2, §12.3, §14.1, §18.13, §6.2, §7.1)
-- ---------------------------------------------------------------------------------------------
CREATE FUNCTION trg_fn_immutable_except_active() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (to_jsonb(NEW) - 'active') IS DISTINCT FROM (to_jsonb(OLD) - 'active') THEN
    RAISE EXCEPTION '% rows are immutable except active (create a new version)', TG_TABLE_NAME USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_policies_immutable BEFORE UPDATE ON policies
  FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_except_active();
CREATE TRIGGER trg_quality_profiles_immutable BEFORE UPDATE ON quality_profiles
  FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_except_active();

CREATE FUNCTION trg_fn_quality_gates_ws_match() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_ws uuid;
BEGIN
  SELECT workspace_id INTO v_ws FROM quality_profiles WHERE id = NEW.profile_id;
  IF NOT FOUND OR NEW.workspace_id IS DISTINCT FROM v_ws THEN
    RAISE EXCEPTION 'quality gate workspace must match its profile' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_quality_gates_ws_match BEFORE INSERT OR UPDATE ON quality_gates
  FOR EACH ROW EXECUTE FUNCTION trg_fn_quality_gates_ws_match();

CREATE FUNCTION trg_fn_runs_quality_profile_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_ws uuid; v_code text;
BEGIN
  SELECT workspace_id, code INTO v_ws, v_code FROM quality_profiles WHERE id = NEW.quality_profile_id;
  IF NOT FOUND OR (v_ws IS NOT NULL AND v_ws <> NEW.workspace_id) OR v_code <> NEW.quality_profile THEN
    RAISE EXCEPTION 'run quality profile must be global or of the same workspace and match code %', NEW.quality_profile
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_runs_quality_profile_scope BEFORE INSERT OR UPDATE OF quality_profile, quality_profile_id, workspace_id ON runs
  FOR EACH ROW EXECUTE FUNCTION trg_fn_runs_quality_profile_scope();

-- ---------------------------------------------------------------------------------------------
-- DAG: один run (§18.1), ацикличность (§18.2)
-- ---------------------------------------------------------------------------------------------
CREATE FUNCTION trg_fn_task_dependencies_same_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (SELECT count(*) FROM tasks
       WHERE id IN (NEW.task_id, NEW.depends_on_task_id)
         AND run_id = NEW.run_id AND workspace_id = NEW.workspace_id) <> 2 THEN
    RAISE EXCEPTION 'task dependency must link tasks of the same run %', NEW.run_id USING ERRCODE = '23514';
  END IF;
  -- сериализация изменений плана run (см. 18.2)
  PERFORM 1 FROM runs WHERE id = NEW.run_id FOR NO KEY UPDATE;
  -- утверждённый план неизменяем: replan создаёт новую plan_version
  IF EXISTS (SELECT 1 FROM tasks t JOIN runs r ON r.id = t.run_id
              WHERE t.id = NEW.task_id AND r.approved_plan_version IS NOT NULL
                AND t.plan_version <= r.approved_plan_version) THEN
    RAISE EXCEPTION 'dependencies of an approved plan version are immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_task_dependencies_same_run BEFORE INSERT OR UPDATE ON task_dependencies
  FOR EACH ROW EXECUTE FUNCTION trg_fn_task_dependencies_same_run();

-- Отложенная проверка ацикличности: один обход на run в транзакции. Отметка хранит число рёбер run
-- на момент проверки — если после неё рёбра добавлены (SET CONSTRAINTS … IMMEDIATE), проверка повторяется.
CREATE FUNCTION trg_fn_task_dependencies_acyclic() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_key   text := 'aicd.dag_' || replace(NEW.run_id::text, '-', '');
  v_edges text;
  v_cycle boolean;
BEGIN
  SELECT count(*)::text INTO v_edges FROM task_dependencies WHERE run_id = NEW.run_id;
  IF current_setting(v_key, true) IS NOT DISTINCT FROM v_edges THEN
    RETURN NULL;
  END IF;
  WITH RECURSIVE walk(start_id, cur_id) AS (
      SELECT d.task_id, d.depends_on_task_id
        FROM task_dependencies d WHERE d.run_id = NEW.run_id
    UNION ALL
      SELECT w.start_id, d.depends_on_task_id
        FROM walk w JOIN task_dependencies d ON d.task_id = w.cur_id AND d.run_id = NEW.run_id
  ) CYCLE cur_id SET is_cycle USING path
  SELECT EXISTS (SELECT 1 FROM walk WHERE start_id = cur_id OR is_cycle) INTO v_cycle;
  IF v_cycle THEN
    RAISE EXCEPTION 'task dependency cycle in run %', NEW.run_id USING ERRCODE = '23514';
  END IF;
  PERFORM set_config(v_key, v_edges, true);
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_task_dependencies_acyclic
  AFTER INSERT OR UPDATE ON task_dependencies DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION trg_fn_task_dependencies_acyclic();

-- ---------------------------------------------------------------------------------------------
-- COMPLETED/SUCCEEDED guards и гонки (§18.4)
-- ---------------------------------------------------------------------------------------------
CREATE FUNCTION trg_fn_runs_completion_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'COMPLETED' AND OLD.status <> 'COMPLETED' THEN
    -- 1) все задачи актуального плана успешны (заменённые replan'ом — CANCELLED + superseded_at)
    IF EXISTS (SELECT 1 FROM tasks t WHERE t.run_id = NEW.id AND t.superseded_at IS NULL
                AND t.status <> 'SUCCEEDED') THEN
      RAISE EXCEPTION 'run %: not all tasks SUCCEEDED', NEW.id USING ERRCODE = '23514';
    END IF;
    -- 2) каждый актуальный обязательный gate: PASS или обоснованный NOT_APPLICABLE (SKIPPED ≠ PASS)
    IF EXISTS (SELECT 1 FROM gate_executions g WHERE g.run_id = NEW.id AND g.superseded_at IS NULL
                AND g.required AND g.status NOT IN ('PASS','NOT_APPLICABLE')) THEN
      RAISE EXCEPTION 'run %: required gates not passed', NEW.id USING ERRCODE = '23514';
    END IF;
    -- 3) хотя бы одна проверка реально выполнена (нельзя завершить run «пустой» верификацией)
    IF NOT EXISTS (SELECT 1 FROM gate_executions g WHERE g.run_id = NEW.id AND g.superseded_at IS NULL
                    AND g.status = 'PASS') THEN
      RAISE EXCEPTION 'run %: no PASS gate execution', NEW.id USING ERRCODE = '23514';
    END IF;
    -- 4) нет незакрытых approvals; утверждённый план существует
    IF EXISTS (SELECT 1 FROM approvals a WHERE a.run_id = NEW.id AND a.status = 'PENDING')
       OR NEW.approved_plan_version IS NULL THEN
      RAISE EXCEPTION 'run %: approval policy not satisfied', NEW.id USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_runs_completion_guard BEFORE UPDATE OF status ON runs
  FOR EACH ROW EXECUTE FUNCTION trg_fn_runs_completion_guard();

-- Задача → SUCCEEDED: все актуальные обязательные gates задачи в PASS/NOT_APPLICABLE
-- (FAIL/ERROR/PENDING/RUNNING/SKIPPED обязательного gate блокируют успех).
CREATE FUNCTION trg_fn_tasks_success_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'SUCCEEDED' AND OLD.status <> 'SUCCEEDED' THEN
    IF EXISTS (SELECT 1 FROM gate_executions g WHERE g.task_id = NEW.id AND g.superseded_at IS NULL
                AND g.required AND g.status NOT IN ('PASS','NOT_APPLICABLE')) THEN
      RAISE EXCEPTION 'task %: required gates not passed', NEW.id USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_tasks_success_guard BEFORE UPDATE OF status ON tasks
  FOR EACH ROW EXECUTE FUNCTION trg_fn_tasks_success_guard();

-- Писатели tasks.status / gate_executions / approvals берут FOR SHARE на run (конфликтует с
-- FOR NO KEY UPDATE перехода run в COMPLETED). В COMPLETED run запись запрещена; в FAILED/CANCELLED
-- допустимы только закрывающие записи (отмена задач, истечение/отмена approvals, ERROR gate).
CREATE FUNCTION trg_fn_lock_run_share() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_status text;
BEGIN
  IF NEW.run_id IS NULL THEN RETURN NEW; END IF;
  SELECT status INTO v_status FROM runs WHERE id = NEW.run_id FOR SHARE;
  IF v_status = 'COMPLETED'
     OR (v_status IN ('FAILED','CANCELLED') AND NOT (
           (TG_TABLE_NAME = 'tasks'           AND NEW.status IN ('CANCELLED','FAILED'))
        OR (TG_TABLE_NAME = 'approvals'       AND NEW.status IN ('CANCELLED','EXPIRED'))
        OR (TG_TABLE_NAME = 'gate_executions' AND NEW.status IN ('ERROR','SKIPPED')))) THEN
    RAISE EXCEPTION 'run % is % — % cannot be changed', NEW.run_id, v_status, TG_TABLE_NAME USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_tasks_lock_run_share BEFORE INSERT OR UPDATE OF status ON tasks
  FOR EACH ROW EXECUTE FUNCTION trg_fn_lock_run_share();
CREATE TRIGGER trg_gate_executions_lock_run_share BEFORE INSERT OR UPDATE ON gate_executions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_lock_run_share();
CREATE TRIGGER trg_approvals_lock_run_share BEFORE INSERT OR UPDATE ON approvals
  FOR EACH ROW EXECUTE FUNCTION trg_fn_lock_run_share();

-- PASS требует VERIFIED evidence (ТЗ §9, §14: exit code / слова агента не дают PASS).
CREATE FUNCTION trg_fn_gate_executions_evidence_verified() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'PASS' AND NOT EXISTS (
       SELECT 1 FROM evidence e WHERE e.workspace_id = NEW.workspace_id AND e.id = NEW.evidence_id
          AND e.verification_status = 'VERIFIED') THEN
    RAISE EXCEPTION 'gate execution %: PASS requires VERIFIED evidence', NEW.id USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_gate_executions_evidence_verified BEFORE INSERT OR UPDATE OF status, evidence_id ON gate_executions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_gate_executions_evidence_verified();

-- evidence неизменяемо, кроме verification_status/verified_at и только из PENDING/UNVERIFIED (§12.7).
CREATE FUNCTION trg_fn_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (to_jsonb(NEW) - 'verification_status' - 'verified_at') IS DISTINCT FROM (to_jsonb(OLD) - 'verification_status' - 'verified_at')
     OR (NEW.verification_status IS DISTINCT FROM OLD.verification_status
         AND OLD.verification_status NOT IN ('PENDING','UNVERIFIED')) THEN
    RAISE EXCEPTION 'evidence is immutable except verification of PENDING/UNVERIFIED rows' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_evidence_immutable BEFORE UPDATE ON evidence
  FOR EACH ROW EXECUTE FUNCTION trg_fn_evidence_immutable();

-- ---------------------------------------------------------------------------------------------
-- Approvals и опасные действия (§18.7), независимость ревью (§18.8)
-- ---------------------------------------------------------------------------------------------
CREATE FUNCTION approval_min_role(p_action_type text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN p_action_type IN ('MANUAL_REVIEW','SELF_REVIEW_FALLBACK') THEN 'reviewer'
    WHEN p_action_type IN ('PLAN_APPROVAL','DOCS_DOWNLOAD') THEN 'developer'
    WHEN p_action_type IN ('MERGE','DESTRUCTIVE_MIGRATION','DATA_DELETION','CRITICAL_DIRECTIVE','LOCAL_EXECUTION_FALLBACK') THEN 'admin'
    WHEN p_action_type IN ('PRODUCTION_DEPLOY','SECRET_CHANGE','UNRESTRICTED_NETWORK') THEN 'owner'
  END
$$;

CREATE FUNCTION trg_fn_approvals_decider_role() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_role text;
BEGIN
  IF workspace_role_rank(NEW.required_role) < workspace_role_rank(approval_min_role(NEW.action_type)) THEN
    RAISE EXCEPTION 'approval %: required_role % is below minimum % for %',
      NEW.id, NEW.required_role, approval_min_role(NEW.action_type), NEW.action_type USING ERRCODE = '23514';
  END IF;
  IF NEW.decided_by IS NOT NULL
     AND (TG_OP = 'INSERT' OR NEW.decided_by IS DISTINCT FROM OLD.decided_by OR NEW.status IS DISTINCT FROM OLD.status) THEN
    SELECT COALESCE(o.role, m.role) INTO v_role
      FROM workspace_members m
      LEFT JOIN project_member_overrides o ON o.project_id = NEW.project_id AND o.user_id = m.user_id
     WHERE m.workspace_id = NEW.workspace_id AND m.user_id = NEW.decided_by;
    IF v_role IS NULL OR workspace_role_rank(v_role) < workspace_role_rank(NEW.required_role) THEN
      RAISE EXCEPTION 'approval %: decider role % is below required %', NEW.id, v_role, NEW.required_role
        USING ERRCODE = '42501';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_approvals_decider_role BEFORE INSERT OR UPDATE ON approvals
  FOR EACH ROW EXECUTE FUNCTION trg_fn_approvals_decider_role();

CREATE FUNCTION trg_fn_external_actions_approval_gate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status = 'EXECUTING' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'EXECUTING')
     AND NEW.approval_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM approvals a
                      WHERE a.workspace_id = NEW.workspace_id AND a.id = NEW.approval_id
                        AND a.status = 'APPROVED' AND a.subject_hash = NEW.request_hash
                        AND (a.expires_at IS NULL OR a.expires_at > now())) THEN
    RAISE EXCEPTION 'external action %: approval % is not APPROVED for this request', NEW.id, NEW.approval_id
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_external_actions_approval_gate BEFORE INSERT OR UPDATE OF status, approval_id, request_hash ON external_actions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_external_actions_approval_gate();

CREATE FUNCTION trg_fn_agent_assignments_independence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_reviewed_provider text;
BEGIN
  IF NEW.review_independence = 'independent_provider' THEN
    SELECT provider INTO v_reviewed_provider FROM task_attempts WHERE id = NEW.reviewed_attempt_id;
    IF v_reviewed_provider IS NULL OR v_reviewed_provider = NEW.provider THEN
      RAISE EXCEPTION 'review is not independent: same provider %', NEW.provider USING ERRCODE = '23514';
    END IF;
  ELSIF NEW.review_independence = 'same_provider_fallback' AND NOT EXISTS (
      SELECT 1 FROM approvals a JOIN task_attempts ta ON ta.id = NEW.reviewed_attempt_id
       WHERE a.task_id = ta.task_id AND a.action_type = 'SELF_REVIEW_FALLBACK' AND a.status = 'APPROVED') THEN
    RAISE EXCEPTION 'same-provider review requires SELF_REVIEW_FALLBACK approval' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_agent_assignments_independence BEFORE INSERT OR UPDATE ON agent_assignments
  FOR EACH ROW EXECUTE FUNCTION trg_fn_agent_assignments_independence();

-- ---------------------------------------------------------------------------------------------
-- Fencing (§18.6), outbox, append-only (§18.5)
-- ---------------------------------------------------------------------------------------------
CREATE FUNCTION trg_fn_worker_leases_fencing() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.fencing_token < OLD.fencing_token THEN
    RAISE EXCEPTION 'fencing token must not decrease' USING ERRCODE = '23514';
  END IF;
  IF (NEW.worker_id IS DISTINCT FROM OLD.worker_id OR (OLD.released_at IS NOT NULL AND NEW.released_at IS NULL))
     AND NEW.fencing_token <= OLD.fencing_token THEN
    RAISE EXCEPTION 'lease re-acquisition requires a new fencing token' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_worker_leases_fencing_monotonic BEFORE UPDATE ON worker_leases
  FOR EACH ROW EXECUTE FUNCTION trg_fn_worker_leases_fencing();

-- tasks.lease_fencing_token не убывает и не сбрасывается в NULL (иначе меньший токен прошёл бы позже).
CREATE FUNCTION trg_fn_tasks_fencing_monotonic() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.lease_fencing_token IS NOT NULL
     AND (NEW.lease_fencing_token IS NULL OR NEW.lease_fencing_token < OLD.lease_fencing_token) THEN
    RAISE EXCEPTION 'task %: lease fencing token must not decrease', NEW.id USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_tasks_fencing_monotonic BEFORE UPDATE OF lease_fencing_token ON tasks
  FOR EACH ROW EXECUTE FUNCTION trg_fn_tasks_fencing_monotonic();

CREATE FUNCTION trg_fn_outbox_events_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (to_jsonb(NEW) - 'published_at' - 'publish_attempts' - 'last_error' - 'available_at')
     IS DISTINCT FROM (to_jsonb(OLD) - 'published_at' - 'publish_attempts' - 'last_error' - 'available_at') THEN
    RAISE EXCEPTION 'outbox events are immutable except publication fields' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_outbox_events_guard BEFORE UPDATE ON outbox_events
  FOR EACH ROW EXECUTE FUNCTION trg_fn_outbox_events_guard();

CREATE TRIGGER trg_run_event_log_append_only BEFORE UPDATE OR DELETE ON run_event_log
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
"""

DEFERRED_FKS = r"""
-- FK, отложенные миграциями 0001/0002 (§6.2, §6.4, §8.5, §12.1)
ALTER TABLE worktrees
  ADD CONSTRAINT fk_worktrees_run  FOREIGN KEY (workspace_id, run_id)  REFERENCES runs(workspace_id, id)  ON DELETE RESTRICT,
  ADD CONSTRAINT fk_worktrees_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE SET NULL (task_id);
ALTER TABLE cli_executions
  ADD CONSTRAINT fk_cli_executions_attempt FOREIGN KEY (workspace_id, task_attempt_id) REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT;
ALTER TABLE artifacts
  ADD CONSTRAINT fk_artifacts_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT;
ALTER TABLE project_settings
  ADD CONSTRAINT fk_project_settings_routing_policy FOREIGN KEY (routing_policy_id) REFERENCES policies(id) ON DELETE RESTRICT;

-- §6.2: обычный FK не проверяет tenancy M-таблицы — политика маршрутизации global или своего workspace/проекта.
CREATE FUNCTION trg_fn_project_settings_policy_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_type text; v_ws uuid; v_project uuid;
BEGIN
  IF NEW.routing_policy_id IS NULL THEN RETURN NEW; END IF;
  SELECT policy_type, workspace_id, project_id INTO v_type, v_ws, v_project FROM policies WHERE id = NEW.routing_policy_id;
  IF NOT FOUND OR v_type <> 'routing' OR (v_ws IS NOT NULL AND v_ws <> NEW.workspace_id)
     OR (v_project IS NOT NULL AND v_project <> NEW.project_id) THEN
    RAISE EXCEPTION 'routing policy must be a routing policy of this workspace/project or global' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_project_settings_policy_scope BEFORE INSERT OR UPDATE OF routing_policy_id, workspace_id ON project_settings
  FOR EACH ROW EXECUTE FUNCTION trg_fn_project_settings_policy_scope();
"""

RLS = r"""
-- T-таблицы (§19.3, §19.4)
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['runs','tasks','task_dependencies','task_attempts','routing_decisions','agent_assignments',
                           'agent_decisions','chat_threads','chat_messages','approvals','checkpoints','user_directives',
                           'directive_impacts','directive_acknowledgements','evidence','gate_executions',
                           'external_actions','run_event_log']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO aicd_app, aicd_worker '
                   'USING (workspace_id = app_current_workspace()) '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_tenant', t);
  END LOOP;
END $$;

-- M-таблицы: чтение global + своё, запись — только своё (§19.3)
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['quality_profiles','quality_gates','policies']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR SELECT TO aicd_app, aicd_worker '
                   'USING (workspace_id IS NULL OR workspace_id = app_current_workspace())', 'p_' || t || '_read', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR INSERT TO aicd_app '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_write', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE TO aicd_app '
                   'USING (workspace_id = app_current_workspace()) '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_update', t);
  END LOOP;
END $$;

-- S-таблицы: api только вставляет события своего workspace; воркеры видят все строки
ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY p_outbox_events_app_insert ON outbox_events FOR INSERT TO aicd_app
  WITH CHECK (workspace_id = app_current_workspace());
CREATE POLICY p_outbox_events_worker ON outbox_events FOR ALL TO aicd_worker
  USING (true) WITH CHECK (workspace_id IS NULL OR workspace_id = app_current_workspace()
                           OR current_setting('app.actor_type', true) = 'system');
ALTER TABLE worker_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE worker_leases FORCE ROW LEVEL SECURITY;
CREATE POLICY p_worker_leases_worker ON worker_leases FOR ALL TO aicd_worker USING (true) WITH CHECK (true);
"""

SYSFN = r"""
-- §19.5: межарендаторные выборки — только идентификаторы.
-- Планировщик: workspace с готовой работой, упорядоченные по давности самой старой единицы работы.
CREATE FUNCTION sys_schedulable_workspaces(p_limit int)
RETURNS TABLE (workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT s.ws
    FROM (SELECT t.workspace_id AS ws, t.created_at AS since FROM aicd.tasks t WHERE t.status = 'READY'
          UNION ALL
          SELECT t.workspace_id, t.next_attempt_at FROM aicd.tasks t
           WHERE t.status = 'RETRY_WAIT' AND t.next_attempt_at <= now()
          UNION ALL
          SELECT a.workspace_id, a.expires_at FROM aicd.approvals a
           WHERE a.status = 'PENDING' AND a.expires_at <= now()
          UNION ALL
          SELECT d.workspace_id, d.created_at FROM aicd.user_directives d
           WHERE d.status NOT IN ('COMPLETED','REJECTED','CANCELLED','FAILED')) s
   GROUP BY s.ws
   ORDER BY min(s.since), s.ws
   LIMIT greatest(p_limit, 0)
$$;

-- Workspace run/задачи, только если текущий пользователь — член этого workspace (как sys_resolve_project_workspace).
CREATE FUNCTION sys_resolve_run_workspace(p_run_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT r.workspace_id
    FROM aicd.runs r
    JOIN aicd.workspace_members m ON m.workspace_id = r.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE r.id = p_run_id
$$;

CREATE FUNCTION sys_resolve_task_workspace(p_task_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT t.workspace_id
    FROM aicd.tasks t
    JOIN aicd.workspace_members m ON m.workspace_id = t.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE t.id = p_task_id
$$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_schedulable_workspaces(int) OWNER TO aicd_sysfn;
ALTER FUNCTION sys_resolve_run_workspace(uuid) OWNER TO aicd_sysfn;
ALTER FUNCTION sys_resolve_task_workspace(uuid) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_schedulable_workspaces(int), sys_resolve_run_workspace(uuid),
                       sys_resolve_task_workspace(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_schedulable_workspaces(int) TO aicd_worker;
GRANT EXECUTE ON FUNCTION sys_resolve_run_workspace(uuid), sys_resolve_task_workspace(uuid) TO aicd_app;
GRANT SELECT (id, workspace_id) ON runs TO aicd_sysfn;
GRANT SELECT (id, workspace_id, status, created_at, next_attempt_at) ON tasks TO aicd_sysfn;
GRANT SELECT (workspace_id, status, expires_at) ON approvals TO aicd_sysfn;
GRANT SELECT (workspace_id, status, created_at) ON user_directives TO aicd_sysfn;
"""

GRANTS = r"""
REVOKE ALL ON quality_profiles, quality_gates, policies, runs, tasks, task_dependencies, task_attempts,
              routing_decisions, agent_assignments, agent_decisions, chat_threads, chat_messages, approvals,
              checkpoints, user_directives, directive_impacts, directive_acknowledgements, evidence,
              gate_executions, outbox_events, processed_events, worker_leases, external_actions, run_event_log
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION approval_min_role(text) TO aicd_app, aicd_worker;

-- api: чтение оркестрации; команды пользователя (run, план, approvals, directives, чат); outbox — только INSERT.
GRANT SELECT ON quality_profiles, quality_gates, runs, tasks, task_dependencies, task_attempts, routing_decisions,
               agent_assignments, agent_decisions, chat_threads, chat_messages, approvals, checkpoints,
               user_directives, directive_impacts, directive_acknowledgements, evidence, gate_executions,
               external_actions, run_event_log
  TO aicd_app;
GRANT INSERT, UPDATE ON runs, tasks, task_dependencies, approvals, user_directives, directive_impacts,
                       chat_threads, chat_messages
  TO aicd_app;
GRANT SELECT, INSERT ON policies TO aicd_app;
GRANT UPDATE (active) ON policies TO aicd_app;
GRANT INSERT ON outbox_events TO aicd_app;

-- workers: DML оркестрации, верификации, outbox, leases, ledger, журнал SSE.
GRANT SELECT ON quality_profiles, quality_gates, policies TO aicd_worker;
GRANT SELECT, INSERT, UPDATE ON runs, tasks, task_attempts, approvals, user_directives, directive_impacts,
                               directive_acknowledgements, evidence, gate_executions, external_actions
  TO aicd_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON task_dependencies TO aicd_worker;
GRANT SELECT, INSERT ON routing_decisions, agent_assignments, agent_decisions, chat_threads, chat_messages,
                        checkpoints, run_event_log, processed_events
  TO aicd_worker;
GRANT SELECT, INSERT, UPDATE ON outbox_events TO aicd_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON worker_leases TO aicd_worker;
REVOKE UPDATE, DELETE, TRUNCATE ON run_event_log FROM aicd_app, aicd_worker;

-- maintenance: retention/архив (§19.6, §20)
GRANT SELECT ON quality_profiles, quality_gates, policies, runs, tasks, task_dependencies, task_attempts,
               routing_decisions, agent_assignments, agent_decisions, chat_threads, chat_messages, approvals,
               checkpoints, user_directives, directive_impacts, directive_acknowledgements, evidence,
               gate_executions, outbox_events, processed_events, worker_leases, external_actions, run_event_log
  TO aicd_maintenance;
GRANT DELETE ON processed_events, worker_leases TO aicd_maintenance;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_resolve_task_workspace(uuid);
DROP FUNCTION IF EXISTS sys_resolve_run_workspace(uuid);
DROP FUNCTION IF EXISTS sys_schedulable_workspaces(int);

DROP TRIGGER IF EXISTS trg_project_settings_policy_scope ON project_settings;
DROP FUNCTION IF EXISTS trg_fn_project_settings_policy_scope();
ALTER TABLE project_settings DROP CONSTRAINT IF EXISTS fk_project_settings_routing_policy;
ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS fk_artifacts_run;
ALTER TABLE cli_executions DROP CONSTRAINT IF EXISTS fk_cli_executions_attempt;
ALTER TABLE worktrees DROP CONSTRAINT IF EXISTS fk_worktrees_task, DROP CONSTRAINT IF EXISTS fk_worktrees_run;

DROP TABLE IF EXISTS run_event_log, external_actions, worker_leases, processed_events, outbox_events,
                     gate_executions, evidence, directive_acknowledgements, directive_impacts, user_directives,
                     checkpoints, approvals, chat_messages, chat_threads, agent_decisions, agent_assignments,
                     task_attempts, routing_decisions, task_dependencies, tasks, runs, policies, quality_gates,
                     quality_profiles CASCADE;

DROP FUNCTION IF EXISTS trg_fn_outbox_events_guard();
DROP FUNCTION IF EXISTS trg_fn_tasks_fencing_monotonic();
DROP FUNCTION IF EXISTS trg_fn_worker_leases_fencing();
DROP FUNCTION IF EXISTS trg_fn_agent_assignments_independence();
DROP FUNCTION IF EXISTS trg_fn_external_actions_approval_gate();
DROP FUNCTION IF EXISTS trg_fn_approvals_decider_role();
DROP FUNCTION IF EXISTS approval_min_role(text);
DROP FUNCTION IF EXISTS trg_fn_evidence_immutable();
DROP FUNCTION IF EXISTS trg_fn_gate_executions_evidence_verified();
DROP FUNCTION IF EXISTS trg_fn_lock_run_share();
DROP FUNCTION IF EXISTS trg_fn_tasks_success_guard();
DROP FUNCTION IF EXISTS trg_fn_runs_completion_guard();
DROP FUNCTION IF EXISTS trg_fn_task_dependencies_acyclic();
DROP FUNCTION IF EXISTS trg_fn_task_dependencies_same_run();
DROP FUNCTION IF EXISTS trg_fn_runs_quality_profile_scope();
DROP FUNCTION IF EXISTS trg_fn_quality_gates_ws_match();
DROP FUNCTION IF EXISTS trg_fn_immutable_except_active();
DROP FUNCTION IF EXISTS trg_fn_runs_resume_guard();

DELETE FROM allowed_transitions WHERE entity IN ('run','task','directive','approval','external_action');

CREATE OR REPLACE FUNCTION maint_ensure_monthly_partitions(p_table text, p_from date, p_months int)
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


def upgrade() -> None:
    for block in (TABLES, TRIGGERS, DEFERRED_FKS, RLS, SYSFN, GRANTS):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
