/**
 * Code Intelligence (B3): символы и граф зависимостей по снимку индекса кода, задания индексации.
 * Все операции — `project:read`. Снимок: без `commit` — последний READY-снимок репозитория
 * (нет снимка — `snapshot: null` и пустой ответ), с `commit` — снимок этого commit (нет — 404).
 */
import { request, seg, type JsonResponse, type Query, type Schemas } from './client';

export type CodeSymbol = Schemas['CodeSymbol'];
export type CodeSymbolKind = Schemas['CodeSymbolKind'];
export type CodeSymbolPage = Schemas['CodeSymbolPage'];
export type IndexSnapshotRef = Schemas['IndexSnapshotRef'];
export type DependencyGraph = Schemas['DependencyGraph'];
export type DependencyNode = Schemas['DependencyNode'];
export type DependencyEdge = Schemas['DependencyEdge'];
export type IndexingJob = Schemas['IndexingJob'];
export type IndexingJobStatus = Schemas['IndexingJobStatus'];
export type SymbolsQuery = Query<'listRepositorySymbols'>;
export type DependenciesQuery = Query<'getRepositoryDependencies'>;
export type IndexingJobsQuery = Query<'listIndexingJobs'>;

export const CODE_SYMBOL_KINDS: readonly CodeSymbolKind[] = [
  'module',
  'class',
  'interface',
  'function',
  'method',
  'variable',
  'constant',
  'type',
  'enum',
  'test',
  'route',
  'other',
];
export const INDEXING_JOB_STATUSES: readonly IndexingJobStatus[] = ['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'];

const repoPath = (projectId: string, repositoryId: string) => `/projects/${seg(projectId)}/repositories/${seg(repositoryId)}`;

export const codeIntelKeys = {
  symbols: (projectId: string, repositoryId: string, query: Omit<SymbolsQuery, 'cursor'>) =>
    ['project', projectId, 'repository', repositoryId, 'symbols', query] as const,
  dependencies: (projectId: string, repositoryId: string, query: DependenciesQuery) =>
    ['project', projectId, 'repository', repositoryId, 'dependencies', query] as const,
  indexingJobs: (projectId: string, repositoryId: string, query: Omit<IndexingJobsQuery, 'cursor'> = {}) =>
    ['project', projectId, 'repository', repositoryId, 'indexing-jobs', query] as const,
};

export const codeIntelApi = {
  /** Ранжированный поиск символов (score, имя, путь, строка); курсор привязан к снимку и фильтрам. */
  symbols(projectId: string, repositoryId: string, query: SymbolsQuery, signal?: AbortSignal): Promise<JsonResponse<'listRepositorySymbols', 200>> {
    return request({ method: 'GET', path: `${repoPath(projectId, repositoryId)}/symbols`, query, signal });
  },

  /** Подграф зависимостей вокруг файла/каталога (`path`) или символа (`symbol`); ≤ 200 узлов / 500 рёбер. */
  dependencies(projectId: string, repositoryId: string, query: DependenciesQuery, signal?: AbortSignal): Promise<JsonResponse<'getRepositoryDependencies', 200>> {
    return request({ method: 'GET', path: `${repoPath(projectId, repositoryId)}/dependencies`, query, signal });
  },

  /** Очередь и история индексации репозитория (новые первыми), ошибки — безопасный текст сервера. */
  indexingJobs(projectId: string, repositoryId: string, query: IndexingJobsQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listIndexingJobs', 200>> {
    return request({ method: 'GET', path: `${repoPath(projectId, repositoryId)}/indexing-jobs`, query, signal });
  },
};
