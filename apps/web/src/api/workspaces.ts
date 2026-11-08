/** Рабочие пространства (tenancy, API_DESIGN §4). */
import { request, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type Workspace = Schemas['Workspace'];
export type WorkspacePage = Schemas['WorkspacePage'];
export type WorkspaceRole = Schemas['WorkspaceRole'];
export type WorkspaceCreateRequest = JsonBody<'createWorkspace'>;
export type ListWorkspacesQuery = Query<'listWorkspaces'>;

export const workspaceKeys = {
  all: ['workspaces'] as const,
};

export const workspacesApi = {
  list(query: ListWorkspacesQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listWorkspaces', 200>> {
    return request({ method: 'GET', path: '/workspaces', query, signal });
  },

  create(body: WorkspaceCreateRequest, idempotencyKey: string): Promise<JsonResponse<'createWorkspace', 201>> {
    return request({ method: 'POST', path: '/workspaces', body, idempotencyKey });
  },
};
