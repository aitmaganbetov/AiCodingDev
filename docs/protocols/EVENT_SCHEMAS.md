# Event Schemas v1.0

**Статус:** Phase 0, проект на утверждение. Ничего из описанного не реализовано.
**Требования:** ТЗ §6, §7.2 (`outbox_events`, `agent_activity_events`, `chat_threads`, `chat_messages`, `user_directives`, `directive_impacts`, `directive_acknowledgements`, `audit_events`), §8 (`/runs/{run_id}/events`, `/activity`, `/chat`), §8.2, §8.3, §11, §16, §19 AC-05/AC-06/AC-08.
**Решения:** ADR-0002 (transactional outbox), ADR-0007, ADR-0011 (SSE из `run_event_log`, Redis pub/sub только wake-up), ADR-0013. Файл Phase 1: `packages/contracts/events.schema.json` (ТЗ §21).
**Версия схем:** `schema_version: "1.0"`.

---

## 1. Три класса событий

| Класс | Назначение | Хранилище | Потребители | Гарантии |
|---|---|---|---|---|
| **Domain / outbox events** | Факты изменения агрегатов, записанные в одной транзакции с изменением состояния (ТЗ §16) | `outbox_events` → Redis/Dramatiq | внутренние воркеры (orchestrator, projectors, verify, index, analytics) | at-least-once, идемпотентные потребители (`processed_events`) |
| **SSE events** | Публичный поток run для UI (ТЗ §8.3) | `run_event_log` (PK `run_id, seq`) | браузер через `GET /api/v1/runs/{run_id}/events` | порядок по `seq` в run, replay по `Last-Event-ID`, дедупликация по `seq`/`event_id` |
| **Live Agent Chat** | Пользовательские потоки чата, поручения, их статусы (ТЗ §11) | `chat_threads`, `chat_messages`, `user_directives`, `directive_impacts`, `directive_acknowledgements` | UI Live Agent Chat; SSE `chat.message`, `directive.*` | проекция идемпотентна по `source_event_id` |

Кроме того, `agent_activity_events` — детальная история run (ТЗ §8 `/activity`), включая нормализованные CLI-события; `audit_events` — append-only журнал действий (вне этого документа, см. SECURITY_ARCHITECTURE).

```mermaid
flowchart LR
  TX["Транзакция домена: UPDATE состояния + INSERT outbox_events"] --> OB[(outbox_events)]
  OB --> D[worker-outbox dispatcher]
  D -->|Dramatiq| W1[orchestrator / verify / index consumers]
  D -->|"1 tx: run_event_log(seq) + published_at"| REL[(run_event_log)]
  D -->|"PUBLISH run:{run_id} wake-up"| PS[(Redis pub/sub)]
  PS --> API[api SSE handler]
  REL --> API
  API -->|"id: seq"| B[Browser]
  D -->|Dramatiq| CP[chat.projector] --> CH[(chat_messages)] --> OB
```

## 2. Domain / outbox events

### 2.1 Конверт outbox-события

