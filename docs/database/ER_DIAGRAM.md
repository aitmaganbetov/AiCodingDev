# ER_DIAGRAM — ER-диаграммы по доменам

**Статус:** Proposed (Phase 0). **Основание:** ТЗ §7, §21 (`data-model.md`: ERD, cardinality, invariants); полное DDL — [DATABASE_DESIGN.md](DATABASE_DESIGN.md).

> Диаграммы описывают проектируемую схему; ничего не реализовано. Атрибуты показаны выборочно (ключи и колонки, важные для связей и инвариантов). Во всех tenant-таблицах есть `workspace_id`, а связи между tenant-таблицами реализованы составными FK `(workspace_id, <parent>_id)` (ADR-0008) — на диаграммах `workspace_id` опущен, кроме корневых сущностей, чтобы не загромождать.

Обозначения кардинальностей Mermaid: `||--||` один-к-одному, `||--o{` один-ко-многим (0..N), `||--|{` один-к-1..N, `|o--o{` необязательный родитель (FK nullable).

## Содержание

1. [Обзор доменов](#1-обзор-доменов)
2. [IAM и tenancy](#2-iam-и-tenancy)
3. [Проекты, репозитории, стек](#3-проекты-репозитории-стек)
4. [Оркестрация](#4-оркестрация)
5. [CLI Gateway](#5-cli-gateway)
6. [ACP, чат, поручения](#6-acp-чат-поручения)
7. [Code Intelligence и документация](#7-code-intelligence-и-документация)
8. [Качество, evidence, стандарты](#8-качество-evidence-стандарты)
9. [Approvals, политики, надёжность](#9-approvals-политики-надёжность)
10. [GitHub и аудит](#10-github-и-аудит)
11. [Ключевые инварианты](#11-ключевые-инварианты)

---

## 1. Обзор доменов

Связи между доменами (одна стрелка — одна или несколько FK; направление — от дочерней к родительской сущности по смыслу «принадлежит»).

```mermaid
flowchart LR
  subgraph IAM[IAM / tenancy]
    users; workspaces; workspace_members
  end
  subgraph PRJ[Projects]
    projects; repositories; project_settings; worktrees
  end
  subgraph ORCH[Orchestration]
    runs; tasks; task_attempts; routing_decisions
  end
  subgraph CLI[CLI Gateway]
    cli_installations; cli_executions; provider_health
  end
  subgraph COLLAB[ACP / Chat / Directives]
    agent_messages; chat_messages; user_directives
  end
  subgraph CI[Code Intelligence / Docs]
    code_index_snapshots; context_packs; documentation_sources
  end
  subgraph QA[Quality / Standards]
    gate_executions; evidence; findings; compliance_results
  end
  subgraph REL[Approvals / Reliability]
    approvals; external_actions; outbox_events; run_event_log; worker_leases; checkpoints
  end
  subgraph GH[GitHub / Audit]
    github_installations; pull_requests; audit_events
  end

  workspaces --> users
  projects --> workspaces
  repositories --> projects
  runs --> projects
  runs --> repositories
  tasks --> runs
  task_attempts --> tasks
  cli_executions --> task_attempts
  cli_executions --> cli_installations
  agent_messages --> runs
  user_directives --> runs
  code_index_snapshots --> repositories
  context_packs --> task_attempts
  context_packs --> code_index_snapshots
  gate_executions --> runs
  evidence --> runs
  findings --> gate_executions
  compliance_results --> evidence
  approvals --> runs
  external_actions --> approvals
  run_event_log --> runs
  checkpoints --> runs
  pull_requests --> repositories
  pull_requests --> external_actions
  repositories --> github_installations
  worktrees --> runs
  cli_executions --> worktrees
```

Сущности верхнего уровня и их место в tenancy:

```mermaid
erDiagram
  workspaces ||--o{ projects : contains
  workspaces ||--o{ workspace_members : has
  users ||--o{ workspace_members : "is member"
  projects ||--|| project_settings : configures
  projects ||--o{ repositories : has
  projects ||--o{ runs : executes
  repositories ||--o{ runs : "base of"
  runs ||--|{ tasks : "DAG of"
  runs ||--o{ approvals : requires
  runs ||--o{ gate_executions : verifies
  runs ||--o{ evidence : collects
  runs ||--o{ agent_messages : "ACP log"
  runs ||--o{ run_event_log : "SSE log"
  tasks ||--o{ task_attempts : "attempted by"
  task_attempts ||--o{ cli_executions : runs
```

---

## 2. IAM и tenancy

```mermaid
erDiagram
  users {
    uuid id PK
    text email UK
    text display_name
    text status
  }
  user_credentials {
    uuid user_id PK,FK
    text password_hash "argon2id"
    int failed_attempts
    timestamptz locked_until
  }
  user_sessions {
    uuid id PK
    uuid user_id FK
    text token_hash UK "sha256"
    text csrf_token_hash
    timestamptz expires_at
    timestamptz revoked_at
  }
  workspaces {
    uuid id PK
    text slug UK
    text status
    uuid created_by FK
    bigint version
  }
  workspace_members {
    uuid workspace_id PK,FK
    uuid user_id PK,FK
    text role "workspace_role"
  }
  project_member_overrides {
    uuid project_id PK,FK
    uuid user_id PK,FK
    uuid workspace_id FK
    text role "only lower"
  }
  idempotency_keys {
    uuid id PK
    uuid workspace_id FK "nullable"
    uuid user_id FK
    text key
    text request_hash
  }
  projects {
    uuid id PK
    uuid workspace_id FK
  }

  users ||--o| user_credentials : "authenticates with"
  users ||--o{ user_sessions : has
  users ||--o{ workspace_members : "member via"
  workspaces ||--|{ workspace_members : "has (>=1 owner)"
  workspaces ||--o{ projects : contains
  workspace_members ||--o{ project_member_overrides : "downgraded in"
  projects ||--o{ project_member_overrides : scopes
  users ||--o{ idempotency_keys : sends
  workspaces |o--o{ idempotency_keys : scopes
  users ||--o{ workspaces : creates
```

---

## 3. Проекты, репозитории, стек

```mermaid
erDiagram
  projects {
    uuid id PK
    uuid workspace_id FK
    text name "UK per workspace"
    text status
    bigint version
  }
  project_settings {
    uuid project_id PK,FK
    text documentation_mode "OFF|SMART|STRICT"
    uuid routing_policy_id FK
    text quality_profile
    text approval_policy
  }
  repositories {
    uuid id PK
    uuid project_id FK
    text provider "local|github"
    text local_root_ref
    uuid github_installation_id FK
    bigint github_repo_id
  }
  worktrees {
    uuid id PK
    uuid repository_id FK
    uuid run_id FK
    uuid task_id FK
    text branch_name
    text base_commit_sha
    text status
  }
  technologies {
    uuid id PK
    text ecosystem
    text name
  }
  technology_versions {
    uuid id PK
    uuid technology_id FK
    text version_label
    text support_status
  }
  project_technologies {
    uuid project_id PK,FK
    uuid technology_version_id PK,FK
    text detected_from
    uuid confirmed_by FK
  }
  policies {
    uuid id PK
    uuid workspace_id "nullable = global"
    text policy_type
    int version
  }
  github_installations {
    uuid id PK
    bigint installation_id UK
  }

  projects ||--|| project_settings : "configured by"
  projects ||--o{ repositories : has
  policies |o--o{ project_settings : "routing policy"
  github_installations |o--o{ repositories : "grants access"
  repositories ||--o{ worktrees : "checked out as"
  technologies ||--|{ technology_versions : "has"
  technology_versions ||--o{ project_technologies : "detected in"
  projects ||--o{ project_technologies : uses
```

---

## 4. Оркестрация

```mermaid
erDiagram
  runs {
    uuid id PK
    uuid project_id FK
    uuid repository_id FK
    uuid requested_by FK
    text mode "run_mode"
    text status "run_status"
    text blocked_reason
    int plan_version
    int approved_plan_version
    bigint last_event_seq
    uuid quality_profile_id FK
    jsonb policy_snapshot
    bigint version
  }
  tasks {
    uuid id PK
    uuid run_id FK
    int plan_version
    text task_key "UK per run+plan"
    uuid origin_task_id FK
    text agent_role
    text status "task_status"
    smallint complexity "1..5"
    text risk_level
    int attempt_count
    int max_attempts
    bigint lease_fencing_token
    bigint version
  }
  task_dependencies {
    uuid task_id PK,FK
    uuid depends_on_task_id PK,FK
    uuid run_id FK "same run for both"
    text dependency_type
  }
  task_attempts {
    uuid id PK
    uuid task_id FK
    int attempt_no "UK per task"
    text status
    text provider
    text failure_category
    bigint fencing_token
    uuid agent_assignment_id FK
    uuid routing_decision_id FK
  }
  agent_profiles {
    uuid id PK
    text role UK
    text instruction_template_version
    uuid policy_profile_id FK
  }
  agent_assignments {
    uuid id PK
    uuid task_id FK
    uuid agent_profile_id FK
    text provider
    uuid reviewed_attempt_id FK
    text review_independence
  }
  routing_decisions {
    uuid id PK
    uuid task_id FK
    text outcome "SELECTED|FALLBACK|BLOCKED"
    text selected_provider
    jsonb score_breakdown
    int policy_version
  }
  allowed_transitions {
    text entity PK
    text from_status PK
    text to_status PK
  }
  quality_profiles {
    uuid id PK
    text code
    int version
  }

  runs ||--|{ tasks : "plan (DAG)"
  tasks ||--o{ task_dependencies : "depends (child)"
  tasks ||--o{ task_dependencies : "is prerequisite"
  tasks |o--o{ tasks : "origin (fix/replan)"
  tasks ||--o{ task_attempts : "attempted"
  tasks ||--o{ agent_assignments : "assigned"
  tasks ||--o{ routing_decisions : "routed"
  agent_profiles ||--o{ agent_assignments : "used by"
  agent_assignments |o--o{ task_attempts : "executes"
  routing_decisions |o--o{ task_attempts : "selected for"
  routing_decisions |o--o{ agent_assignments : "justifies"
  task_attempts |o--o{ agent_assignments : "reviewed attempt"
  quality_profiles ||--o{ runs : "snapshot for"
```

`allowed_transitions` не связана FK — это справочник, используемый триггерами `trg_*_transition` (DATABASE_DESIGN §18.3).

---

## 5. CLI Gateway

```mermaid
erDiagram
  worker_hosts {
    uuid id PK
    text hostname
    text kind
    timestamptz last_heartbeat_at
  }
  cli_providers {
    uuid id PK
    text code UK "codex|claude"
    bool enabled
  }
  cli_installations {
    uuid id PK
    uuid provider_id FK
    uuid host_id FK
    text binary_path
    text detected_version
    text auth_status
    text compatibility
    jsonb capabilities
    text credential_volume_ref "name only"
  }
  cli_sessions {
    uuid id PK
    uuid installation_id FK
    text session_ref "no credentials"
    text status
  }
  cli_executions {
    uuid id PK "execution_id"
    uuid workspace_id FK
    uuid task_attempt_id FK
    uuid installation_id FK
    uuid session_id FK
    uuid worktree_id FK
    text status "execution_status"
    text failure_category "cli_error_code"
    int exit_code
    bigint fencing_token
    uuid output_artifact_id FK
    uuid raw_log_artifact_id FK
  }
  provider_health {
    uuid id PK
    uuid installation_id FK
    text auth_status
    text availability
    timestamptz observed_at
  }
  usage_metrics {
    uuid id PK
    uuid execution_id FK
    text metric_name
    numeric metric_value "NULL if not exposed"
    text source
  }
  task_attempts {
    uuid id PK
  }
  worktrees {
    uuid id PK
  }
  artifacts {
    uuid id PK
  }

  cli_providers ||--o{ cli_installations : "installed as"
  worker_hosts ||--o{ cli_installations : hosts
  cli_installations ||--o{ cli_sessions : opens
  cli_installations ||--o{ provider_health : "probed"
  cli_installations ||--o{ cli_executions : runs
  cli_sessions |o--o{ cli_executions : "reused by"
  task_attempts ||--o{ cli_executions : "executed via"
  worktrees |o--o{ cli_executions : "workdir"
  artifacts |o--o{ cli_executions : "output / raw log"
  cli_executions ||--o{ usage_metrics : reports
```

Граница tenancy: `worker_hosts`, `cli_providers`, `cli_installations`, `cli_sessions`, `provider_health` — глобальные; `cli_executions`, `usage_metrics` — tenant.

---

## 6. ACP, чат, поручения

```mermaid
erDiagram
  runs {
    uuid id PK
  }
  tasks {
    uuid id PK
  }
  agent_messages {
    uuid id PK "message_id"
    uuid run_id FK
    uuid task_id FK
    text sender_role
    text recipient_role
    text message_type "acp_message_type"
    uuid correlation_id
    uuid reply_to_id FK
    jsonb payload "<= 32 KiB"
    uuid source_execution_id FK
  }
  message_deliveries {
    uuid message_id PK,FK
    text consumer_id PK
    int delivery_count
    timestamptz acked_at
  }
  agent_decisions {
    uuid id PK
    uuid run_id FK
    uuid task_id FK
    text decision_type
    uuid acp_message_id FK
    text decided_by_type
  }
  chat_threads {
    uuid id PK
    uuid run_id FK
    text scope
    text target_role
    text target_provider
    uuid target_task_id FK
  }
  chat_messages {
    uuid id PK
    uuid run_id FK
    uuid thread_id FK
    text sender_type
    text message_kind
    uuid acp_message_id FK
    uuid source_event_id "no FK"
  }
  user_directives {
    uuid id PK
    uuid run_id FK
    uuid chat_message_id FK,UK
    text target_type
    text directive_type
    text status "directive_status"
    text impact_status "directive_impact_type"
    uuid approval_id FK
    uuid checkpoint_id FK
    bigint version
  }
  directive_impacts {
    uuid directive_id PK,FK
    uuid task_id PK,FK
    text impact_type
    text resolution
  }
  directive_acknowledgements {
    uuid id PK
    uuid directive_id FK
    text recipient_role
    uuid recipient_task_id FK
    uuid acp_message_id FK
    text status
  }
  agent_activity_events {
    uuid id PK
    timestamptz occurred_at PK "partition key"
    uuid run_id FK
    uuid task_id FK
    uuid execution_id FK
    text event_type
  }
  cli_executions {
    uuid id PK
  }
  approvals {
    uuid id PK
  }
  checkpoints {
    uuid id PK
  }

  runs ||--o{ agent_messages : logs
  tasks |o--o{ agent_messages : "about"
  agent_messages |o--o{ agent_messages : "reply_to"
  cli_executions |o--o{ agent_messages : "extracted from"
  agent_messages ||--o{ message_deliveries : "delivered to"
  runs ||--o{ agent_decisions : records
  agent_messages |o--o{ agent_decisions : "DECISION_RECORDED"
  runs ||--|{ chat_threads : "streams"
  tasks |o--o{ chat_threads : "task stream"
  chat_threads ||--o{ chat_messages : contains
  agent_messages |o--o{ chat_messages : "projected as"
  chat_messages ||--o| user_directives : "submits"
  runs ||--o{ user_directives : receives
  user_directives ||--o{ directive_impacts : "impact preview"
  tasks ||--o{ directive_impacts : "affected"
  user_directives ||--o{ directive_acknowledgements : "acked by roles"
  agent_messages |o--o{ directive_acknowledgements : "delivery msg"
  approvals |o--o{ user_directives : "CRITICAL_DIRECTIVE"
  checkpoints |o--o{ user_directives : "safe checkpoint"
  runs ||--o{ agent_activity_events : timeline
  cli_executions |o--o{ agent_activity_events : "cli.* events"
```

---

## 7. Code Intelligence и документация

```mermaid
erDiagram
  repositories {
    uuid id PK
  }
  code_index_snapshots {
    uuid id PK
    uuid repository_id FK
    text commit_sha
    int index_version
    uuid parent_snapshot_id FK
    text status
  }
  code_files {
    uuid id PK
    uuid snapshot_id FK
    text path "UK per snapshot"
    text blob_sha
    bool is_test
  }
  code_symbols {
    uuid id PK
    uuid snapshot_id FK
    uuid file_id FK
    text symbol_name
    text symbol_kind
    int start_line
    int end_line
  }
  code_dependencies {
    uuid id PK
    uuid snapshot_id FK
    uuid source_symbol_id FK
    uuid target_symbol_id FK
    text target_ref
    text edge_type
  }
  knowledge_chunks {
    uuid id PK
    text source_type
    uuid source_id "polymorphic"
    uuid snapshot_id FK
    uuid documentation_snapshot_id FK
    text text_ref
    tsvector search_vector
  }
  retrieval_queries {
    uuid id PK
    uuid task_id FK
    uuid task_attempt_id FK
    text strategy
    uuid snapshot_id FK
    text documentation_mode
  }
  retrieval_results {
    uuid query_id PK,FK
    uuid chunk_id PK,FK
    int rank
    bool selected
    bool is_stale
  }
  context_packs {
    uuid id PK
    uuid task_attempt_id FK,UK
    uuid snapshot_id FK
    text source_commit
    int policy_version
    text content_hash
    uuid artifact_id FK
  }
  indexing_jobs {
    uuid id PK
    uuid repository_id FK
    text commit_sha
    text status
    uuid base_snapshot_id FK
    uuid snapshot_id FK
  }
  knowledge_entries {
    uuid id PK
    uuid project_id FK
    text kind
    uuid body_ref FK
  }
  architecture_decisions {
    uuid id PK
    uuid project_id FK
    int adr_number "UK per project"
    text status
    uuid content_ref FK
    uuid superseded_by_id FK
  }
  documentation_sources {
    uuid id PK
    uuid technology_version_id FK
    text source_type
    text uri_or_file_ref "https only"
    text trust_level
    uuid approval_id FK
  }
  documentation_snapshots {
    uuid id PK
    uuid source_id FK
    text content_hash
    text status
    uuid storage_ref FK
    uuid external_action_id FK
  }
  documentation_sections {
    uuid id PK
    uuid snapshot_id FK
    text section_path
    text body_ref
  }
  task_attempts {
    uuid id PK
  }
  artifacts {
    uuid id PK
  }

  repositories ||--o{ code_index_snapshots : indexed
  code_index_snapshots |o--o{ code_index_snapshots : "incremental base"
  code_index_snapshots ||--o{ code_files : contains
  code_files ||--o{ code_symbols : declares
  code_symbols ||--o{ code_dependencies : "source"
  code_symbols |o--o{ code_dependencies : "target"
  code_index_snapshots |o--o{ knowledge_chunks : "anchors code chunks"
  documentation_snapshots |o--o{ knowledge_chunks : "anchors doc chunks"
  retrieval_queries ||--o{ retrieval_results : returns
  knowledge_chunks ||--o{ retrieval_results : "ranked in"
  task_attempts ||--o| context_packs : "gets"
  code_index_snapshots ||--o{ context_packs : "built from"
  artifacts ||--o{ context_packs : stores
  repositories ||--o{ indexing_jobs : schedules
  code_index_snapshots |o--o{ indexing_jobs : produces
  documentation_sources ||--o{ documentation_snapshots : fetched
  documentation_snapshots ||--o{ documentation_sections : splits
  artifacts |o--o{ documentation_snapshots : stores
  artifacts ||--o{ knowledge_entries : body
  artifacts ||--o{ architecture_decisions : content
  architecture_decisions |o--o{ architecture_decisions : "superseded by"
```

---

## 8. Качество, evidence, стандарты

```mermaid
erDiagram
  quality_profiles {
    uuid id PK
    uuid workspace_id "nullable = global"
    text code "basic|strict|critical"
    int version
  }
  quality_gates {
    uuid id PK
    uuid profile_id FK
    text name
    text gate_type
    bool required
  }
  gate_executions {
    uuid id PK
    uuid run_id FK
    uuid task_id FK
    uuid gate_id FK
    bool required "snapshot"
    text status "gate_execution_status"
    text commit_sha
    uuid evidence_id FK "required for PASS/FAIL"
    timestamptz superseded_at
  }
  test_results {
    uuid id PK
    uuid gate_execution_id FK
    text suite
    int passed
    int failed
    int skipped
    uuid report_artifact_id FK
  }
  findings {
    uuid id PK
    uuid run_id FK
    uuid task_id FK
    text source
    uuid source_gate_id FK
    uuid gate_execution_id FK
    text severity
    text status
    text fingerprint "UK per run"
    uuid evidence_id FK
  }
  evidence {
    uuid id PK
    uuid run_id FK
    uuid task_id FK
    text kind
    uuid artifact_id FK
    text producer
    text verification_status
    text commit_sha
    text command
    int exit_code
  }
  evidence_links {
    uuid id PK
    uuid evidence_id FK
    text target_type
    uuid target_id "polymorphic"
    text relation
  }
  artifacts {
    uuid id PK
    text storage_ref UK
    text sha256
    bigint size_bytes
  }
  standards {
    uuid id PK
    text code
    text edition
  }
  standard_controls {
    uuid id PK
    uuid standard_id FK
    text control_code
    text verification_method
  }
  project_controls {
    uuid project_id PK,FK
    uuid control_id PK,FK
    text applicability
    uuid approved_by FK
  }
  compliance_results {
    uuid id PK
    uuid run_id FK
    uuid control_id FK
    text status "compliance_status"
    uuid evidence_id FK
    uuid gate_execution_id FK
  }
  runs {
    uuid id PK
  }
  projects {
    uuid id PK
  }

  quality_profiles ||--|{ quality_gates : defines
  quality_gates ||--o{ gate_executions : "executed as"
  runs ||--o{ gate_executions : verifies
  gate_executions ||--o{ test_results : reports
  gate_executions |o--o{ findings : produces
  quality_gates |o--o{ findings : "source gate"
  evidence |o--o{ gate_executions : "proves"
  evidence |o--o{ findings : "proves fix"
  artifacts ||--o{ evidence : "content + checksum"
  artifacts ||--o{ test_results : report
  evidence ||--o{ evidence_links : traces
  runs ||--o{ evidence : collects
  runs ||--o{ findings : has
  standards ||--|{ standard_controls : contains
  standard_controls ||--o{ project_controls : "applied to"
  projects ||--o{ project_controls : "applicability"
  standard_controls ||--o{ compliance_results : "checked"
  runs ||--o{ compliance_results : "compliance matrix"
  evidence |o--o{ compliance_results : "proves"
  gate_executions |o--o{ compliance_results : "via gate"
```

Трассировка ТЗ §13: `standards → standard_controls → project_controls (applicability, agent_role) → gate_executions (check) → evidence → compliance_results (verdict)`.

---

## 9. Approvals, политики, надёжность

```mermaid
erDiagram
  policies {
    uuid id PK
    uuid workspace_id "nullable = global"
    uuid project_id FK
    text policy_type
    int version
    bool active
  }
  approvals {
    uuid id PK
    uuid project_id FK
    uuid run_id FK
    uuid task_id FK
    text action_type "approval_action_type"
    text status "approval_status"
    text required_role
    text subject_hash
    uuid requested_by FK
    uuid decided_by FK
    bigint version
  }
  external_actions {
    uuid id PK
    uuid run_id FK
    uuid task_id FK
    text action_type
    text idempotency_key UK
    text status "PLANNED..CONFIRMED"
    uuid approval_id FK
    text request_hash
    bigint fencing_token
  }
  checkpoints {
    uuid id PK
    uuid run_id FK
    uuid task_id FK
    bigint state_version
    uuid state_ref FK
    uuid worktree_id FK
  }
  outbox_events {
    uuid id PK
    timestamptz created_at PK "partition key"
    uuid workspace_id
    uuid run_id
    text event_type
    timestamptz published_at
  }
  run_event_log {
    uuid run_id PK,FK
    bigint seq PK "SSE id"
    uuid event_id UK
    text event_type
  }
  processed_events {
    text consumer PK
    uuid event_id PK
  }
  worker_leases {
    uuid id PK
    text resource_type
    uuid resource_id "UK with type"
    text worker_id
    bigint fencing_token "monotonic seq"
    timestamptz lease_until
  }
  runs {
    uuid id PK
    bigint last_event_seq
  }
  tasks {
    uuid id PK
  }
  projects {
    uuid id PK
  }
  users {
    uuid id PK
  }
  artifacts {
    uuid id PK
  }
  worker_hosts {
    uuid id PK
  }

  projects |o--o{ policies : "project override"
  projects ||--o{ approvals : scopes
  runs |o--o{ approvals : requires
  tasks |o--o{ approvals : "task-level"
  users |o--o{ approvals : requests
  users |o--o{ approvals : decides
  approvals |o--o{ external_actions : authorizes
  runs |o--o{ external_actions : "side effects"
  runs ||--o{ checkpoints : "safe points"
  artifacts ||--o{ checkpoints : "state blob"
  runs ||--o{ run_event_log : "SSE replay"
  worker_hosts |o--o{ worker_leases : holds
```

`outbox_events` → `run_event_log` — логическая связь (`run_event_log.event_id = outbox_events.id`), без FK; `processed_events.event_id` — также без FK (outbox партиционирован и очищается). `worker_leases.resource_id` — полиморфная ссылка на `tasks`/`runs`/`cli_executions`/`indexing_jobs`/`external_actions`.

---

## 10. GitHub и аудит

```mermaid
erDiagram
  workspaces {
    uuid id PK
  }
  github_installations {
    uuid id PK
    uuid workspace_id FK
    bigint installation_id UK
    text account_login
    text status
  }
  github_webhook_deliveries {
    uuid id PK
    uuid delivery_guid UK
    text event
    bigint installation_id "no FK (global)"
    bool signature_valid
    text status
  }
  repositories {
    uuid id PK
    uuid github_installation_id FK
  }
  pull_requests {
    uuid id PK
    uuid repository_id FK
    uuid run_id FK
    int pr_number "UK per repo"
    text head_sha
    text state
    uuid create_action_id FK
    uuid merge_action_id FK
  }
  external_actions {
    uuid id PK
  }
  runs {
    uuid id PK
  }
  audit_events {
    uuid id PK
    timestamptz created_at PK "partition key"
    uuid workspace_id "nullable, no FK"
    text actor_type
    uuid actor_id
    text action
    text resource_type
    uuid resource_id
  }

  workspaces ||--o{ github_installations : connects
  github_installations |o--o{ repositories : "access to"
  repositories ||--o{ pull_requests : has
  runs |o--o{ pull_requests : opened
  external_actions |o--o| pull_requests : "pr_create"
  external_actions |o--o| pull_requests : "pr_merge (approved)"
```

`audit_events` намеренно без FK (переживает удаление субъектов, append-only, партиционирована). `github_webhook_deliveries` → workspace определяется функцией `sys_resolve_installation_workspace(installation_id)` после проверки подписи.

---

## 11. Ключевые инварианты

| # | Инвариант | Источник | Механизм в БД | Где описано |
|---|---|---|---|---|
| I-01 | Межтенантные ссылки невозможны | ТЗ §7.1 | составные FK `(workspace_id, parent_id)`; `workspace_id` неизменяем | DD §1.3, §18.13 |
| I-02 | Чужой workspace не виден и не изменяем | ТЗ §15, AC-12 | RLS `ENABLE`+`FORCE`, контекст `SET LOCAL`, роли без `BYPASSRLS` | DD §19 |
| I-03 | Зависимость задач только внутри одного run | ТЗ §7.3 | `run_id` в обоих составных FK `task_dependencies` + защитный триггер | DD §7.3, §18.1 |
| I-04 | DAG без циклов; утверждённый план неизменяем | ТЗ §6, AC-02 | транзакционное утверждение с блокировкой run; deferred constraint trigger (recursive CTE `CYCLE`); запрет изменения рёбер утверждённой версии | DD §18.2 |
| I-05 | Только разрешённые переходы FSM (Run/Task/Execution/Directive/Approval/Ledger) | ТЗ §6 | `allowed_transitions` + `trg_*_transition`; основной контроль — код (ADR-0007) | DD §18.3 |
| I-06 | `COMPLETED` только при PASS обязательных gates, всех задачах SUCCEEDED, отсутствии PENDING approvals | ТЗ §6, §14, AC-11 | `trg_runs_completion_guard` + `FOR SHARE` блокировки писателей | DD §18.4 |
| I-07 | `PASS` gate только с сохранённым VERIFIED evidence; `SKIPPED ≠ PASS` | ТЗ §14, §9 | `ck_gate_executions_pass_evidence`, `trg_gate_executions_evidence_verified` | DD §12.4 |
| I-08 | Evidence процесса содержит commit, команду, exit code, время; checksum — в artifact | AC-14 | `ck_evidence_process_fields`; `artifacts.sha256 NOT NULL` | DD §12.7 |
| I-09 | Слова агента не являются verified-доказательством | ТЗ §2 п.4 | `ck_evidence_verified_producer`, `ck_evidence_agent_claim` | DD §12.7 |
| I-10 | Optimistic locking агрегатов | ТЗ §6, §7.1 | `version` + `trg_fn_enforce_version`; для версий политик — UNIQUE `(…, version)` | DD §1.5, §14.1 |
| I-11 | Аренда с монотонным fencing token; запись результата только владельцем актуального токена | ТЗ §16 | `worker_lease_fencing_seq`, `trg_worker_leases_fencing_monotonic`, условие `lease_fencing_token = :token` | DD §15.4, §18.6 |
| I-12 | Подтверждённое внешнее действие не повторяется | ТЗ §6, AC-09, ADR-0012 | `external_actions.idempotency_key UNIQUE`; FSM ledger; `CONFIRMED` терминален (кроме `COMPENSATED`) | DD §15.5 |
| I-13 | Merge / production deploy / destructive migration — только с approval конкретного предмета | ТЗ §15, AC-13 | `ck_external_actions_approval_required`, `trg_external_actions_approval_gate` (`subject_hash = request_hash`) | DD §15.5, §18.7 |
| I-14 | Четыре глаза: инициатор не утверждает своё критическое действие | бриф | `ck_approvals_four_eyes`, `trg_approvals_decider_role` | DD §14.2, §18.7 |
| I-15 | Самопроверка не маркируется как независимое ревью | ТЗ §4 | `trg_agent_assignments_independence` (разные провайдеры или approval `SELF_REVIEW_FALLBACK`) | DD §18.8 |
| I-16 | ACP-сообщение ≤ 32 KiB | ТЗ §10 | `ck_agent_messages_payload_size` (`octet_length(payload::text) <= 32768`) | DD §9.1 |
| I-17 | SSE без дублей при переподключении | AC-08, ADR-0011 | `run_event_log` PK `(run_id, seq)`, `UNIQUE (event_id)`, seq из `runs.last_event_seq` | DD §15.6 |
| I-18 | Идемпотентные потребители | ТЗ §10, §16 | `processed_events` PK `(consumer, event_id)`; `message_deliveries` PK `(message_id, consumer_id)` | DD §15.3, §9.2 |
| I-19 | Аудит append-only | ТЗ §15 | `REVOKE UPDATE/DELETE/TRUNCATE` + `trg_audit_events_append_only`; удаление только партиций после архива | DD §17.1, §18.5 |
| I-20 | Секреты и CLI-токены не хранятся | ТЗ §7.1 | нет колонок; schema-lint; redaction; параметры не логируются | DD §18.9 |
| I-21 | В workspace всегда ≥ 1 owner; проектные роли только понижают | бриф | deferred trigger `trg_workspace_members_last_owner`; `trg_project_member_overrides_downgrade_only` | DD §18.10 |
| I-22 | Фрагмент кода привязан к commit-снимку; устаревший помечается | ТЗ §12 | `ck_knowledge_chunks_freshness_anchor`; `retrieval_results.is_stale` | DD §10.5, §10.7 |
| I-23 | `OFF` не использует документацию | ТЗ §12, AC-10 | `ck_retrieval_queries_docs_off` | DD §10.6 |
| I-24 | Риск принимает / находку закрывает как ложную только человек | ТЗ §10 | `ck_agent_decisions_risk_by_user`, `ck_findings_human_resolution` | DD §9.3, §12.6 |
| I-25 | Токены/лимиты CLI показываются только если реально предоставлены | ТЗ §9, §16 | `ck_usage_metrics_value` (`not_exposed_by_cli` ⇔ `metric_value IS NULL`) | DD §8.7 |

(DD — [DATABASE_DESIGN.md](DATABASE_DESIGN.md).)
