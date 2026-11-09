"""Жизненный цикл findings, вердикт человека по контролю, блокирующие контроли (Phase 7, OQ-43 (c), (d)).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-08

Дополнения QUALITY_GATES.md §9, принятые в DATABASE_DESIGN §12.6, §13.3 (OQ-43 (c)):
  * `findings.control_refs text[]` — связь находки с контролями (`standard_controls.control_code`);
    Compliance Engine считает такую находку относящейся к контролю (QUALITY_GATES §8.2 «FAIL»);
  * `findings.version` — сильный ETag находки для `POST /findings/{id}/transitions` (If-Match, API_DESIGN §9);
    увеличивается триггером при каждом UPDATE (находки меняют и worker, и api — счётчик не доверен коду);
  * `project_controls.blocking boolean` — контроль блокирует завершение run при `compliance_results = FAIL`
    (QUALITY_GATES §5.3 п.6). NULL — по умолчанию платформы `control_default_blocking(profile, role)`:
    контроли роли `security` блокируют в профилях `strict`/`critical`; явное значение утверждает admin
    (триггер утверждения §13.3 теперь охраняет и `blocking`).

Вердикт человека по контролю (OQ-43 (d), `POST /runs/{run_id}/compliance/{control_id}/review`): у aicd_app нет
INSERT на compliance_results — узкая SECURITY DEFINER функция `sys_record_compliance_review` (§19.5): только
текущий workspace и член с эффективной ролью проекта ≥ approval_min_role('MANUAL_REVIEW') (= reviewer);
вердикт PASS/FAIL с обоснованием и evidence этого run; прежняя актуальная строка получает `superseded_at`;
evidence связывается с новым вердиктом через `evidence_links`. `sys_resolve_finding_workspace` — резолвер
`GET /findings/{id}` для 404 на чужие находки (как `sys_resolve_evidence_workspace`, 0008).

Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).
Обратимость: downgrade удаляет функции, колонки (значения теряются), индекс и выданные aicd_sysfn права,
возвращает триггер утверждения project_controls к виду 0008.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

COLUMNS = r"""
-- §12.6: связь находки с контролями (QUALITY_GATES §9) и версия для ETag/If-Match (API_DESIGN §9).
ALTER TABLE findings
  ADD COLUMN control_refs text[] NOT NULL DEFAULT '{}'
    CONSTRAINT ck_findings_control_refs CHECK (
      cardinality(control_refs) <= 50 AND array_position(control_refs, NULL) IS NULL
      AND array_to_string(control_refs, ',') ~ '^([A-Za-z0-9._-]{1,64}(,[A-Za-z0-9._-]{1,64})*)?$'),
  ADD COLUMN version bigint NOT NULL DEFAULT 1 CONSTRAINT ck_findings_version CHECK (version >= 1);
CREATE INDEX ix_findings_control_refs ON findings USING gin (control_refs) WHERE cardinality(control_refs) > 0;

CREATE FUNCTION trg_fn_findings_bump_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.version := OLD.version + 1;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_findings_bump_version BEFORE UPDATE ON findings
  FOR EACH ROW EXECUTE FUNCTION trg_fn_findings_bump_version();

-- §13.3: блокирующий контроль (QUALITY_GATES §5.3 п.6). NULL — по умолчанию платформы.
ALTER TABLE project_controls ADD COLUMN blocking boolean;

