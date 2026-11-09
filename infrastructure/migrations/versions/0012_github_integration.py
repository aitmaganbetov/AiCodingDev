"""GitHub-интеграция (P8-01): github_installations, github_webhook_deliveries, pull_requests.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-08

Источник DDL: docs/database/DATABASE_DESIGN.md §16.1–16.3 (таблицы), §6.3 (FK repositories →
github_installations, отложенный миграцией 0001), §19.3–19.5 (RLS, классы T/G, SECURITY DEFINER),
SECURITY_ARCHITECTURE §10 (вебхуки, merge только по approval MERGE). CREATE TABLE перенесены из документа
без изменений. Дополнительно:
  * repositories.fk_repositories_gh_installation — `ADD CONSTRAINT … NOT VALID` + `VALIDATE` (§21 «FK на таблицу
    более поздней фазы»);
  * pull_requests: составной FK на repositories/runs/external_actions (как в §16.3); триггеры updated_at и
    неизменяемости workspace_id; FSM для PR в документе не задана — переходы OPEN/CLOSED/MERGED проверяет
    trg_pull_requests_state (MERGED — терминальное состояние, как у GitHub);
  * классы (§19.4): github_installations, pull_requests — T (RLS ENABLE+FORCE, политика p_<t>_tenant
    для app/worker); github_webhook_deliveries — G (без RLS): у aicd_app нет прямых прав на таблицу —
    запись приёма вебхука выполняет узкая SECURITY DEFINER функция `sys_github_webhook_receive`
    (§19.5; строка доставки + событие outbox без workspace — у aicd_app нет INSERT в outbox_events вне
    своего workspace), обработку (статус PROCESSED/IGNORED/FAILED) — aicd_worker;
  * SECURITY DEFINER (§19.5): `sys_resolve_installation_workspace(bigint)` — aicd_worker (workspace установки
    после проверки подписи вебхука); `sys_github_webhook_receive(...)` — aicd_app;
  * trg_fn_lock_run_share (0003): в COMPLETED run допускаются approvals с `subject.scope = 'github'`
    (MERGE PR и MANUAL_REVIEW пуша CI-конфигурации, SECURITY_ARCHITECTURE §10–11): merge — шаг после
    успешного завершения run (ck_approvals_run_bound требует run_id для MERGE), прочие записи в COMPLETED run
    по-прежнему запрещены.
Секретов в таблицах нет (§18.9): ключ App, webhook secret и installation tokens — только файлы
секретов/память процесса. Payload вебхука не сохраняется (`payload_storage_key` остаётся NULL): payload —
подсказка (SC-GH-03), состояние перечитывается через GitHub API; хранится только sha256 тела.
Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).

Обратимость: downgrade удаляет таблицы, функции, FK repositories и возвращает trg_fn_lock_run_share к виду
0003 (строки approvals scope=github в COMPLETED run должны быть закрыты заранее — иначе их дальнейшее
изменение будет отклонено триггером).
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

TABLES = r"""
-- §16.1 github_installations (T, integrations_github)
CREATE TABLE github_installations (
  id                    uuid7       NOT NULL,
  workspace_id          uuid        NOT NULL,
  installation_id       bigint      NOT NULL CHECK (installation_id > 0),
  account_login         text        NOT NULL CHECK (account_login ~ '^[A-Za-z0-9-]{1,39}$'),
  account_type          text        NOT NULL CHECK (account_type IN ('user','organization')),
  repository_selection  text        NOT NULL DEFAULT 'selected' CHECK (repository_selection IN ('all','selected')),
  permissions           jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(permissions) = 'object'),
  status                text        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED','REVOKED')),
  installed_by          uuid,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_github_installations PRIMARY KEY (id),
  CONSTRAINT uq_github_installations_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_github_installations_installation_id UNIQUE (installation_id),
  CONSTRAINT fk_github_installations_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_github_installations_installed_by FOREIGN KEY (installed_by) REFERENCES users(id) ON DELETE SET NULL
);

