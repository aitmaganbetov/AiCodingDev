"""Метрики Prometheus (ТЗ §16, FR-OBS-02, P8-06).

Единый реестр метрик платформы. Правила (SC-OBS-03, TH-48):

* только метки с ограниченным множеством значений (статусы FSM, типы событий, провайдер, шаблон маршрута);
  идентификаторы (workspace/run/task/user), e-mail, пути файлов, тексты ошибок в метки **не** попадают;
* значения метрик — счётчики, длительности и размеры; содержимое промптов/кода/вывода CLI не экспортируется;
* `/metrics` не публикуется через reverse-proxy (только внутренняя сеть) и защищается bearer-токеном
  (`AICD_METRICS_TOKEN_FILE`); в production без токена endpoint выключен (fail closed).

Доменные счётчики (runs/tasks/approvals/gates/ACP) считает worker-outbox по **зафиксированным** outbox-событиям
(`observe_outbox_event`): откатившаяся транзакция не попадает в метрики, повторная доставка (at-least-once)
может дать редкий двойной счёт — допустимая погрешность для rate-метрик. Метрики исполнения CLI, HTTP, SSE,
аутентификации считаются в процессе, где происходит событие.
"""

from __future__ import annotations

import hmac
import logging
import socketserver
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import REGISTRY as REGISTRY  # реэкспорт: реестр процесса (экспозиция, тесты)

log = logging.getLogger("aicd.metrics")

NAMESPACE = "aicd"

# --- допустимые значения меток (нормализация: неизвестное ⇒ "other") ------------------------------------
RUN_STATUSES = frozenset(
    {"CREATED", "DISCOVERY", "PLANNED", "AWAITING_APPROVAL", "RUNNING", "VERIFYING", "PAUSED", "BLOCKED"}
    | {"COMPLETED", "FAILED", "CANCELLED"}
)
TASK_STATUSES = frozenset(
    {"PENDING", "READY", "LEASED", "RUNNING", "VERIFYING", "RETRY_WAIT", "BLOCKED", "SUCCEEDED", "FAILED", "CANCELLED"}
)
APPROVAL_STATUSES = frozenset({"PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"})
GATE_STATUSES = frozenset({"PENDING", "RUNNING", "PASS", "FAIL", "ERROR", "SKIPPED", "NOT_APPLICABLE"})
PROVIDERS = frozenset({"codex", "claude", "fake"})
EXECUTION_STATUSES = frozenset({"COLLECTED", "TIMED_OUT", "CANCELLED", "CRASHED", "AUTH_REQUIRED", "RATE_LIMITED"})
OTHER = "other"


def label(value: Any, allowed: Iterable[str] | None = None, *, max_len: int = 64) -> str:
    """Безопасное значение метки: из allowlist (иначе `other`) или короткий идентификатор `[a-z0-9_.:-]`."""
    if value is None:
        return "none"
    text = str(value)
    if allowed is not None:
        return text if text in allowed else OTHER
    if len(text) > max_len or not all(c.isalnum() or c in "_.:-/{}" for c in text):
        return OTHER
    return text


# --- HTTP API -------------------------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP-запросы API по шаблону маршрута и коду ответа",
    ["method", "route", "status"],
    namespace=NAMESPACE,
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "Длительность обработки HTTP-запроса API (без времени жизни SSE-потока)",
    ["method", "route"],
    namespace=NAMESPACE,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
