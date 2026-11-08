/** Журнал аудита (GET /audit-events, право audit:read; сам запрос аудируется как audit.query). */
import { request, type JsonResponse, type Query, type Schemas } from './client';

export type AuditEvent = Schemas['AuditEvent'];
export type ListAuditEventsQuery = Query<'listAuditEvents'>;
export type AuditFilters = Pick<ListAuditEventsQuery, 'actor_id' | 'action' | 'resource_type' | 'resource_id' | 'from' | 'to'>;

export const auditKeys = {
  list: (workspaceId: string, filters: AuditFilters) => ['audit-events', workspaceId, filters] as const,
};

export const auditApi = {
  list(workspaceId: string, query: ListAuditEventsQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listAuditEvents', 200>> {
    return request({ method: 'GET', path: '/audit-events', query, workspaceId, signal });
  },
};