Колонки `outbox_events` (ТЗ §7.2) + `workspace_id`, `run_id` (дополнение для RLS и порядка). Поле `payload` хранит объект ниже целиком (это же тело Dramatiq-сообщения).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://aicodingdev.local/contracts/events/1.0/outbox-envelope.schema.json",
  "title": "OutboxEventEnvelope",
  "type": "object",
  "additionalProperties": false,
  "required": ["event_id", "event_type", "schema_version", "aggregate_type", "aggregate_id", "workspace_id", "occurred_at", "trace", "data"],
  "properties": {
    "event_id": {"type": "string", "format": "uuid", "description": "= outbox_events.id (UUIDv7)"},
    "event_type": {"type": "string", "pattern": "^[a-z_]+\\.[a-z_]+$"},
    "schema_version": {"const": "1.0"},
    "aggregate_type": {"enum": ["run", "task", "task_attempt", "cli_execution", "agent_message", "chat_message", "directive", "approval", "gate_execution", "evidence", "finding", "compliance_result", "cli_installation", "routing_decision", "checkpoint", "external_action", "code_index_snapshot", "context_pack", "documentation_snapshot", "github_webhook_delivery"]},
    "aggregate_id": {"type": "string", "format": "uuid"},
    "aggregate_version": {"type": ["integer", "null"], "minimum": 0},
    "workspace_id": {"type": ["string", "null"], "format": "uuid", "description": "null только для глобальных агрегатов (cli_installation)"},
    "run_id": {"type": ["string", "null"], "format": "uuid"},
    "occurred_at": {"type": "string", "format": "date-time"},
    "trace": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "task_id": {"type": ["string", "null"], "format": "uuid"},
        "execution_id": {"type": ["string", "null"], "format": "uuid"},
        "correlation_id": {"type": ["string", "null"], "format": "uuid"},
        "causation_id": {"type": ["string", "null"], "format": "uuid", "description": "event_id/команда, вызвавшая это событие"},
        "actor_type": {"enum": ["user", "agent", "system", "github"]},
        "actor_id": {"type": ["string", "null"]},
        "otel_trace_id": {"type": ["string", "null"], "pattern": "^[0-9a-f]{32}$"}
      }
    },
    "data": {"type": "object"}
  }
}
```

### 2.2 Каталог

Колонка «SSE» указывает проекцию в `run_event_log` (раздел 3). Поля `data` перечислены кратко; все ID — UUID; тексты — санитизированы.

| `event_type` | Агрегат | Триггер (в одной tx с) | Поля `data` | SSE |
|---|---|---|---|---|
| `run.created` | run | `INSERT runs` | `mode`, `objective_excerpt`, `quality_profile`, `approval_policy`, `policy_snapshot_hash` | `run.state_changed` (from=null) |
| `run.state_changed` | run | переход Run FSM | `from_status`, `to_status`, `blocked_reason?`, `run_version`, `reason` | `run.state_changed` |
| `run.plan_materialized` | run | утверждение DAG (`plan_version`) | `plan_version`, `task_count`, `edge_count` | — (видно через `task.state_changed`) |
| `run.terminal` | run | переход в `COMPLETED`/`FAILED`/`CANCELLED` | `final_status`, `gate_summary`, `open_findings_by_severity`, `report_artifact_id?` | `run.completed` |
| `task.created` | task | материализация плана, `NEW_TASK`, replan | `agent_role`, `complexity`, `risk_level`, `depends_on[]`, `plan_version` | `task.state_changed` (from=null, to=PENDING) |
| `task.state_changed` | task | переход Task FSM | `from_status`, `to_status`, `blocked_reason?`, `task_version`, `attempt_no?`, `failure_category?` | `task.state_changed` |
| `task_attempt.started` | task_attempt | `INSERT task_attempts` | `task_id`, `attempt_no`, `provider`, `routing_decision_id` | — |
| `task_attempt.finished` | task_attempt | завершение попытки | `status`, `failure_category?`, `execution_id?` | — |
| `routing.decision_recorded` | routing_decision | `INSERT routing_decisions` | `task_id`, `selected_provider`, `score_breakdown`, `policy_version`, `review_independence?` | — (в Task Detail через API) |
| `cli_execution.state_changed` | cli_execution | переход Execution FSM | `from_status`, `to_status`, `failure_category?`, `exit_code?` | — |
| `cli_execution.output_batch` | cli_execution | worker-exec, батч ≤1 с нормализованных событий (после записи в `agent_activity_events`) | `events[]` (санитизированные NormalizedCliEvent) | `cli.output` (по одному SSE-событию на нормализованное событие) |
| `cli_execution.finished` | cli_execution | сохранение `ExecutionResult` | `status`, `execution_status`, `error_code?`, `artifact_ids[]`, `usage_source` | — |
| `acp.message_accepted` | agent_message | шаг 7 конвейера ACP | `type`, `from`, `to`, `correlation_id`, `reply_to_id`, `summary` | `agent.message` |
| `acp.message_rejected` | agent_message | отказ валидации | `rejection_code`, `type?`, `from_role`, `payload_sha256` | — (только activity/audit) |
| `chat.message_created` | chat_message | `INSERT chat_messages` | `thread_id`, `message_kind`, `sender_type`, `source_event_id?` | `chat.message` |
| `directive.received` | directive | `POST /runs/{id}/directives` | `directive_type`, `target`, `requires_impact_preview` | `directive.state_changed` |
| `directive.state_changed` | directive | переход Directive FSM | `from_status`, `to_status`, `reason?` | `directive.state_changed` |
| `directive.impact_ready` | directive | `INSERT directive_impacts` завершён | `impacts[]`, `requires_confirmation`, `approval_id?` | `directive.impact_ready` |
| `directive.ack_changed` | directive | изменение `directive_acknowledgements` | `recipient_role`, `ack_status`, `execution_id?` | `directive.state_changed` (поле `recipients`) |
| `approval.requested` | approval | `INSERT approvals (PENDING)` | `action_type`, `task_id?`, `required_role`, `expires_at`, `summary` | `approval.requested` |
| `approval.decided` | approval | `PENDING → APPROVED/REJECTED/EXPIRED/CANCELLED` | `status`, `decided_by?`, `decision_reason?` | `approval.decided` |
| `gate.started` | gate_execution | `PENDING → RUNNING` | `gate_type`, `commit_sha`, `task_id?` | — |
| `gate.completed` | gate_execution | терминальный статус гейта | `status`, `gate_type`, `required`, `evidence_id?`, `findings_count` | `gate.completed` |
| `evidence.recorded` | evidence | `INSERT evidence` | `kind`, `producer`, `verification_status`, `artifact_sha256` | — |
| `evidence.verification_changed` | evidence | `PENDING → VERIFIED/REJECTED/UNVERIFIED` | `from`, `to` | — |
| `finding.created` | finding | `INSERT findings` | `severity`, `rule_id`, `file_path?`, `source_gate_id?` | — (агрегат в `gate.completed`, ACP `FINDING` → `agent.message`) |
| `finding.status_changed` | finding | смена `finding_status` | `from`, `to`, `actor` | — |
| `compliance.result_recorded` | compliance_result | `INSERT compliance_results` | `control_id`, `status`, `evidence_id?` | — |
| `provider.health_changed` | cli_installation | новое наблюдение `provider_health`, если изменилось `availability`/`auth_status` | `provider`, `availability`, `auth_status`, `error_category?`, `cooldown_until?` | — (глобальный; UI CLI Providers опрашивает API; затронутые run получают `task.state_changed`) |
| `provider.auth_restored` | cli_installation | `check_auth` → `AUTHENTICATED` после `AUTH_REQUIRED` | `provider`, `installation_id` | — |
| `checkpoint.created` | checkpoint | `INSERT checkpoints` | `state_version`, `reason` (`directive`,`pause`,`periodic`) | — |
| `external_action.state_changed` | external_action | `PLANNED→EXECUTING→CONFIRMED/FAILED/COMPENSATED` | `action_type`, `status`, `idempotency_key`, `external_ref?` | — (видно через approvals/run) |
| `code_index.snapshot_ready` | code_index_snapshot | индекс построен | `repository_id`, `commit_sha`, `index_version` | — |
| `context_pack.created` | context_pack | `INSERT context_packs` | `task_attempt_id`, `content_hash`, `size_bytes`, `stale_items` | — |
| `documentation.snapshot_fetched` | documentation_snapshot | загрузка завершена | `source_id`, `status`, `content_hash` | — |
| `github.webhook_received` | github_webhook_delivery | `INSERT github_webhook_deliveries` (подпись валидна) | `event`, `delivery_guid` | — |

Правило: событие без строки в этом каталоге не может быть записано в outbox (валидация `event_type` по реестру в коде; CI-тест сверки реестра и каталога).

## 3. SSE events

### 3.1 Протокол

- Endpoint: `GET /api/v1/runs/{run_id}/events` (`text/event-stream`), авторизация cookie-сессией + RBAC: любой член workspace с ролью ≥ `viewer` (ТЗ §8.3 «разграничение доступа»); RLS по `workspace_id`.
- Формат кадра:

```text
id: 1042
event: task.state_changed
data: {"event_id":"…","run_id":"…","seq":1042,"type":"task.state_changed","timestamp":"…","schema_version":"1.0","payload":{…}}

