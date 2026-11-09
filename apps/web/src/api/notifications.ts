/**
 * Персональные уведомления (UI redesign B8, API_DESIGN §13.2, ADR-0016): `GET /notifications`,
 * `GET /notifications/unread-count`, `POST /notifications/mark-read` (Idempotency-Key). Live — кадры
 * `notification.created` потока workspace (`GET /workspaces/{id}/events`, EVENT_SCHEMAS §3.2a):
 * `event_id` = id уведомления, `payload` = Notification. Права: `notification:read` / `notification:update`.
 */
import { request, type JsonBody, type Query, type Schemas } from './client';
import type { StreamEvent } from './sse';

export type Notification = Schemas['Notification'];
export type NotificationKind = Schemas['NotificationKind'];
export type NotificationSeverity = Schemas['NotificationSeverity'];
export type NotificationPage = Schemas['NotificationPage'];
export type NotificationUnreadCount = Schemas['NotificationUnreadCount'];
export type NotificationMarkReadRequest = JsonBody<'markNotificationsRead'>;
export type NotificationMarkReadResult = Schemas['NotificationMarkReadResult'];
export type ListNotificationsQuery = Query<'listNotifications'>;

export const NOTIFICATION_CREATED = 'notification.created';

export const notificationKeys = {
  all: (workspaceId: string) => ['notifications', workspaceId] as const,
  list: (workspaceId: string, query: ListNotificationsQuery = {}) => ['notifications', workspaceId, 'list', query] as const,
  unread: (workspaceId: string) => ['notifications', workspaceId, 'unread'] as const,
};

export const notificationsApi = {
  list(workspaceId: string, query: ListNotificationsQuery = {}, signal?: AbortSignal): Promise<NotificationPage> {
    // client.ts принимает строковые query: boolean → "true" / "false".
    const { unread_only: unreadOnly, ...rest } = query;
    return request({
      method: 'GET',
      path: '/notifications',
      query: { ...rest, unread_only: unreadOnly === undefined ? undefined : String(unreadOnly) },
      workspaceId,
      signal,
    });
  },

  unreadCount(workspaceId: string, signal?: AbortSignal): Promise<NotificationUnreadCount> {
    return request({ method: 'GET', path: '/notifications/unread-count', workspaceId, signal });
  },

  /** Ровно одно из: `notification_ids`, `up_to`, `all: true`. Один Idempotency-Key на намерение пользователя. */
  markRead(workspaceId: string, body: NotificationMarkReadRequest, idempotencyKey: string): Promise<NotificationMarkReadResult> {
    return request({ method: 'POST', path: '/notifications/mark-read', body, idempotencyKey, workspaceId });
  },
};

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/**
 * Уведомление из кадра `notification.created` (payload = Notification). Кадр без обязательных полей
 * (повреждённый или иной версии) игнорируется — список затем всё равно перечитывается из REST.
 */
export function notificationFromEvent(e: StreamEvent): Notification | null {
  if (e.type !== NOTIFICATION_CREATED || !isRecord(e.payload)) return null;
  const p = e.payload;
  const ok =
    typeof p.id === 'string' &&
    typeof p.kind === 'string' &&
    typeof p.severity === 'string' &&
    typeof p.title === 'string' &&
    typeof p.body === 'string' &&
    typeof p.resource_type === 'string' &&
    typeof p.resource_id === 'string' &&
    typeof p.created_at === 'string' &&
    typeof p.read === 'boolean' &&
    isRecord(p.data);
  return ok ? (p as unknown as Notification) : null;
}

/** Ссылка уведомления в UI (только идентификаторы контракта; маршруты F2). */
export function notificationHref(workspaceId: string, n: Pick<Notification, 'resource_type' | 'resource_id' | 'project_id' | 'run_id'>): string {
  const base = `/w/${workspaceId}`;
  if (n.resource_type === 'approval') return `${base}/approvals/${n.resource_id}`;
  if (n.resource_type === 'gate_execution' && n.run_id) return `${base}/quality/runs/${n.run_id}`;
  if (n.run_id && n.project_id) return `${base}/projects/${n.project_id}/runs/${n.run_id}`;
  if (n.run_id) return `${base}/mission-control?run=${n.run_id}`;
  return `${base}/dashboard`;
}
