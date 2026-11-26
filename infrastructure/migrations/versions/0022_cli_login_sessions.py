"""Вход CLI-провайдера из веб-интерфейса: сессии device-code входа (P2-01, SECURITY_ARCHITECTURE §6.2).

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-09

`cli_login_sessions` (T, модуль cli_adapters) — состояние одной попытки интерактивного входа официального CLI,
запущенной оператором из UI. Строку создаёт API (`POST /providers/{id}/login`), ведёт worker-exec: он запускает
официальную команду входа в PTY на хосте исполнения и записывает сюда найденную пару «ссылка + код устройства»
и итог.

**Учётных данных в таблице нет и быть не может** (ТЗ §1, §7.1, cli-auth.md §0): токен пишет сам CLI в свой
credential store, платформа его не видит. `verification_url` и `user_code` — публичная часть device-code потока
(бесполезны без входа владельца подписки на сайте провайдера); `output_tail` — последние строки вывода CLI,
прошедшие `login.scrub()` (ссылка на вход, подсказки оператору), под ограничением длины.

* одна активная сессия на провайдера и workspace: частичный уникальный индекс по состояниям `PENDING`/`CHALLENGE`
  (`max_concurrent_sessions=1`, OQ-01/OQ-24 — одна подписка, один хост исполнения);
* RLS (ENABLE + FORCE): `aicd_app` и `aicd_worker` — строки workspace текущего контекста;
* права: `aicd_app` — SELECT, INSERT и UPDATE только `state`/`error_category`/`updated_at` (отмена оператором,
  без правки вывода и challenge); `aicd_worker` — SELECT, UPDATE (ведёт сессию); `aicd_maintenance` — SELECT
  (pg_dump, 0016) и DELETE (retention: сессии живут минутами, хранятся 30 дней как след аудита);
* индексы: активные сессии провайдера `(workspace_id, provider_id, state)` и `created_at` (retention).

Миграция — только SQL через op.execute (offline `alembic upgrade --sql`). Обратимость: downgrade удаляет
таблицу — сессии входа производны и не восстанавливаются (сам вход хранится в credential store CLI).
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

STATES = "'PENDING','CHALLENGE','AUTHENTICATED','FAILED','CANCELLED','EXPIRED'"
ERRORS = "'CLI_NOT_INSTALLED','AUTH_REQUIRED','TIMEOUT','PROCESS_FAILED','CANCELLED'"
ACTIVE = "'PENDING','CHALLENGE'"

TABLE = rf"""
CREATE TABLE cli_login_sessions (
  id                uuid7       NOT NULL,
  workspace_id      uuid        NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
  provider_id       uuid        NOT NULL REFERENCES cli_providers (id) ON DELETE CASCADE,
  host_id           uuid        REFERENCES worker_hosts (id) ON DELETE SET NULL,
  state             text        NOT NULL DEFAULT 'PENDING' CHECK (state IN ({STATES})),
  verification_url  text        CHECK (verification_url IS NULL OR length(verification_url) <= 2048),
  user_code         text        CHECK (user_code IS NULL OR length(user_code) BETWEEN 1 AND 64),
  output_tail       text        NOT NULL DEFAULT '' CHECK (length(output_tail) <= 8192),
  error_category    text        CHECK (error_category IS NULL OR error_category IN ({ERRORS})),
  requested_by      uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL,
  CONSTRAINT pk_cli_login_sessions PRIMARY KEY (id),
  CONSTRAINT ck_cli_login_sessions_expiry CHECK (expires_at > created_at),
  CONSTRAINT ck_cli_login_sessions_updated CHECK (updated_at >= created_at),
  CONSTRAINT ck_cli_login_sessions_terminal_error CHECK (
    (state IN ({ACTIVE},'AUTHENTICATED') AND error_category IS NULL)
    OR (state IN ('FAILED','CANCELLED','EXPIRED') AND error_category IS NOT NULL)
  )
);
CREATE UNIQUE INDEX uq_cli_login_sessions_active ON cli_login_sessions (workspace_id, provider_id)
  WHERE state IN ({ACTIVE});
CREATE INDEX ix_cli_login_sessions_provider ON cli_login_sessions (workspace_id, provider_id, created_at DESC, id DESC);
CREATE INDEX ix_cli_login_sessions_created_at ON cli_login_sessions (created_at);
CREATE INDEX ix_cli_login_sessions_host ON cli_login_sessions (host_id);
CREATE INDEX ix_cli_login_sessions_requested_by ON cli_login_sessions (requested_by);
"""

RLS = r"""
ALTER TABLE cli_login_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cli_login_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY p_cli_login_sessions_app ON cli_login_sessions FOR ALL TO aicd_app
  USING (workspace_id = app_current_workspace())
  WITH CHECK (workspace_id = app_current_workspace());
CREATE POLICY p_cli_login_sessions_worker ON cli_login_sessions FOR ALL TO aicd_worker
  USING (workspace_id = app_current_workspace())
  WITH CHECK (workspace_id = app_current_workspace());
"""

GRANTS = r"""
REVOKE ALL ON cli_login_sessions FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE (state, error_category, updated_at) ON cli_login_sessions TO aicd_app;
GRANT SELECT, UPDATE ON cli_login_sessions TO aicd_worker;
GRANT SELECT, DELETE ON cli_login_sessions TO aicd_maintenance;
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS cli_login_sessions;
"""


def upgrade() -> None:
    for block in (TABLE, RLS, GRANTS):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