```

- `id` = `seq` (bigint, монотонно возрастает в рамках run, ADR-0011). Переподключение: браузер передаёт `Last-Event-ID`; сервер отдаёт `run_event_log WHERE run_id=? AND seq > ?` по возрастанию, затем переходит в live-режим (ожидание wake-up из Redis pub/sub + опрос раз в 5 с как страховка).
- Если `Last-Event-ID` меньше минимального сохранённого `seq` (retention), сервер первым отправляет синтетический `run.state_changed` с `payload.resync = true` (снимок текущего статуса, `id` = текущий максимальный `seq`), после чего клиент обязан перезагрузить состояние через REST.
- `heartbeat` каждые 15 с; **не** сохраняется в `run_event_log`, отправляется без строки `id:` (чтобы не сдвигать `Last-Event-ID`).
- Ограничения: ≤ 3 одновременных SSE-соединения на пользователя на run; `cli.output` поток прореживается сервером до ≤ 20 событий/с на соединение (лишние объединяются; полный поток — в Task Detail через `/activity` с пагинацией).

### 3.2 Схема SSE-события

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://aicodingdev.local/contracts/events/1.0/sse-event.schema.json",
  "title": "SseEvent",
  "type": "object",
  "additionalProperties": false,
  "required": ["event_id", "run_id", "seq", "type", "timestamp", "schema_version", "payload"],
  "properties": {
    "event_id": {"type": "string", "format": "uuid"},
    "run_id": {"type": "string", "format": "uuid"},
    "seq": {"type": ["integer", "null"], "minimum": 1, "description": "null только для heartbeat"},
    "type": {"enum": ["run.state_changed", "task.state_changed", "agent.message", "cli.output", "directive.impact_ready", "directive.state_changed", "approval.requested", "approval.decided", "gate.completed", "run.completed", "chat.message", "heartbeat"]},
    "timestamp": {"type": "string", "format": "date-time"},
    "schema_version": {"const": "1.0"},
    "source_event_id": {"type": ["string", "null"], "format": "uuid", "description": "outbox event_id, из которого спроецировано"},
    "payload": {"type": "object"}
  },
  "allOf": [
    {"if": {"properties": {"type": {"const": "heartbeat"}}}, "then": {"properties": {"seq": {"type": "null"}, "payload": {"$ref": "#/$defs/heartbeat"}}}, "else": {"properties": {"seq": {"type": "integer"}}}},
    {"if": {"properties": {"type": {"const": "run.state_changed"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/run_state_changed"}}}},
    {"if": {"properties": {"type": {"const": "task.state_changed"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/task_state_changed"}}}},
    {"if": {"properties": {"type": {"const": "agent.message"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/agent_message"}}}},
    {"if": {"properties": {"type": {"const": "cli.output"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/cli_output"}}}},
    {"if": {"properties": {"type": {"const": "directive.impact_ready"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/directive_impact_ready"}}}},
    {"if": {"properties": {"type": {"const": "directive.state_changed"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/directive_state_changed"}}}},
    {"if": {"properties": {"type": {"const": "approval.requested"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/approval_requested"}}}},
    {"if": {"properties": {"type": {"const": "approval.decided"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/approval_decided"}}}},
    {"if": {"properties": {"type": {"const": "gate.completed"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/gate_completed"}}}},
    {"if": {"properties": {"type": {"const": "run.completed"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/run_completed"}}}},
    {"if": {"properties": {"type": {"const": "chat.message"}}}, "then": {"properties": {"payload": {"$ref": "#/$defs/chat_message"}}}}
  ],
  "$defs": {
    "uuid": {"type": "string", "format": "uuid"},
    "run_status": {"enum": ["CREATED", "DISCOVERY", "PLANNED", "AWAITING_APPROVAL", "RUNNING", "VERIFYING", "COMPLETED", "PAUSED", "BLOCKED", "FAILED", "CANCELLED"]},
    "task_status": {"enum": ["PENDING", "READY", "LEASED", "RUNNING", "VERIFYING", "SUCCEEDED", "BLOCKED", "RETRY_WAIT", "FAILED", "CANCELLED"]},
    "blocked_reason": {"enum": ["APPROVAL_REQUIRED", "PROVIDER_UNAVAILABLE", "AUTH_REQUIRED", "DIRECTIVE_REPLAN", "RUN_PAUSED", "DOCS_REQUIRED", "MANUAL_REVIEW", "REVIEW_LOOP_EXHAUSTED", "DEPENDENCY_FAILED", null]},
    "agent_role": {"enum": ["requirements", "architect", "planner", "developer", "reviewer", "security", "qa", "database", "performance", "devops", "maintenance", "documentation"]},
    "acp_participant": {"enum": ["requirements", "architect", "planner", "developer", "reviewer", "security", "qa", "database", "performance", "devops", "maintenance", "documentation", "orchestrator", "user"]},
    "provider": {"enum": ["codex", "claude"]},
    "directive_status": {"enum": ["RECEIVED", "IMPACT_ANALYSIS_PENDING", "IMPACT_READY", "AWAITING_CONFIRMATION", "CONFIRMED", "CHECKPOINTING", "REPLANNING", "DISPATCHED", "ACKNOWLEDGED", "COMPLETED", "REJECTED", "CANCELLED", "FAILED"]},
    "severity_counts": {
      "type": "object", "additionalProperties": false,
      "properties": {"info": {"type": "integer", "minimum": 0}, "low": {"type": "integer", "minimum": 0}, "medium": {"type": "integer", "minimum": 0}, "high": {"type": "integer", "minimum": 0}, "critical": {"type": "integer", "minimum": 0}}
    },
    "heartbeat": {
      "type": "object", "additionalProperties": false,
      "required": ["server_time", "last_seq"],
      "properties": {"server_time": {"type": "string", "format": "date-time"}, "last_seq": {"type": "integer", "minimum": 0}}
    },
    "run_state_changed": {
      "type": "object", "additionalProperties": false,
      "required": ["from_status", "to_status", "run_version"],
      "properties": {
        "from_status": {"anyOf": [{"$ref": "#/$defs/run_status"}, {"type": "null"}]},
        "to_status": {"$ref": "#/$defs/run_status"},
        "blocked_reason": {"$ref": "#/$defs/blocked_reason"},
        "run_version": {"type": "integer", "minimum": 0},
        "reason": {"type": "string", "maxLength": 1000},
        "resync": {"type": "boolean"}
      }
    },
    "task_state_changed": {
      "type": "object", "additionalProperties": false,
      "required": ["task_id", "agent_role", "from_status", "to_status", "task_version"],
      "properties": {
        "task_id": {"$ref": "#/$defs/uuid"},
        "title": {"type": "string", "maxLength": 500},
        "agent_role": {"$ref": "#/$defs/agent_role"},
        "from_status": {"anyOf": [{"$ref": "#/$defs/task_status"}, {"type": "null"}]},
        "to_status": {"$ref": "#/$defs/task_status"},
        "blocked_reason": {"$ref": "#/$defs/blocked_reason"},
        "task_version": {"type": "integer", "minimum": 0},
        "attempt_no": {"type": ["integer", "null"], "minimum": 1},
        "provider": {"anyOf": [{"$ref": "#/$defs/provider"}, {"type": "null"}]},
        "complexity": {"enum": ["L1", "L2", "L3", "L4", "L5"]},
        "failure_category": {"type": ["string", "null"], "enum": ["CLI_NOT_INSTALLED", "AUTH_REQUIRED", "RATE_LIMITED", "TIMEOUT", "SANDBOX_DENIED", "PROCESS_FAILED", "OUTPUT_PARSE_ERROR", "UNSUPPORTED_VERSION", "CANCELLED", "GATE_FAILED", "REVIEW_REJECTED", "LEASE_LOST", "WORKER_CRASH", "POLICY_DENIED", "INTERNAL_ERROR", null]}
      }
    },
    "agent_message": {
      "type": "object", "additionalProperties": false,
      "required": ["message_id", "message_type", "from", "to", "correlation_id", "summary"],
      "properties": {
        "message_id": {"$ref": "#/$defs/uuid"},
        "message_type": {"enum": ["TASK_ASSIGN", "HANDOFF", "QUESTION", "ANSWER", "REVIEW_REQUEST", "FINDING", "FIX_REQUEST", "EVIDENCE_SUBMITTED", "DECISION_PROPOSAL", "DECISION_RECORDED", "BLOCKED", "TASK_COMPLETED"]},
        "task_id": {"type": ["string", "null"], "format": "uuid"},
        "from": {
          "type": "object", "additionalProperties": false, "required": ["role"],
          "properties": {"role": {"$ref": "#/$defs/acp_participant"}, "provider": {"$ref": "#/$defs/provider"}}
        },
        "to": {
          "type": "object", "additionalProperties": false, "required": ["role"],
          "properties": {"role": {"$ref": "#/$defs/acp_participant"}}
        },
        "correlation_id": {"$ref": "#/$defs/uuid"},
        "reply_to_id": {"type": ["string", "null"], "format": "uuid"},
        "summary": {"type": "string", "maxLength": 1000, "description": "санитизированная выдержка; полный payload — GET /tasks/{task_id}/messages"},
        "severity": {"enum": ["info", "low", "medium", "high", "critical"]},
        "suspected_injection": {"type": "boolean"}
      }
    },
    "cli_output": {
      "type": "object", "additionalProperties": false,
      "required": ["execution_id", "task_id", "provider", "agent_role", "cli_seq", "cli_event_type"],
      "properties": {
        "execution_id": {"$ref": "#/$defs/uuid"},
        "task_id": {"$ref": "#/$defs/uuid"},
        "provider": {"$ref": "#/$defs/provider"},
        "agent_role": {"$ref": "#/$defs/agent_role"},
        "cli_seq": {"type": "integer", "minimum": 1},
        "cli_event_type": {"enum": ["started", "output", "tool_event", "artifact", "warning", "finished"]},
        "stream": {"enum": ["stdout", "stderr", "agent_message"]},
        "text": {"type": "string", "maxLength": 4096},
        "redactions": {"type": "integer", "minimum": 0},
        "truncated": {"type": "boolean"},
        "coalesced_count": {"type": "integer", "minimum": 1},
        "tool_kind": {"enum": ["file_read", "file_write", "file_patch", "command", "search", "network", "other"]},
        "tool_phase": {"enum": ["requested", "started", "completed", "denied", "failed"]},
        "target": {"type": "string", "maxLength": 1024},
        "artifact_id": {"$ref": "#/$defs/uuid"},
        "warning_code": {"type": "string", "maxLength": 50},
        "execution_status": {"enum": ["EXITED", "TIMED_OUT", "CANCELLED", "CRASHED", "AUTH_REQUIRED", "RATE_LIMITED"]}
      }
    },
    "directive_impact_ready": {
      "type": "object", "additionalProperties": false,
      "required": ["directive_id", "impacts", "requires_confirmation", "expected_run_version"],
      "properties": {
        "directive_id": {"$ref": "#/$defs/uuid"},
        "summary": {"type": "string", "maxLength": 2000},
        "impacts": {
          "type": "array", "maxItems": 500,
          "items": {
            "type": "object", "additionalProperties": false,
            "required": ["task_id", "impact_type"],
            "properties": {
              "task_id": {"type": ["string", "null"], "format": "uuid", "description": "null для TASK_ADD (новая задача ещё не создана)"},
              "impact_type": {"enum": ["NONE", "CONTEXT_ONLY", "REPLAN_REQUIRED", "TASK_CANCEL", "TASK_ADD", "RESTART_REQUIRED"]},
              "resolution": {"type": "string", "maxLength": 500}
            }
          }
        },
        "requires_confirmation": {"type": "boolean"},
        "approval_id": {"type": ["string", "null"], "format": "uuid"},
        "expected_run_version": {"type": "integer", "minimum": 0}
      }
    },
    "directive_state_changed": {
      "type": "object", "additionalProperties": false,
      "required": ["directive_id", "directive_type", "from_status", "to_status"],
      "properties": {
        "directive_id": {"$ref": "#/$defs/uuid"},
        "directive_type": {"enum": ["ASK", "NEW_TASK", "CHANGE_REQUEST", "STOP", "PAUSE", "RESUME", "REVIEW", "APPROVE", "REJECT"]},
        "from_status": {"anyOf": [{"$ref": "#/$defs/directive_status"}, {"type": "null"}]},
        "to_status": {"$ref": "#/$defs/directive_status"},
        "reason": {"type": "string", "maxLength": 1000},
        "recipients": {
          "type": "array", "maxItems": 12,
          "items": {
            "type": "object", "additionalProperties": false,
            "required": ["role", "ack_status"],
            "properties": {
              "role": {"$ref": "#/$defs/agent_role"},
              "ack_status": {"enum": ["PENDING", "DELIVERED", "ACKNOWLEDGED", "COMPLETED", "FAILED"]},
              "execution_id": {"type": ["string", "null"], "format": "uuid"},
              "at": {"type": ["string", "null"], "format": "date-time"}
            }
          }
        }
      }
    },
    "approval_requested": {
      "type": "object", "additionalProperties": false,
      "required": ["approval_id", "action_type", "summary", "required_role", "expires_at"],
      "properties": {
        "approval_id": {"$ref": "#/$defs/uuid"},
        "action_type": {"enum": ["PLAN_APPROVAL", "MERGE", "PRODUCTION_DEPLOY", "DESTRUCTIVE_MIGRATION", "DATA_DELETION", "SECRET_CHANGE", "UNRESTRICTED_NETWORK", "DOCS_DOWNLOAD", "CRITICAL_DIRECTIVE", "MANUAL_REVIEW", "SELF_REVIEW_FALLBACK", "LOCAL_EXECUTION_FALLBACK"]},
        "task_id": {"type": ["string", "null"], "format": "uuid"},
        "summary": {"type": "string", "maxLength": 2000},
        "required_role": {"enum": ["owner", "admin", "developer", "reviewer"]},
        "requested_by": {
          "type": "object", "additionalProperties": false, "required": ["type"],
          "properties": {"type": {"enum": ["user", "agent", "system"]}, "id": {"type": ["string", "null"]}}
        },
        "expires_at": {"type": "string", "format": "date-time"},
        "evidence_refs": {"type": "array", "maxItems": 50, "items": {"type": "string"}}
      }
    },
    "approval_decided": {
      "type": "object", "additionalProperties": false,
      "required": ["approval_id", "action_type", "status"],
      "properties": {
        "approval_id": {"$ref": "#/$defs/uuid"},
        "action_type": {"enum": ["PLAN_APPROVAL", "MERGE", "PRODUCTION_DEPLOY", "DESTRUCTIVE_MIGRATION", "DATA_DELETION", "SECRET_CHANGE", "UNRESTRICTED_NETWORK", "DOCS_DOWNLOAD", "CRITICAL_DIRECTIVE", "MANUAL_REVIEW", "SELF_REVIEW_FALLBACK", "LOCAL_EXECUTION_FALLBACK"]},
        "status": {"enum": ["APPROVED", "REJECTED", "EXPIRED", "CANCELLED"]},
        "decided_by": {"type": ["string", "null"], "format": "uuid"},
        "decision_reason": {"type": ["string", "null"], "maxLength": 2000}
      }
    },
    "gate_completed": {
      "type": "object", "additionalProperties": false,
      "required": ["gate_execution_id", "gate_type", "status", "required", "commit_sha"],
      "properties": {
        "gate_execution_id": {"$ref": "#/$defs/uuid"},
        "gate_type": {"type": "string", "pattern": "^[A-Z_]+$"},
        "task_id": {"type": ["string", "null"], "format": "uuid"},
        "status": {"enum": ["PASS", "FAIL", "ERROR", "SKIPPED", "NOT_APPLICABLE"]},
        "required": {"type": "boolean"},
        "commit_sha": {"type": "string", "pattern": "^[0-9a-f]{40}([0-9a-f]{24})?$"},
        "evidence_id": {"type": ["string", "null"], "format": "uuid"},
        "findings": {"$ref": "#/$defs/severity_counts"},
        "duration_ms": {"type": "integer", "minimum": 0},
        "tool": {"type": "string", "maxLength": 100}
      }
    },
    "run_completed": {
      "type": "object", "additionalProperties": false,
      "required": ["final_status", "required_gates_total", "required_gates_passed"],
      "properties": {
        "final_status": {"enum": ["COMPLETED", "FAILED", "CANCELLED"]},
        "required_gates_total": {"type": "integer", "minimum": 0},
        "required_gates_passed": {"type": "integer", "minimum": 0},
        "open_findings": {"$ref": "#/$defs/severity_counts"},
        "report_artifact_id": {"type": ["string", "null"], "format": "uuid"},
        "reason": {"type": "string", "maxLength": 1000}
      }
    },
    "chat_message": {
      "type": "object", "additionalProperties": false,
      "required": ["chat_message_id", "thread_id", "scope", "message_kind", "sender_type", "body", "created_at"],
      "properties": {
        "chat_message_id": {"$ref": "#/$defs/uuid"},
        "thread_id": {"$ref": "#/$defs/uuid"},
        "scope": {"enum": ["RUN", "ROLE", "TASK"]},
        "message_kind": {"enum": ["USER_TEXT", "DIRECTIVE", "AGENT_MESSAGE", "CLI_HIGHLIGHT", "STATUS", "APPROVAL", "DIRECTIVE_STATUS", "SYSTEM_NOTICE"]},
        "sender_type": {"enum": ["user", "agent", "system"]},
        "sender_id": {"type": ["string", "null"]},
        "sender_role": {"anyOf": [{"$ref": "#/$defs/acp_participant"}, {"type": "null"}]},
        "provider": {"anyOf": [{"$ref": "#/$defs/provider"}, {"type": "null"}]},
        "task_id": {"type": ["string", "null"], "format": "uuid"},
        "target_role": {"anyOf": [{"$ref": "#/$defs/agent_role"}, {"type": "null"}]},
        "mentions": {"type": "array", "maxItems": 12, "items": {"$ref": "#/$defs/agent_role"}},
        "body": {"type": "string", "maxLength": 8000},
        "source_event_id": {"type": ["string", "null"], "format": "uuid"},
        "directive_id": {"type": ["string", "null"], "format": "uuid"},
        "approval_id": {"type": ["string", "null"], "format": "uuid"},
        "links": {
          "type": "array", "maxItems": 20,
          "items": {
            "type": "object", "additionalProperties": false, "required": ["kind", "id"],
            "properties": {"kind": {"enum": ["diff", "test_report", "evidence", "finding", "artifact", "task", "approval", "gate_execution"]}, "id": {"$ref": "#/$defs/uuid"}}
          }
        },
        "created_at": {"type": "string", "format": "date-time"}
      }
    }
  }
}
```

