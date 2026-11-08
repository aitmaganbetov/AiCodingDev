"""Phase 9 security hardening: step-up re-auth, nullable task timeout, GitHub verification in the worker.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-08

Источник: SECURITY_ARCHITECTURE §3.2 (step-up re-auth, `user_sessions.reauthenticated_at` — «предлагаемое
дополнение»), §6.1 (закрытый ключ GitHub App читает только воркер), SC-IAM-04/SC-IAM-08, OQ-45 (f) и
OQ-45 «тайм-аут», OQ-47 (2).

* `user_sessions.reauthenticated_at` — время последнего ввода пароля в сессии (вход или
  `POST /auth/reauthenticate`); step-up требует, чтобы он был не старше 10 минут. Новое значение
  `revoke_reason = 'rotated'`: сессия, заменённая новой после step-up (ротация, SC-IAM-04).
* `tasks.timeout_seconds` — nullable без DEFAULT: NULL = «тайм-аут задачи не задан» (действует
  `default_timeout_seconds` провайдера в workspace), явное 1800 теперь отличимо от «не задано». Данные:
  в незавершённых задачах значение 1800 (раньше означавшее «не задано») заменяется на NULL — эффективный
  тайм-аут не меняется. Триггер `trg_tasks_version` требует инкремента `version`; триггеры статуса/run не
  срабатывают (статус не меняется). FORCE RLS распространяется на владельца, поэтому на время UPDATE в
  транзакции миграции снимается и сразу возвращается (как в 0005 для партиций).
* `github_verification_requests` (T, integrations_github) — запросы проверки установки GitHub App и доступа
  установки к репозиторию. API (без ключа App) фиксирует запрос + событие outbox `github.verification.requested`
  и отвечает 202; воркер (единственный процесс с ключом App) проверяет через GitHub API и записывает результат.
  Права: aicd_app — SELECT/INSERT (создание и опрос статуса); aicd_worker — SELECT и UPDATE полей результата.
  Воркер получает INSERT на github_installations и repositories (подключение установки/привязка репозитория
  после проверки), у aicd_app INSERT на github_installations отзывается (API больше не создаёт установки).

Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).
Обратимость: downgrade удаляет таблицу и столбец, возвращает NOT NULL DEFAULT 1800 (NULL → 1800), строки
`revoke_reason = 'rotated'` переводятся в 'logout' (ближайшее прежнее значение) и восстанавливает права 0012.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

SESSIONS = r"""
ALTER TABLE user_sessions ADD COLUMN reauthenticated_at timestamptz;
UPDATE user_sessions SET reauthenticated_at = created_at WHERE reauthenticated_at IS NULL;
ALTER TABLE user_sessions ADD CONSTRAINT ck_user_sessions_reauth
  CHECK (reauthenticated_at IS NULL OR reauthenticated_at >= created_at);
ALTER TABLE user_sessions DROP CONSTRAINT IF EXISTS user_sessions_revoke_reason_check;
ALTER TABLE user_sessions ADD CONSTRAINT user_sessions_revoke_reason_check
  CHECK (revoke_reason IN ('logout','expired','password_changed','admin_revoked','user_disabled','rotated'));
