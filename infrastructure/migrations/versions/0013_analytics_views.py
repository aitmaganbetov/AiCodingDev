"""Отчётные представления Analytics (P8-07, OQ-46 (a)): v_run_outcomes, v_task_latency, v_attempt_outcomes,
v_gate_outcomes, v_cli_usage, v_review_loops, v_approval_wait, v_context_usefulness.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-08

Источник: DATABASE_DESIGN §17.2 (метрики ТЗ §16 из таблиц-источников через read-only представления `v_*`
модулей-владельцев), MODULE_BOUNDARIES §2 п.6 (`analytics` читает только `v_*`). Агрегатов и материализованных
представлений нет: каждое представление — построчная проекция таблиц владельца (без межтенантной агрегации),
агрегирует `aicd_analytics.summary` за окно времени.

* `security_invoker = true` (PostgreSQL ≥ 15): представление читает таблицы с правами и RLS вызывающего
  (`aicd_app`), а не владельца-мигратора — изоляция workspace (ADR-0008) действует через представления
  без изменений; без RLS-контекста — ноль строк (fail closed).
* Права: `REVOKE ALL … FROM PUBLIC`, `GRANT SELECT … TO aicd_app` (только API; воркеры и обслуживание
  представления не используют).
* Каждое представление содержит `workspace_id` и `project_id` (через runs) — фильтр проекта и явный фильтр
  workspace (defence in depth, индексы) без доступа к чужим таблицам.
* `v_attempt_outcomes` — дополнение к перечню §17.2 («например»): попытки задач для verified completion rate
  и retry count (зерно попытки, у `v_task_latency` — задача).
* `v_review_loops` — зерно ACP-сообщения (цепочки по `correlation_id` агрегирует запрос), т.к. окно отчёта
  применяется ко времени сообщения; она же — источник agent message count.

Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).
Обратимость: downgrade удаляет представления.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

VIEWS = (
    "v_run_outcomes",
    "v_task_latency",
    "v_attempt_outcomes",
    "v_gate_outcomes",
    "v_cli_usage",
    "v_review_loops",
    "v_approval_wait",
    "v_context_usefulness",
)

UPGRADE = r"""
-- orchestration: runs
CREATE VIEW v_run_outcomes WITH (security_invoker = true) AS
SELECT r.id AS run_id, r.workspace_id, r.project_id, r.mode, r.status, r.created_at, r.started_at, r.finished_at,
       CASE WHEN r.finished_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM r.finished_at - COALESCE(r.started_at, r.created_at)) END AS duration_seconds
  FROM runs r;

-- orchestration: tasks (task latency, задачи в BLOCKED)
CREATE VIEW v_task_latency WITH (security_invoker = true) AS
SELECT t.id AS task_id, t.workspace_id, t.run_id, r.project_id, t.agent_role, t.status, t.blocked_reason,
       t.created_at, t.updated_at, t.finished_at,
       CASE WHEN t.finished_at IS NOT NULL THEN EXTRACT(EPOCH FROM t.finished_at - t.created_at) END AS latency_seconds
  FROM tasks t
  JOIN runs r ON r.id = t.run_id AND r.workspace_id = t.workspace_id;

-- orchestration: task_attempts (verified completion rate, retry count)
CREATE VIEW v_attempt_outcomes WITH (security_invoker = true) AS
SELECT a.id AS attempt_id, a.workspace_id, a.task_id, t.run_id, r.project_id, t.agent_role, a.attempt_no,
       a.provider, a.status, a.failure_category, a.started_at, a.finished_at
  FROM task_attempts a
  JOIN tasks t ON t.id = a.task_id AND t.workspace_id = a.workspace_id
  JOIN runs r ON r.id = t.run_id AND r.workspace_id = t.workspace_id;

-- verification: gate_executions — только актуальные (не заменённые повтором) исполнения
CREATE VIEW v_gate_outcomes WITH (security_invoker = true) AS
SELECT g.id AS gate_execution_id, g.workspace_id, g.run_id, r.project_id, g.task_id, g.gate_type, g.required,
       g.status, g.started_at, g.finished_at,
       CASE WHEN g.started_at IS NOT NULL AND g.finished_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM g.finished_at - g.started_at) END AS duration_seconds
  FROM gate_executions g
  JOIN runs r ON r.id = g.run_id AND r.workspace_id = g.workspace_id
 WHERE g.superseded_at IS NULL;