AUTH_FAILURES = Counter(
    "auth_failures_total",
    "Неудачи аутентификации: неверный вход, недействительная сессия, CSRF",
    ["reason"],
    namespace=NAMESPACE,
)
AUTHZ_DENIED = Counter(
    "authz_denied_total",
    "Отказы авторизации: permission — нет права (403), tenant — не член workspace (404, AC-12)",
    ["reason"],
    namespace=NAMESPACE,
)
SSE_STREAMS = frozenset({"run", "workspace"})
SSE_CONNECTIONS = Gauge(
    "sse_connections",
    "Открытые SSE-потоки процесса API по виду потока (run, workspace)",
    ["stream"],
    namespace=NAMESPACE,
)
SSE_CONNECTIONS_OPENED = Counter(
    "sse_connections_opened_total", "Открытые SSE-потоки по виду потока (накопительно)", ["stream"], namespace=NAMESPACE
)
SSE_REJECTED = Counter(
    "sse_rejected_total",
    "Отклонённые SSE-подключения с 503 (P9-04): process_limit — лимит потоков процесса API,"
    " redis_unavailable — сбой учёта слотов в Redis",
    ["reason"],
    namespace=NAMESPACE,
)
WEBHOOK_SIGNATURE_INVALID = Counter(
    "webhook_signature_invalid_total",
    "Отклонённые доставки вебхука GitHub с неверной подписью (SC-OBS-02): missing — нет заголовка,"
    " malformed — не sha256=<hex64>, mismatch — подпись не совпала ни с одним активным секретом",
    ["reason"],
    namespace=NAMESPACE,
)
WEBHOOK_SIGNATURE_REASONS = frozenset({"missing", "malformed", "mismatch"})
ARTIFACT_STORAGE_FREE = Gauge(
    "artifact_storage_free_bytes", "Свободное место FS-хранилища артефактов", namespace=NAMESPACE
)
ARTIFACT_STORAGE_TOTAL = Gauge("artifact_storage_size_bytes", "Размер FS-хранилища артефактов", namespace=NAMESPACE)

# --- домен (по outbox-событиям) -----------------------------------------------------------------------
OUTBOX_EVENTS = Counter(
    "outbox_events_published_total", "Опубликованные outbox-события по типу", ["event_type"], namespace=NAMESPACE
)
OUTBOX_FAILURES = Counter(
    "outbox_publish_failures_total", "Ошибки проекции/публикации outbox-событий (повтор с backoff)", namespace=NAMESPACE
)
OUTBOX_LAG = Histogram(
    "outbox_lag_seconds",
    "Задержка доставки: от записи события в outbox до публикации в очередь (queue lag)",
    namespace=NAMESPACE,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300, 900),
)
OUTBOX_PENDING = Gauge("outbox_pending_events", "Неопубликованные outbox-события", namespace=NAMESPACE)
OUTBOX_OLDEST_AGE = Gauge(
    "outbox_oldest_pending_age_seconds", "Возраст самого старого неопубликованного outbox-события", namespace=NAMESPACE
)
RUN_TRANSITIONS = Counter(
    "run_transitions_total", "Переходы Run FSM по целевому статусу", ["to_status"], namespace=NAMESPACE
)
RUNS_FINISHED = Counter(
    "runs_finished_total",
    "Завершённые run по итоговому статусу (run success rate = COMPLETED / все)",
    ["final_status"],
    namespace=NAMESPACE,
)
TASK_TRANSITIONS = Counter(
    "task_transitions_total",
    "Переходы Task FSM по целевому статусу (RETRY_WAIT — повторы, SUCCEEDED — verified completion)",
    ["to_status"],
    namespace=NAMESPACE,
)
APPROVALS_REQUESTED = Counter(
    "approvals_requested_total", "Запрошенные approvals по типу действия", ["action_type"], namespace=NAMESPACE
)
APPROVALS_DECIDED = Counter(
    "approvals_decided_total",
    "Решения по approvals (включая EXPIRED/CANCELLED)",
    ["action_type", "status"],
    namespace=NAMESPACE,
)
APPROVAL_WAIT = Histogram(
    "approval_wait_seconds",
    "Время ожидания решения человека по approval (approval wait time)",
    ["action_type"],
    namespace=NAMESPACE,
    buckets=(10, 30, 60, 300, 900, 1800, 3600, 4 * 3600, 12 * 3600, 24 * 3600, 3 * 86400, 7 * 86400),
)
GATE_EXECUTIONS = Counter(
    "gate_executions_total",
    "Завершённые quality gates по типу и статусу (gate failures = FAIL/ERROR)",
    ["gate_type", "status", "required"],
    namespace=NAMESPACE,
)
GATE_DURATION = Histogram(
    "gate_duration_seconds",
    "Длительность исполнения quality gate",
    ["gate_type"],
    namespace=NAMESPACE,
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1200, 3600),
)
AGENT_MESSAGES = Counter(
    "agent_messages_total",
    "Принятые ACP-сообщения агентов по типу (agent message count)",
    ["message_type"],
    namespace=NAMESPACE,
)
DIRECTIVE_TRANSITIONS = Counter(
    "directive_transitions_total",
    "Переходы поручений пользователя по целевому статусу",
    ["to_status"],
    namespace=NAMESPACE,
)

