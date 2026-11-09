"""UI redesign B5 (UI_REDESIGN_PLAN §3.5, §3.10 G-11; ADR-0015): inline-комментарии Code Review Workspace.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-09

Модель (ADR-0015 §1):

* `review_threads` (T) — тред, привязанный к репозиторию + commit (`commit_sha`, для стороны `base` — также
  `base_commit_sha`), стороне diff (`base`/`head`), пути файла и диапазону строк; опционально — run, задача run
  (составной FK `tasks(workspace_id, run_id, id)`), находка. Якорь неизменяем (триггер); статус
  `OPEN ⇄ RESOLVED` — FSM `allowed_transitions('review_thread')`; `resolved_by` — только пользователь сессии;
  `version` — ETag (увеличивается триггером при каждом UPDATE);
* `review_comments` (T) — комментарии треда; автор — только пользователь сессии (`author_id = app_current_user()`),
  тело правит только автор; прежнее тело при каждой правке копируется триггером в
  `review_comment_revisions` (T, append-only), `revision` растёт на 1 (ETag комментария);
* `review_submissions` (T, append-only) — отправленное ревью run: `APPROVE` (решение существующего approval —
  `approval_id`), `REQUEST_CHANGES` (поручение `CHANGE_REQUEST` — `directive_id`), `COMMENT`;
  `review_submission_threads` (T, append-only) — треды, включённые в ревью.

Удаление строк не предусмотрено (DELETE нет ни у одной прикладной роли): треды и ревью — деловые записи, как
findings/approvals (ADR-0015 §7). SSE: тип `review.comment` добавлен в домен `sse_event_type_persisted_t`
(проекция outbox → `run_event_log` только для тредов с `run_id`, EVENT_SCHEMAS §2.2).

Резолверы tenant (§19.5, как `sys_resolve_finding_workspace`): `sys_resolve_review_thread_workspace`,
`sys_resolve_review_comment_workspace` — workspace ресурса, только если текущий пользователь — член (иначе NULL → 404).

Права: `aicd_app` — SELECT/INSERT и UPDATE лишь изменяемых колонок; `aicd_worker` — без прав (комментарии агентам не
доставляются, ADR-0015 §4); `aicd_maintenance` — SELECT (pg_dump, 0016). Миграция — только SQL через op.execute
(offline `alembic upgrade --sql`). Обратимость: downgrade удаляет таблицы, функции, строки FSM и возвращает CHECK
`allowed_transitions` и домен SSE к виду 0008/0001; при наличии событий `review.comment` в `run_event_log`
(append-only) downgrade прерывается.
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

SSE_TYPES_V1 = (
    "'run.state_changed','task.state_changed','agent.message','cli.output','directive.impact_ready',"
    "'directive.state_changed','approval.requested','approval.decided','gate.completed','run.completed','chat.message'"
)
ENTITIES_V8 = "'run','task','execution','directive','approval','external_action','indexing_job','finding'"

TABLES = r"""
-- ADR-0015 §1: тред inline-комментариев (T). Якорь: репозиторий + commit + сторона diff + путь + диапазон строк.
CREATE TABLE review_threads (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  repository_id    uuid        NOT NULL,
  run_id           uuid,
  task_id          uuid,
  finding_id       uuid,
  commit_sha       git_sha     NOT NULL,       -- head-commit просматриваемого diff (сторона head) / commit файла
  base_commit_sha  git_sha,                    -- база diff; обязательна для стороны base
  side             text        NOT NULL DEFAULT 'head' CHECK (side IN ('base','head')),
  file_path        text        NOT NULL,
  start_line       int         NOT NULL CHECK (start_line >= 1),
  end_line         int         NOT NULL,
  status           text        NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED')),
  resolved_by      uuid,
  resolved_at      timestamptz,
  created_by       uuid        NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  version          bigint      NOT NULL DEFAULT 1 CHECK (version >= 1),
  CONSTRAINT pk_review_threads PRIMARY KEY (id),
  CONSTRAINT uq_review_threads_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_review_threads_repository FOREIGN KEY (workspace_id, repository_id)
      REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_threads_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_threads_task FOREIGN KEY (workspace_id, run_id, task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,           -- задача только из того же run
  CONSTRAINT fk_review_threads_finding FOREIGN KEY (workspace_id, finding_id)
      REFERENCES findings(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_threads_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_threads_resolved_by FOREIGN KEY (resolved_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_review_threads_path CHECK (
      length(file_path) BETWEEN 1 AND 1024 AND file_path !~ '[[:cntrl:]\\]'
      AND file_path !~ '(^/|(^|/)\.\.?(/|$)|//|/$)'),
  CONSTRAINT ck_review_threads_lines CHECK (end_line >= start_line AND end_line - start_line < 1000),
  CONSTRAINT ck_review_threads_task_run CHECK (task_id IS NULL OR run_id IS NOT NULL),
  CONSTRAINT ck_review_threads_base_side CHECK (side = 'head' OR base_commit_sha IS NOT NULL),
  CONSTRAINT ck_review_threads_resolved CHECK (
      (status = 'RESOLVED') = (resolved_at IS NOT NULL) AND (resolved_at IS NULL) = (resolved_by IS NULL))
);
CREATE INDEX ix_review_threads_repo_file ON review_threads (workspace_id, repository_id, file_path, created_at, id);
CREATE INDEX ix_review_threads_run ON review_threads (workspace_id, run_id, created_at, id) WHERE run_id IS NOT NULL;
CREATE INDEX ix_review_threads_task ON review_threads (workspace_id, task_id) WHERE task_id IS NOT NULL;
CREATE INDEX ix_review_threads_finding ON review_threads (finding_id) WHERE finding_id IS NOT NULL;

-- ADR-0015 §1: комментарии треда (T). Автор — только пользователь; агенты комментариев не пишут.
CREATE TABLE review_comments (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  thread_id     uuid        NOT NULL,
  author_id     uuid        NOT NULL,
  body          text        NOT NULL CHECK (length(body) BETWEEN 1 AND 8000),
  revision      int         NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  edited_at     timestamptz,
  CONSTRAINT pk_review_comments PRIMARY KEY (id),
  CONSTRAINT uq_review_comments_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_review_comments_thread FOREIGN KEY (workspace_id, thread_id)
      REFERENCES review_threads(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_comments_author FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_review_comments_edited CHECK ((revision = 1) = (edited_at IS NULL))
);
CREATE INDEX ix_review_comments_thread ON review_comments (workspace_id, thread_id, created_at, id);

-- ADR-0015 §1: история правок (T, append-only): прежнее тело комментария на каждую правку.
CREATE TABLE review_comment_revisions (
  workspace_id  uuid        NOT NULL,
  comment_id    uuid        NOT NULL,
  revision      int         NOT NULL CHECK (revision >= 1),
  body          text        NOT NULL CHECK (length(body) BETWEEN 1 AND 8000),
  authored_at   timestamptz NOT NULL,          -- когда это тело было записано (created_at / прежний edited_at)
  superseded_at timestamptz NOT NULL DEFAULT now(),
  superseded_by uuid        NOT NULL,          -- пользователь, внёсший правку (= автор)
  CONSTRAINT pk_review_comment_revisions PRIMARY KEY (comment_id, revision),
  CONSTRAINT fk_review_comment_revisions_comment FOREIGN KEY (workspace_id, comment_id)
      REFERENCES review_comments(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_comment_revisions_user FOREIGN KEY (superseded_by) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE INDEX ix_review_comment_revisions_ws ON review_comment_revisions (workspace_id, comment_id);

-- ADR-0015 §3: отправленное ревью run (T, append-only). Воздействие — только через approval/поручение.
CREATE TABLE review_submissions (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  run_id        uuid        NOT NULL,
  task_id       uuid,
  verdict       text        NOT NULL CHECK (verdict IN ('APPROVE','REQUEST_CHANGES','COMMENT')),
  body          text        CHECK (length(body) BETWEEN 1 AND 8000),
  approval_id   uuid,
  directive_id  uuid,
  submitted_by  uuid        NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_review_submissions PRIMARY KEY (id),
  CONSTRAINT uq_review_submissions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_review_submissions_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_submissions_task FOREIGN KEY (workspace_id, run_id, task_id)
      REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_submissions_approval FOREIGN KEY (workspace_id, approval_id)
      REFERENCES approvals(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_submissions_directive FOREIGN KEY (workspace_id, directive_id)
      REFERENCES user_directives(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_submissions_user FOREIGN KEY (submitted_by) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_review_submissions_effect CHECK (
      (verdict = 'APPROVE' AND approval_id IS NOT NULL AND directive_id IS NULL) OR
      (verdict = 'REQUEST_CHANGES' AND directive_id IS NOT NULL AND approval_id IS NULL) OR
      (verdict = 'COMMENT' AND approval_id IS NULL AND directive_id IS NULL))
);
CREATE INDEX ix_review_submissions_run ON review_submissions (workspace_id, run_id, created_at, id);
CREATE INDEX ix_review_submissions_directive ON review_submissions (directive_id) WHERE directive_id IS NOT NULL;
CREATE INDEX ix_review_submissions_approval ON review_submissions (approval_id) WHERE approval_id IS NOT NULL;

CREATE TABLE review_submission_threads (
  workspace_id  uuid NOT NULL,
  review_id     uuid NOT NULL,
  thread_id     uuid NOT NULL,
  CONSTRAINT pk_review_submission_threads PRIMARY KEY (review_id, thread_id),
  CONSTRAINT fk_review_submission_threads_review FOREIGN KEY (workspace_id, review_id)
      REFERENCES review_submissions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_review_submission_threads_thread FOREIGN KEY (workspace_id, thread_id)
      REFERENCES review_threads(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_review_submission_threads_thread ON review_submission_threads (workspace_id, thread_id);
"""

FSM = rf"""
-- ADR-0015 §1: FSM треда (DATABASE_DESIGN §18.3): OPEN ⇄ RESOLVED.
ALTER TABLE allowed_transitions DROP CONSTRAINT ck_allowed_transitions_entity;
ALTER TABLE allowed_transitions ADD CONSTRAINT ck_allowed_transitions_entity
  CHECK (entity IN ({ENTITIES_V8},'review_thread'));
INSERT INTO allowed_transitions (entity, from_status, to_status) VALUES
  ('review_thread','__new__','OPEN'), ('review_thread','OPEN','RESOLVED'), ('review_thread','RESOLVED','OPEN');
CREATE TRIGGER trg_review_threads_transition BEFORE INSERT OR UPDATE OF status ON review_threads
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('review_thread');

-- EVENT_SCHEMAS §2.2/§3: SSE-событие review.comment (проекция в run_event_log для тредов с run_id).
ALTER DOMAIN sse_event_type_persisted_t DROP CONSTRAINT sse_event_type_persisted_t_check;
ALTER DOMAIN sse_event_type_persisted_t ADD CONSTRAINT sse_event_type_persisted_t_check
  CHECK (VALUE IN ({SSE_TYPES_V1},'review.comment'));
"""

TRIGGERS = r"""
CREATE TRIGGER trg_review_threads_immutable_ws BEFORE UPDATE OF workspace_id ON review_threads
  FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();
CREATE TRIGGER trg_review_comments_immutable_ws BEFORE UPDATE OF workspace_id ON review_comments
  FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace();

-- ADR-0015 §1–2: автор/решивший — только пользователь сессии; якорь неизменяем; run ↔ репозиторий,
-- находка ↔ run согласованы; version растёт на каждый UPDATE (ETag).
CREATE FUNCTION trg_fn_review_threads_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_user uuid := app_current_user();
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.created_by IS DISTINCT FROM v_user THEN
      RAISE EXCEPTION 'review thread must be created by the session user' USING ERRCODE = '42501';
    END IF;
    IF NEW.run_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM runs r WHERE r.workspace_id = NEW.workspace_id AND r.id = NEW.run_id
            AND r.repository_id = NEW.repository_id) THEN
      RAISE EXCEPTION 'review thread run % is not bound to repository %', NEW.run_id, NEW.repository_id
        USING ERRCODE = '23514';
    END IF;
    IF NEW.finding_id IS NOT NULL AND NEW.run_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM findings f WHERE f.workspace_id = NEW.workspace_id AND f.id = NEW.finding_id
            AND f.run_id = NEW.run_id) THEN
      RAISE EXCEPTION 'finding % does not belong to run %', NEW.finding_id, NEW.run_id USING ERRCODE = '23514';
    END IF;
    NEW.version := 1;
    RETURN NEW;
  END IF;
  IF (NEW.repository_id, NEW.run_id, NEW.task_id, NEW.finding_id, NEW.commit_sha, NEW.base_commit_sha, NEW.side,
      NEW.file_path, NEW.start_line, NEW.end_line, NEW.created_by, NEW.created_at)
     IS DISTINCT FROM
     (OLD.repository_id, OLD.run_id, OLD.task_id, OLD.finding_id, OLD.commit_sha, OLD.base_commit_sha, OLD.side,
      OLD.file_path, OLD.start_line, OLD.end_line, OLD.created_by, OLD.created_at) THEN
    RAISE EXCEPTION 'review thread anchor is immutable' USING ERRCODE = '42501';
  END IF;
  IF NEW.resolved_by IS NOT NULL AND NEW.resolved_by IS DISTINCT FROM OLD.resolved_by
     AND NEW.resolved_by IS DISTINCT FROM v_user THEN
    RAISE EXCEPTION 'review thread must be resolved by the session user' USING ERRCODE = '42501';
  END IF;
  NEW.version := OLD.version + 1;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER trg_review_threads_guard BEFORE INSERT OR UPDATE ON review_threads
  FOR EACH ROW EXECUTE FUNCTION trg_fn_review_threads_guard();

-- ADR-0015 §2: тело правит только автор; прежняя редакция сохраняется в review_comment_revisions.
CREATE FUNCTION trg_fn_review_comments_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_user uuid := app_current_user();
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.author_id IS DISTINCT FROM v_user THEN
      RAISE EXCEPTION 'review comment must be authored by the session user' USING ERRCODE = '42501';
    END IF;
    NEW.revision := 1;
    NEW.edited_at := NULL;
    RETURN NEW;
  END IF;
  IF (NEW.thread_id, NEW.author_id, NEW.created_at) IS DISTINCT FROM (OLD.thread_id, OLD.author_id, OLD.created_at) THEN
    RAISE EXCEPTION 'review comment identity is immutable' USING ERRCODE = '42501';
  END IF;
  IF NEW.body IS NOT DISTINCT FROM OLD.body THEN
    RAISE EXCEPTION 'review comment update must change the body' USING ERRCODE = '23514';
  END IF;
  IF OLD.author_id IS DISTINCT FROM v_user THEN
    RAISE EXCEPTION 'only the author may edit a review comment' USING ERRCODE = '42501';
  END IF;
  INSERT INTO review_comment_revisions (workspace_id, comment_id, revision, body, authored_at, superseded_by)
  VALUES (OLD.workspace_id, OLD.id, OLD.revision, OLD.body, coalesce(OLD.edited_at, OLD.created_at), v_user);
  NEW.revision := OLD.revision + 1;
  NEW.edited_at := now();
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER trg_review_comments_guard BEFORE INSERT OR UPDATE ON review_comments
  FOR EACH ROW EXECUTE FUNCTION trg_fn_review_comments_guard();
CREATE TRIGGER trg_review_comments_no_delete BEFORE DELETE ON review_comments
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
CREATE TRIGGER trg_review_threads_no_delete BEFORE DELETE ON review_threads
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
CREATE TRIGGER trg_review_comment_revisions_append_only BEFORE UPDATE OR DELETE ON review_comment_revisions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
CREATE TRIGGER trg_review_submissions_append_only BEFORE UPDATE OR DELETE ON review_submissions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();
CREATE TRIGGER trg_review_submission_threads_append_only BEFORE UPDATE OR DELETE ON review_submission_threads
  FOR EACH ROW EXECUTE FUNCTION trg_fn_append_only();

-- ADR-0015 §3: ревью отправляет пользователь сессии.
CREATE FUNCTION trg_fn_review_submissions_author() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.submitted_by IS DISTINCT FROM app_current_user() THEN
    RAISE EXCEPTION 'review must be submitted by the session user' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_review_submissions_author BEFORE INSERT ON review_submissions
  FOR EACH ROW EXECUTE FUNCTION trg_fn_review_submissions_author();
"""

RLS = r"""
-- T-таблицы (§19.3, §19.4): только api; worker комментариев не читает (ADR-0015 §4).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['review_threads','review_comments','review_comment_revisions','review_submissions',
                           'review_submission_threads']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO aicd_app '
                   'USING (workspace_id = app_current_workspace()) '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_tenant', t);
  END LOOP;
END $$;
"""

GRANTS = r"""
REVOKE ALL ON review_threads, review_comments, review_comment_revisions, review_submissions, review_submission_threads
  FROM PUBLIC;
GRANT SELECT, INSERT ON review_threads, review_comments, review_comment_revisions, review_submissions,
  review_submission_threads TO aicd_app;
GRANT UPDATE (status, resolved_by, resolved_at, updated_at, version) ON review_threads TO aicd_app;
GRANT UPDATE (body, revision, updated_at, edited_at) ON review_comments TO aicd_app;
-- backup (pg_dump, 0016) и архив: только чтение.
GRANT SELECT ON review_threads, review_comments, review_comment_revisions, review_submissions,
  review_submission_threads TO aicd_maintenance;
"""

SYSFN = r"""
-- §19.5: workspace треда/комментария, только если текущий пользователь — член этого workspace (иначе NULL → 404).
CREATE FUNCTION sys_resolve_review_thread_workspace(p_thread_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT t.workspace_id
    FROM aicd.review_threads t
    JOIN aicd.workspace_members m ON m.workspace_id = t.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE t.id = p_thread_id
$$;
CREATE FUNCTION sys_resolve_review_comment_workspace(p_comment_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
  SELECT c.workspace_id
    FROM aicd.review_comments c
    JOIN aicd.workspace_members m ON m.workspace_id = c.workspace_id AND m.user_id = aicd.app_current_user()
   WHERE c.id = p_comment_id
$$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_resolve_review_thread_workspace(uuid) OWNER TO aicd_sysfn;
ALTER FUNCTION sys_resolve_review_comment_workspace(uuid) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_resolve_review_thread_workspace(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION sys_resolve_review_comment_workspace(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_resolve_review_thread_workspace(uuid) TO aicd_app;
GRANT EXECUTE ON FUNCTION sys_resolve_review_comment_workspace(uuid) TO aicd_app;
GRANT SELECT (id, workspace_id) ON review_threads TO aicd_sysfn;
GRANT SELECT (id, workspace_id) ON review_comments TO aicd_sysfn;
"""

DOWNGRADE = rf"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM run_event_log WHERE event_type = 'review.comment') THEN
    RAISE EXCEPTION 'downgrade 0019: run_event_log contains review.comment events (append-only); archive the runs first'
      USING ERRCODE = '55000';
  END IF;
END $$;
DROP FUNCTION IF EXISTS sys_resolve_review_comment_workspace(uuid);
DROP FUNCTION IF EXISTS sys_resolve_review_thread_workspace(uuid);
DROP TABLE IF EXISTS review_submission_threads, review_submissions, review_comment_revisions, review_comments,
  review_threads CASCADE;
DROP FUNCTION IF EXISTS trg_fn_review_submissions_author();
DROP FUNCTION IF EXISTS trg_fn_review_comments_guard();
DROP FUNCTION IF EXISTS trg_fn_review_threads_guard();
DELETE FROM allowed_transitions WHERE entity = 'review_thread';
ALTER TABLE allowed_transitions DROP CONSTRAINT IF EXISTS ck_allowed_transitions_entity;
ALTER TABLE allowed_transitions ADD CONSTRAINT ck_allowed_transitions_entity CHECK (entity IN ({ENTITIES_V8}));
ALTER DOMAIN sse_event_type_persisted_t DROP CONSTRAINT sse_event_type_persisted_t_check;
ALTER DOMAIN sse_event_type_persisted_t ADD CONSTRAINT sse_event_type_persisted_t_check CHECK (VALUE IN ({SSE_TYPES_V1}));
"""


def upgrade() -> None:
    for block in (TABLES, FSM, TRIGGERS, RLS, GRANTS, SYSFN):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
