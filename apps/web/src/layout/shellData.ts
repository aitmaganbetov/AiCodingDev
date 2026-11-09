import { useQuery } from '@tanstack/react-query';
import { approvalKeys, approvalsApi, type ListApprovalsQuery } from '../api/approvals';
import { providerKeys, providersApi } from '../api/providers';
import { can } from '../lib/permissions';

/**
 * Данные оболочки (план §3.1): статус CLI-провайдеров, счётчик approvals (уведомления — NotificationsData.ts).
 * Только реальные операции контракта; поллинг TanStack Query паузится, пока вкладка скрыта.
 */

/** Опрос провайдеров: SSE-события о провайдерах нет (план §3.1) → 60 с + кнопка «Проверить» в Настройках. */
export const PROVIDERS_POLL_MS = 60_000;
/** Approvals без run (DOCS_DOWNLOAD) в поток workspace не попадают (OQ-45 c) → дополнительно опрос 60 с. */
export const APPROVALS_POLL_MS = 60_000;
export const PENDING_APPROVALS_LIMIT = 50;

export function useProvidersStatus(workspaceId: string | null, role: string | null | undefined) {
  return useQuery({
    queryKey: providerKeys.list(workspaceId ?? ''),
    queryFn: ({ signal }) => providersApi.list(workspaceId ?? '', signal),
    enabled: Boolean(workspaceId && can(role, 'provider:read')),
    refetchInterval: PROVIDERS_POLL_MS,
    staleTime: 30_000,
  });
}

export const PENDING_MINE_QUERY: ListApprovalsQuery = { status: ['PENDING'], decidable_by_me: true, limit: PENDING_APPROVALS_LIMIT };

/** Ожидающие моего решения approvals: число (и признак «больше лимита»). */
export function usePendingApprovals(workspaceId: string | null, role: string | null | undefined) {
  const query = useQuery({
    queryKey: approvalKeys.list(workspaceId ?? '', PENDING_MINE_QUERY),
    queryFn: ({ signal }) => approvalsApi.list(workspaceId ?? '', PENDING_MINE_QUERY, signal),
    enabled: Boolean(workspaceId && can(role, 'approval:read')),
    refetchInterval: APPROVALS_POLL_MS,
    staleTime: 15_000,
  });
  const count = query.data ? query.data.items.filter((a) => a.status === 'PENDING').length : null;
  return { query, count, more: Boolean(query.data?.next_cursor) };
}
