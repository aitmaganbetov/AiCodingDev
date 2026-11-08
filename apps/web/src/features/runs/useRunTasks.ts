import { useInfiniteQuery, type InfiniteData, type QueryClient } from '@tanstack/react-query';
import { useEffect, useMemo } from 'react';
import { runKeys, runsApi, type TaskDependency, type TaskGraph, type TaskSummary } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';

export const TASKS_PAGE_SIZE = 200;

/**
 * DAG задач run (getRunTasks, cursor-пагинация). Один кэш на run для Run view, графа, чата
 * и Task Detail. `all` — догрузить все страницы (графу нужен весь DAG).
 */
export function useRunTasks(runId: string, { all = false, enabled = true }: { all?: boolean; enabled?: boolean } = {}) {
  const query = useInfiniteQuery({
    queryKey: runKeys.tasks(runId),
    queryFn: ({ pageParam, signal }) =>
      runsApi.tasks(runId, { limit: TASKS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: Boolean(runId) && enabled,
  });
  const { hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage } = query;
  useEffect(() => {
    if (all && hasNextPage && !isFetchingNextPage && !isFetchNextPageError) void fetchNextPage();
  }, [all, hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage]);

  const pages = query.data?.pages;
  const tasks = useMemo(() => pages?.flatMap((p) => p.tasks) ?? [], [pages]);
  const edges = useMemo(() => {
    const seen = new Set<string>();
    const out: TaskDependency[] = [];
    const push = (e: TaskDependency) => {
      const k = `${e.depends_on_task_id}>${e.task_id}`;
      if (!seen.has(k)) {
        seen.add(k);
        out.push(e);
      }
    };
    for (const p of pages ?? []) for (const e of p.edges) push(e);
    // depends_on задачи — тот же DAG; страница может не нести рёбра чужих страниц.
    for (const task of tasks) for (const d of task.depends_on) push({ task_id: task.id, depends_on_task_id: d, dependency_type: 'blocks' });
    return out;
  }, [pages, tasks]);
  const planVersion = pages?.[0]?.plan_version ?? 0;
  return { query, tasks, edges, planVersion };
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined;
}

/**
 * Применяет task.state_changed к кэшу DAG сразу (живой статус без ожидания REST);
 * версия задачи защищает от отката более старым событием. Возвращает true, если что-то изменилось.
 */
export function applyTaskEvents(queryClient: QueryClient, runId: string, events: readonly RunEvent[]): boolean {
  const updates = events.filter((e) => e.type === 'task.state_changed');
  if (updates.length === 0) return false;
  let changed = false;
  queryClient.setQueryData<InfiniteData<TaskGraph, string | null>>(runKeys.tasks(runId), (data) => {
    if (!data) return data;
    const pages = data.pages.map((page) => ({
      ...page,
      tasks: page.tasks.map((task) => {
        let next: TaskSummary = task;
        for (const e of updates) {
          const p = e.payload;
          if (p.task_id !== task.id) continue;
          const version = typeof p.task_version === 'number' ? p.task_version : null;
          if (version !== null && version < next.version) continue;
          const status = str(p.to_status) as TaskSummary['status'] | undefined;
          if (!status) continue;
          next = {
            ...next,
            status,
            blocked_reason: (str(p.blocked_reason) as TaskSummary['blocked_reason']) ?? null,
            ...(version !== null ? { version } : {}),
            ...(typeof p.attempt_no === 'number' ? { attempt_count: Math.max(next.attempt_count, p.attempt_no) } : {}),
            ...(p.provider === 'codex' || p.provider === 'claude' || p.provider === null
              ? { current_provider: p.provider }
              : {}),
          };
        }
        if (next !== task) changed = true;
        return next;
      }),
    }));
    return changed ? { ...data, pages } : data;
  });
  return changed;
}