-- Умолчание платформы для project_controls.blocking IS NULL: контроли безопасности (роль security)
-- блокируют завершение run в профилях strict/critical. Одна формула для guard завершения и API.
CREATE FUNCTION control_default_blocking(p_quality_profile text, p_agent_role text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(p_quality_profile IN ('strict','critical') AND p_agent_role = 'security', false)
$$;
GRANT EXECUTE ON FUNCTION control_default_blocking(text, text) TO aicd_app, aicd_worker, aicd_maintenance;

-- §13.3: утверждение применимости (и теперь blocking) — только пользователь сессии.
CREATE OR REPLACE FUNCTION trg_fn_project_controls_approval() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('UPDATE','DELETE') AND OLD.approved_by IS NOT NULL AND app_current_user() IS NULL THEN
    RAISE EXCEPTION 'approved applicability of control % can be changed only by a user', OLD.control_id
      USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  IF NEW.approved_by IS NOT NULL
     AND (TG_OP = 'INSERT' OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
          OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
          OR NEW.applicability IS DISTINCT FROM OLD.applicability
          OR NEW.rationale IS DISTINCT FROM OLD.rationale
          OR NEW.agent_role IS DISTINCT FROM OLD.agent_role
          OR NEW.blocking IS DISTINCT FROM OLD.blocking)
     AND NEW.approved_by IS DISTINCT FROM app_current_user() THEN
    RAISE EXCEPTION 'applicability of control % must be approved by the session user', NEW.control_id
      USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
"""

SYSFN = r"""
-- §19.5: workspace находки, только если текущий пользователь — член этого workspace (GET /findings/{id}).
CREATE FUNCTION sys_resolve_finding_workspace(p_finding_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT f.workspace_id
    FROM aicd.findings f
    JOIN aicd.workspace_members m ON m.workspace_id = f.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE f.id = p_finding_id
$$;

-- §19.5: POST /runs/{run_id}/compliance/{control_id}/review — вердикт человека (QUALITY_GATES §8.2) без
-- INSERT на compliance_results у aicd_app. Run и контроль ищутся только в app_current_workspace();
-- вызывающий — член workspace с эффективной ролью проекта ≥ approval_min_role('MANUAL_REVIEW') (reviewer).
-- Разрешено, если применимость контроля утверждена человеком и он применим, run не терминален, а текущий
-- вердикт открыт для человека: UNVERIFIED (в т.ч. автоматического контроля — переопределение с
-- обоснованием), MANUAL_REVIEW (не automated_gate), прежний вердикт человека или вердикта ещё нет.
-- p_evidence_ids: первый — evidence вердикта (для PASS — VERIFIED, проверяет trg_compliance_results_guard),
-- все — evidence этого run, связываются с вердиктом (supports для PASS, refutes для FAIL).
-- Результат: id новой строки; NULL — run/контроль не найден или вызывающий не член (→ 404);
-- 42501 — роли недостаточно (→ 403); 55000 — состояние не допускает вердикт (→ 409);
-- 22023 — неверные аргументы, 23514 — evidence не этого run / не VERIFIED для PASS (→ 422).
CREATE FUNCTION sys_record_compliance_review(p_run_id uuid, p_control_id uuid, p_status text, p_notes text,
                                             p_evidence_ids uuid[])
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
DECLARE
  v_ws          uuid := aicd.app_current_workspace();
  v_user        uuid := aicd.app_current_user();
  v_project     uuid;
  v_run_status  text;
  v_role        text;
  v_method      text;
  v_applic      text;
  v_approved    uuid;
  v_cur_id      uuid;
  v_cur_status  text;
  v_cur_review  uuid;
  v_id          uuid;
  v_ev          uuid;
BEGIN
  IF p_run_id IS NULL OR p_control_id IS NULL OR p_status IS NULL OR p_status NOT IN ('PASS','FAIL')
     OR p_notes IS NULL OR length(btrim(p_notes)) = 0 OR length(p_notes) > 4000
     OR p_evidence_ids IS NULL OR cardinality(p_evidence_ids) NOT BETWEEN 1 AND 20
     OR array_position(p_evidence_ids, NULL) IS NOT NULL
     OR (SELECT count(DISTINCT x) FROM unnest(p_evidence_ids) AS x) <> cardinality(p_evidence_ids) THEN
    RAISE EXCEPTION 'sys_record_compliance_review: invalid arguments' USING ERRCODE = '22023';
  END IF;
  IF v_ws IS NULL OR v_user IS NULL THEN
    RETURN NULL;
  END IF;
  SELECT r.project_id, r.status::text INTO v_project, v_run_status
    FROM aicd.runs r WHERE r.id = p_run_id AND r.workspace_id = v_ws;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  SELECT coalesce(o.role, m.role)::text INTO v_role
    FROM aicd.workspace_members m
    LEFT JOIN aicd.project_member_overrides o ON o.project_id = v_project AND o.user_id = m.user_id
   WHERE m.workspace_id = v_ws AND m.user_id = v_user;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  SELECT c.verification_method INTO v_method FROM aicd.standard_controls c WHERE c.id = p_control_id AND c.active;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  IF aicd.workspace_role_rank(v_role) < aicd.workspace_role_rank(aicd.approval_min_role('MANUAL_REVIEW')) THEN
    RAISE EXCEPTION 'role % may not record compliance verdicts', v_role USING ERRCODE = '42501';
  END IF;
  IF v_run_status IN ('COMPLETED','FAILED','CANCELLED') THEN
    RAISE EXCEPTION 'run % is %', p_run_id, v_run_status USING ERRCODE = '55000';
  END IF;
  SELECT pc.applicability, pc.approved_by INTO v_applic, v_approved
    FROM aicd.project_controls pc WHERE pc.project_id = v_project AND pc.control_id = p_control_id;
  IF v_approved IS NULL OR v_applic = 'not_applicable' THEN
    RAISE EXCEPTION 'control % is not approved as applicable for the project', p_control_id USING ERRCODE = '55000';
  END IF;
  SELECT cr.id, cr.status::text, cr.reviewed_by INTO v_cur_id, v_cur_status, v_cur_review
    FROM aicd.compliance_results cr
   WHERE cr.run_id = p_run_id AND cr.control_id = p_control_id AND cr.superseded_at IS NULL
   FOR UPDATE;
  IF v_cur_id IS NOT NULL AND v_cur_review IS NULL
     AND NOT (v_cur_status = 'UNVERIFIED' OR (v_cur_status = 'MANUAL_REVIEW' AND v_method <> 'automated_gate')) THEN
    RAISE EXCEPTION 'verdict % of control % is not open for human review', v_cur_status, p_control_id
      USING ERRCODE = '55000';
  END IF;
  IF EXISTS (SELECT 1 FROM unnest(p_evidence_ids) AS x
              WHERE NOT EXISTS (SELECT 1 FROM aicd.evidence e
                                 WHERE e.id = x AND e.workspace_id = v_ws AND e.run_id = p_run_id)) THEN
    RAISE EXCEPTION 'evidence must belong to run %', p_run_id USING ERRCODE = '23514';
  END IF;
  IF v_cur_id IS NOT NULL THEN
    UPDATE aicd.compliance_results SET superseded_at = now() WHERE id = v_cur_id;
  END IF;
  -- UUIDv7 (PostgreSQL 17 не умеет uuidv7()): 48 бит unix-ms поверх случайного v4, версия 0100 → 0111.
  v_id := encode(set_bit(set_bit(overlay(uuid_send(gen_random_uuid())
            PLACING substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
            FROM 1 FOR 6), 52, 1), 53, 1), 'hex')::uuid;
  INSERT INTO aicd.compliance_results (id, workspace_id, run_id, control_id, status, evidence_id, reviewed_by, notes)
  VALUES (v_id, v_ws, p_run_id, p_control_id, p_status::aicd.compliance_status_t, p_evidence_ids[1], v_user, p_notes);
  FOREACH v_ev IN ARRAY p_evidence_ids LOOP
    INSERT INTO aicd.evidence_links (id, workspace_id, evidence_id, target_type, target_id, relation)
    VALUES (encode(set_bit(set_bit(overlay(uuid_send(gen_random_uuid())
              PLACING substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
              FROM 1 FOR 6), 52, 1), 53, 1), 'hex')::uuid,
            v_ws, v_ev, 'compliance_result', v_id, CASE p_status WHEN 'PASS' THEN 'supports' ELSE 'refutes' END);
  END LOOP;
  RETURN v_id;
END $$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_resolve_finding_workspace(uuid) OWNER TO aicd_sysfn;
ALTER FUNCTION sys_record_compliance_review(uuid, uuid, text, text, uuid[]) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_resolve_finding_workspace(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION sys_record_compliance_review(uuid, uuid, text, text, uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_resolve_finding_workspace(uuid) TO aicd_app;
GRANT EXECUTE ON FUNCTION sys_record_compliance_review(uuid, uuid, text, text, uuid[]) TO aicd_app;
-- Только колонки, которых у aicd_sysfn ещё не было (downgrade отзывает ровно их). Триггеры compliance_results
-- и evidence_links исполняются от имени владельца функции и читают evidence/compliance_results.
GRANT SELECT (id, workspace_id) ON findings TO aicd_sysfn;
GRANT SELECT (project_id) ON runs TO aicd_sysfn;
GRANT SELECT (id, verification_method, active) ON standard_controls TO aicd_sysfn;
GRANT SELECT (project_id, control_id, applicability, approved_by) ON project_controls TO aicd_sysfn;
GRANT SELECT (id, workspace_id, run_id, control_id, status, reviewed_by, superseded_at) ON compliance_results TO aicd_sysfn;
GRANT INSERT (id, workspace_id, run_id, control_id, status, evidence_id, reviewed_by, notes) ON compliance_results TO aicd_sysfn;
GRANT UPDATE (superseded_at) ON compliance_results TO aicd_sysfn;
GRANT SELECT (run_id, verification_status) ON evidence TO aicd_sysfn;
GRANT INSERT (id, workspace_id, evidence_id, target_type, target_id, relation) ON evidence_links TO aicd_sysfn;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_record_compliance_review(uuid, uuid, text, text, uuid[]);
DROP FUNCTION IF EXISTS sys_resolve_finding_workspace(uuid);
REVOKE INSERT (id, workspace_id, evidence_id, target_type, target_id, relation) ON evidence_links FROM aicd_sysfn;
REVOKE SELECT (run_id, verification_status) ON evidence FROM aicd_sysfn;
REVOKE UPDATE (superseded_at) ON compliance_results FROM aicd_sysfn;
REVOKE INSERT (id, workspace_id, run_id, control_id, status, evidence_id, reviewed_by, notes) ON compliance_results FROM aicd_sysfn;
REVOKE SELECT (id, workspace_id, run_id, control_id, status, reviewed_by, superseded_at) ON compliance_results FROM aicd_sysfn;
REVOKE SELECT (project_id, control_id, applicability, approved_by) ON project_controls FROM aicd_sysfn;
REVOKE SELECT (id, verification_method, active) ON standard_controls FROM aicd_sysfn;
REVOKE SELECT (project_id) ON runs FROM aicd_sysfn;
REVOKE SELECT (id, workspace_id) ON findings FROM aicd_sysfn;

CREATE OR REPLACE FUNCTION trg_fn_project_controls_approval() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP IN ('UPDATE','DELETE') AND OLD.approved_by IS NOT NULL AND app_current_user() IS NULL THEN
    RAISE EXCEPTION 'approved applicability of control % can be changed only by a user', OLD.control_id
      USING ERRCODE = '42501';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  IF NEW.approved_by IS NOT NULL
     AND (TG_OP = 'INSERT' OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
          OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
          OR NEW.applicability IS DISTINCT FROM OLD.applicability
          OR NEW.rationale IS DISTINCT FROM OLD.rationale
          OR NEW.agent_role IS DISTINCT FROM OLD.agent_role)
     AND NEW.approved_by IS DISTINCT FROM app_current_user() THEN
    RAISE EXCEPTION 'applicability of control % must be approved by the session user', NEW.control_id
      USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
DROP FUNCTION IF EXISTS control_default_blocking(text, text);
ALTER TABLE project_controls DROP COLUMN IF EXISTS blocking;

DROP TRIGGER IF EXISTS trg_findings_bump_version ON findings;
DROP FUNCTION IF EXISTS trg_fn_findings_bump_version();
DROP INDEX IF EXISTS ix_findings_control_refs;
ALTER TABLE findings DROP COLUMN IF EXISTS version, DROP COLUMN IF EXISTS control_refs;
"""


def upgrade() -> None:
    for block in (COLUMNS, SYSFN):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
