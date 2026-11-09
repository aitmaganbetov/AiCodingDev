/** Глобальный поиск по workspace (B6, `GET /search`, operationId searchWorkspace). */
import { request, type JsonResponse, type Query, type Schemas } from './client';

export type SearchResult = Schemas['SearchResult'];
export type SearchResults = Schemas['SearchResults'];
export type SearchResultType = Schemas['SearchResultType'];
export type SearchQuery = Query<'searchWorkspace'>;

/** Минимальная длина запроса (контракт: minLength 2 без пробелов по краям, иначе 422). */
export const SEARCH_MIN_LENGTH = 2;
export const SEARCH_MAX_LENGTH = 200;

export const searchKeys = {
  query: (workspaceId: string, q: string, limit: number) => ['search', workspaceId, q, limit] as const,
};

export const searchApi = {
  search(workspaceId: string, query: SearchQuery, signal?: AbortSignal): Promise<JsonResponse<'searchWorkspace', 200>> {
    return request({ method: 'GET', path: '/search', query, workspaceId, signal });
  },
};
