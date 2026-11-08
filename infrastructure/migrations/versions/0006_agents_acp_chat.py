"""Агенты и ACP (P4-01): agent_profiles, agent_messages, message_deliveries, agent_activity_events, отложенные FK.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08

Источник DDL: docs/database/DATABASE_DESIGN.md §7.5, §9.1, §9.2, §9.9, §19.3–19.4, §20;
docs/protocols/AGENT_COMMUNICATION_PROTOCOL.md §2 (конверт), §5 (лимит 32 KiB), §7 (доставка), §11.
agent_assignments, routing_decisions, chat_threads, chat_messages, agent_decisions, directive_* созданы
миграцией 0003 и здесь не меняются, кроме FK, отложенных 0003 до Phase 4:
  - fk_agent_assignments_profile       agent_assignments.agent_profile_id → agent_profiles;
  - fk_agent_decisions_message         agent_decisions.acp_message_id → agent_messages;
  - fk_chat_messages_acp               chat_messages.acp_message_id → agent_messages;
  - fk_directive_ack_message           directive_acknowledgements.acp_message_id → agent_messages.
CREATE TABLE перенесены из документа, кроме (отмечено в §9.1/§9.2 документа):
  * agent_messages: отправитель `user` (ACP §1, §4.2: QUESTION/ANSWER из Live Agent Chat) — sender_role
    допускает 'user', добавлена sender_user_id (FK users), ck_agent_messages_sender вместо
    ck_agent_messages_sender_provider (agent → provider, user → user_id, orchestrator → ни того, ни другого);
  * agent_messages: поля конверта ACP §2 / дополнения ACP §11 — task_attempt_id (from-попытка) и
    recipient_task_id (to.task_id, inbox), составные FK; correlation_id NOT NULL (ACP 1.0 требует значение);
  * message_deliveries: next_attempt_at (backoff ретраев, ACP §7/§11), индекс ожидающих — по нему;
  * append-only agent_messages: DELETE разрешён только aicd_maintenance в сессии обслуживания (архив run,
    §20.2), как run_event_log в 0005 (общая функция trg_fn_append_only_except_maintenance).
agent_activity_events партиционирована помесячно (§20.1): партиции создаёт maint_ensure_monthly_partitions
(миграция — месяц назад + 4 вперёд), drop — maint_drop_partition с архивом (обе функции знают таблицу с 0005).
Seed agent_profiles здесь не выполняется: профили версионируются файлами и синхронизируются отдельно
(P4-02), хотя §7.5 документа говорит о seed-миграции (открытый вопрос, см. §22).
Права aicd_maintenance для архива run (§20.2–20.3): DELETE agent_messages/chat_messages/chat_threads,
UPDATE (body) chat_messages, INSERT artifacts (строка kind='archive').
Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).

Обратимость: downgrade удаляет отложенные FK, новые таблицы (с партициями), функции и выданные права.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TABLES = r"""
-- §7.5 agent_profiles (G)
CREATE TABLE agent_profiles (
  id                            uuid7        NOT NULL,
  role                          agent_role_t NOT NULL,
  instruction_template_version  text         NOT NULL CHECK (instruction_template_version ~ '^\d+\.\d+\.\d+$'),
  allowed_tools                 jsonb        NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(allowed_tools) = 'array'),
  required_capabilities         jsonb        NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(required_capabilities) = 'array'),
  policy_profile_id             uuid,
  enabled                       boolean      NOT NULL DEFAULT true,
  created_at                    timestamptz  NOT NULL DEFAULT now(),
  updated_at                    timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_profiles PRIMARY KEY (id),
  CONSTRAINT uq_agent_profiles_role UNIQUE (role),
  CONSTRAINT fk_agent_profiles_policy FOREIGN KEY (policy_profile_id) REFERENCES policies(id) ON DELETE RESTRICT
);
-- trg_agent_profiles_global_policy: policy_profile_id должен ссылаться на policies с workspace_id IS NULL.

