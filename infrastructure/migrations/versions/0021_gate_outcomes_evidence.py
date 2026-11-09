"""UI redesign D-8: `v_gate_outcomes` раскрывает статус верификации evidence исполнения gate.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-09

Решение D-8 (docs/planning/OPEN_QUESTIONS.md, UI_REDESIGN_PLAN §5.2): «Пройдено» показывается только для PASS
с evidence в статусе VERIFIED, иначе — «Недостаточно доказательств». Сводка Analytics (`GET /analytics/summary`,
`gates[].passed_verified/passed_unverified`) считает это в SQL через представление модуля-владельца
(verification: gate_executions + evidence; MODULE_BOUNDARIES §2 п.6 — `analytics` читает только `v_*`).

* В конец `v_gate_outcomes` добавлена колонка `evidence_verification_status` (LEFT JOIN `evidence` по
  `(workspace_id, evidence_id)`; NULL — у исполнения нет evidence). `CREATE OR REPLACE VIEW` добавляет колонку,
  не меняя существующие (порядок и типы прежние).
* `security_invoker = true` сохраняется явно: evidence читается с правами и RLS вызывающего (`aicd_app`),
  изоляция workspace (ADR-0008) действует через представление; без RLS-контекста — ноль строк.
* Права повторены явно: `REVOKE ALL … FROM PUBLIC`, `GRANT SELECT … TO aicd_app`.

Миграция — только SQL через op.execute (offline `alembic upgrade --sql`). Обратимость: downgrade пересоздаёт
представление в определении 0013 (колонку из представления `CREATE OR REPLACE` удалить не может) и права.
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE OR REPLACE VIEW v_gate_outcomes WITH (security_invoker = true) AS
SELECT g.id AS gate_execution_id, g.workspace_id, g.run_id, r.project_id, g.task_id, g.gate_type, g.required,
       g.status, g.started_at, g.finished_at,
       CASE WHEN g.started_at IS NOT NULL AND g.finished_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM g.finished_at - g.started_at) END AS duration_seconds,
       e.verification_status::text AS evidence_verification_status
  FROM gate_executions g
  JOIN runs r ON r.id = g.run_id AND r.workspace_id = g.workspace_id
  LEFT JOIN evidence e ON e.id = g.evidence_id AND e.workspace_id = g.workspace_id
 WHERE g.superseded_at IS NULL;

REVOKE ALL ON v_gate_outcomes FROM PUBLIC;
GRANT SELECT ON v_gate_outcomes TO aicd_app;
"""

DOWNGRADE = r"""
DROP VIEW v_gate_outcomes;
CREATE VIEW v_gate_outcomes WITH (security_invoker = true) AS
SELECT g.id AS gate_execution_id, g.workspace_id, g.run_id, r.project_id, g.task_id, g.gate_type, g.required,
       g.status, g.started_at, g.finished_at,
       CASE WHEN g.started_at IS NOT NULL AND g.finished_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM g.finished_at - g.started_at) END AS duration_seconds
  FROM gate_executions g
  JOIN runs r ON r.id = g.run_id AND r.workspace_id = g.workspace_id
 WHERE g.superseded_at IS NULL;

REVOKE ALL ON v_gate_outcomes FROM PUBLIC;
GRANT SELECT ON v_gate_outcomes TO aicd_app;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
