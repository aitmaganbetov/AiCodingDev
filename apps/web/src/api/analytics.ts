/** Analytics (GET /analytics/summary, право run:read; X-Workspace-Id). P8-07, FR-OBS-05, FR-CI-08. */
import { request, type JsonResponse, type Query, type Schemas } from './client';

export type AnalyticsSummary = Schemas['AnalyticsSummary'];
export type AnalyticsQuery = Query<'getAnalyticsSummary'>;
export type AnalyticsDurationStats = Schemas['AnalyticsDurationStats'];

export const analyticsKeys = {
  summary: (workspaceId: string, query: AnalyticsQuery) => ['analytics', workspaceId, query] as const,
};

export const analyticsApi = {
  summary(workspaceId: string, query: AnalyticsQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'getAnalyticsSummary', 200>> {
    return request({ method: 'GET', path: '/analytics/summary', query, workspaceId, signal });
  },
};
