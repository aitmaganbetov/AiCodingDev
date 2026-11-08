"""Code Intelligence, документация и стек (P5-01): индекс по commit, поиск, context packs, источники документации.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08

Источник DDL: docs/database/DATABASE_DESIGN.md §6.5–6.7 (technologies, technology_versions,
project_technologies), §10.1–10.9 (code_index_snapshots, code_files, code_symbols, code_dependencies,
knowledge_chunks, retrieval_queries, retrieval_results, context_packs, indexing_jobs), §11.1–11.5
(knowledge_entries, architecture_decisions, documentation_sources, documentation_snapshots,
documentation_sections), §18.3 (переходы indexing_job), §18.11 (полиморфная ссылка knowledge_chunks),
§19.3–19.4 (RLS, классы T/G). CREATE TABLE перенесены из документа без изменений, кроме:
  * порядок создания: документация раньше knowledge_chunks (FK fk_knowledge_chunks_doc_snapshot);
  * индексы ссылающихся колонок для RESTRICT/CASCADE при retention снимков (§20.3):
    ix_context_packs_snapshot, ix_knowledge_chunks_doc_snapshot;
  * §18.11: проверка источника knowledge_chunks — статический CASE по source_type (а не динамический
    EXECUTE): цель в том же workspace и, для code_*/documentation_section, в том же снимке;
  * allowed_transitions: CHECK entity (безымянный allowed_transitions_entity_check из 0002) заменён на
    ck_allowed_transitions_entity с 'indexing_job'; строки Indexing Job FSM — §18.3 документа
    (дополнены этой миграцией: в §10.9 FSM не было).
Документационная политика OFF/SMART/STRICT уже хранится в project_settings.documentation_mode (0001) и
снимках runs/retrieval_queries/context_packs; policies(policy_type='documentation') — 0003. Здесь не дублируется.
FK других фаз на эти таблицы миграции 0001–0006 не откладывали (evidence_links/architecture_decision —
Phase 6, P6-01). Seed technologies не выполняется: §6.5 его не задаёт (OQ-DB-15: справочник ведут
миграции/admin CLI; определение стека P5-07 регистрирует версии через aicd_worker).
Классы (§19.4): technologies, technology_versions — G (SELECT app/worker, INSERT только worker, без
UPDATE/DELETE); остальные 15 таблиц — T (RLS ENABLE+FORCE, политика p_<t>_tenant для app/worker).
sys_plan_docs_download(p_source_id uuid, p_idempotency_key text, p_request jsonb) RETURNS uuid —
SECURITY DEFINER (владелец aicd_sysfn, EXECUTE только aicd_app): планирует загрузку документации
(external_actions PLANNED, 'docs_download', run_id NULL) для POST /documentation/sources/{id}/refresh;
INSERT на external_actions у aicd_app нет (дополнение к §19.5).
Миграция — только SQL через op.execute (работает в offline-режиме `alembic upgrade --sql`).

Обратимость: downgrade удаляет таблицы, функции (включая sys_plan_docs_download и выданные aicd_sysfn права), строки indexing_job в allowed_transitions и возвращает
CHECK entity к виду 0002.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TABLES = r"""
-- §6.5 technologies (G)
CREATE TABLE technologies (
  id          uuid7       NOT NULL,
  name        text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  ecosystem   text        NOT NULL CHECK (ecosystem ~ '^[a-z0-9][a-z0-9_.+-]{0,63}$'),  -- python, npm, php, go, docker, postgresql…
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_technologies PRIMARY KEY (id),
  CONSTRAINT uq_technologies_ecosystem_name UNIQUE (ecosystem, name)
);