### 3.3 Правила проекции outbox → SSE

1. Проекцию выполняет `worker-outbox` в той же транзакции, где выставляется `outbox_events.published_at` для событий из каталога с непустой колонкой «SSE»: `INSERT run_event_log(workspace_id, run_id, seq, event_id, event_type, schema_version, payload)`.
2. `seq` назначается под per-run advisory lock (`pg_advisory_xact_lock(hashtext(run_id))`) как `max(seq)+1` (или счётчик `runs.last_event_seq`); откат транзакции откатывает и `seq` → разрывов нет, но клиенты не обязаны на это полагаться.
3. `run_event_log.event_id` = новый UUIDv7, `source_event_id` = outbox `event_id`; `UNIQUE(source_event_id, event_type)` обеспечивает идемпотентность повторной публикации (одно outbox-событие `cli_execution.output_batch` даёт несколько `cli.output` — уникальность по `(source_event_id, payload.cli_seq)` через частичный индекс).
4. После коммита — `PUBLISH run:{run_id} <seq>` в Redis (только сигнал; потеря сигнала компенсируется опросом раз в 5 с).
5. Payload SSE не содержит секретов, полных промптов, raw log, скрытых рассуждений; тексты прошли санитизацию CLI Gateway (CLI_ADAPTER_CONTRACT §11) и ограничены по длине.