-- §9.1 agent_messages (T), append-only
CREATE TABLE agent_messages (
  id                   uuid7              NOT NULL,    -- = message_id конверта
  workspace_id         uuid               NOT NULL,
  run_id               uuid               NOT NULL,
  task_id              uuid,
  task_attempt_id      uuid,              -- попытка-отправитель (конверт ACP §2); NULL для orchestrator/user
  protocol             text               NOT NULL DEFAULT 'acp/1.0' CHECK (protocol ~ '^acp/\d+\.\d+$'),
  sender_role          text               NOT NULL CHECK (sender_role IN
                         ('requirements','architect','planner','developer','reviewer','security','qa','database',
                          'performance','devops','maintenance','documentation','orchestrator','user')),
  sender_provider      cli_provider_t,
  sender_user_id       uuid,              -- from.user_id при sender_role = 'user' (ACP §2)
  recipient_role       text               NOT NULL CHECK (recipient_role IN
                         ('requirements','architect','planner','developer','reviewer','security','qa','database',
                          'performance','devops','maintenance','documentation','orchestrator','user')),
  recipient_task_id    uuid,              -- to.task_id: конкретная задача-получатель (inbox, ACP §7)
  message_type         acp_message_type_t NOT NULL,
  correlation_id       uuid               NOT NULL,   -- ACP 1.0: обязателен (= message_id первого сообщения)
  reply_to_id          uuid,
  schema_version       text               NOT NULL DEFAULT '1.0' CHECK (schema_version ~ '^\d+\.\d+$'),
  payload              jsonb              NOT NULL,
  source_execution_id  uuid,              -- исполнение CLI, из вывода которого извлечено сообщение
  created_at           timestamptz        NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_messages PRIMARY KEY (id),
  CONSTRAINT uq_agent_messages_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_agent_messages_run  FOREIGN KEY (workspace_id, run_id)  REFERENCES runs(workspace_id, id)  ON DELETE RESTRICT,
  CONSTRAINT fk_agent_messages_task FOREIGN KEY (workspace_id, task_id) REFERENCES tasks(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_messages_attempt FOREIGN KEY (workspace_id, task_attempt_id)
      REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_messages_recipient_task FOREIGN KEY (workspace_id, run_id, recipient_task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,          -- получатель только из того же run
  CONSTRAINT fk_agent_messages_sender_user FOREIGN KEY (sender_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_messages_reply_to FOREIGN KEY (workspace_id, reply_to_id) REFERENCES agent_messages(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_agent_messages_execution FOREIGN KEY (workspace_id, source_execution_id) REFERENCES cli_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_agent_messages_payload_object CHECK (jsonb_typeof(payload) = 'object'),
  CONSTRAINT ck_agent_messages_payload_size CHECK (octet_length(payload::text) <= 32768),   -- ТЗ §10: ≤ 32 KiB
  CONSTRAINT ck_agent_messages_no_self_reply CHECK (reply_to_id IS NULL OR reply_to_id <> id),
  CONSTRAINT ck_agent_messages_sender CHECK (
      (sender_role = 'orchestrator' AND sender_provider IS NULL AND sender_user_id IS NULL) OR
      (sender_role = 'user'         AND sender_provider IS NULL AND sender_user_id IS NOT NULL) OR
      (sender_role NOT IN ('orchestrator','user') AND sender_provider IS NOT NULL AND sender_user_id IS NULL)),
  CONSTRAINT ck_agent_messages_attempt CHECK (task_attempt_id IS NULL OR (task_id IS NOT NULL
      AND sender_role NOT IN ('orchestrator','user')))
);
CREATE INDEX ix_agent_messages_run_time ON agent_messages (workspace_id, run_id, created_at);     -- ТЗ §7.3 + workspace_id
CREATE INDEX ix_agent_messages_correlation ON agent_messages (correlation_id);
CREATE INDEX ix_agent_messages_task_time ON agent_messages (workspace_id, task_id, created_at) WHERE task_id IS NOT NULL;
CREATE INDEX ix_agent_messages_reply_to ON agent_messages (reply_to_id) WHERE reply_to_id IS NOT NULL;
CREATE INDEX ix_agent_messages_attempt ON agent_messages (workspace_id, task_attempt_id) WHERE task_attempt_id IS NOT NULL;
CREATE INDEX ix_agent_messages_inbox ON agent_messages (workspace_id, recipient_task_id, created_at)
  WHERE recipient_task_id IS NOT NULL;
-- trg_agent_messages_append_only: BEFORE UPDATE OR DELETE (DELETE — только aicd_maintenance в сессии, §20).

-- §9.2 message_deliveries (T)
CREATE TABLE message_deliveries (
  workspace_id        uuid        NOT NULL,
  message_id          uuid        NOT NULL,
  consumer_id         text        NOT NULL CHECK (consumer_id ~ '^[a-z0-9_.:-]{1,128}$'),   -- напр. 'task:<uuid>', 'ui-projection'
  delivery_count      int         NOT NULL DEFAULT 0 CHECK (delivery_count >= 0),
  first_delivered_at  timestamptz,
  last_delivered_at   timestamptz,
  next_attempt_at     timestamptz,  -- backoff следующей попытки (ACP §7); NULL — доставлять сразу
  acked_at            timestamptz,
  dead_lettered_at    timestamptz,
  last_error          text        CHECK (length(last_error) <= 2000),
  CONSTRAINT pk_message_deliveries PRIMARY KEY (message_id, consumer_id),    -- = UNIQUE(message_id,consumer_id) ТЗ
  CONSTRAINT fk_message_deliveries_message FOREIGN KEY (workspace_id, message_id) REFERENCES agent_messages(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT ck_message_deliveries_ack CHECK (acked_at IS NULL OR delivery_count >= 1),
  CONSTRAINT ck_message_deliveries_terminal CHECK (acked_at IS NULL OR dead_lettered_at IS NULL)
);
CREATE INDEX ix_message_deliveries_pending ON message_deliveries (consumer_id, next_attempt_at)
  WHERE acked_at IS NULL AND dead_lettered_at IS NULL;
CREATE INDEX ix_message_deliveries_ws_message ON message_deliveries (workspace_id, message_id);

-- §9.9 agent_activity_events (T), помесячные партиции, append-only
CREATE TABLE agent_activity_events (
  id                uuid7                  NOT NULL,
  workspace_id      uuid                   NOT NULL,
  run_id            uuid                   NOT NULL,
  task_id           uuid,
  execution_id      uuid,
  event_type        text                   NOT NULL CHECK (event_type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),  -- каталог: EVENT_SCHEMAS.md
  cli_event         normalized_cli_event_t,     -- для event_type='cli.*': нормализованный тип (ТЗ §9)
  schema_version    text                   NOT NULL DEFAULT '1.0',
  payload           jsonb                  NOT NULL DEFAULT '{}'::jsonb,
  correlation_id    uuid,
  occurred_at       timestamptz            NOT NULL,
  recorded_at       timestamptz            NOT NULL DEFAULT now(),
  CONSTRAINT pk_agent_activity_events PRIMARY KEY (id, occurred_at),
  CONSTRAINT fk_activity_run       FOREIGN KEY (workspace_id, run_id)       REFERENCES runs(workspace_id, id)           ON DELETE RESTRICT,
  CONSTRAINT fk_activity_task      FOREIGN KEY (workspace_id, task_id)      REFERENCES tasks(workspace_id, id)          ON DELETE RESTRICT,
  CONSTRAINT fk_activity_execution FOREIGN KEY (workspace_id, execution_id) REFERENCES cli_executions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_activity_payload CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 32768),
  CONSTRAINT ck_activity_cli_event CHECK ((event_type LIKE 'cli.%') = (cli_event IS NOT NULL))
) PARTITION BY RANGE (occurred_at);
CREATE INDEX ix_activity_run_time ON agent_activity_events (workspace_id, run_id, occurred_at);
CREATE INDEX ix_activity_task_time ON agent_activity_events (workspace_id, task_id, occurred_at) WHERE task_id IS NOT NULL;
CREATE INDEX ix_activity_execution ON agent_activity_events (execution_id, occurred_at) WHERE execution_id IS NOT NULL;
-- Партиции: agent_activity_events_yYYYYmMM (§20.1); задача retention держит 3 месяца вперёд.
SELECT maint_ensure_monthly_partitions('agent_activity_events', (now() - interval '1 month')::date, 4);
"""

TRIGGERS = r"""
CREATE TRIGGER trg_agent_profiles_updated_at BEFORE UPDATE ON agent_profiles FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_message_deliveries_immutable_ws BEFORE UPDATE OF workspace_id ON message_deliveries
  FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- §7.5: обычный FK не проверяет tenancy M-таблицы — профиль роли ссылается только на глобальную политику.
CREATE FUNCTION trg_fn_agent_profiles_global_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.policy_profile_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM policies p WHERE p.id = NEW.policy_profile_id AND p.workspace_id IS NULL) THEN
    RAISE EXCEPTION 'agent profile %: policy must be global (workspace_id IS NULL)', NEW.role USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_agent_profiles_global_policy BEFORE INSERT OR UPDATE OF policy_profile_id ON agent_profiles
  FOR EACH ROW EXECUTE FUNCTION trg_fn_agent_profiles_global_policy();

-- Append-only (§1.5, §9.1): UPDATE запрещён всем; DELETE — только aicd_maintenance в сессии обслуживания
-- (архив run, §20.2). Совпадает по смыслу с trg_fn_run_event_log_append_only (0005).
CREATE FUNCTION trg_fn_append_only_except_maintenance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_user = 'aicd_maintenance'
     AND COALESCE(current_setting('aicd.maint_session', true), '') <> '' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = '42501';
END $$;
CREATE TRIGGER trg_agent_messages_append_only BEFORE UPDATE OR DELETE ON agent_messages
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only_except_maintenance();
-- agent_activity_events: строки неизменяемы всегда; retention — только DETACH + DROP партиции (§20.2).
CREATE TRIGGER trg_agent_activity_events_append_only BEFORE UPDATE OR DELETE ON agent_activity_events
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
"""

DEFERRED_FKS = r"""
-- FK, отложенные миграцией 0003 до Phase 4 (§7.6, §9.3, §9.5, §9.8)
ALTER TABLE agent_assignments
  ADD CONSTRAINT fk_agent_assignments_profile FOREIGN KEY (agent_profile_id) REFERENCES agent_profiles(id) ON DELETE RESTRICT;
ALTER TABLE agent_decisions
  ADD CONSTRAINT fk_agent_decisions_message FOREIGN KEY (workspace_id, acp_message_id)
      REFERENCES agent_messages(workspace_id, id) ON DELETE RESTRICT;
ALTER TABLE chat_messages
  ADD CONSTRAINT fk_chat_messages_acp FOREIGN KEY (workspace_id, acp_message_id)
      REFERENCES agent_messages(workspace_id, id) ON DELETE RESTRICT;
ALTER TABLE directive_acknowledgements
  ADD CONSTRAINT fk_directive_ack_message FOREIGN KEY (workspace_id, acp_message_id)
      REFERENCES agent_messages(workspace_id, id) ON DELETE RESTRICT;
-- Индексы ссылающихся колонок: проверка RESTRICT при удалении сообщений (архив run) без seq scan.
CREATE INDEX ix_agent_decisions_acp_message ON agent_decisions (acp_message_id) WHERE acp_message_id IS NOT NULL;
CREATE INDEX ix_chat_messages_acp_message ON chat_messages (acp_message_id) WHERE acp_message_id IS NOT NULL;
CREATE INDEX ix_directive_ack_acp_message ON directive_acknowledgements (acp_message_id) WHERE acp_message_id IS NOT NULL;
"""

RLS = r"""
-- T-таблицы (§19.3, §19.4); для партиционированной — политика на родителе (партиции: ENABLE+FORCE без прав).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['agent_messages','message_deliveries','agent_activity_events']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO aicd_app, aicd_worker '
                   'USING (workspace_id = app_current_workspace()) '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_tenant', t);
  END LOOP;
END $$;
"""

GRANTS = r"""
REVOKE ALL ON agent_profiles, agent_messages, message_deliveries, agent_activity_events FROM PUBLIC;

-- G-справочник (§19.4): чтение app/worker; запись — миграции/операторская синхронизация под aicd_migrator.
GRANT SELECT ON agent_profiles TO aicd_app, aicd_worker;

-- api: чтение ACP, доставок и ленты активности (GET /tasks/{id}/messages, /runs/{id}/activity).
GRANT SELECT ON agent_messages, message_deliveries, agent_activity_events TO aicd_app;

-- workers: ACP-брокер (persist + deliveries в одной транзакции), потребители (ack/retry/DLQ), лента активности.
GRANT SELECT, INSERT ON agent_messages, agent_activity_events TO aicd_worker;
GRANT SELECT, INSERT, UPDATE ON message_deliveries TO aicd_worker;
REVOKE UPDATE, DELETE, TRUNCATE ON agent_messages, agent_activity_events FROM aicd_app, aicd_worker;

-- maintenance: retention/архив run (§19.6, §20.2–20.3).
GRANT SELECT ON agent_profiles, agent_messages, message_deliveries, agent_activity_events TO aicd_maintenance;
GRANT DELETE ON agent_messages, chat_messages, chat_threads TO aicd_maintenance;
GRANT UPDATE (body) ON chat_messages TO aicd_maintenance;
GRANT INSERT ON artifacts TO aicd_maintenance;
"""

DOWNGRADE = r"""
REVOKE INSERT ON artifacts FROM aicd_maintenance;
REVOKE UPDATE (body) ON chat_messages FROM aicd_maintenance;
REVOKE DELETE ON chat_messages, chat_threads FROM aicd_maintenance;

DROP INDEX IF EXISTS ix_directive_ack_acp_message;
DROP INDEX IF EXISTS ix_chat_messages_acp_message;
DROP INDEX IF EXISTS ix_agent_decisions_acp_message;
ALTER TABLE directive_acknowledgements DROP CONSTRAINT IF EXISTS fk_directive_ack_message;
ALTER TABLE chat_messages DROP CONSTRAINT IF EXISTS fk_chat_messages_acp;
ALTER TABLE agent_decisions DROP CONSTRAINT IF EXISTS fk_agent_decisions_message;
ALTER TABLE agent_assignments DROP CONSTRAINT IF EXISTS fk_agent_assignments_profile;

DROP TABLE IF EXISTS agent_activity_events, message_deliveries, agent_messages, agent_profiles CASCADE;

DROP FUNCTION IF EXISTS trg_fn_append_only_except_maintenance();
DROP FUNCTION IF EXISTS trg_fn_agent_profiles_global_policy();
"""


def upgrade() -> None:
    for block in (TABLES, TRIGGERS, DEFERRED_FKS, RLS, GRANTS):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
