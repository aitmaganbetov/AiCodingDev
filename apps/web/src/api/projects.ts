/** Проекты, репозитории и стек (X-Workspace-Id для коллекции, API_DESIGN §4). */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type Project = Schemas['Project'];
export type ProjectPage = Schemas['ProjectPage'];
export type DocumentationMode = Schemas['DocumentationMode'];
export type QualityProfile = Schemas['QualityProfile'];
export type ProjectCreateRequest = JsonBody<'createProject'>;
export type ListProjectsQuery = Query<'listProjects'>;
export type Repository = Schemas['Repository'];
export type RepositoryProvider = Schemas['RepositoryProvider'];
export type RepositoryAttachRequest = JsonBody<'attachRepository'>;
export type Technology = Schemas['Technology'];
export type ListRepositoriesQuery = Query<'listRepositories'>;
export type ListTechnologiesQuery = Query<'listTechnologies'>;
export type RepositoryTreePage = Schemas['RepositoryTreePage'];
export type RepositoryTreeEntry = Schemas['RepositoryTreeEntry'];
export type RepositoryFile = Schemas['RepositoryFile'];
export type RepositoryTreeQuery = Query<'listRepositoryTree'>;
export type RepositoryFileQuery = Query<'getRepositoryFile'>;

export const projectKeys = {
  list: (workspaceId: string) => ['projects', workspaceId] as const,
  /** Первая страница для сводок (Dashboard, селекторы проекта): префикс list → общая инвалидация. */
  summary: (workspaceId: string) => ['projects', workspaceId, 'summary'] as const,
  detail: (projectId: string) => ['project', projectId] as const,
  repositories: (projectId: string) => ['project', projectId, 'repositories'] as const,
  technologies: (projectId: string) => ['project', projectId, 'technologies'] as const,
  /** Repository Explorer: commit '' — ветка по умолчанию (сервер разрешает в SHA). */
  tree: (projectId: string, repositoryId: string, commit: string, path: string) =>
    ['project', projectId, 'repository', repositoryId, 'tree', commit, path] as const,
  file: (projectId: string, repositoryId: string, commit: string, path: string) =>
    ['project', projectId, 'repository', repositoryId, 'file', commit, path] as const,
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

  repositories(projectId: string, query: ListRepositoriesQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listRepositories', 200>> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}/repositories`, query, signal });
  },

  /** 201: репозиторий зарегистрирован, индексация поставлена в очередь (асинхронно). */
  attachRepository(projectId: string, body: RepositoryAttachRequest, idempotencyKey: string): Promise<JsonResponse<'attachRepository', 201>> {
    return request({ method: 'POST', path: `/projects/${seg(projectId)}/repositories`, body, idempotencyKey });
  },

  /** FR-PRJ-05: один уровень дерева каталога `path` в commit (без commit — HEAD ветки по умолчанию). */
  tree(projectId: string, repositoryId: string, query: RepositoryTreeQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listRepositoryTree', 200>> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}/repositories/${seg(repositoryId)}/tree`, query, signal });
  },

  /** FR-PRJ-05: файл как текст (усечённый до max_bytes) или признак бинарного содержимого. */
  file(projectId: string, repositoryId: string, query: RepositoryFileQuery, signal?: AbortSignal): Promise<JsonResponse<'getRepositoryFile', 200>> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}/repositories/${seg(repositoryId)}/file`, query, signal });
  },

  technologies(projectId: string, query: ListTechnologiesQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listTechnologies', 200>> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}/technologies`, query, signal });
  },
};