## 4. Live Agent Chat

### 4.1 Треды и потоки

Решение: **потоки** `All Agents`, `Codex`, `Claude`, `Role`, `Task`, `Approvals` (ТЗ §11) — это **представления (фильтры)** над единой лентой `chat_messages` run, а не отдельные копии сообщений. **Треды** (`chat_threads`) — адресация пользовательских сообщений.

| `chat_threads.scope` | Создаётся | `target_role` | `task_id` (доп.) | Назначение |
|---|---|---|---|---|
| `RUN` | автоматически при создании run (ровно один) | NULL | NULL | общий тред: системные события, ACP, поручения без адресата |
| `ROLE` | лениво, при первом сообщении пользователя роли или от роли | роль | NULL | диалог пользователя с ролью (`@developer …`) |
| `TASK` | лениво, при первом сообщении по задаче | роль задачи | задача | обсуждение конкретной задачи |

| Поток UI | Фильтр по `chat_messages` (денормализованные поля, см. дополнения) |
|---|---|
| All Agents | все сообщения run |
| Codex | `provider = 'codex'` (сообщения агентов и CLI highlights исполнений на Codex) |
| Claude | `provider = 'claude'` |
| Role | `sender_role = :role OR target_role = :role OR :role = ANY(mentions)` |
| Task | `task_id = :task_id` |
| Approvals | `message_kind IN ('APPROVAL') OR (message_kind = 'DIRECTIVE_STATUS' AND status = 'AWAITING_CONFIRMATION')` |

