import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { approvalKeys, approvalsApi, type ListApprovalsQuery } from '../api/approvals';
import { providerKeys, providersApi } from '../api/providers';
import type { StreamEvent, StreamState } from '../api/sse';
import { useEventStream } from '../api/useEventStream';
import { WORKSPACE_NOTIFICATION_TYPES, workspaceEventsSpec } from '../api/workspaceEvents';
import { can } from '../lib/permissions';

/**
 * Данные оболочки (план §3.1): статус CLI-провайдеров, счётчик approvals, уведомления workspace.
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

/* ── Уведомления: курсор «прочитано» на этом устройстве (до B8 — серверного хранения нет) ── */

export const NOTIFICATIONS_READ_PREFIX = 'aicd.notifications.read.';
const readListeners = new Set<() => void>();

function readCursor(workspaceId: string): string | null {
  try {
    return window.localStorage.getItem(`${NOTIFICATIONS_READ_PREFIX}${workspaceId}`);
  } catch {
    return null;
  }
}

function writeCursor(workspaceId: string, iso: string): void {
  try {
    window.localStorage.setItem(`${NOTIFICATIONS_READ_PREFIX}${workspaceId}`, iso);
  } catch {
    /* хранилище недоступно — отметка действует до перезагрузки (в памяти списка) */
  }
  for (const l of readListeners) l();
}

function subscribeRead(listener: () => void): () => void {
  readListeners.add(listener);
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key.startsWith(NOTIFICATIONS_READ_PREFIX)) listener();
  };
  window.addEventListener('storage', onStorage);
  return () => {
    readListeners.delete(listener);
    window.removeEventListener('storage', onStorage);
  };
}

/** Непрочитанные: время события позже курсора (время сервера → без расхождения часов). */
export function countUnread(events: readonly StreamEvent[], cursor: string | null): number {
  const since = cursor ? Date.parse(cursor) : Number.NEGATIVE_INFINITY;
  return events.filter((e) => Date.parse(e.timestamp) > since).length;
}

/* ── Один поток уведомлений на браузер: вкладка-лидер (Web Locks) держит SSE, остальные слушают BroadcastChannel ── */

type Bus = { leader: boolean };

function hasCoordination(): boolean {
  return typeof navigator !== 'undefined' && 'locks' in navigator && typeof BroadcastChannel !== 'undefined';
}

/**
 * Лидерство вкладки по workspace: лимит сервера — 5 SSE-соединений на пользователя (API_DESIGN §13),
 * поэтому поток уведомлений открывает только одна вкладка, а события раздаются остальным через
 * BroadcastChannel. Без Web Locks (старые браузеры, jsdom) каждая вкладка подписывается сама.
 */
function useNotificationsLeader(workspaceId: string | null): Bus {
  const [leader, setLeader] = useState(() => !hasCoordination());
  useEffect(() => {
    if (!workspaceId || !hasCoordination()) return undefined;
    const abort = new AbortController();
    let release: (() => void) | undefined;
    navigator.locks
      .request(`aicd.workspace-events.${workspaceId}`, { signal: abort.signal }, () => {
        setLeader(true);
        return new Promise<void>((resolve) => {
          release = resolve;
        });
      })
      .catch(() => undefined);
    return () => {
      abort.abort();
      release?.();
      setLeader(false);
    };
  }, [workspaceId]);
  return { leader };
}

export interface WorkspaceNotifications {
  events: StreamEvent[];
  unread: number;
  /** Состояние потока в этой вкладке; `shared` — поток держит другая вкладка. */
  state: StreamState | 'shared';
  markAllRead: () => void;
}

const MAX_NOTIFICATIONS = 50;

export function useWorkspaceNotifications(workspaceId: string | null): WorkspaceNotifications {
  const queryClient = useQueryClient();
  const { leader } = useNotificationsLeader(workspaceId);
  const [shared, setShared] = useState<{ key: string | null; list: StreamEvent[] }>({ key: null, list: [] });
  const channelRef = useRef<BroadcastChannel | null>(null);

  const ingest = useCallback(
    (fresh: StreamEvent[]) => {
      if (!workspaceId) return;
      setShared((prev) => {
        const base = prev.key === workspaceId ? prev.list : [];
        const seen = new Set(base.map((e) => e.event_id));
        const merged = base.concat(fresh.filter((e) => !seen.has(e.event_id)));
        return { key: workspaceId, list: merged.slice(-MAX_NOTIFICATIONS) };
      });
      if (fresh.some((e) => e.type.startsWith('approval.'))) void queryClient.invalidateQueries({ queryKey: approvalKeys.all(workspaceId) });
    },
    [queryClient, workspaceId],
  );

  useEffect(() => {
    if (!workspaceId || !hasCoordination()) return undefined;
    const channel = new BroadcastChannel(`aicd.workspace-events.${workspaceId}`);
    channelRef.current = channel;
    channel.onmessage = (e: MessageEvent<{ events?: StreamEvent[]; hello?: boolean }>) => {
      if (e.data.events) ingest(e.data.events);
    };
    return () => {
      channel.close();
      channelRef.current = null;
    };
  }, [ingest, workspaceId]);

  const spec = useMemo(() => (workspaceId && leader ? workspaceEventsSpec(workspaceId, WORKSPACE_NOTIFICATION_TYPES) : null), [workspaceId, leader]);
  const { state } = useEventStream(spec, {
    limit: 0,
    onEvents: (fresh) => {
      ingest(fresh);
      channelRef.current?.postMessage({ events: fresh });
    },
    onResync: () => {
      if (workspaceId) void queryClient.invalidateQueries({ queryKey: approvalKeys.all(workspaceId) });
    },
  });

  const cursor = useSyncExternalStore(
    subscribeRead,
    () => (workspaceId ? readCursor(workspaceId) : null),
    () => null,
  );
  const events = useMemo(() => (shared.key === workspaceId ? shared.list : []), [shared, workspaceId]);
  const markAllRead = useCallback(() => {
    if (!workspaceId) return;
    const latest = events.map((e) => e.timestamp).sort((a, b) => Date.parse(a) - Date.parse(b)).at(-1);
    if (latest && (!cursor || Date.parse(latest) > Date.parse(cursor))) writeCursor(workspaceId, latest);
  }, [cursor, events, workspaceId]);

  return { events, unread: countUnread(events, cursor), state: leader ? state : 'shared', markAllRead };
}