"""

TASK_TIMEOUT = r"""
ALTER TABLE tasks ALTER COLUMN timeout_seconds DROP DEFAULT;
ALTER TABLE tasks ALTER COLUMN timeout_seconds DROP NOT NULL;
-- FORCE RLS действует и на владельца (мигратора): перенос данных — без FORCE в пределах транзакции миграции.
ALTER TABLE tasks NO FORCE ROW LEVEL SECURITY;
UPDATE tasks SET timeout_seconds = NULL, version = version + 1
 WHERE timeout_seconds = 1800 AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED');
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
"""

VERIFICATIONS = r"""
CREATE TABLE github_verification_requests (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  kind          text        NOT NULL CHECK (kind IN ('installation','repository')),
  status        text        NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SUCCEEDED','FAILED')),
  project_id    uuid,
  params        jsonb       NOT NULL CHECK (jsonb_typeof(params) = 'object' AND octet_length(params::text) <= 8192),
  requested_by  uuid        NOT NULL,
  result_id     uuid,       -- github_installations.id | repositories.id
  error_code    text        CHECK (error_code ~ '^[a-z_]{1,64}$'),
  error_detail  text        CHECK (length(error_detail) <= 500),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  CONSTRAINT pk_github_verification_requests PRIMARY KEY (id),
  CONSTRAINT uq_github_verification_requests_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_gvr_workspace FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT,
  CONSTRAINT fk_gvr_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_gvr_requested_by FOREIGN KEY (requested_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_gvr_completed CHECK ((status = 'PENDING') = (completed_at IS NULL)),
  CONSTRAINT ck_gvr_succeeded CHECK (status <> 'SUCCEEDED' OR (result_id IS NOT NULL AND error_code IS NULL)),
  CONSTRAINT ck_gvr_failed CHECK (status <> 'FAILED' OR error_code IS NOT NULL),
  CONSTRAINT ck_gvr_repository CHECK ((kind = 'repository') = (project_id IS NOT NULL))
);
CREATE INDEX ix_gvr_pending ON github_verification_requests (created_at) WHERE status = 'PENDING';

CREATE TRIGGER trg_gvr_updated_at BEFORE UPDATE ON github_verification_requests
  FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_gvr_immutable_ws BEFORE UPDATE OF workspace_id ON github_verification_requests
  FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- Результат проверки фиксируется один раз: PENDING → SUCCEEDED | FAILED, терминальные состояния неизменяемы.
CREATE FUNCTION trg_fn_gvr_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'PENDING' THEN
    RAISE EXCEPTION 'github verification %: % is terminal', NEW.id, OLD.status USING ERRCODE = '23514';
  END IF;
  IF NEW.kind IS DISTINCT FROM OLD.kind OR NEW.params IS DISTINCT FROM OLD.params
     OR NEW.requested_by IS DISTINCT FROM OLD.requested_by OR NEW.project_id IS DISTINCT FROM OLD.project_id THEN
    RAISE EXCEPTION 'github verification %: request fields are immutable', NEW.id USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_gvr_transition BEFORE UPDATE ON github_verification_requests
  FOR EACH ROW EXECUTE FUNCTION trg_fn_gvr_transition();

ALTER TABLE github_verification_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE github_verification_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY p_github_verification_requests_tenant ON github_verification_requests FOR ALL TO aicd_app, aicd_worker
  USING (workspace_id = app_current_workspace()) WITH CHECK (workspace_id = app_current_workspace());

REVOKE ALL ON github_verification_requests FROM PUBLIC;
GRANT SELECT, INSERT ON github_verification_requests TO aicd_app;
GRANT SELECT ON github_verification_requests TO aicd_worker;
GRANT UPDATE (status, result_id, error_code, error_detail, completed_at) ON github_verification_requests TO aicd_worker;
GRANT SELECT, DELETE ON github_verification_requests TO aicd_maintenance;

-- OQ-47 (2): установку подключает и репозиторий привязывает воркер после проверки через GitHub API.
REVOKE INSERT ON github_installations FROM aicd_app;
REVOKE UPDATE (account_login, account_type, repository_selection, permissions, installed_by)
  ON github_installations FROM aicd_app;
GRANT INSERT ON github_installations TO aicd_worker;
GRANT UPDATE (account_type, installed_by) ON github_installations TO aicd_worker;
GRANT INSERT ON repositories TO aicd_worker;
"""

DOWNGRADE = r"""
REVOKE INSERT ON repositories FROM aicd_worker;
REVOKE UPDATE (account_type, installed_by) ON github_installations FROM aicd_worker;
REVOKE INSERT ON github_installations FROM aicd_worker;
GRANT INSERT ON github_installations TO aicd_app;
GRANT UPDATE (status, account_login, account_type, repository_selection, permissions, installed_by)
  ON github_installations TO aicd_app;
DROP TABLE IF EXISTS github_verification_requests;
DROP FUNCTION IF EXISTS trg_fn_gvr_transition();

ALTER TABLE tasks NO FORCE ROW LEVEL SECURITY;
UPDATE tasks SET timeout_seconds = 1800, version = version + 1 WHERE timeout_seconds IS NULL;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE tasks ALTER COLUMN timeout_seconds SET DEFAULT 1800;
ALTER TABLE tasks ALTER COLUMN timeout_seconds SET NOT NULL;

UPDATE user_sessions SET revoke_reason = 'logout' WHERE revoke_reason = 'rotated';
ALTER TABLE user_sessions DROP CONSTRAINT IF EXISTS user_sessions_revoke_reason_check;
ALTER TABLE user_sessions ADD CONSTRAINT user_sessions_revoke_reason_check
  CHECK (revoke_reason IN ('logout','expired','password_changed','admin_revoked','user_disabled'));
ALTER TABLE user_sessions DROP CONSTRAINT IF EXISTS ck_user_sessions_reauth;
ALTER TABLE user_sessions DROP COLUMN IF EXISTS reauthenticated_at;
"""


def upgrade() -> None:
    for block in (SESSIONS, TASK_TIMEOUT, VERIFICATIONS):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
