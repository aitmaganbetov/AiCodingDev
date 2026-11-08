"""ACP 1.0 — брокер сообщений агентов (AGENT_COMMUNICATION_PROTOCOL; backlog P4-04, P4-05).

Публичный API для оркестрации и API:

- `AcpBroker(escalation=…).submit(conn, candidate, sender=Sender(...), run_id=…)` → `Accepted | Rejected`
  (конвейер §5 + persist/outbox в транзакции вызывающего, роль БД `aicd_worker`);
- `pipeline.evaluate(envelope, sender=…, facts=…)` — чистое ядро шагов 0–6 (без БД);
- доставка §7: `process_delivery`, `handle_outbox_event`, `deliver_pending`, `ack`, `replay`;
- проекции: `project_chat` (`chat.projector`), `project_activity` (`activity.projector`).
"""

from aicd_collaboration.acp.broker import (
    CONSUMER_ACP_HANDLER,
    CONSUMER_ACTIVITY,
    CONSUMER_CHAT,
    DEFAULT_CONSUMERS,
    AcpBroker,
    ReviewLoopEscalation,
)
from aicd_collaboration.acp.delivery import (
    Delivery,
    DeliveryStatus,
    ack,
    deliver_pending,
    dlq_publisher,
    handle_outbox_event,
    process_delivery,
    replay,
)
from aicd_collaboration.acp.facts import BrokerFacts, RunInfo, StoredMessage, TaskInfo
from aicd_collaboration.acp.model import (
    MESSAGE_TYPES,
    PARTICIPANTS,
    Accepted,
    LoopExhausted,
    Outcome,
    Rejected,
    RejectionCode,
    Sender,
)
from aicd_collaboration.acp.pipeline import AcpLimits, Evaluation, build_envelope, evaluate
from aicd_collaboration.acp.projection import HANDLERS, project_activity, project_chat

__all__ = [
    "CONSUMER_ACP_HANDLER",
    "CONSUMER_ACTIVITY",
    "CONSUMER_CHAT",
    "DEFAULT_CONSUMERS",
    "HANDLERS",
    "MESSAGE_TYPES",
    "PARTICIPANTS",
    "Accepted",
    "AcpBroker",
    "AcpLimits",
    "BrokerFacts",
    "Delivery",
    "DeliveryStatus",
    "Evaluation",
    "LoopExhausted",
    "Outcome",
    "Rejected",
    "RejectionCode",
    "ReviewLoopEscalation",
    "RunInfo",
    "Sender",
    "StoredMessage",
    "TaskInfo",
    "ack",
    "build_envelope",
    "deliver_pending",
    "dlq_publisher",
    "evaluate",
    "handle_outbox_event",
    "process_delivery",
    "project_activity",
    "project_chat",
    "replay",
]
