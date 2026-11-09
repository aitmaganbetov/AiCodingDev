"""UI redesign B6 (UI_REDESIGN_PLAN §3.10 G-12): trigram-индексы глобального поиска `GET /search`.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-09

Поиск сопоставляет текстовые поля `ILIKE '%q%'` в пределах workspace. Без индекса это полный просмотр строк
workspace в самых больших таблицах; GIN `(workspace_id, <текст> gin_trgm_ops)` (btree_gin + pg_trgm, оба расширения
— bootstrap-database.sql) обслуживает ILIKE с подстрокой ≥ 3 символов и фильтр tenant одним индексом
(как `ix_knowledge_chunks_fts`):

* `ix_runs_objective_trgm` — `runs.objective`;
* `ix_tasks_title_trgm` — `tasks.title`;
* `ix_findings_search_trgm` — выражение `title || ' ' || rule_id || ' ' || coalesce(cwe_id, '')` (поиск находки
  по заголовку, правилу и CWE одним условием; запрос использует то же выражение).

Проекты, approvals и источники документации — малые таблицы (десятки строк на workspace): индексы не нужны.
Сообщения чата уже индексированы FTS (`ix_chat_messages_search`, 0003). Архитектура не меняется: только индексы.

Миграция — только SQL через op.execute (offline `alembic upgrade --sql`). Обратимость: downgrade удаляет индексы.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

INDEXES = r"""
CREATE INDEX ix_runs_objective_trgm ON runs USING gin (workspace_id, objective gin_trgm_ops);
CREATE INDEX ix_tasks_title_trgm ON tasks USING gin (workspace_id, title gin_trgm_ops);
CREATE INDEX ix_findings_search_trgm ON findings
  USING gin (workspace_id, (title || ' ' || rule_id || ' ' || coalesce(cwe_id, '')) gin_trgm_ops);
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS ix_findings_search_trgm;
DROP INDEX IF EXISTS ix_tasks_title_trgm;
DROP INDEX IF EXISTS ix_runs_objective_trgm;
"""


def upgrade() -> None:
    op.execute(INDEXES)


def downgrade() -> None:
    op.execute(DOWNGRADE)
