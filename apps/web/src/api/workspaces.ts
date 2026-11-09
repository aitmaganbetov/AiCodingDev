/** Рабочие пространства (tenancy, API_DESIGN §4). */
import { request, requestWithMeta, seg, type ApiResponse, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type Workspace = Schemas['Workspace'];
export type WorkspacePage = Schemas['WorkspacePage'];
export type WorkspaceRole = Schemas['WorkspaceRole'];
export type WorkspaceCreateRequest = JsonBody<'createWorkspace'>;
export type ListWorkspacesQuery = Query<'listWorkspaces'>;
export type WorkspaceSettings = Schemas['WorkspaceSettings'];
export type WorkspaceUpdateRequest = JsonBody<'updateWorkspace'>;

export const workspaceKeys = {
  all: ['workspaces'] as const,
  detail: (workspaceId: string) => ['workspaces', workspaceId, 'detail'] as const,
};

export const workspacesApi = {
  list(query: ListWorkspacesQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listWorkspaces', 200>> {
    return request({ method: 'GET', path: '/workspaces', query, signal });
  },

  create(body: WorkspaceCreateRequest, idempotencyKey: string): Promise<JsonResponse<'createWorkspace', 201>> {
    return request({ method: 'POST', path: '/workspaces', body, idempotencyKey });
  },

  /** OQ-45 (f): workspace с настройками; ETag ответа — основа If-Match для update. */
  get(workspaceId: string, signal?: AbortSignal): Promise<ApiResponse<JsonResponse<'getWorkspace', 200>>> {
    return requestWithMeta({ method: 'GET', path: `/workspaces/${seg(workspaceId)}`, signal });
  },

  /**
   * Merge-patch названия/политик. If-Match — ETag последнего ответа (предпочтительно) или `"v<version>"`.
   * Политики меняет только owner (403 для admin).
   */
  update(
    workspaceId: string,
    body: WorkspaceUpdateRequest,
    precondition: { etag?: string | null; version?: number | undefined },
  ): Promise<ApiResponse<JsonResponse<'updateWorkspace', 200>>> {
    return requestWithMeta({
      method: 'PATCH',
      path: `/workspaces/${seg(workspaceId)}`,
      body,
      ifMatch: precondition.etag ?? undefined,
      ifMatchVersion: precondition.version,
    });
  },
};
