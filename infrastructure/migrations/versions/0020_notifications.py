"""UI redesign B8 (UI_REDESIGN_PLAN §3.1, §3.10 G-15; ADR-0016): персональные уведомления с отметкой прочтения.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-09

`notifications` (T, модуль collaboration; DATABASE_DESIGN §9.8) — проекция доменных событий в уведомления
пользователя в workspace. Строки пишет только потребитель outbox `notifications.project` (роль `aicd_worker`,
`aicd_orchestration.notifications`): одна строка на получателя, вид и исходное событие —
`uq_notifications_source (workspace_id, user_id, source_event_id, kind)` отсекает дубли повторной доставки.
Содержимое — шаблонный заголовок, редактированная выжимка (≤ 1000 символов, на практике ≤ 300), идентификаторы
для ссылок и небольшой `data` (скаляры, ≤ 4 KiB); секретов и payload событий нет (§18.9).

* получатель — участник workspace: составной FK `(workspace_id, user_id) → workspace_members ON DELETE CASCADE`
  (удаление участника удаляет его уведомления — после удаления они недоступны ему всё равно);
* RLS (ENABLE + FORCE): `aicd_app` — только свои строки текущего workspace
  (`workspace_id = app_current_workspace() AND user_id = app_current_user()`); `aicd_worker` — строки workspace
  контекста (запись уведомлений любым участникам);
* права: `aicd_app` — SELECT и UPDATE только `read_at` (отметка прочтения, без удаления); `aicd_worker` — SELECT,
  INSERT; `aicd_maintenance` — SELECT (pg_dump, 0016) и DELETE (retention 90 дней, `aicd_platform.retention`);
* индексы: лента `(workspace_id, user_id, created_at DESC, id DESC)` (также покрывает FK), непрочитанные
  (частичный, `read_at IS NULL`) и `created_at` (retention).

Миграция — только SQL через op.execute (offline `alembic upgrade --sql`). Обратимость: downgrade удаляет таблицу
(уведомления — производные данные, восстанавливаются заново только для новых событий).
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

KINDS = (
    "'approval.requested','approval.decided','run.completed','run.failed','run.blocked',"
    "'directive.awaiting_confirmation','directive.acknowledged','directive.completed','directive.failed',"
    "'pull_request.merged','pull_request.failed','finding.critical'"
)

TABLE = rf"""
CREATE TABLE notifications (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  user_id          uuid        NOT NULL,
  kind             text        NOT NULL CHECK (kind IN ({KINDS})),
  severity         text        NOT NULL CHECK (severity IN ('info','success','warning','error')),
  title            text        NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  body             text        NOT NULL DEFAULT '' CHECK (length(body) <= 1000),
  project_id       uuid,
  run_id           uuid,
  task_id          uuid,
  resource_type    text        NOT NULL CHECK (resource_type IN ('approval','run','directive','gate_execution','external_action')),
  resource_id      uuid        NOT NULL,
  data             jsonb       NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(data) = 'object' AND pg_column_size(data) <= 4096),
  source_event_id  uuid        NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  read_at          timestamptz,
  CONSTRAINT pk_notifications PRIMARY KEY (id),
  CONSTRAINT uq_notifications_source UNIQUE (workspace_id, user_id, source_event_id, kind),
  CONSTRAINT ck_notifications_read_after_created CHECK (read_at IS NULL OR read_at >= created_at),
  CONSTRAINT fk_notifications_member FOREIGN KEY (workspace_id, user_id)
    REFERENCES workspace_members (workspace_id, user_id) ON DELETE CASCADE
);
CREATE INDEX ix_notifications_feed ON notifications (workspace_id, user_id, created_at DESC, id DESC);
CREATE INDEX ix_notifications_unread ON notifications (workspace_id, user_id, created_at, id) WHERE read_at IS NULL;
CREATE INDEX ix_notifications_created_at ON notifications (created_at);
"""

RLS = r"""
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE ROW LEVEL SECURITY;
CREATE POLICY p_notifications_owner ON notifications FOR ALL TO aicd_app
  USING (workspace_id = app_current_workspace() AND user_id = app_current_user())
  WITH CHECK (workspace_id = app_current_workspace() AND user_id = app_current_user());
CREATE POLICY p_notifications_worker ON notifications FOR ALL TO aicd_worker
  USING (workspace_id = app_current_workspace())
  WITH CHECK (workspace_id = app_current_workspace());
"""

GRANTS = r"""
REVOKE ALL ON notifications FROM PUBLIC;
GRANT SELECT, UPDATE (read_at) ON notifications TO aicd_app;
GRANT SELECT, INSERT ON notifications TO aicd_worker;
GRANT SELECT, DELETE ON notifications TO aicd_maintenance;
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS notifications;
"""


def upgrade() -> None:
    for block in (TABLE, RLS, GRANTS):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
