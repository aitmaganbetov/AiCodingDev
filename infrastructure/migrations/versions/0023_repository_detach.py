"""Отвязка репозитория от проекта: `DETACHED` освобождает каталог и GitHub-репозиторий (FR-PRJ-02/03).

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-10

`repositories.status` знал `DETACHED` с 0001 (DATABASE_DESIGN §270), но ни один путь кода его не ставил:
привязанный по ошибке репозиторий оставался в проекте навсегда. Отвязка — смена статуса, а не удаление
строки: на репозиторий ссылаются `runs`, `worktrees`, `pull_requests`, `review_threads`,
`code_index_snapshots`, `indexing_jobs` (все FK — `ON DELETE RESTRICT`), и история прогонов должна
остаться читаемой.

Чтобы отвязка имела смысл, занятость каталога и GitHub-репозитория должна сниматься вместе со статусом —
иначе тот же каталог нельзя привязать заново (`local_path_taken`), а сам репозиторий дал бы
`repository_taken`. Поэтому оба уникальных ограничения становятся частичными (`WHERE status <> 'DETACHED'`):

* `uq_repositories_local_root` из UNIQUE-ограничения становится частичным уникальным индексом того же имени
  (ограничение частичным быть не может); имя сохранено — по нему `aicd_projects.api` различает, что занято;
* `uq_repositories_github_repo` уже был частичным индексом (`github_repo_id IS NOT NULL`) — добавляется
  условие по статусу.

Миграция — только SQL через op.execute (offline `alembic upgrade --sql`). Обратимость: downgrade возвращает
полные ограничения. Если к этому моменту в workspace есть отвязанный и заново привязанный репозиторий с тем же
каталогом (ровно то, что разрешает эта миграция), downgrade упрётся в дубликат — такие строки разводят вручную
до отката, автоматически выбрать «лишнюю» нельзя.
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE repositories DROP CONSTRAINT uq_repositories_local_root;
CREATE UNIQUE INDEX uq_repositories_local_root ON repositories (workspace_id, local_root_ref)
  WHERE status <> 'DETACHED';

DROP INDEX uq_repositories_github_repo;
CREATE UNIQUE INDEX uq_repositories_github_repo ON repositories (workspace_id, github_repo_id)
  WHERE github_repo_id IS NOT NULL AND status <> 'DETACHED';
"""

DOWNGRADE = r"""
DROP INDEX uq_repositories_github_repo;
CREATE UNIQUE INDEX uq_repositories_github_repo ON repositories (workspace_id, github_repo_id)
  WHERE github_repo_id IS NOT NULL;

DROP INDEX uq_repositories_local_root;
ALTER TABLE repositories ADD CONSTRAINT uq_repositories_local_root UNIQUE (workspace_id, local_root_ref);
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
