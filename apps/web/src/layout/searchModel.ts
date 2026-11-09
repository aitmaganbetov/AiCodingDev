import type { SearchResult, SearchResultType } from '../api/search';
import { SEARCH_MAX_LENGTH, SEARCH_MIN_LENGTH } from '../api/search';
import type { IconName } from '../components/ui/Icon';

/** Порядок групп результатов (как в контракте SearchResultType). */
export const SEARCH_TYPES: readonly SearchResultType[] = ['project', 'run', 'task', 'finding', 'approval', 'documentation_source', 'chat_message'];

export const SEARCH_TYPE_ICON: Record<SearchResultType, IconName> = {
  project: 'folder',
  run: 'workflow',
  task: 'listChecks',
  finding: 'security',
  approval: 'approval',
  documentation_source: 'book',
  chat_message: 'message',
};

/** Запрос для API: обрезка пробелов и длины; `null` — короче минимума (запрос не отправляется). */
export function normalizeQuery(raw: string): string | null {
  const q = raw.trim().slice(0, SEARCH_MAX_LENGTH).trim();
  return q.length >= SEARCH_MIN_LENGTH ? q : null;
}

/**
 * Маршрут SPA для результата поиска (подсказки контракта: id контекста; `api_path` — путь API,
 * не маршрут). Run без project_id открывается через Mission Control `?run=` (резолвер по одному id).
 */
export function searchResultHref(workspaceId: string, r: SearchResult): string {
  const w = `/w/${workspaceId}`;
  const runHref = (runId: string) => (r.project_id ? `${w}/projects/${r.project_id}/runs/${runId}` : `${w}/mission-control?run=${runId}`);
  switch (r.type) {
    case 'project':
      return `${w}/projects/${r.id}`;
    case 'run':
      return runHref(r.id);
    case 'task':
      return r.run_id && r.project_id ? `${w}/projects/${r.project_id}/runs/${r.run_id}/tasks/${r.id}` : r.run_id ? runHref(r.run_id) : `${w}/runs`;
    case 'finding':
      return `${w}/quality/findings/${r.id}`;
    case 'approval':
      return `${w}/approvals/${r.id}`;
    case 'documentation_source':
      return `${w}/knowledge`;
    case 'chat_message':
      return r.run_id && r.project_id ? `${w}/projects/${r.project_id}/runs/${r.run_id}/chat` : r.run_id ? runHref(r.run_id) : `${w}/runs`;
    default:
      return `${w}/dashboard`;
  }
}

/** Группы результатов в порядке SEARCH_TYPES (внутри группы — порядок сервера: score, тип, новизна). */
export function groupResults(items: readonly SearchResult[]): { type: SearchResultType; items: SearchResult[] }[] {
  const groups = new Map<SearchResultType, SearchResult[]>();
  for (const item of items) groups.set(item.type, [...(groups.get(item.type) ?? []), item]);
  return SEARCH_TYPES.filter((type) => groups.has(type)).map((type) => ({ type, items: groups.get(type) ?? [] }));
}
