/**
 * Запросы Dashboard (F3): каждая плитка — отдельный запрос со своим состоянием (ошибка одной
 * операции не ломает остальные). Опрос TanStack Query останавливается, пока вкладка скрыта
 * (refetchIntervalInBackground = false). Живые обновления — поток уведомлений workspace (общее
 * соединение с оболочкой): события инвалидируют затронутые плитки и дополняют ленту активности.
 */
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo } from 'react';
import { analyticsApi, analyticsKeys } from '../../api/analytics';
import { approvalKeys, approvalsApi, type ListApprovalsQuery } from '../../api/approvals';
import { findingKeys, findingsApi, type ListWorkspaceFindingsQuery } from '../../api/findings';
import { projectKeys, projectsApi } from '../../api/projects';
import { runKeys, runsApi, type ListRunsQuery, type WorkspaceActivityQuery } from '../../api/runs';
import type { StreamEvent } from '../../api/sse';
import { systemApi, systemKeys } from '../../api/system';
import { taskKeys, tasksApi, type ListWorkspaceTasksQuery } from '../../api/tasks';
import { useEventStream } from '../../api/useEventStream';
import { WORKSPACE_NOTIFICATION_TYPES, workspaceEventsSpec } from '../../api/workspaceEvents';
import { can } from '../../lib/permissions';
import {
  ACTIVE_RUN_STATUSES,
  ACTIVE_RUNS_LIMIT,
  ACTIVITY_LIMIT,
  APPROVALS_LIMIT,
  FINDINGS_LIMIT,
  RUNNING_TASK_STATUSES,
  RUNNING_TASKS_LIMIT,
  WORKFLOW_DETAILS_LIMIT,
  dashboardWindow,
  eventTypesFor,
  type ActivityCategory,
} from './model';

export const PROJECTS_LIMIT = 50;
const RUNNING_TASKS_POLL_MS = 30_000;
const FINDINGS_POLL_MS = 60_000;
const HEALTH_POLL_MS = 60_000;
const ANALYTICS_POLL_MS = 5 * 60_000;

export function useActiveProjects(workspaceId: string) {
  return useQuery({
    queryKey: [...projectKeys.list(workspaceId), 'dashboard', 'active'] as const,
    queryFn: ({ signal }) => projectsApi.list(workspaceId, { status: 'ACTIVE', limit: PROJECTS_LIMIT }, signal),
  });
}

export const ACTIVE_RUNS_QUERY: ListRunsQuery = { status: [...ACTIVE_RUN_STATUSES], limit: ACTIVE_RUNS_LIMIT };

export function useActiveRuns(workspaceId: string) {
  return useQuery({
    queryKey: [...runKeys.list(workspaceId), 'dashboard', 'active'] as const,
    queryFn: ({ signal }) => runsApi.list(workspaceId, ACTIVE_RUNS_QUERY, signal),
  });
}

/** getRun (task_counts, pending_approvals) для первых N активных runs — N+1 ограничен. */
export function useRunDetails(runIds: readonly string[]) {
  return useQueries({
    queries: runIds.slice(0, WORKFLOW_DETAILS_LIMIT).map((id) => ({
      queryKey: runKeys.detail(id),
      queryFn: ({ signal }: { signal: AbortSignal }) => runsApi.get(id, signal),
      staleTime: 15_000,
    })),
  });
}

export const RUNNING_TASKS_QUERY: ListWorkspaceTasksQuery = { status: [...RUNNING_TASK_STATUSES], limit: RUNNING_TASKS_LIMIT };

export function useRunningTasks(workspaceId: string) {
  return useQuery({
    queryKey: taskKeys.workspace(workspaceId, RUNNING_TASKS_QUERY),
    queryFn: ({ signal }) => tasksApi.listWorkspace(workspaceId, RUNNING_TASKS_QUERY, signal),
    refetchInterval: RUNNING_TASKS_POLL_MS,
  });
}

/** Окно фиксируется при монтировании (конец — начало следующего часа). */
export function useDashboardWindow() {
  return useMemo(() => dashboardWindow(), []);
}