-- §16.2 github_webhook_deliveries (G, integrations_github)
CREATE TABLE github_webhook_deliveries (
  id                   uuid7       NOT NULL,
  delivery_guid        uuid        NOT NULL,       -- X-GitHub-Delivery
  event                text        NOT NULL CHECK (event ~ '^[a-z_]{1,64}$'),
  action               text        CHECK (action ~ '^[a-z_]{1,64}$'),
  installation_id      bigint,
  signature_valid      boolean     NOT NULL,
  payload_sha256       sha256_hex  NOT NULL,
  payload_storage_key  text        CHECK (payload_storage_key ~ '^(s3|file)://[A-Za-z0-9._/-]{1,1024}$'),  -- глобальный префикс webhooks/
  status               text        NOT NULL DEFAULT 'RECEIVED' CHECK (status IN ('RECEIVED','REJECTED','PROCESSED','IGNORED','FAILED')),
  error                text        CHECK (length(error) <= 2000),
  received_at          timestamptz NOT NULL DEFAULT now(),
  processed_at         timestamptz,
  CONSTRAINT pk_github_webhook_deliveries PRIMARY KEY (id),
  CONSTRAINT uq_github_webhook_deliveries_guid UNIQUE (delivery_guid),
  CONSTRAINT ck_gwd_signature CHECK (signature_valid OR (status = 'REJECTED' AND payload_storage_key IS NULL)),
  CONSTRAINT ck_gwd_processed CHECK ((status IN ('PROCESSED','IGNORED','FAILED')) = (processed_at IS NOT NULL))
);
CREATE INDEX ix_gwd_pending ON github_webhook_deliveries (received_at) WHERE status = 'RECEIVED';

-- §16.3 pull_requests (T, integrations_github)
CREATE TABLE pull_requests (
  id                        uuid7       NOT NULL,
  workspace_id              uuid        NOT NULL,
  repository_id             uuid        NOT NULL,
  run_id                    uuid,
  create_action_id          uuid,       -- external_actions (pr_create)
  merge_action_id           uuid,       -- external_actions (pr_merge)
  pr_number                 int         NOT NULL CHECK (pr_number > 0),
  head_branch               text        NOT NULL CHECK (length(head_branch) BETWEEN 1 AND 255),
  base_branch               text        NOT NULL CHECK (length(base_branch) BETWEEN 1 AND 255),
  head_sha                  git_sha     NOT NULL,
  state                     text        NOT NULL CHECK (state IN ('OPEN','CLOSED','MERGED')),
  url                       text        NOT NULL CHECK (url ~ '^https://'),
  merged_commit_sha         git_sha,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  merged_at                 timestamptz,
  closed_at                 timestamptz,
  CONSTRAINT pk_pull_requests PRIMARY KEY (id),
  CONSTRAINT uq_pull_requests_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_pull_requests_number UNIQUE (repository_id, pr_number),
  CONSTRAINT fk_pull_requests_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_pull_requests_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE SET NULL (run_id),
  CONSTRAINT fk_pull_requests_create_action FOREIGN KEY (workspace_id, create_action_id) REFERENCES external_actions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_pull_requests_merge_action FOREIGN KEY (workspace_id, merge_action_id) REFERENCES external_actions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_pull_requests_merged CHECK ((state = 'MERGED') = (merged_at IS NOT NULL AND merged_commit_sha IS NOT NULL)),
  CONSTRAINT ck_pull_requests_closed CHECK (state <> 'CLOSED' OR closed_at IS NOT NULL)
);
CREATE INDEX ix_pull_requests_run ON pull_requests (workspace_id, run_id) WHERE run_id IS NOT NULL;
CREATE INDEX ix_pull_requests_open ON pull_requests (workspace_id, repository_id) WHERE state = 'OPEN';

-- §6.3: FK repositories → github_installations, отложенный миграцией 0001.
ALTER TABLE repositories ADD CONSTRAINT fk_repositories_gh_installation FOREIGN KEY (workspace_id, github_installation_id)
  REFERENCES github_installations(workspace_id, id) ON DELETE SET NULL (github_installation_id) NOT VALID;
ALTER TABLE repositories VALIDATE CONSTRAINT fk_repositories_gh_installation;
"""

TRIGGERS = r"""
CREATE TRIGGER trg_github_installations_updated_at BEFORE UPDATE ON github_installations
  FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_github_installations_immutable_ws BEFORE UPDATE OF workspace_id ON github_installations
  FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_pull_requests_updated_at BEFORE UPDATE ON pull_requests
  FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_pull_requests_immutable_ws BEFORE UPDATE OF workspace_id ON pull_requests
  FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- Состояние PR: MERGED терминально; merge_action_id — только pr_merge того же workspace (AC-13: merge
-- платформой — только через ledger), create_action_id — только pr_create.
CREATE FUNCTION trg_fn_pull_requests_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.state = 'MERGED' AND NEW.state <> 'MERGED' THEN
    RAISE EXCEPTION 'pull request %: MERGED is terminal', NEW.id USING ERRCODE = '23514';
  END IF;
  IF NEW.merge_action_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM external_actions a WHERE a.workspace_id = NEW.workspace_id AND a.id = NEW.merge_action_id
          AND a.action_type = 'pr_merge') THEN
    RAISE EXCEPTION 'pull request %: merge_action_id must reference pr_merge', NEW.id USING ERRCODE = '23514';
  END IF;
  IF NEW.create_action_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM external_actions a WHERE a.workspace_id = NEW.workspace_id AND a.id = NEW.create_action_id
          AND a.action_type = 'pr_create') THEN
    RAISE EXCEPTION 'pull request %: create_action_id must reference pr_create', NEW.id USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_pull_requests_state BEFORE INSERT OR UPDATE ON pull_requests
  FOR EACH ROW EXECUTE FUNCTION trg_fn_pull_requests_state();

