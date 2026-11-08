"""P3-18 / OQ-41 (a) / OQ-47 (7): права роли aicd_maintenance на retention оставшихся таблиц.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-08

Источник: DATABASE_DESIGN §20.3 (сроки хранения), §19.6 (роль aicd_maintenance). Retention job
(`aicd_platform.retention`) удаляет по срокам:

* `usage_metrics` — 365 дней (`observed_at`);
* `code_index_snapshots` — `FAILED`/`SUPERSEDED` старше 7 дней и `READY` сверх 5 последних на репозиторий;
  снимки, на которые ссылаются `context_packs` (FK RESTRICT) или активные `indexing_jobs`, не удаляются;
  `code_files`/`code_symbols`/`code_dependencies`/`knowledge_chunks` — каскадом FK (RI исполняется от
  владельца таблиц); завершённые `indexing_jobs`, ссылающиеся на удаляемый снимок, удаляются вместе с ним
  (иначе `ON DELETE SET NULL` нарушил бы `ck_indexing_jobs_kind` у инкрементальных заданий);
* `documentation_snapshots` — `SUPERSEDED` старше 90 дней, если на них нет ссылок (FK RESTRICT ⇒ пропуск).

`github_webhook_deliveries` (30 дней) и `artifacts` (по `retention_class`) — права DELETE уже выданы
(0012, 0001). SELECT на эти таблицы и `context_packs` у роли уже есть (выдан при их создании).

Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).
Обратимость: downgrade отзывает выданное право DELETE.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

TABLES = "usage_metrics, code_index_snapshots, documentation_snapshots, indexing_jobs"

UPGRADE = f"""
GRANT DELETE ON {TABLES} TO aicd_maintenance;
"""

DOWNGRADE = f"""
REVOKE DELETE ON {TABLES} FROM aicd_maintenance;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
