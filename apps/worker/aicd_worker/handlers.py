"""Маршрутизация доменных событий в оркестратор (ADR-0002 п.3, ADR-0007).

Сообщение несёт идентификаторы и тип события; состояние перечитывается из PostgreSQL. Повторная доставка
(at-least-once) отсекается `processed_events(consumer, event_id)`: отметка вставляется в отдельной короткой
транзакции, которая фиксируется только после успешной обработки (ошибка ⇒ откат ⇒ Dramatiq повторит).
Конкурентный дубль ждёт на уникальном ключе и после commit первого получает конфликт ⇒ no-op.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aicd_cli_adapters import limits as cli_limits
from aicd_cli_adapters import login, login_registry, registry
from aicd_collaboration import acp
from aicd_platform import consumer, context
from aicd_platform.db import DbContext, apply_context, transaction
from aicd_platform.ids import uuid7
from aicd_platform.telemetry import current_traceparent
from aicd_worker.runtime import CodeInput, Runtime

log = logging.getLogger("aicd.worker")

ORCHESTRATOR_CONSUMER = "orchestrator.handle_event"
EXEC_CONSUMER = "exec.execute_task"
PROBE_CONSUMER = "exec.probe_provider"
# Запрос перепроверки установки CLI (POST /providers/{id}/probe): очередь exec, обработчик `probe_provider`.
PROBE_EVENT_TYPES = frozenset({registry.PROBE_REQUESTED})
LOGIN_CONSUMER = "exec.login_provider"
# Запрос интерактивного входа CLI (POST /providers/{id}/login): очередь exec, обработчик `login_provider`.
LOGIN_EVENT_TYPES = frozenset({login_registry.LOGIN_REQUESTED})

# Типы, которые диспетчер outbox направляет в очередь оркестратора (остальные — только SSE/прочие потребители).
ORCHESTRATOR_EVENT_TYPES = frozenset(
    {
        "run.created",
        "run.state_changed",
        "task.state_changed",
        "approval.decided",
        "agent.message",
        "directive.state_changed",
    }
)
# Шаги поручения, которые выполняет воркер (`aicd_orchestration.directives.process`): impact analysis после
# приёма и apply (checkpoint → replan → dispatch) после подтверждения пользователем или решения approval.
DIRECTIVE_STEPS = frozenset({"IMPACT_ANALYSIS_PENDING", "CONFIRMED"})
# Запрос загрузки документации (без run): очередь docs, обработчик `aicd_worker.docs.import_documentation`.
DOCS_EVENT_TYPES = frozenset({"documentation_source.refresh_requested"})
# Потребители доставки ACP (ACP §7), обслуживаемые воркером: проекции в чат run и ленту активности.
ACP_PROJECTORS = ((acp.CONSUMER_CHAT, acp.project_chat), (acp.CONSUMER_ACTIVITY, acp.project_activity))

Outcome = Literal[
    "acp_delivered",
    "duplicate",
    "ignored",
    "on_run_created",
    "on_plan_approval_decided",
    "on_run_paused_or_cancelled",
    "on_run_resumed",
    "execute_task",
    "directive_processed",
    "provider_probed",
    "provider_login_finished",
    "tick",
]


@asynccontextmanager
async def claimed(engine: AsyncEngine, consumer_name: str, event_id: uuid.UUID) -> AsyncIterator[bool]:
    """Отметка обработки в собственной транзакции; commit — по успешному выходу из блока."""
    async with engine.begin() as conn:
        await apply_context(conn, DbContext(actor_type="system"))
        yield await consumer.claim(conn, consumer_name, event_id)


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def probe_provider(rt: Runtime, event: dict[str, Any]) -> Outcome:
    """Перепроверка установки CLI на этом хосте (worker-exec) по запросу пользователя (OQ-45 (c)).

    Результат: upsert установки и наблюдение provider_health (`registry.record_probe`) плюс audit
    `provider.probe_completed` в workspace запроса — по нему API отдаёт статус. Повторная доставка — no-op.
    API не передаёт и не получает credentials: probe только читает состояние CLI (ADR-0003).
    """
    event_id = _uuid(event.get("event_id"))
    ws = _uuid(event.get("workspace_id"))
    payload = event.get("payload") or {}
    provider_id = _uuid(payload.get("provider_id")) or _uuid(event.get("aggregate_id"))
    request_id = _uuid(payload.get("probe_request_id"))
    provider = payload.get("provider")
    if (
        event_id is None
        or ws is None
        or provider_id is None
        or request_id is None
        or provider not in ("codex", "claude")
    ):
        return "ignored"
    if rt.provider_prober is None or rt.worker_host_id is None:
        raise RuntimeError("probe провайдера выполняет только воркер exec")
    async with claimed(rt.engine, PROBE_CONSUMER, event_id) as first:
        pass
    if not first:
        return "duplicate"
    context.bind(workspace_id=ws)
    started = time.monotonic()
    probe = None
    error: str | None = None
    try:
        probe = await rt.provider_prober(str(provider))
    except Exception:
        log.exception("provider probe failed", extra={"provider": provider})
        error = "PROCESS_FAILED"
    duration_ms = int((time.monotonic() - started) * 1000)
    installation_id: uuid.UUID | None = None
    if probe is not None:
        async with transaction(rt.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
            if probe.installed and probe.binary_path is not None:
                installation_id = await registry.record_probe(
                    conn, host_id=rt.worker_host_id, probe=probe, duration_ms=duration_ms
                )
        error = registry.availability_of(probe)[1]
        # Лимиты читаются до записи итога: когда UI увидит «проверка завершена», наблюдение уже сохранено.
        # У Codex чтение лимитов бесплатно (без запроса к модели) — обновляем при каждой проверке;
        # у Claude Code оно расходует подписку, поэтому только по явной просьбе оператора.
        wants_limits = payload.get("limits") is True or str(provider) not in cli_limits.COSTLY
        if wants_limits and installation_id is not None:
            await _refresh_limits(
                rt, str(provider), installation_id, authenticated=probe.auth_status == "AUTHENTICATED"
            )
    async with transaction(rt.engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        await registry.record_probe_outcome(
            conn,
            workspace_id=ws,
            provider_id=provider_id,
            probe_request_id=request_id,
            installation_id=installation_id,
            error_category=error,
        )
    return "provider_probed"


async def _refresh_limits(rt: Runtime, provider: str, installation_id: uuid.UUID, *, authenticated: bool) -> None:
    """Лимиты подписки по запросу оператора: один минимальный запрос CLI. Сбой — не ошибка проверки."""
    if rt.limits_reader is None or rt.limits_sink is None or not authenticated:
        return
    try:
        observed = await rt.limits_reader(provider)
        if observed:
            await rt.limits_sink(installation_id, observed)
    except Exception:
        log.exception("subscription limits refresh failed", extra={"provider": provider})


async def login_provider(rt: Runtime, event: dict[str, Any]) -> Outcome:
    """Интерактивный вход CLI по подписке на этом хосте (worker-exec) по запросу оператора из UI.

    Запускает официальную команду входа CLI в PTY (`login.run_login`), записывает в сессию найденную пару
    «ссылка + код устройства» и итог. Учётные данные платформа не получает: токен пишет сам CLI в свой
    credential store (ADR-0003, cli-auth.md §0). Вывод CLI проходит `login.scrub()` до любой записи.

    После успешного входа сразу выполняется probe: установка получает актуальный `auth_status`, и задачи,
    стоявшие `BLOCKED (AUTH_REQUIRED)`, видят доступного провайдера.
    """
    event_id = _uuid(event.get("event_id"))
    ws = _uuid(event.get("workspace_id"))
    payload = event.get("payload") or {}
    provider_id = _uuid(payload.get("provider_id")) or _uuid(event.get("aggregate_id"))
    session_id = _uuid(payload.get(login_registry.PAYLOAD_ID_KEY))
    provider = payload.get("provider")
    if (
        event_id is None
        or ws is None
        or provider_id is None
        or session_id is None
        or provider not in ("codex", "claude")
    ):
        return "ignored"
    if rt.provider_login is None or rt.worker_host_id is None:
        raise RuntimeError("вход провайдера выполняет только воркер exec")
    async with claimed(rt.engine, LOGIN_CONSUMER, event_id) as first:
        pass
    if not first:
        return "duplicate"
    context.bind(workspace_id=ws)
    db = DbContext(workspace_id=ws, actor_type="system")
    async with transaction(rt.engine, db) as conn:
        if not await login_registry.claim_session(
            conn, session_id=session_id, provider_id=provider_id, host_id=rt.worker_host_id
        ):
            return "ignored"  # отменена или просрочена до старта

    async def cancelled() -> bool:
        async with transaction(rt.engine, db) as conn:
            current = await login_registry.session_state(conn, session_id=session_id)
        return current not in login_registry.ACTIVE_STATES

    lines: list[str] = []
    outcome = login.LoginFinished(state="FAILED", error_category="PROCESS_FAILED")
    # Коды авторизации оператора идут в stdin CLI через pub/sub и нигде не сохраняются (`login_input`).
    async with _code_inbox(rt, session_id) as code_input:
        async for item in rt.provider_login(str(provider), cancelled, code_input):
            if isinstance(item, login.LoginLine):
                lines.append(item.text)
                del lines[: max(0, len(lines) - login.MAX_LINES)]
                async with transaction(rt.engine, db) as conn:
                    await login_registry.record_output(conn, session_id=session_id, output_tail="\n".join(lines))
            elif isinstance(item, login.DeviceChallenge):
                async with transaction(rt.engine, db) as conn:
                    await login_registry.record_challenge(
                        conn, session_id=session_id, challenge=item, output_tail="\n".join(lines)
                    )
            else:
                outcome = item
    async with transaction(rt.engine, db) as conn:
        await login_registry.finish_session(
            conn,
            workspace_id=ws,
            provider_id=provider_id,
            session_id=session_id,
            state=outcome.state,
            error_category=outcome.error_category,
            output_tail="\n".join(lines),
        )
    if outcome.state == "AUTHENTICATED" and rt.provider_prober is not None:
        try:
            probe = await rt.provider_prober(str(provider))
        except Exception:
            log.exception("probe после входа не выполнен", extra={"provider": provider})
        else:
            if probe.installed and probe.binary_path is not None:
                async with transaction(rt.engine, db) as conn:
                    await registry.record_probe(conn, host_id=rt.worker_host_id, probe=probe)
    return "provider_login_finished"


@asynccontextmanager
async def _code_inbox(rt: Runtime, session_id: uuid.UUID) -> AsyncIterator[CodeInput | None]:
    """Подписка на коды авторизации сессии; без канала вход идёт как раньше, без ввода."""
    if rt.login_code_inbox is None:
        yield None
        return
    async with rt.login_code_inbox(session_id) as receive:
        yield receive


async def handle_event(rt: Runtime, event: dict[str, Any]) -> Outcome:
    event_id = _uuid(event.get("event_id"))
    run_id = _uuid(event.get("run_id"))
    ws = _uuid(event.get("workspace_id"))
    if event_id is None or run_id is None or ws is None:
        return "ignored"
    context.bind(run_id=run_id, workspace_id=ws)
    async with claimed(rt.engine, ORCHESTRATOR_CONSUMER, event_id) as first:
        if not first:
            return "duplicate"
        return await _route(rt, event, event_id, run_id, ws)


async def _route(rt: Runtime, event: dict[str, Any], event_id: uuid.UUID, run_id: uuid.UUID, ws: uuid.UUID) -> Outcome:
    etype = event.get("event_type")
    payload = event.get("payload") or {}
    orch = rt.orchestrator
    if etype == "agent.message":
        # Каждая доставка идемпотентна сама (message_deliveries: блокировка строки, ack); сбой → backoff/DLQ.
        for consumer_id, handler in ACP_PROJECTORS:
            await acp.handle_outbox_event(rt.engine, event, consumer_id=consumer_id, handler=handler)
        return "acp_delivered"
    if etype == "run.created":
        await orch.on_run_created(run_id, ws)
        return "on_run_created"
    if etype == "directive.state_changed":
        to_status = payload.get("to_status", payload.get("to"))
        directive_id = _uuid(payload.get("directive_id")) or _uuid(event.get("aggregate_id"))
        if to_status not in DIRECTIVE_STEPS or directive_id is None or rt.process_directive is None:
            return "ignored"
        await rt.process_directive(ws, directive_id)
        # Новая plan_version: задачи замены, ставшие READY, — в exec (как после любого перехода).
        for ready_id in await orch.tick(run_id, ws):
            await rt.dispatch_execution(execution_message(ready_id, ws, event_id))
        return "directive_processed"
    if etype == "approval.decided":
        decision = await _plan_decision(rt.engine, _uuid(event.get("aggregate_id")), run_id, ws, payload)
        if decision is not None:
            approved, plan_version = decision
            await orch.on_plan_approval_decided(run_id, ws, approved=approved, plan_version=plan_version)
            return "on_plan_approval_decided"
        approval_id = _uuid(event.get("aggregate_id"))
        if approval_id is not None:  # approvals уровня задачи (MANUAL_REVIEW / SELF_REVIEW_FALLBACK)
            await orch.on_task_approval_decided(approval_id, ws)
    if etype == "run.state_changed":
        to_status = payload.get("to_status", payload.get("to"))
        from_status = payload.get("from_status", payload.get("from"))
        if to_status in ("PAUSED", "CANCELLED"):
            await orch.on_run_paused_or_cancelled(run_id, ws, str(to_status))
            return "on_run_paused_or_cancelled"
        if from_status == "PAUSED":
            await orch.on_run_resumed(run_id, ws)
            return "on_run_resumed"
    if etype == "task.state_changed" and payload.get("to_status", payload.get("to")) == "READY":
        task_id = _uuid(payload.get("task_id")) or _uuid(event.get("aggregate_id"))
        if task_id is not None:
            await rt.dispatch_execution(execution_message(task_id, ws, event_id))
            return "execute_task"
    # tick возвращает READY-задачи, для которых освободился слот параллелизма (они уже были READY и
    # нового события READY не получат) — ставим их в exec; повторная постановка безопасна (execute_task идемпотентен).
    for ready_id in await orch.tick(run_id, ws):
        await rt.dispatch_execution(execution_message(ready_id, ws, event_id))
    return "tick"


async def _plan_decision(
    engine: AsyncEngine, approval_id: uuid.UUID | None, run_id: uuid.UUID, ws: uuid.UUID, payload: dict[str, Any]
) -> tuple[bool, int] | None:
    """(approved, plan_version) для решения по PLAN_APPROVAL; None — иное действие/статус."""
    if approval_id is None:
        return None
    async with transaction(engine, DbContext(workspace_id=ws, actor_type="system")) as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT a.action_type, a.status, a.subject, r.plan_version FROM approvals a"
                        " JOIN runs r ON r.id = a.run_id WHERE a.id = :a AND a.run_id = :r"
                    ),
                    {"a": approval_id, "r": run_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None or row["action_type"] != "PLAN_APPROVAL" or row["status"] not in ("APPROVED", "REJECTED"):
        return None
    subject = row["subject"] if isinstance(row["subject"], dict) else {}
    raw = payload.get("plan_version", subject.get("plan_version", row["plan_version"]))
    try:
        plan_version = int(raw)
    except (TypeError, ValueError):
        plan_version = int(row["plan_version"])
    return row["status"] == "APPROVED", plan_version


def execution_message(task_id: uuid.UUID, ws: uuid.UUID, cause: uuid.UUID | None = None) -> dict[str, Any]:
    """Сообщение очереди exec. `event_id` уникален на постановку (дедупликация повторной доставки);
    `traceparent` продолжает трассу обработчика события в воркере exec (P8-06)."""
    return {
        "event_id": str(uuid7()),
        "task_id": str(task_id),
        "workspace_id": str(ws),
        "causation_id": str(cause) if cause else None,
        "traceparent": current_traceparent(),
    }


async def execute_task(rt: Runtime, message: dict[str, Any]) -> Outcome:
    """Исполнение задачи. Отметка фиксируется до запуска (длинная операция не держит транзакцию);
    восстановление после падения — reaper аренд и периодический tick (maintenance)."""
    event_id = _uuid(message.get("event_id"))
    task_id = _uuid(message.get("task_id"))
    ws = _uuid(message.get("workspace_id"))
    if event_id is None or task_id is None or ws is None:
        return "ignored"
    async with claimed(rt.engine, EXEC_CONSUMER, event_id) as first:
        pass
    if not first:
        return "duplicate"
    context.bind(task_id=task_id, workspace_id=ws)
    await rt.orchestrator.execute_task(task_id, ws)
    return "execute_task"