-- 0003 + approvals GitHub-предмета (MERGE / MANUAL_REVIEW пуша CI-конфигурации) в COMPLETED run.
CREATE OR REPLACE FUNCTION trg_fn_lock_run_share() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_status text;
BEGIN
  IF NEW.run_id IS NULL THEN RETURN NEW; END IF;
  SELECT status INTO v_status FROM runs WHERE id = NEW.run_id FOR SHARE;
  -- Триггер общий для tasks/gate_executions/approvals: поля approvals читаются через to_jsonb(NEW).
  IF v_status = 'COMPLETED' AND TG_TABLE_NAME = 'approvals'
     AND to_jsonb(NEW) ->> 'action_type' IN ('MERGE','MANUAL_REVIEW')
     AND to_jsonb(NEW) -> 'subject' ->> 'scope' = 'github' THEN
    RETURN NEW;
  END IF;
  IF v_status = 'COMPLETED'
     OR (v_status IN ('FAILED','CANCELLED') AND NOT (
           (TG_TABLE_NAME = 'tasks'           AND NEW.status IN ('CANCELLED','FAILED'))
        OR (TG_TABLE_NAME = 'approvals'       AND NEW.status IN ('CANCELLED','EXPIRED'))
        OR (TG_TABLE_NAME = 'gate_executions' AND NEW.status IN ('ERROR','SKIPPED')))) THEN
    RAISE EXCEPTION 'run % is % — % cannot be changed', NEW.run_id, v_status, TG_TABLE_NAME USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
"""

RLS = r"""
-- T-таблицы (§19.3, §19.4); github_webhook_deliveries — G (без RLS, доступ ограничен GRANT).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['github_installations','pull_requests']
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
REVOKE ALL ON github_installations, github_webhook_deliveries, pull_requests FROM PUBLIC;

-- api: подключение/отключение установки GitHub App (github:manage), просмотр установок и PR.
GRANT SELECT, INSERT ON github_installations TO aicd_app;
GRANT UPDATE (status, account_login, account_type, repository_selection, permissions, installed_by)
  ON github_installations TO aicd_app;
GRANT SELECT ON pull_requests TO aicd_app;

-- workers: статус установки по вебхуку/API GitHub, PR workflow (ledger external_actions), обработка вебхуков.
GRANT SELECT ON github_installations TO aicd_worker;
GRANT UPDATE (status, account_login, repository_selection, permissions) ON github_installations TO aicd_worker;
GRANT SELECT, INSERT, UPDATE ON pull_requests TO aicd_worker;
GRANT SELECT ON github_webhook_deliveries TO aicd_worker;
GRANT UPDATE (status, error, processed_at) ON github_webhook_deliveries TO aicd_worker;

-- maintenance: retention доставок вебхуков (30 дней, §20.3) и архив.
GRANT SELECT ON github_installations, pull_requests TO aicd_maintenance;
GRANT SELECT, DELETE ON github_webhook_deliveries TO aicd_maintenance;
"""

SYSFN = r"""
-- §19.5: workspace установки GitHub App (после проверки подписи вебхука) — только идентификатор.
CREATE FUNCTION sys_resolve_installation_workspace(p_installation_id bigint)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT i.workspace_id FROM aicd.github_installations i WHERE i.installation_id = p_installation_id
$$;

-- §19.5, SC-GH-01/02: приём доставки вебхука (вызывает api ПОСЛЕ проверки HMAC по сырому телу).
-- Строка github_webhook_deliveries + (для RECEIVED) событие outbox `github.webhook.received` без workspace
-- в одной транзакции. Дедупликация по delivery_guid: повтор → 'duplicate' без нового события. Строка
-- REJECTED (неверная подпись — например, подделка с угаданным GUID) не блокирует подлинную доставку
-- с тем же GUID: она заменяется. Результат: 'accepted' | 'ignored' | 'rejected' | 'duplicate'.
-- p_status: RECEIVED (подпись верна, событие из allowlist), IGNORED (подпись верна, событие вне allowlist),
-- REJECTED (подпись неверна/отсутствует; payload не учитывается). 22023 — неверные аргументы.
CREATE FUNCTION sys_github_webhook_receive(
  p_id uuid, p_event_id uuid, p_delivery_guid uuid, p_event text, p_action text, p_installation_id bigint,
  p_signature_valid boolean, p_payload_sha256 text, p_status text, p_error text, p_hint jsonb)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
