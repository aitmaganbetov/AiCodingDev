/**
 * Поток уведомлений workspace `GET /workspaces/{id}/events` (API_DESIGN §13.1, OQ-45 (c)):
 * approval.requested / approval.decided / run.state_changed / run.completed всех run workspace.
 * `id:` — курсор (время записи события), не seq: возобновление по Last-Event-ID с перекрытием,
 * дедупликация — по event_id (EventStream resume = 'cursor'). Поток не терминальный.
 */
import { seg, type Schemas } from './client';
import type { StreamSpec } from './streamManager';

export type WorkspaceSseEventType = Schemas['WorkspaceSseEventType'];

export function workspaceEventsSpec(workspaceId: string, types: readonly WorkspaceSseEventType[] = []): StreamSpec {
  return {
    path: `/workspaces/${seg(workspaceId)}/events`,
    ...(types.length > 0 ? { query: { types: [...types] } } : {}),
    resume: 'cursor',
  };
}
