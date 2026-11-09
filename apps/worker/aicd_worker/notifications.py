"""Очередь `notifications` (UI redesign B8, G-15): персональные уведомления из outbox-событий.

Worker-outbox ставит в очередь события `aicd_orchestration.notifications.SOURCE_EVENT_TYPES` (в дополнение к
обычной маршрутизации); актор `notifications.project` (воркер orchestrator) перечитывает состояние агрегата,
пишет строки `notifications` и `notification.created` в одной транзакции с отметкой `processed_events`.
"""

from __future__ import annotations

from typing import Any, Final

from aicd_orchestration import notifications
from aicd_platform import context
from aicd_worker.runtime import Runtime

NOTIFICATIONS_CONSUMER: Final = notifications.CONSUMER


def is_notification_source(event: dict[str, Any]) -> bool:
    return notifications.is_source(event)


async def handle_event(rt: Runtime, event: dict[str, Any]) -> str:
    ws = event.get("workspace_id")
    if ws:
        context.bind(workspace_id=ws)
    return await notifications.project(rt.engine, event)