# --- воркеры: очереди, аренды, хранилище -----------------------------------------------------------------
QUEUE_DEPTH = Gauge(
    "queue_depth", "Сообщения в очереди Dramatiq (Redis): готовые и отложенные", ["queue"], namespace=NAMESPACE
)
QUEUE_DEAD_LETTERS = Gauge(
    "queue_dead_letters", "Сообщения Dramatiq в dead-letter очереди (исчерпаны повторы)", ["queue"], namespace=NAMESPACE
)
LEASES_EXPIRED = Gauge(
    "worker_leases_expired",
    "Неосвобождённые аренды с истёкшим lease_until (stuck leases)",
    ["resource_type"],
    namespace=NAMESPACE,
)
DATABASE_SIZE = Gauge("database_size_bytes", "Размер базы данных PostgreSQL", namespace=NAMESPACE)
PROVIDER_AVAILABLE = Gauge(
    "provider_available",
    "1 — у провайдера есть доступная установка CLI (AVAILABLE/DEGRADED по последнему наблюдению), иначе 0",
    ["provider"],
    namespace=NAMESPACE,
)
WORKER_MESSAGES = Counter(
    "worker_messages_total",
    "Обработанные сообщения Dramatiq по актору и исходу",
    ["actor", "outcome"],
    namespace=NAMESPACE,
)
WORKER_MESSAGE_DURATION = Histogram(
    "worker_message_duration_seconds",
    "Длительность обработки сообщения Dramatiq",
    ["actor"],
    namespace=NAMESPACE,
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 30, 60, 300, 900, 3600),
)
SAMPLER_ERRORS = Counter(
    "metrics_sampler_errors_total", "Ошибки периодического сбора gauge-метрик", namespace=NAMESPACE
)

# --- резервное копирование (P9-05, ТЗ §16 «failed backups», SC-OBS-02) ---------------------------------
# Источник — файл статуса утилиты бэкапа (`python -m scripts.backup`, AICD_BACKUP_STATUS_FILE, только чтение) и
# pg_stat_archiver сервера (архивирование WAL). Собирает worker-outbox (`aicd_worker.observability`).
BACKUP_COMPONENTS = frozenset({"postgres_base", "postgres_wal", "postgres_dump", "artifacts", "secrets"})
BACKUP_LAST_SUCCESS = Gauge(
    "backup_last_success_timestamp_seconds",
    "Время (unix) последнего успешного бэкапа компонента (postgres_base, postgres_wal, postgres_dump, artifacts,"
    " secrets)",
    ["component"],
    namespace=NAMESPACE,
)
BACKUP_LAST_FAILED = Gauge(
    "backup_last_attempt_failed",
    "1 — последняя попытка бэкапа компонента завершилась ошибкой, 0 — успехом",
    ["component"],
    namespace=NAMESPACE,
)
WAL_ARCHIVE_FAILURES = Gauge(
    "postgres_wal_archive_failures",
    "Накопительное число неудачных вызовов archive_command (pg_stat_archiver.failed_count)",
    namespace=NAMESPACE,
)
WAL_ARCHIVED = Gauge(
    "postgres_wal_archived", "Накопительное число заархивированных файлов WAL (pg_stat_archiver)", namespace=NAMESPACE
)


def observe_backup_status(status: Mapping[str, Any]) -> int:
    """Gauge по файлу статуса бэкапа (`components.<имя>.last_success_unixtime/last_status`); число компонентов."""
    components = status.get("components")
    if not isinstance(components, Mapping):
        return 0
    seen = 0
    for name, entry in components.items():
        if name not in BACKUP_COMPONENTS or not isinstance(entry, Mapping):
            continue
        success = entry.get("last_success_unixtime")
        if isinstance(success, int | float):
            BACKUP_LAST_SUCCESS.labels(name).set(float(success))
        BACKUP_LAST_FAILED.labels(name).set(0 if entry.get("last_status") == "success" else 1)
        seen += 1
    return seen