DECLARE
  v_existing aicd.github_webhook_deliveries%ROWTYPE;
  v_id uuid;
BEGIN
  IF p_status NOT IN ('RECEIVED','IGNORED','REJECTED')
     OR (p_status = 'REJECTED') = p_signature_valid
     OR p_hint IS NULL OR jsonb_typeof(p_hint) <> 'object' OR octet_length(p_hint::text) > 16384 THEN
    RAISE EXCEPTION 'invalid webhook delivery arguments' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v_existing FROM aicd.github_webhook_deliveries WHERE delivery_guid = p_delivery_guid FOR UPDATE;
  IF FOUND THEN
    IF v_existing.signature_valid OR NOT p_signature_valid THEN
      RETURN CASE WHEN p_signature_valid THEN 'duplicate' ELSE 'rejected' END;
    END IF;
    -- Подлинная доставка после поддельной с тем же GUID: строка REJECTED заменяется.
    UPDATE aicd.github_webhook_deliveries
       SET event = p_event, action = p_action, installation_id = p_installation_id, signature_valid = true,
           payload_sha256 = p_payload_sha256, status = p_status, error = p_error, received_at = now(),
           processed_at = CASE WHEN p_status = 'IGNORED' THEN now() END
     WHERE id = v_existing.id;
    v_id := v_existing.id;
  ELSE
    INSERT INTO aicd.github_webhook_deliveries (id, delivery_guid, event, action, installation_id, signature_valid,
                                                payload_sha256, status, error, processed_at)
    VALUES (p_id, p_delivery_guid, p_event, p_action, p_installation_id, p_signature_valid, p_payload_sha256,
            p_status, p_error, CASE WHEN p_status = 'IGNORED' THEN now() END)
    ON CONFLICT ON CONSTRAINT uq_github_webhook_deliveries_guid DO NOTHING
    RETURNING id INTO v_id;
    IF v_id IS NULL THEN   -- конкурентная доставка того же GUID
      RETURN CASE WHEN p_signature_valid THEN 'duplicate' ELSE 'rejected' END;
    END IF;
  END IF;
  IF p_status = 'REJECTED' THEN
    RETURN 'rejected';
  END IF;
  IF p_status = 'IGNORED' THEN
    RETURN 'ignored';
  END IF;
  INSERT INTO aicd.outbox_events (id, workspace_id, run_id, aggregate_type, aggregate_id, event_type, payload)
  VALUES (p_event_id, NULL, NULL, 'github_webhook_delivery', v_id, 'github.webhook.received',
          jsonb_build_object('delivery_id', v_id, 'delivery_guid', p_delivery_guid, 'event', p_event,
                             'action', p_action, 'installation_id', p_installation_id, 'hint', p_hint));
  RETURN 'accepted';
END $$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_resolve_installation_workspace(bigint) OWNER TO aicd_sysfn;
ALTER FUNCTION sys_github_webhook_receive(uuid, uuid, uuid, text, text, bigint, boolean, text, text, text, jsonb)
  OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_resolve_installation_workspace(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION sys_github_webhook_receive(uuid, uuid, uuid, text, text, bigint, boolean, text, text, text, jsonb)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_resolve_installation_workspace(bigint) TO aicd_worker;
GRANT EXECUTE ON FUNCTION sys_github_webhook_receive(uuid, uuid, uuid, text, text, bigint, boolean, text, text, text, jsonb)
  TO aicd_app;
GRANT SELECT (installation_id, workspace_id) ON github_installations TO aicd_sysfn;
GRANT SELECT, INSERT, UPDATE ON github_webhook_deliveries TO aicd_sysfn;
GRANT INSERT ON outbox_events TO aicd_sysfn;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_github_webhook_receive(uuid, uuid, uuid, text, text, bigint, boolean, text, text, text, jsonb);
DROP FUNCTION IF EXISTS sys_resolve_installation_workspace(bigint);
REVOKE INSERT ON outbox_events FROM aicd_sysfn;
ALTER TABLE repositories DROP CONSTRAINT IF EXISTS fk_repositories_gh_installation;
DROP TABLE IF EXISTS pull_requests, github_webhook_deliveries, github_installations CASCADE;
DROP FUNCTION IF EXISTS trg_fn_pull_requests_state();

CREATE OR REPLACE FUNCTION trg_fn_lock_run_share() RETURNS trigger LANGUAGE plpgsql AS $$
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
"""


def upgrade() -> None:
    for block in (TABLES, TRIGGERS, RLS, GRANTS, SYSFN):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