-- §6.6 technology_versions (G)
CREATE TABLE technology_versions (
  id              uuid7 NOT NULL,
  technology_id   uuid  NOT NULL,
  version_label   text  NOT NULL CHECK (length(version_label) BETWEEN 1 AND 100),
  support_status  text  NOT NULL DEFAULT 'unknown'
                        CHECK (support_status IN ('supported','lts','deprecated','eol','unknown')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_technology_versions PRIMARY KEY (id),
  CONSTRAINT uq_technology_versions UNIQUE (technology_id, version_label),
  CONSTRAINT fk_technology_versions_technology FOREIGN KEY (technology_id) REFERENCES technologies(id) ON DELETE RESTRICT
);

-- §6.7 project_technologies (T)
CREATE TABLE project_technologies (
  workspace_id           uuid        NOT NULL,
  project_id             uuid        NOT NULL,
  technology_version_id  uuid        NOT NULL,
  detected_from          text        NOT NULL CHECK (length(detected_from) <= 1024),  -- манифест/файл (относительный путь) или 'manual'
  detected_commit_sha    git_sha,
  detected_at            timestamptz NOT NULL DEFAULT now(),
  confirmed_by           uuid,
  confirmed_at           timestamptz,
  CONSTRAINT pk_project_technologies PRIMARY KEY (project_id, technology_version_id),
  CONSTRAINT fk_project_technologies_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_project_technologies_tv FOREIGN KEY (technology_version_id) REFERENCES technology_versions(id) ON DELETE RESTRICT,
  CONSTRAINT fk_project_technologies_confirmed_by FOREIGN KEY (confirmed_by) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT ck_project_technologies_confirm CHECK ((confirmed_by IS NULL) = (confirmed_at IS NULL))
);
CREATE INDEX ix_project_technologies_ws_project ON project_technologies (workspace_id, project_id);

-- §11.3 documentation_sources (T)
CREATE TABLE documentation_sources (
  id                     uuid7       NOT NULL,
  workspace_id           uuid        NOT NULL,
  technology_version_id  uuid,
  source_type            text        NOT NULL CHECK (source_type IN ('url','pdf','markdown','html','git')),
  uri_or_file_ref        text        NOT NULL CHECK (length(uri_or_file_ref) BETWEEN 1 AND 2048),
  trust_level            text        NOT NULL DEFAULT 'untrusted'
                                     CHECK (trust_level IN ('official','vendor','community','internal','untrusted')),
  license_info           text        CHECK (length(license_info) <= 2000),
  refresh_policy         text        NOT NULL DEFAULT 'manual' CHECK (refresh_policy IN ('manual','daily','weekly','monthly')),
  enabled                boolean     NOT NULL DEFAULT true,
  approval_id            uuid,       -- DOCS_DOWNLOAD, если требуется политикой SMART
  created_by             uuid        NOT NULL,
  last_refreshed_at      timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_documentation_sources PRIMARY KEY (id),
  CONSTRAINT uq_documentation_sources_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_documentation_sources_uri UNIQUE (workspace_id, uri_or_file_ref),
  CONSTRAINT fk_doc_sources_tv FOREIGN KEY (technology_version_id) REFERENCES technology_versions(id) ON DELETE RESTRICT,
  CONSTRAINT fk_doc_sources_approval FOREIGN KEY (workspace_id, approval_id) REFERENCES approvals(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_doc_sources_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT,
  -- Только https:// (без http, file://, gopher://) — первый рубеж SSRF-защиты (ТЗ §15).
  CONSTRAINT ck_doc_sources_uri CHECK (
      (source_type IN ('url','html') AND uri_or_file_ref ~ '^https://') OR
      (source_type = 'git' AND uri_or_file_ref ~ '^https://') OR
      (source_type IN ('pdf','markdown') AND (uri_or_file_ref ~ '^https://' OR uri_or_file_ref ~ '^artifact:[0-9a-f-]{36}$')))
);

-- §11.4 documentation_snapshots (T)
CREATE TABLE documentation_snapshots (
  id                  uuid7       NOT NULL,
  workspace_id        uuid        NOT NULL,
  source_id           uuid        NOT NULL,
  content_hash        sha256_hex,              -- NULL пока FETCHING
  status              text        NOT NULL DEFAULT 'FETCHING' CHECK (status IN ('FETCHING','READY','FAILED','QUARANTINED','SUPERSEDED')),
  storage_ref         uuid,                    -- artifacts.id
  size_bytes          bigint      CHECK (size_bytes >= 0),
  mime_type           text        CHECK (mime_type ~ '^[a-z]+/[a-z0-9.+-]+$'),
  external_action_id  uuid,                    -- ledger загрузки (ADR-0012)
  error               text        CHECK (length(error) <= 4000),
  fetched_at          timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_documentation_snapshots PRIMARY KEY (id),
  CONSTRAINT uq_documentation_snapshots_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_documentation_snapshots_hash UNIQUE (source_id, content_hash),
  CONSTRAINT fk_doc_snapshots_source FOREIGN KEY (workspace_id, source_id) REFERENCES documentation_sources(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_doc_snapshots_storage FOREIGN KEY (workspace_id, storage_ref) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_doc_snapshots_action FOREIGN KEY (workspace_id, external_action_id) REFERENCES external_actions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_doc_snapshots_ready CHECK (status <> 'READY' OR (storage_ref IS NOT NULL AND content_hash IS NOT NULL AND fetched_at IS NOT NULL))
);
CREATE INDEX ix_doc_snapshots_source_ready ON documentation_snapshots (source_id, fetched_at DESC) WHERE status = 'READY';

-- §11.5 documentation_sections (T)
CREATE TABLE documentation_sections (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  snapshot_id   uuid        NOT NULL,
  section_path  text        NOT NULL CHECK (length(section_path) BETWEEN 1 AND 1024),
  heading       text        CHECK (length(heading) <= 1000),
  body_ref      text        NOT NULL CHECK (body_ref ~ '^artifact:[0-9a-f-]{36}#bytes=[0-9]+-[0-9]+$'),
  content_hash  sha256_hex  NOT NULL,
  CONSTRAINT pk_documentation_sections PRIMARY KEY (id),
  CONSTRAINT uq_documentation_sections_path UNIQUE (snapshot_id, section_path),
  CONSTRAINT fk_doc_sections_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES documentation_snapshots(workspace_id, id) ON DELETE CASCADE
);

-- §11.1 knowledge_entries (T)
CREATE TABLE knowledge_entries (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  project_id    uuid        NOT NULL,
  kind          text        NOT NULL CHECK (kind IN ('note','requirement','specification','runbook','glossary','lesson')),
  title         text        NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  body_ref      uuid        NOT NULL,        -- artifacts.id
  content_hash  sha256_hex  NOT NULL,
  visibility    text        NOT NULL DEFAULT 'project' CHECK (visibility IN ('project','workspace')),
  created_by    uuid,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_knowledge_entries PRIMARY KEY (id),
  CONSTRAINT uq_knowledge_entries_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_knowledge_entries_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_knowledge_entries_body FOREIGN KEY (workspace_id, body_ref) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_knowledge_entries_created_by FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX ix_knowledge_entries_project ON knowledge_entries (workspace_id, project_id, kind);

-- §11.2 architecture_decisions (T): ADR проекта пользователя (не ADR платформы)
CREATE TABLE architecture_decisions (
  id               uuid7       NOT NULL,
  workspace_id     uuid        NOT NULL,
  project_id       uuid        NOT NULL,
  adr_number       int         NOT NULL CHECK (adr_number >= 1),
  title            text        NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  status           text        NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED','ACCEPTED','REJECTED','DEPRECATED','SUPERSEDED')),
  content_ref      uuid        NOT NULL,     -- artifacts.id
  run_id           uuid,                     -- run, в котором предложено (роль architect)
  superseded_by_id uuid,
  approved_by      uuid,
  approved_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_architecture_decisions PRIMARY KEY (id),
  CONSTRAINT uq_architecture_decisions_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_architecture_decisions_number UNIQUE (project_id, adr_number),
  CONSTRAINT fk_adr_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_adr_content FOREIGN KEY (workspace_id, content_ref) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_adr_run FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE SET NULL (run_id),
  CONSTRAINT fk_adr_superseded_by FOREIGN KEY (workspace_id, superseded_by_id) REFERENCES architecture_decisions(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_adr_approved_by FOREIGN KEY (approved_by) REFERENCES users(id) ON DELETE RESTRICT,
  -- Импликация: ACCEPTED ⇒ указан человек-утверждающий (ТЗ §4 L5: ADR + approval).
  CONSTRAINT ck_adr_approved CHECK ((status = 'ACCEPTED') <= (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
  CONSTRAINT ck_adr_superseded CHECK ((status = 'SUPERSEDED') = (superseded_by_id IS NOT NULL))
);

-- §10.1 code_index_snapshots (T): commit-aware снимок индекса (ADR-0005)
CREATE TABLE code_index_snapshots (
  id                  uuid7       NOT NULL,
  workspace_id        uuid        NOT NULL,
  repository_id       uuid        NOT NULL,
  commit_sha          git_sha     NOT NULL,
  index_version       int         NOT NULL CHECK (index_version >= 1),   -- версия формата/алгоритма индексатора
  parent_snapshot_id  uuid,       -- база инкрементального построения
  status              text        NOT NULL DEFAULT 'BUILDING' CHECK (status IN ('BUILDING','READY','FAILED','SUPERSEDED')),
  file_count          int         CHECK (file_count >= 0),
  symbol_count        int         CHECK (symbol_count >= 0),
  error               text        CHECK (length(error) <= 4000),
  created_at          timestamptz NOT NULL DEFAULT now(),
  completed_at        timestamptz,
  CONSTRAINT pk_code_index_snapshots PRIMARY KEY (id),
  CONSTRAINT uq_code_index_snapshots_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_code_index_snapshots_commit UNIQUE (repository_id, commit_sha, index_version),     -- ТЗ
  CONSTRAINT fk_cis_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_cis_parent FOREIGN KEY (workspace_id, parent_snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE SET NULL (parent_snapshot_id),
  CONSTRAINT ck_cis_completed CHECK ((status IN ('READY','FAILED','SUPERSEDED')) = (completed_at IS NOT NULL))
);
CREATE INDEX ix_cis_repo_ready ON code_index_snapshots (workspace_id, repository_id, created_at DESC) WHERE status = 'READY';

-- §10.2 code_files (T) — дополнение к ТЗ: file tree и инкрементальность по blob_sha
CREATE TABLE code_files (
  id            uuid7       NOT NULL,
  workspace_id  uuid        NOT NULL,
  snapshot_id   uuid        NOT NULL,
  path          text        NOT NULL CHECK (length(path) BETWEEN 1 AND 4096 AND path !~ '(^/|(^|/)\.\.(/|$))'),
  language      text        CHECK (language ~ '^[a-z0-9_+#-]{1,32}$'),
  blob_sha      git_sha     NOT NULL,          -- git blob id: ключ инкрементальности
  content_hash  sha256_hex  NOT NULL,
  size_bytes    bigint      NOT NULL CHECK (size_bytes >= 0),
  is_test       boolean     NOT NULL DEFAULT false,
  is_generated  boolean     NOT NULL DEFAULT false,
  is_binary     boolean     NOT NULL DEFAULT false,
  CONSTRAINT pk_code_files PRIMARY KEY (id),
  CONSTRAINT uq_code_files_ws_snapshot_id UNIQUE (workspace_id, snapshot_id, id),
  CONSTRAINT uq_code_files_path UNIQUE (snapshot_id, path),
  CONSTRAINT fk_code_files_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_code_files_path_trgm ON code_files USING gin (path gin_trgm_ops);    -- pg_trgm
CREATE INDEX ix_code_files_blob ON code_files (workspace_id, blob_sha);

-- §10.3 code_symbols (T): символы (tree-sitter / LSP)
CREATE TABLE code_symbols (
  id              uuid7       NOT NULL,
  workspace_id    uuid        NOT NULL,
  snapshot_id     uuid        NOT NULL,
  file_id         uuid        NOT NULL,
  file_path       text        NOT NULL,          -- денормализовано из code_files (ТЗ §7.2)
  symbol_name     text        NOT NULL CHECK (length(symbol_name) BETWEEN 1 AND 1024),
  qualified_name  text        CHECK (length(qualified_name) <= 4096),
  symbol_kind     text        NOT NULL CHECK (symbol_kind IN
                    ('module','class','interface','function','method','variable','constant','type','enum','test','route','other')),
  start_line      int         NOT NULL CHECK (start_line >= 1),
  end_line        int         NOT NULL,
  signature       text        CHECK (length(signature) <= 2000),
  content_hash    sha256_hex  NOT NULL,
  CONSTRAINT pk_code_symbols PRIMARY KEY (id),
  CONSTRAINT uq_code_symbols_ws_snapshot_id UNIQUE (workspace_id, snapshot_id, id),
  CONSTRAINT fk_code_symbols_file FOREIGN KEY (workspace_id, snapshot_id, file_id)
      REFERENCES code_files(workspace_id, snapshot_id, id) ON DELETE CASCADE,       -- символ и файл из одного снимка
  CONSTRAINT ck_code_symbols_lines CHECK (end_line >= start_line)
);
CREATE INDEX ix_code_symbols_snapshot_name ON code_symbols (snapshot_id, symbol_name);
CREATE INDEX ix_code_symbols_name_trgm ON code_symbols USING gin (symbol_name gin_trgm_ops);    -- pg_trgm
CREATE INDEX ix_code_symbols_file ON code_symbols (file_id, start_line);

-- §10.4 code_dependencies (T): граф зависимостей и test links; оба конца — из одного снимка
CREATE TABLE code_dependencies (
  id                uuid7       NOT NULL,
  workspace_id      uuid        NOT NULL,
  snapshot_id       uuid        NOT NULL,
  source_symbol_id  uuid        NOT NULL,
  target_symbol_id  uuid,
  target_ref        text        CHECK (length(target_ref) <= 1024),   -- внешний модуль/пакет, неразрешённый символ
  edge_type         text        NOT NULL CHECK (edge_type IN ('imports','calls','inherits','implements','references','tests')),
  CONSTRAINT pk_code_dependencies PRIMARY KEY (id),
  CONSTRAINT fk_code_dependencies_source FOREIGN KEY (workspace_id, snapshot_id, source_symbol_id)
      REFERENCES code_symbols(workspace_id, snapshot_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_code_dependencies_target FOREIGN KEY (workspace_id, snapshot_id, target_symbol_id)
      REFERENCES code_symbols(workspace_id, snapshot_id, id) ON DELETE CASCADE,
  CONSTRAINT ck_code_dependencies_target CHECK (num_nonnulls(target_symbol_id, target_ref) >= 1),
  CONSTRAINT ck_code_dependencies_no_self CHECK (target_symbol_id IS NULL OR target_symbol_id <> source_symbol_id)
);
CREATE INDEX ix_code_dependencies_source ON code_dependencies (snapshot_id, source_symbol_id, edge_type);
CREATE INDEX ix_code_dependencies_target ON code_dependencies (snapshot_id, target_symbol_id, edge_type) WHERE target_symbol_id IS NOT NULL;

-- §10.5 knowledge_chunks (T): фрагменты для FTS/trigram-поиска (без embeddings в v1)
CREATE TABLE knowledge_chunks (
  id                         uuid7       NOT NULL,
  workspace_id               uuid        NOT NULL,
  project_id                 uuid,        -- NULL для документации уровня workspace
  source_type                text        NOT NULL CHECK (source_type IN
                               ('code_file','code_symbol','documentation_section','knowledge_entry','architecture_decision')),
  source_id                  uuid        NOT NULL,     -- полиморфная ссылка; целостность — §18.11
  snapshot_id                uuid,        -- code_index_snapshots (для source_type code_*)
  documentation_snapshot_id  uuid,        -- documentation_snapshots (для documentation_section)
  chunk_index                int         NOT NULL CHECK (chunk_index >= 0),
  text_ref                   text        NOT NULL CHECK (text_ref ~ '^(git:[0-9a-f]{40}([0-9a-f]{24})?#L[0-9]+-L[0-9]+|artifact:[0-9a-f-]{36}#bytes=[0-9]+-[0-9]+)$'),
  content_hash               sha256_hex  NOT NULL,
  token_estimate             int         CHECK (token_estimate >= 0),
  search_vector              tsvector    NOT NULL,
  metadata                   jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
  created_at                 timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_knowledge_chunks PRIMARY KEY (id),
  CONSTRAINT uq_knowledge_chunks_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT uq_knowledge_chunks_source UNIQUE NULLS NOT DISTINCT (source_type, source_id, snapshot_id, documentation_snapshot_id, chunk_index),
  CONSTRAINT fk_knowledge_chunks_project FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_knowledge_chunks_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_knowledge_chunks_doc_snapshot FOREIGN KEY (workspace_id, documentation_snapshot_id)
      REFERENCES documentation_snapshots(workspace_id, id) ON DELETE CASCADE,
  -- Каждый фрагмент кода привязан к commit-снимку: основа правила «устаревший фрагмент блокируется/маркируется» (ТЗ §12).
  CONSTRAINT ck_knowledge_chunks_freshness_anchor CHECK (
      (source_type IN ('code_file','code_symbol') AND snapshot_id IS NOT NULL AND documentation_snapshot_id IS NULL) OR
      (source_type = 'documentation_section' AND documentation_snapshot_id IS NOT NULL AND snapshot_id IS NULL) OR
      (source_type IN ('knowledge_entry','architecture_decision') AND snapshot_id IS NULL AND documentation_snapshot_id IS NULL))
);
CREATE INDEX ix_knowledge_chunks_fts ON knowledge_chunks USING gin (workspace_id, search_vector);   -- btree_gin
CREATE INDEX ix_knowledge_chunks_snapshot ON knowledge_chunks (snapshot_id) WHERE snapshot_id IS NOT NULL;
CREATE INDEX ix_knowledge_chunks_doc_snapshot ON knowledge_chunks (documentation_snapshot_id) WHERE documentation_snapshot_id IS NOT NULL;
CREATE INDEX ix_knowledge_chunks_source ON knowledge_chunks (source_type, source_id);

-- §10.6 retrieval_queries (T)
CREATE TABLE retrieval_queries (
  id                  uuid7                NOT NULL,
  workspace_id        uuid                 NOT NULL,
  run_id              uuid                 NOT NULL,
  task_id             uuid                 NOT NULL,
  task_attempt_id     uuid,
  query_text          text                 NOT NULL CHECK (length(query_text) BETWEEN 1 AND 4000),
  strategy            text                 NOT NULL CHECK (strategy IN ('exact','symbol','graph','fts','trigram','docs','hybrid')),
  snapshot_id         uuid,
  documentation_mode  documentation_mode_t NOT NULL,
  created_at          timestamptz          NOT NULL DEFAULT now(),
  CONSTRAINT pk_retrieval_queries PRIMARY KEY (id),
  CONSTRAINT uq_retrieval_queries_ws_id UNIQUE (workspace_id, id),
  CONSTRAINT fk_retrieval_queries_task FOREIGN KEY (workspace_id, run_id, task_id) REFERENCES tasks(workspace_id, run_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_retrieval_queries_attempt FOREIGN KEY (workspace_id, task_attempt_id) REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_retrieval_queries_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE SET NULL (snapshot_id),
  CONSTRAINT ck_retrieval_queries_docs_off CHECK (NOT (strategy = 'docs' AND documentation_mode = 'OFF'))   -- AC-10
);
CREATE INDEX ix_retrieval_queries_task ON retrieval_queries (workspace_id, task_id, created_at);

-- §10.7 retrieval_results (T)
CREATE TABLE retrieval_results (
  workspace_id  uuid    NOT NULL,
  query_id      uuid    NOT NULL,
  chunk_id      uuid    NOT NULL,
  rank          int     NOT NULL CHECK (rank >= 1),
  score         real    NOT NULL,
  selected      boolean NOT NULL DEFAULT false,
  is_stale      boolean NOT NULL DEFAULT false,   -- фрагмент не из снимка текущего commit: явная маркировка (ТЗ §12)
  CONSTRAINT pk_retrieval_results PRIMARY KEY (query_id, chunk_id),             -- = UNIQUE(query_id,chunk_id) ТЗ
  CONSTRAINT uq_retrieval_results_rank UNIQUE (query_id, rank),
  CONSTRAINT fk_retrieval_results_query FOREIGN KEY (workspace_id, query_id) REFERENCES retrieval_queries(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_retrieval_results_chunk FOREIGN KEY (workspace_id, chunk_id) REFERENCES knowledge_chunks(workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_retrieval_results_chunk ON retrieval_results (chunk_id);

-- §10.8 context_packs (T): кэш — по content_hash + source_commit + policy_version
CREATE TABLE context_packs (
  id                  uuid7                NOT NULL,
  workspace_id        uuid                 NOT NULL,
  task_attempt_id     uuid                 NOT NULL,
  snapshot_id         uuid                 NOT NULL,
  source_commit       git_sha              NOT NULL,
  policy_version      int                  NOT NULL CHECK (policy_version >= 1),
  documentation_mode  documentation_mode_t NOT NULL,
  token_budget        int                  NOT NULL CHECK (token_budget > 0),
  size_bytes          bigint               NOT NULL CHECK (size_bytes >= 0),
  manifest            jsonb                NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),  -- состав: chunk ids, hashes, stale-флаги
  content_hash        sha256_hex           NOT NULL,
  artifact_id         uuid                 NOT NULL,
  created_at          timestamptz          NOT NULL DEFAULT now(),
  CONSTRAINT pk_context_packs PRIMARY KEY (id),
  CONSTRAINT uq_context_packs_attempt UNIQUE (task_attempt_id),
  CONSTRAINT fk_context_packs_attempt FOREIGN KEY (workspace_id, task_attempt_id) REFERENCES task_attempts(workspace_id, id) ON DELETE RESTRICT,
  -- RESTRICT: снимок, на который ссылается context pack, не удаляется (воспроизводимость, §20.3).
  CONSTRAINT fk_context_packs_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_context_packs_artifact FOREIGN KEY (workspace_id, artifact_id) REFERENCES artifacts(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_context_packs_cache ON context_packs (workspace_id, source_commit, policy_version, content_hash);
CREATE INDEX ix_context_packs_snapshot ON context_packs (snapshot_id);

-- §10.9 indexing_jobs (T); переходы — allowed_transitions('indexing_job'), §18.3
CREATE TABLE indexing_jobs (
  id                uuid7       NOT NULL,
  workspace_id      uuid        NOT NULL,
  repository_id     uuid        NOT NULL,
  commit_sha        git_sha     NOT NULL,
  kind              text        NOT NULL DEFAULT 'incremental' CHECK (kind IN ('full','incremental')),
  status            text        NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  base_snapshot_id  uuid,
  snapshot_id       uuid,
  attempt_count     int         NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  created_at        timestamptz NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  error             text        CHECK (length(error) <= 4000),
  CONSTRAINT pk_indexing_jobs PRIMARY KEY (id),
  CONSTRAINT fk_indexing_jobs_repository FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_indexing_jobs_base FOREIGN KEY (workspace_id, base_snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE SET NULL (base_snapshot_id),
  CONSTRAINT fk_indexing_jobs_snapshot FOREIGN KEY (workspace_id, snapshot_id) REFERENCES code_index_snapshots(workspace_id, id) ON DELETE SET NULL (snapshot_id),
  CONSTRAINT ck_indexing_jobs_finished CHECK ((status IN ('SUCCEEDED','FAILED','CANCELLED')) = (finished_at IS NOT NULL)),
  CONSTRAINT ck_indexing_jobs_kind CHECK (kind = 'full' OR base_snapshot_id IS NOT NULL OR status IN ('QUEUED','FAILED','CANCELLED'))
);
CREATE UNIQUE INDEX uq_indexing_jobs_active ON indexing_jobs (repository_id, commit_sha) WHERE status IN ('QUEUED','RUNNING');
CREATE INDEX ix_indexing_jobs_queue ON indexing_jobs (created_at) WHERE status = 'QUEUED';
"""

TRIGGERS = r"""
-- Стандартные триггеры (§1.5, §4)
CREATE TRIGGER trg_documentation_sources_updated_at BEFORE UPDATE ON documentation_sources FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_knowledge_entries_updated_at BEFORE UPDATE ON knowledge_entries FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
CREATE TRIGGER trg_architecture_decisions_updated_at BEFORE UPDATE ON architecture_decisions FOR EACH ROW EXECUTE FUNCTION trg_fn_set_updated_at();
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['project_technologies','documentation_sources','documentation_snapshots',
                           'documentation_sections','knowledge_entries','architecture_decisions','code_index_snapshots',
                           'code_files','code_symbols','code_dependencies','knowledge_chunks','retrieval_queries',
                           'retrieval_results','context_packs','indexing_jobs']
  LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OF workspace_id ON %I '
                   'FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_workspace()', 'trg_' || t || '_immutable_ws', t);
  END LOOP;
END $$;

-- §18.3: Indexing Job FSM — защитный рубеж поверх кода worker-index.
ALTER TABLE allowed_transitions DROP CONSTRAINT allowed_transitions_entity_check;
ALTER TABLE allowed_transitions ADD CONSTRAINT ck_allowed_transitions_entity
  CHECK (entity IN ('run','task','execution','directive','approval','external_action','indexing_job'));
INSERT INTO allowed_transitions (entity, from_status, to_status)
SELECT 'indexing_job', v.from_status, unnest(v.to_statuses)
  FROM (VALUES
    ('__new__', ARRAY['QUEUED']),
    ('QUEUED',  ARRAY['RUNNING','CANCELLED','FAILED']),
    ('RUNNING', ARRAY['SUCCEEDED','FAILED','CANCELLED','QUEUED'])   -- QUEUED: повтор после потери аренды
  ) AS v(from_status, to_statuses);
CREATE TRIGGER trg_indexing_jobs_transition BEFORE INSERT OR UPDATE OF status ON indexing_jobs
  FOR EACH ROW EXECUTE FUNCTION trg_fn_check_transition('indexing_job');

-- §18.11: полиморфная ссылка knowledge_chunks(source_type, source_id) — цель существует в том же workspace
-- и (для code_*/documentation_section) в том же снимке, что и фрагмент.
CREATE FUNCTION trg_fn_knowledge_chunks_source() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_ok boolean;
BEGIN
  CASE NEW.source_type
    WHEN 'code_file' THEN
      SELECT EXISTS (SELECT 1 FROM code_files t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.source_id
                        AND t.snapshot_id = NEW.snapshot_id) INTO v_ok;
    WHEN 'code_symbol' THEN
      SELECT EXISTS (SELECT 1 FROM code_symbols t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.source_id
                        AND t.snapshot_id = NEW.snapshot_id) INTO v_ok;
    WHEN 'documentation_section' THEN
      SELECT EXISTS (SELECT 1 FROM documentation_sections t WHERE t.workspace_id = NEW.workspace_id
                        AND t.id = NEW.source_id AND t.snapshot_id = NEW.documentation_snapshot_id) INTO v_ok;
    WHEN 'knowledge_entry' THEN
      SELECT EXISTS (SELECT 1 FROM knowledge_entries t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.source_id) INTO v_ok;
    WHEN 'architecture_decision' THEN
      SELECT EXISTS (SELECT 1 FROM architecture_decisions t WHERE t.workspace_id = NEW.workspace_id AND t.id = NEW.source_id) INTO v_ok;
    ELSE
      v_ok := false;
  END CASE;
  IF NOT v_ok THEN
    RAISE EXCEPTION 'knowledge_chunks: % % not found in this workspace/snapshot', NEW.source_type, NEW.source_id
      USING ERRCODE = '23503';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_knowledge_chunks_source
  BEFORE INSERT OR UPDATE OF source_type, source_id, snapshot_id, documentation_snapshot_id ON knowledge_chunks
  FOR EACH ROW EXECUTE FUNCTION trg_fn_knowledge_chunks_source();
"""

RLS = r"""
-- T-таблицы (§19.3, §19.4); technologies/technology_versions — G (без RLS, доступ ограничен GRANT).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['project_technologies','documentation_sources','documentation_snapshots',
                           'documentation_sections','knowledge_entries','architecture_decisions','code_index_snapshots',
                           'code_files','code_symbols','code_dependencies','knowledge_chunks','retrieval_queries',
                           'retrieval_results','context_packs','indexing_jobs']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO aicd_app, aicd_worker '
                   'USING (workspace_id = app_current_workspace()) '
                   'WITH CHECK (workspace_id = app_current_workspace())', 'p_' || t || '_tenant', t);
  END LOOP;
END $$;
"""

GRANTS = r"""
REVOKE ALL ON technologies, technology_versions, project_technologies, documentation_sources, documentation_snapshots,
              documentation_sections, knowledge_entries, architecture_decisions, code_index_snapshots, code_files,
              code_symbols, code_dependencies, knowledge_chunks, retrieval_queries, retrieval_results, context_packs,
              indexing_jobs
  FROM PUBLIC;

-- G-справочники (§19.4, OQ-DB-15): чтение app/worker; worker регистрирует обнаруженные технологии/версии
-- (определение стека, P5-07), но не меняет и не удаляет их; support_status ведут миграции/admin CLI.
GRANT SELECT ON technologies, technology_versions TO aicd_app, aicd_worker;
GRANT INSERT ON technologies, technology_versions TO aicd_worker;

-- api: чтение индекса/поиска/документации; команды пользователя — подтверждение стека, база знаний, ADR,
-- регистрация источников документации (POST /documentation/sources), постановка/отмена индексации.
GRANT SELECT ON project_technologies, documentation_sources, documentation_snapshots, documentation_sections,
               knowledge_entries, architecture_decisions, code_index_snapshots, code_files, code_symbols,
               code_dependencies, knowledge_chunks, retrieval_queries, retrieval_results, context_packs, indexing_jobs
  TO aicd_app;
GRANT INSERT, UPDATE ON project_technologies, documentation_sources, knowledge_entries, architecture_decisions,
                       indexing_jobs
  TO aicd_app;

-- workers (worker-index, исполнители агентов, импортёр документации): построение снимков, поиск, context packs.
-- Содержимое снимка и журнал поиска только добавляются (снимок неизменяем, новый commit — новый снимок).
GRANT SELECT ON project_technologies, documentation_sources, documentation_snapshots, documentation_sections,
               knowledge_entries, architecture_decisions, code_index_snapshots, code_files, code_symbols,
               code_dependencies, knowledge_chunks, retrieval_queries, retrieval_results, context_packs, indexing_jobs
  TO aicd_worker;
GRANT INSERT, UPDATE ON code_index_snapshots, indexing_jobs, documentation_snapshots, knowledge_entries,
                       architecture_decisions
  TO aicd_worker;
GRANT INSERT, UPDATE, DELETE ON project_technologies TO aicd_worker;
GRANT INSERT ON code_files, code_symbols, code_dependencies, knowledge_chunks, retrieval_queries, retrieval_results,
                context_packs, documentation_sections
  TO aicd_worker;
GRANT UPDATE (selected, is_stale) ON retrieval_results TO aicd_worker;
GRANT UPDATE (last_refreshed_at) ON documentation_sources TO aicd_worker;

-- maintenance: retention снимков и архив run (§20.2–20.3; удаление — вместе с реализацией retention).
GRANT SELECT ON technologies, technology_versions, project_technologies, documentation_sources,
               documentation_snapshots, documentation_sections, knowledge_entries, architecture_decisions,
               code_index_snapshots, code_files, code_symbols, code_dependencies, knowledge_chunks, retrieval_queries,
               retrieval_results, context_packs, indexing_jobs
  TO aicd_maintenance;
"""

SYSFN = r"""
-- §19.5: POST /documentation/sources/{id}/refresh — запланировать загрузку (external_actions PLANNED,
-- action_type 'docs_download', run_id NULL, ADR-0012) без права INSERT на external_actions у aicd_app.
-- Источник ищется только в app_current_workspace(); вызывающий — член workspace с ролью
-- ≥ approval_min_role('DOCS_DOWNLOAD') (= developer, как documentation:refresh в API_DESIGN §4).
-- Результат: id действия; NULL — источник не найден в workspace или вызывающий не член (→ 404);
-- 42501 — роли недостаточно (→ 403); 55000 — источник отключён (→ 409); 22023 — неверные аргументы.
-- Идемпотентность: ключ = sha256('docs_download' | workspace | source | p_idempotency_key);
-- повтор возвращает id уже созданного действия.
CREATE FUNCTION sys_plan_docs_download(p_source_id uuid, p_idempotency_key text, p_request jsonb)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, aicd AS $$
DECLARE
  v_ws     uuid := aicd.app_current_workspace();
  v_user   uuid := aicd.app_current_user();
  v_uri    text;
  v_enabled boolean;
  v_role   text;
  v_key    text;
  v_id     uuid;
BEGIN
  IF p_source_id IS NULL OR p_idempotency_key IS NULL OR length(p_idempotency_key) NOT BETWEEN 1 AND 255
     OR p_request IS NULL OR jsonb_typeof(p_request) <> 'object' OR octet_length(p_request::text) > 16384 THEN
    RAISE EXCEPTION 'sys_plan_docs_download: invalid arguments' USING ERRCODE = '22023';
  END IF;
  IF v_ws IS NULL OR v_user IS NULL THEN
    RETURN NULL;
  END IF;
  SELECT s.uri_or_file_ref, s.enabled INTO v_uri, v_enabled
    FROM aicd.documentation_sources s WHERE s.id = p_source_id AND s.workspace_id = v_ws;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  SELECT m.role INTO v_role FROM aicd.workspace_members m WHERE m.workspace_id = v_ws AND m.user_id = v_user;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  IF aicd.workspace_role_rank(v_role) < aicd.workspace_role_rank(aicd.approval_min_role('DOCS_DOWNLOAD')) THEN
    RAISE EXCEPTION 'role % may not refresh documentation sources', v_role USING ERRCODE = '42501';
  END IF;
  IF NOT v_enabled THEN
    RAISE EXCEPTION 'documentation source % is disabled', p_source_id USING ERRCODE = '55000';
  END IF;
  v_key := encode(sha256(convert_to(concat_ws('|', 'docs_download', v_ws::text, p_source_id::text, p_idempotency_key),
                                    'UTF8')), 'hex');
  -- UUIDv7 (PostgreSQL 17 не умеет uuidv7()): 48 бит unix-ms поверх случайного v4, версия 0100 → 0111.
  v_id := encode(set_bit(set_bit(overlay(uuid_send(gen_random_uuid())
                   PLACING substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
                   FROM 1 FOR 6), 52, 1), 53, 1), 'hex')::uuid;
  INSERT INTO aicd.external_actions (id, workspace_id, action_type, idempotency_key, target, request, request_hash)
  VALUES (v_id, v_ws, 'docs_download', v_key,
          CASE WHEN length(v_uri) <= 1024 THEN v_uri ELSE 'documentation_source:' || p_source_id::text END,
          p_request, encode(sha256(convert_to(p_request::text, 'UTF8')), 'hex'))
  ON CONFLICT (idempotency_key) DO NOTHING;
  IF NOT FOUND THEN
    SELECT e.id INTO v_id FROM aicd.external_actions e WHERE e.idempotency_key = v_key;
  END IF;
  RETURN v_id;
END $$;

GRANT CREATE ON SCHEMA aicd TO aicd_sysfn;
ALTER FUNCTION sys_plan_docs_download(uuid, text, jsonb) OWNER TO aicd_sysfn;
REVOKE CREATE ON SCHEMA aicd FROM aicd_sysfn;
REVOKE ALL ON FUNCTION sys_plan_docs_download(uuid, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sys_plan_docs_download(uuid, text, jsonb) TO aicd_app;
GRANT EXECUTE ON FUNCTION approval_min_role(text) TO aicd_sysfn;
GRANT SELECT (id, workspace_id, uri_or_file_ref, enabled) ON documentation_sources TO aicd_sysfn;
-- workspace_id/status/executing_at выданы миграцией 0005; здесь — недостающие колонки и вставка.
GRANT SELECT (id, idempotency_key) ON external_actions TO aicd_sysfn;
GRANT INSERT (id, workspace_id, action_type, idempotency_key, target, request, request_hash) ON external_actions TO aicd_sysfn;
-- trg_external_actions_transition выполняется от имени владельца функции (aicd_sysfn) и читает справочник.
GRANT SELECT ON allowed_transitions TO aicd_sysfn;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS sys_plan_docs_download(uuid, text, jsonb);
REVOKE SELECT ON allowed_transitions FROM aicd_sysfn;
REVOKE INSERT (id, workspace_id, action_type, idempotency_key, target, request, request_hash) ON external_actions FROM aicd_sysfn;
REVOKE SELECT (id, idempotency_key) ON external_actions FROM aicd_sysfn;
REVOKE EXECUTE ON FUNCTION approval_min_role(text) FROM aicd_sysfn;

DROP TABLE IF EXISTS indexing_jobs, context_packs, retrieval_results, retrieval_queries, knowledge_chunks,
                     code_dependencies, code_symbols, code_files, code_index_snapshots, architecture_decisions,
                     knowledge_entries, documentation_sections, documentation_snapshots, documentation_sources,
                     project_technologies, technology_versions, technologies CASCADE;
DROP FUNCTION IF EXISTS trg_fn_knowledge_chunks_source();

DELETE FROM allowed_transitions WHERE entity = 'indexing_job';
ALTER TABLE allowed_transitions DROP CONSTRAINT IF EXISTS ck_allowed_transitions_entity;
ALTER TABLE allowed_transitions ADD CONSTRAINT allowed_transitions_entity_check
  CHECK (entity IN ('run','task','execution','directive','approval','external_action'));
"""


def upgrade() -> None:
    for block in (TABLES, TRIGGERS, RLS, GRANTS, SYSFN):
        op.execute(block)


def downgrade() -> None:
    op.execute(DOWNGRADE)
