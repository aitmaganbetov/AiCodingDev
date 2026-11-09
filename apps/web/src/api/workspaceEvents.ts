/**
 * Поток уведомлений workspace `GET /workspaces/{id}/events` (API_DESIGN §13.1, OQ-45 (c)):
 * approval.requested / approval.decided / run.state_changed / run.completed всех run workspace.
 * `id:` — курсор (время записи события), не seq: возобновление по Last-Event-ID с перекрытием,
 * дедупликация — по event_id (EventStream resume = 'cursor'). Поток не терминальный.
 */
import { seg, type Schemas } from './client';
import type { StreamSpec } from './streamManager';

export type WorkspaceSseEventType = Schemas['WorkspaceSseEventType'];

/**
 * Все типы потока уведомлений. Оболочка (уведомления, счётчик approvals) и экраны workspace
 * подписываются одним и тем же набором, чтобы делить одно соединение StreamManager (лимит сервера —
 * 5 потоков на пользователя, API_DESIGN §13), и фильтруют события у себя.
 */
export const WORKSPACE_NOTIFICATION_TYPES: readonly WorkspaceSseEventType[] = [
  'approval.decided',
  'approval.requested',
  'run.completed',
  'run.state_changed',
];

export function workspaceEventsSpec(workspaceId: string, types: readonly WorkspaceSseEventType[] = []): StreamSpec {
  return {
    path: `/workspaces/${seg(workspaceId)}/events`,
    ...(types.length > 0 ? { query: { types: [...types] } } : {}),
    resume: 'cursor',
  };
}