export function useAnalytics(workspaceId: string, window: { from: string; to: string }) {
  return useQuery({
    queryKey: analyticsKeys.summary(workspaceId, window),
    queryFn: ({ signal }) => analyticsApi.summary(workspaceId, window, signal),
    staleTime: 60_000,
    refetchInterval: ANALYTICS_POLL_MS,
  });
}

export const OPEN_FINDINGS_QUERY: ListWorkspaceFindingsQuery = { status: ['OPEN'], limit: FINDINGS_LIMIT };

export function useOpenFindings(workspaceId: string) {
  return useQuery({
    queryKey: findingKeys.workspace(workspaceId, OPEN_FINDINGS_QUERY),
    queryFn: ({ signal }) => findingsApi.listWorkspace(workspaceId, OPEN_FINDINGS_QUERY, signal),
    refetchInterval: FINDINGS_POLL_MS,
  });
}

export const PENDING_APPROVALS_QUERY: ListApprovalsQuery = { status: ['PENDING'], limit: APPROVALS_LIMIT };

export function usePendingApprovalsList(workspaceId: string) {
  return useQuery({
    queryKey: approvalKeys.list(workspaceId, PENDING_APPROVALS_QUERY),
    queryFn: ({ signal }) => approvalsApi.list(workspaceId, PENDING_APPROVALS_QUERY, signal),
  });
}

/** GET /system/health — только для ролей с system:health; без права запрос не выполняется. */
export function useSystemHealth(workspaceId: string, role: string | null | undefined) {
  return useQuery({
    queryKey: systemKeys.health(workspaceId),
    queryFn: ({ signal }) => systemApi.health(workspaceId, signal),
    enabled: can(role, 'system:health'),
    refetchInterval: HEALTH_POLL_MS,
  });
}

export function activityQuery(categories: readonly ActivityCategory[]): WorkspaceActivityQuery {
  const types = eventTypesFor(categories);
  return types ? { event_type: types, limit: ACTIVITY_LIMIT } : { limit: ACTIVITY_LIMIT };
}

export function useActivity(workspaceId: string, categories: readonly ActivityCategory[]) {
  const query = activityQuery(categories);
  return useQuery({
    queryKey: runKeys.workspaceActivity(workspaceId, query),
    queryFn: ({ signal }) => runsApi.workspaceActivity(workspaceId, query, signal),
    enabled: categories.length > 0,
  });
}

/**
 * Поток уведомлений workspace (тот же набор типов, что у оболочки → одно соединение StreamManager).
 * Возвращает принятые события (для ленты) и состояние; события инвалидируют затронутые плитки,
 * 410/перечитывание — всё.
 */
export function useDashboardLive(workspaceId: string) {
  const queryClient = useQueryClient();
  const spec = useMemo(() => workspaceEventsSpec(workspaceId, WORKSPACE_NOTIFICATION_TYPES), [workspaceId]);
  const invalidateAll = useCallback(() => {
    for (const key of [
      ['runs', workspaceId],
      ['run'],
      ['tasks', workspaceId],
      ['analytics', workspaceId],
      ['findings', 'workspace', workspaceId],
      approvalKeys.all(workspaceId),
      ['activity', workspaceId],
    ]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  }, [queryClient, workspaceId]);
  const onEvents = useCallback(
    (events: StreamEvent[]) => {
      const types = new Set(events.map((e) => e.type));
      if (types.has('run.state_changed') || types.has('run.completed')) {
        void queryClient.invalidateQueries({ queryKey: ['runs', workspaceId] });
        void queryClient.invalidateQueries({ queryKey: ['tasks', workspaceId] });
        for (const runId of new Set(events.filter((e) => e.type.startsWith('run.')).map((e) => e.run_id))) {
          void queryClient.invalidateQueries({ queryKey: runKeys.detail(runId) });
        }
      }
      if (types.has('run.completed')) {
        void queryClient.invalidateQueries({ queryKey: ['analytics', workspaceId] });
        void queryClient.invalidateQueries({ queryKey: ['findings', 'workspace', workspaceId] });
      }
      if (types.has('approval.requested') || types.has('approval.decided')) {
        void queryClient.invalidateQueries({ queryKey: approvalKeys.all(workspaceId) });
      }
    },
    [queryClient, workspaceId],
  );
  return useEventStream(spec, { limit: 200, onEvents, onResync: invalidateAll });
}
