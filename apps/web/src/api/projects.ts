/** Проекты (X-Workspace-Id для коллекции, API_DESIGN §4). */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type Project = Schemas['Project'];
export type ProjectPage = Schemas['ProjectPage'];
export type DocumentationMode = Schemas['DocumentationMode'];
export type QualityProfile = Schemas['QualityProfile'];
export type ProjectCreateRequest = JsonBody<'createProject'>;
export type ListProjectsQuery = Query<'listProjects'>;

export const projectKeys = {
  list: (workspaceId: string) => ['projects', workspaceId] as const,
  detail: (projectId: string) => ['project', projectId] as const,
};

export const projectsApi = {
  list(workspaceId: string, query: ListProjectsQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listProjects', 200>> {
    return request({ method: 'GET', path: '/projects', query, workspaceId, signal });
  },

  create(workspaceId: string, body: ProjectCreateRequest, idempotencyKey: string): Promise<JsonResponse<'createProject', 201>> {
    return request({ method: 'POST', path: '/projects', body, workspaceId, idempotencyKey });
  },

  get(projectId: string, signal?: AbortSignal): Promise<JsonResponse<'getProject', 200>> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}`, signal });
  },
};