Поиск: PostgreSQL FTS по `body` (tsvector, `simple` + `russian` конфигурации) в пределах run; timeline — сортировка по `created_at, id`.

### 4.2 Виды сообщений (`chat_messages.message_kind`)

| `message_kind` | `sender_type` | Источник | Пример |
|---|---|---|---|
| `USER_TEXT` | user | `POST /runs/{id}/chat/messages` без директивы | комментарий |
| `DIRECTIVE` | user | `POST /runs/{id}/directives` (создаёт и сообщение, и `user_directives`) | `CHANGE_REQUEST` |
| `AGENT_MESSAGE` | agent | проекция `acp.message_accepted` | `FIX_REQUEST reviewer→developer` |
| `CLI_HIGHLIGHT` | agent | проекция значимых нормализованных CLI-событий: `tool_event` (`file_patch`, `command` completed/denied), `warning`, `finished` | «developer (codex) изменил 3 файла» |
| `STATUS` | system | `task.state_changed`, `run.state_changed`, `gate.completed` | «Гейт QG-UNIT: FAIL» |
| `APPROVAL` | system | `approval.requested` / `approval.decided` | «Требуется MERGE approval» |
| `DIRECTIVE_STATUS` | system | `directive.state_changed`, `directive.ack_changed` | «Поручение доставлено developer» |
| `SYSTEM_NOTICE` | system | provider/auth события, предупреждения injection | «Claude: требуется вход оператора» |

