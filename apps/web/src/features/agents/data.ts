/**
 * Запросы экрана «Агенты» (F5): каждый — отдельно со своим состоянием (ошибка одной операции не
 * скрывает остальные данные). Выполняемые задачи опрашиваются (30 с, пока вкладка видима).
 */
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { agentKeys, agentsApi } from '../../api/agents';
import { analyticsApi, analyticsKeys } from '../../api/analytics';
import { providerKeys, providersApi } from '../../api/providers';
import { taskKeys, tasksApi, type ListWorkspaceTasksQuery } from '../../api/tasks';
import type { AgentRole } from '../../lib/agentRoles';
import { dashboardWindow } from '../dashboard/model';
import { ACTIVE_TASK_STATUSES, ACTIVE_TASKS_LIMIT, ROLE_HISTORY_PAGE } from './model';

const ACTIVE_POLL_MS = 30_000;

export function useAgentProfiles(workspaceId: string) {
  return useQuery({
    queryKey: agentKeys.profiles(workspaceId),
    queryFn: ({ signal }) => agentsApi.profiles(workspaceId, signal),
    staleTime: 60_000,
  });
}

/** Выполняемые задачи (LEASED/RUNNING) всего workspace или одной роли. */
export function useActiveTasks(workspaceId: string, role?: AgentRole) {
  const query: ListWorkspaceTasksQuery = { status: [...ACTIVE_TASK_STATUSES], limit: ACTIVE_TASKS_LIMIT, ...(role ? { agent_role: [role] } : {}) };
  return useQuery({
    queryKey: taskKeys.workspace(workspaceId, query),
    queryFn: ({ signal }) => tasksApi.listWorkspace(workspaceId, query, signal),
    refetchInterval: ACTIVE_POLL_MS,
  });
}

/** История задач роли (все статусы, новые сверху), cursor-пагинация. */
export function useRoleHistory(workspaceId: string, role: AgentRole) {
  return useInfiniteQuery({
    queryKey: [...taskKeys.workspace(workspaceId, { agent_role: [role] }), 'history'] as const,
    queryFn: ({ pageParam, signal }) =>
      tasksApi.listWorkspace(workspaceId, { agent_role: [role], limit: ROLE_HISTORY_PAGE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** Окно analytics (30 дней, конец — начало следующего часа UTC) фиксируется при монтировании. */
export function useAgentsWindow() {
  return useMemo(() => dashboardWindow(), []);
}

export function useRoleAnalytics(workspaceId: string, window: { from: string; to: string }) {
  return useQuery({
    queryKey: analyticsKeys.summary(workspaceId, window),
    queryFn: ({ signal }) => analyticsApi.summary(workspaceId, window, signal),
    staleTime: 60_000,
  });
}

export function useProviders(workspaceId: string) {
  return useQuery({
    queryKey: providerKeys.list(workspaceId),
    queryFn: ({ signal }) => providersApi.list(workspaceId, signal),
  });
}