-- cli_adapters: cli_executions + usage_metrics tokens.total (usage_* — только cli_reported, ТЗ §9, §16; без слова token в именах колонок, §18.9)
CREATE VIEW v_cli_usage WITH (security_invoker = true) AS
SELECT ce.id AS execution_id, ce.workspace_id, t.run_id, r.project_id, p.code AS provider, ce.status,
       ce.failure_category, ce.created_at, ce.started_at, ce.finished_at,
       CASE WHEN ce.finished_at IS NOT NULL AND ce.started_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM ce.finished_at - ce.started_at) END AS duration_seconds,
       tok.reported AS usage_reported, tok.not_exposed AS usage_not_exposed, tok.total AS usage_total
  FROM cli_executions ce
  JOIN cli_installations i ON i.id = ce.installation_id
  JOIN cli_providers p ON p.id = i.provider_id
  JOIN task_attempts a ON a.id = ce.task_attempt_id AND a.workspace_id = ce.workspace_id
  JOIN tasks t ON t.id = a.task_id AND t.workspace_id = a.workspace_id
  JOIN runs r ON r.id = t.run_id AND r.workspace_id = t.workspace_id
  LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE um.source = 'cli_reported') AS reported,
           count(*) FILTER (WHERE um.source = 'not_exposed_by_cli') AS not_exposed,
           sum(um.metric_value) FILTER (WHERE um.source = 'cli_reported') AS total
      FROM usage_metrics um
     WHERE um.execution_id = ce.id AND um.workspace_id = ce.workspace_id AND um.metric_name = 'tokens.total'
  ) tok ON true;

-- collaboration: agent_messages (зерно — сообщение; цепочки ревью — по correlation_id)
CREATE VIEW v_review_loops WITH (security_invoker = true) AS
SELECT m.id AS message_id, m.workspace_id, m.run_id, r.project_id, m.task_id, m.correlation_id, m.message_type,
       m.sender_role, m.recipient_role, m.created_at
  FROM agent_messages m
  JOIN runs r ON r.id = m.run_id AND r.workspace_id = m.workspace_id;

-- orchestration: approvals (approval wait time — только решения человека)
CREATE VIEW v_approval_wait WITH (security_invoker = true) AS
SELECT ap.id AS approval_id, ap.workspace_id, ap.run_id, r.project_id, ap.action_type, ap.status,
       ap.requested_at, ap.decided_at,
       CASE WHEN ap.status IN ('APPROVED','REJECTED') AND ap.decided_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM ap.decided_at - ap.requested_at) END AS wait_seconds
  FROM approvals ap
  JOIN runs r ON r.id = ap.run_id AND r.workspace_id = ap.workspace_id;

-- code_intelligence: context_packs (+ task_attempts, agent_activity_events) — размер, stale, полезность (FR-CI-08)
CREATE VIEW v_context_usefulness WITH (security_invoker = true) AS
SELECT cp.id AS context_pack_id, cp.workspace_id, t.run_id, r.project_id, cp.task_attempt_id, a.task_id,
       a.status AS attempt_status, cp.created_at, cp.size_bytes,
       jsonb_array_length(COALESCE(cp.manifest->'fragments', '[]'::jsonb)) AS fragments,
       (SELECT count(*) FROM jsonb_array_elements(COALESCE(cp.manifest->'fragments', '[]'::jsonb)) f
         WHERE f->>'stale' = 'true') AS stale_fragments,
       ARRAY(SELECT DISTINCT f->>'path' FROM jsonb_array_elements(COALESCE(cp.manifest->'fragments', '[]'::jsonb)) f
              WHERE f->>'path' IS NOT NULL) AS fragment_paths,
       ARRAY(SELECT DISTINCT fl FROM agent_activity_events e,
                    jsonb_array_elements(COALESCE(e.payload->'claims', '[]'::jsonb)) c,
                    jsonb_array_elements_text(COALESCE(c->'files', '[]'::jsonb)) fl
              WHERE e.workspace_id = cp.workspace_id AND e.task_id = a.task_id
                AND e.event_type = 'agent.result_processed'
                AND e.payload->>'task_attempt_id' = CAST(a.id AS text)) AS claimed_files
  FROM context_packs cp
  JOIN task_attempts a ON a.id = cp.task_attempt_id AND a.workspace_id = cp.workspace_id
  JOIN tasks t ON t.id = a.task_id AND t.workspace_id = a.workspace_id
  JOIN runs r ON r.id = t.run_id AND r.workspace_id = t.workspace_id;

REVOKE ALL ON v_run_outcomes, v_task_latency, v_attempt_outcomes, v_gate_outcomes, v_cli_usage, v_review_loops,
              v_approval_wait, v_context_usefulness FROM PUBLIC;
GRANT SELECT ON v_run_outcomes, v_task_latency, v_attempt_outcomes, v_gate_outcomes, v_cli_usage, v_review_loops,
                v_approval_wait, v_context_usefulness TO aicd_app;
"""

DOWNGRADE = "DROP VIEW IF EXISTS " + ", ".join(reversed(VIEWS)) + ";"


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