# --- CLI Gateway --------------------------------------------------------------------------------------
CLI_EXECUTIONS = Counter(
    "cli_executions_total",
    "Завершённые исполнения CLI по провайдеру, статусу Execution FSM и категории ошибки",
    ["provider", "status", "failure_category"],
    namespace=NAMESPACE,
)
CLI_EXECUTIONS_ACTIVE = Gauge(
    "cli_executions_active", "Исполнения CLI в процессе (процесс worker-exec)", ["provider"], namespace=NAMESPACE
)
CLI_DURATION = Histogram(
    "cli_execution_duration_seconds",
    "Длительность исполнения CLI (wall clock)",
    ["provider"],
    namespace=NAMESPACE,
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 18000),
)
CLI_LIMIT_REJECTIONS = Counter(
    "cli_concurrency_rejections_total",
    "Отказы в слоте установки CLI (лимит параллелизма/установка недоступна)",
    ["provider"],
    namespace=NAMESPACE,
)
CLI_TOKENS = Counter(
    "cli_tokens_total",
    "Токены, достоверно сообщённые CLI (source=cli_reported); оценки платформы сюда не попадают",
    ["provider"],
    namespace=NAMESPACE,
)
PROVIDER_PROBES = Counter(
    "provider_probes_total",
    "Проверки установки CLI по итоговой доступности",
    ["provider", "availability"],
    namespace=NAMESPACE,
)
CONTEXT_PACK_SIZE = Histogram(
    "context_pack_size_bytes",
    "Размер Context Pack, переданного агенту",
    namespace=NAMESPACE,
    buckets=(1024, 4096, 16384, 65536, 131072, 262144, 524288, 1048576, 4194304),
)
CONTEXT_PACK_FRAGMENTS = Counter(
    "context_pack_fragments_total",
    "Фрагменты Context Pack: stale=true — из снимка индекса не на текущем commit (index freshness)",
    ["stale"],
    namespace=NAMESPACE,
)
INDEX_SNAPSHOT_AGE = Histogram(
    "index_snapshot_age_seconds",
    "Возраст снимка индекса, использованного для Context Pack (index freshness)",
    namespace=NAMESPACE,
    buckets=(60, 300, 900, 3600, 6 * 3600, 86400, 7 * 86400, 30 * 86400),
)
INDEXING_JOBS = Counter("indexing_jobs_total", "Задания индексации по исходу", ["outcome"], namespace=NAMESPACE)


def observe_context_pack(*, size_bytes: int, fragments: int, stale: int, snapshot_id: Any | None) -> None:
    """Context pack size и index freshness (P8-06): возраст снимка — по времени UUIDv7 его id."""
    CONTEXT_PACK_SIZE.observe(max(size_bytes, 0))
    CONTEXT_PACK_FRAGMENTS.labels("true").inc(max(stale, 0))
    CONTEXT_PACK_FRAGMENTS.labels("false").inc(max(fragments - stale, 0))
    if snapshot_id is not None and getattr(snapshot_id, "version", None) == 7:
        created = (snapshot_id.int >> 80) / 1000
        INDEX_SNAPSHOT_AGE.observe(max(time.time() - created, 0.0))