Сырые `output`-события CLI в чат **не** проецируются (только в Task Detail / SSE `cli.output`), чтобы чат оставался читаемым.

### 4.3 Проекция в чат

| Источник | Правило | `source_event_id` |
|---|---|---|
| ACP `acp.message_accepted` | 1 сообщение `AGENT_MESSAGE` в тред: `TASK` (если `task_id`) иначе `RUN`; `body` = шаблонное резюме по типу (например, для `FINDING`: severity, title, файл), ссылки → `links` | outbox `event_id` |
| ACP к/от `user` | в тред `ROLE` (роль-собеседник) | outbox `event_id` |
| `cli_execution.output_batch` | только события из 4.2 `CLI_HIGHLIGHT`; не более 1 сообщения на 10 с на execution (коалесцирование) | outbox `event_id` + индекс |
| `task.state_changed` | только переходы в `RUNNING`, `VERIFYING`, `SUCCEEDED`, `BLOCKED`, `FAILED`, `CANCELLED` | outbox `event_id` |
| `gate.completed` | только `required = true` или `status ∈ {FAIL, ERROR}` | outbox `event_id` |
| `approval.*` | всегда | outbox `event_id` |
| `directive.*` | всегда (статусы из 4.4) | outbox `event_id` |

Идемпотентность: `UNIQUE(thread_id, source_event_id, projection_index)` — повторная доставка не создаёт дубль (AC-08). Проекция не изменяет сообщения задним числом: изменения статуса — новые сообщения.

### 4.4 Жизненный цикл поручения и различие «доставлено / принято / выполнено»

```mermaid
stateDiagram-v2
  [*] --> RECEIVED
  RECEIVED --> IMPACT_ANALYSIS_PENDING
  RECEIVED --> REJECTED: RBAC/policy deny
  IMPACT_ANALYSIS_PENDING --> IMPACT_READY
  IMPACT_ANALYSIS_PENDING --> FAILED
  IMPACT_READY --> AWAITING_CONFIRMATION: затронуты критические операции / requires_impact_preview
  IMPACT_READY --> CONFIRMED: подтверждение не требуется
  AWAITING_CONFIRMATION --> CONFIRMED: пользователь подтвердил (+ approval CRITICAL_DIRECTIVE если нужно)
  AWAITING_CONFIRMATION --> CANCELLED
  AWAITING_CONFIRMATION --> REJECTED: approval rejected
  CONFIRMED --> CHECKPOINTING: impact ∈ REPLAN_REQUIRED/RESTART_REQUIRED/TASK_CANCEL
  CONFIRMED --> DISPATCHED: CONTEXT_ONLY / NONE / ASK
  CHECKPOINTING --> REPLANNING
  CHECKPOINTING --> FAILED
  REPLANNING --> DISPATCHED
  REPLANNING --> FAILED
  DISPATCHED --> ACKNOWLEDGED
  DISPATCHED --> FAILED
  ACKNOWLEDGED --> COMPLETED
  ACKNOWLEDGED --> FAILED
  COMPLETED --> [*]
```

Полная FSM и impact analysis — в `architecture/DATA_FLOW.md`; здесь — семантика статусов для UI и событий:

| Понятие в UI | Статус | Точное условие | Событие |
|---|---|---|---|
| «Принято системой» | `RECEIVED` | директива сохранена, RBAC пройден | `directive.received` |
| «Анализ влияния готов» | `IMPACT_READY` | все `directive_impacts` записаны | `directive.impact_ready` |
| «Ожидает подтверждения» | `AWAITING_CONFIRMATION` | нужен confirm/approval | `directive.state_changed` + `approval.requested` |
| «Отправлено» | `DISPATCHED` | директива включена в inbox (ACP `TASK_ASSIGN`/Context Pack) задач-получателей; `directive_acknowledgements.status = PENDING` | `directive.state_changed` |
| **«Доставлено»** | ack `DELIVERED` (по получателю) | исполнение CLI, чей Context Pack содержит директиву, перешло в `RUNNING` (событие `started`) | `directive.ack_changed` |
| **«Принято агентом»** | `ACKNOWLEDGED` (директива) / ack `ACKNOWLEDGED` | структурированный итог исполнения содержит `directive_acks[{directive_id, status:"accepted"}]`, принятый Orchestrator; директива `ACKNOWLEDGED`, когда все обязательные получатели подтвердили | `directive.ack_changed`, `directive.state_changed` |
| **«Выполнено»** | `COMPLETED` | все задачи, созданные/изменённые директивой, `SUCCEEDED` (т.е. прошли обязательные гейты); для `ASK` — принят `ANSWER`; для `PAUSE`/`RESUME`/`STOP` — run достиг целевого статуса | `directive.state_changed` |
| «Не выполнено» | `FAILED` / `REJECTED` / `CANCELLED` | с причиной | `directive.state_changed` |

UI обязан показывать «доставлено», «принято агентом» и «выполнено» разными индикаторами (ТЗ §11); `COMPLETED` никогда не выставляется по факту доставки или по заявлению агента.

`directive_acknowledgements.status`: `PENDING`, `DELIVERED`, `ACKNOWLEDGED`, `COMPLETED`, `FAILED` (предложенное перечисление, раздел 7).

## 5. Дедупликация

| Уровень | Механизм |
|---|---|
| Outbox → потребители | `processed_events(consumer, event_id)` PK; потребитель вставляет строку в той же tx, что и эффект; конфликт → no-op |
| ACP | `message_deliveries UNIQUE(message_id, consumer_id)` + `acked_at` |
| SSE сервер | `run_event_log PK(run_id, seq)`, `event_id UNIQUE`; идемпотентная проекция (3.3) |
| SSE клиент | хранит последний `seq`; событие с `seq ≤ last_seq` отбрасывается; `event_id` — для журналов |
| Chat | `UNIQUE(thread_id, source_event_id, projection_index)` |
| Directives API | `Idempotency-Key` (`idempotency_keys`) + `expected_run_version` |
| GitHub webhooks | `github_webhook_deliveries.delivery_guid UNIQUE` |

## 6. Порядок и retention

**Гарантии порядка:**

- Внутри run: полный порядок SSE-событий по `seq` (единственный источник порядка для UI).
- Внутри агрегата: outbox-события упорядочены по `id` (UUIDv7, монотонен в рамках процесса) и `aggregate_version`; dispatcher обрабатывает события одного run последовательно (advisory lock), поэтому порядок сохраняется в `seq`.
- Между run и между агрегатами разных run порядок не гарантируется.
- Dramatiq-потребители могут получить события вне порядка при повторах; потребитель обязан сравнивать `aggregate_version` и игнорировать устаревшие.
- `cli.output` внутри execution упорядочены по `cli_seq`.

**Retention (по умолчанию, настраивается workspace policy; удаление/архивация выполняет `aicd_maintenance`, с audit):**

| Данные | Срок | После срока |
|---|---|---|
| `outbox_events` (опубликованные) | 7 дней | удаление |
| `processed_events` | 30 дней | удаление |
| `run_event_log` | 90 дней после терминального статуса run | архив NDJSON в объектное хранилище (sha256 в `artifacts`), удаление строк |
| `agent_activity_events` (включая `cli.*`) | 180 дней | архив + удаление |
| `agent_messages`, `chat_messages`, `user_directives`, `directive_*` | срок жизни проекта; архив через 1 год | архив |
| raw CLI logs (artifacts `raw_log`) | 30 дней | удаление объекта, метаданные остаются |
| `audit_events` | ≥ 1 год, append-only | архив, не удаление в v1 |

## 7. Предлагаемые дополнения

| Предложение | Обоснование |
|---|---|
| Перечисление `chat_thread_scope`: `RUN`, `ROLE`, `TASK`; колонка `chat_threads.task_id` | адресация тредов; потоки UI — фильтры |
| Перечисление `chat_message_kind`: `USER_TEXT`, `DIRECTIVE`, `AGENT_MESSAGE`, `CLI_HIGHLIGHT`, `STATUS`, `APPROVAL`, `DIRECTIVE_STATUS`, `SYSTEM_NOTICE` | ТЗ §7.2 `message_kind` без значений |
| Перечисление `chat_sender_type`: `user`, `agent`, `system` | ТЗ §7.2 `sender_type` без значений |
| Колонки `chat_messages`: `workspace_id`, `run_id`, `task_id`, `sender_role`, `target_role`, `provider`, `mentions text[]`, `projection_index`, `search_vector` | фильтры потоков, поиск, дедупликация |
| Перечисление `directive_ack_status`: `PENDING`, `DELIVERED`, `ACKNOWLEDGED`, `COMPLETED`, `FAILED` | различение «доставлено/принято/выполнено» |
| Колонки `outbox_events.workspace_id`, `run_id`, `aggregate_version`; `runs.last_event_seq` | RLS, порядок, назначение `seq` |
| `run_event_log.source_event_id` | идемпотентная проекция |
| SSE `run.completed` эмитируется для всех терминальных статусов run (`COMPLETED`, `FAILED`, `CANCELLED`) с полем `final_status` | имя типа из ТЗ; UI нужен единый сигнал завершения |
