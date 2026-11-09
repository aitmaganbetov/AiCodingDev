"""Резолвер workspace артефакта для `GET /artifacts/{artifact_id}/download` (Phase 7).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-08

`downloadArtifact` не содержит workspace в пути: как `sys_resolve_evidence_workspace` (0008) и
`sys_resolve_finding_workspace` (0010), узкая SECURITY DEFINER функция (§19.5) возвращает workspace
артефакта только члену этого workspace — чужой артефакт неотличим от отсутствующего (404, AC-12).
Владелец aicd_sysfn, фиксированный search_path, EXECUTE — только aicd_app; aicd_sysfn получает SELECT
лишь на колонки (id, workspace_id) таблицы artifacts.

Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).
Обратимость: downgrade удаляет функцию и отзывает выданные aicd_sysfn права.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

SYSFN = r"""
-- §19.5: workspace артефакта, только если текущий пользователь — член этого workspace.
CREATE FUNCTION sys_resolve_artifact_workspace(p_artifact_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT a.workspace_id
    FROM aicd.artifacts a
    JOIN aicd.workspace_members m ON m.workspace_id = a.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE a.id = p_artifact_id
$$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_resolve_artifact_workspace(uuid) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_resolve_artifact_workspace(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_resolve_artifact_workspace(uuid) TO aicd_app;
GRANT SELECT (id, workspace_id) ON artifacts TO aicd_sysfn;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_resolve_artifact_workspace(uuid);
REVOKE SELECT (id, workspace_id) ON artifacts FROM aicd_sysfn;
"""


def upgrade() -> None:
    op.execute(SYSFN)


def downgrade() -> None:
    op.execute(DOWNGRADE)