def _bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def observe_outbox_event(event_type: str, payload: Mapping[str, Any]) -> None:
    """Доменные метрики по опубликованному outbox-событию (вызывает диспетчер после commit)."""
    OUTBOX_EVENTS.labels(label(event_type)).inc()
    to_status = payload.get("to_status", payload.get("to"))
    if event_type == "run.state_changed":
        RUN_TRANSITIONS.labels(label(to_status, RUN_STATUSES)).inc()
    elif event_type == "run.completed":
        RUNS_FINISHED.labels(label(payload.get("final_status"), RUN_STATUSES)).inc()
    elif event_type == "task.state_changed":
        if payload.get("from") != to_status:  # смена причины блокировки — не переход
            TASK_TRANSITIONS.labels(label(to_status, TASK_STATUSES)).inc()
    elif event_type == "approval.requested":
        APPROVALS_REQUESTED.labels(label(payload.get("action_type"))).inc()
    elif event_type == "approval.decided":
        action = label(payload.get("action_type"))
        APPROVALS_DECIDED.labels(action, label(payload.get("status"), APPROVAL_STATUSES)).inc()
        wait = payload.get("wait_seconds")
        if isinstance(wait, int | float) and wait >= 0:
            APPROVAL_WAIT.labels(action).observe(float(wait))
    elif event_type == "gate.completed":
        gate_type = label(payload.get("gate_type"))
        GATE_EXECUTIONS.labels(
            gate_type, label(payload.get("status"), GATE_STATUSES), _bool(payload.get("required"))
        ).inc()
        duration_ms = payload.get("duration_ms")
        if isinstance(duration_ms, int | float) and duration_ms >= 0:
            GATE_DURATION.labels(gate_type).observe(duration_ms / 1000)
    elif event_type == "agent.message":
        AGENT_MESSAGES.labels(label(payload.get("message_type"))).inc()
    elif event_type == "directive.state_changed":
        DIRECTIVE_TRANSITIONS.labels(label(to_status)).inc()


# --- экспозиция -----------------------------------------------------------------------------------------


def render(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    return generate_latest(registry), CONTENT_TYPE_LATEST


def token_matches(expected: str | None, authorization: str | None) -> bool:
    """`Authorization: Bearer <token>` в константное время."""
    if not expected or not authorization:
        return False
    scheme, _, value = authorization.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), expected.encode())


def metrics_allowed(*, token: str | None, environment: str, authorization: str | None) -> bool | None:
    """True — отдать метрики; False — 401; None — endpoint выключен (404: production без токена)."""
    if token:
        return token_matches(token, authorization)
    return None if environment == "production" else True


WsgiApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


def wsgi_app(*, token: str | None, environment: str, registry: CollectorRegistry = REGISTRY) -> WsgiApp:
    """WSGI-приложение `/metrics` воркера с той же политикой доступа, что и у API."""

    def app(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        if environ.get("PATH_INFO") != "/metrics" or environ.get("REQUEST_METHOD") not in ("GET", "HEAD"):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found\n"]
        allowed = metrics_allowed(token=token, environment=environment, authorization=environ.get("HTTP_AUTHORIZATION"))
        if allowed is None:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found\n"]
        if not allowed:
            start_response("401 Unauthorized", [("Content-Type", "text/plain"), ("WWW-Authenticate", "Bearer")])
            return [b"unauthorized\n"]
        body, ctype = render(registry)
        start_response("200 OK", [("Content-Type", ctype), ("Cache-Control", "no-store")])
        return [body]

    return app


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — сигнатура базового класса
        return  # access-лог скрейпов не нужен


class _ThreadingServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


def start_worker_server(
    *, host: str, port: int, token: str | None, environment: str, registry: CollectorRegistry = REGISTRY
) -> WSGIServer:
    """HTTP-сервер `/metrics` воркера в фоновом потоке; возвращает сервер (порт — `server.server_port`)."""
    server = make_server(
        host, port, wsgi_app(token=token, environment=environment, registry=registry), _ThreadingServer, _QuietHandler
    )
    thread = threading.Thread(target=server.serve_forever, name="aicd-metrics", daemon=True)
    thread.start()
    log.info("metrics endpoint started", extra={"port": server.server_port})
    return server


def metric_names(registry: CollectorRegistry = REGISTRY) -> set[str]:
    """Имена всех временных рядов реестра (включая суффиксы `_bucket/_sum/_count/_total/_created`)."""
    names: set[str] = set()
    for family in registry.collect():
        names.add(family.name)
        for sample in family.samples:
            names.add(sample.name)
        if family.type == "counter":
            names.add(family.name + "_total")
        elif family.type == "histogram":
            names.update({family.name + "_bucket", family.name + "_sum", family.name + "_count"})
    return names
