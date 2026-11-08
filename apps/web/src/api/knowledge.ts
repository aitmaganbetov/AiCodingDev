/** Документация: источники (workspace), режим документации проекта (API_DESIGN §4, §8). */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type DocumentationSource = Schemas['DocumentationSource'];
export type DocumentationSourceCreateRequest = JsonBody<'registerDocumentationSource'>;
export type DocumentationSourceType = Schemas['DocumentationSourceType'];
export type DocumentationTrustLevel = Schemas['DocumentationTrustLevel'];
export type DocumentationRefreshPolicy = Schemas['DocumentationRefreshPolicy'];
export type DocumentationPolicy = Schemas['DocumentationPolicy'];
export type ListDocumentationSourcesQuery = Query<'listDocumentationSources'>;

export const knowledgeKeys = {
  sources: (workspaceId: string, filter: string) => ['documentation-sources', workspaceId, filter] as const,
  allSources: (workspaceId: string) => ['documentation-sources', workspaceId] as const,
  policy: (projectId: string) => ['project', projectId, 'documentation-policy'] as const,
};

export const knowledgeApi = {
  listSources(
    workspaceId: string,
    query: ListDocumentationSourcesQuery = {},
    signal?: AbortSignal,
  ): Promise<JsonResponse<'listDocumentationSources', 200>> {
    // Транспорт сериализует только строки/числа: boolean-фильтр передаётся как "true"/"false".
    const { enabled, ...rest } = query;
    return request({
      method: 'GET',
      path: '/documentation/sources',
      query: { ...rest, ...(enabled === undefined ? {} : { enabled: String(enabled) }) },
      workspaceId,
      signal,
    });
  },

  /** Регистрация не загружает содержимое. */
  registerSource(
    workspaceId: string,
    body: DocumentationSourceCreateRequest,
    idempotencyKey: string,
  ): Promise<JsonResponse<'registerDocumentationSource', 201>> {
    return request({ method: 'POST', path: '/documentation/sources', body, workspaceId, idempotencyKey });
  },

  /** Явный запрос человека = согласие на загрузку вне run; 202 QUEUED, disabled ⇒ 409. */
  refreshSource(sourceId: string, idempotencyKey: string): Promise<JsonResponse<'refreshDocumentationSource', 202>> {
    return request({ method: 'POST', path: `/documentation/sources/${seg(sourceId)}/refresh`, idempotencyKey });
  },

  getPolicy(projectId: string, signal?: AbortSignal): Promise<JsonResponse<'getDocumentationPolicy', 200>> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}/documentation-policy`, signal });
  },

  updatePolicy(
    projectId: string,
    body: JsonBody<'updateDocumentationPolicy'>,
    settingsVersion: number,
  ): Promise<JsonResponse<'updateDocumentationPolicy', 200>> {
    return request({ method: 'PATCH', path: `/projects/${seg(projectId)}/documentation-policy`, body, ifMatchVersion: settingsVersion });
  },
};
