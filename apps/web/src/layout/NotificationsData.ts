import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { approvalKeys } from '../api/approvals';
import { IdempotentIntent } from '../api/client';
import {
  NOTIFICATION_CREATED,
  notificationFromEvent,
  notificationKeys,
  notificationsApi,
  type ListNotificationsQuery,
  type Notification,
  type NotificationMarkReadRequest,
  type NotificationPage,
  type NotificationUnreadCount,
} from '../api/notifications';
import type { StreamEvent, StreamState } from '../api/sse';
import { useEventStream } from '../api/useEventStream';
import { WORKSPACE_NOTIFICATION_TYPES, workspaceEventsSpec } from '../api/workspaceEvents';
import { can } from '../lib/permissions';

/**
 * Персональные уведомления оболочки (UI redesign B8, API_DESIGN §13.2): история, счётчик и отметка
 * «прочитано» — на сервере (`/notifications`), live — кадры `notification.created` потока workspace.
 * Прежний курсор «прочитано» в localStorage (`aicd.notifications.read.<ws>`, F2) больше не читается и не
 * пишется: старые ключи просто игнорируются.
 *
 * Один поток уведомлений на браузер (F2): вкладка-лидер (Web Locks) держит SSE, остальные получают
 * события через BroadcastChannel; после отметки «прочитано» вкладка просит остальные перечитать счётчик.
 */

export const NOTIFICATIONS_PAGE_SIZE = 30;
export const NOTIFICATIONS_LIST_QUERY: ListNotificationsQuery = { limit: NOTIFICATIONS_PAGE_SIZE };

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

type BusMessage = { events?: StreamEvent[]; notificationsChanged?: boolean };

/** Новые уведомления в начало загруженной страницы (без дублей по id); null — страница не загружена. */
export function prependNotifications(page: NotificationPage | undefined, fresh: readonly Notification[]): NotificationPage | undefined {
  if (!page) return page;
  const seen = new Set(page.items.map((n) => n.id));
  const added = fresh.filter((n) => !seen.has(n.id)).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  if (added.length === 0) return page;
  return {
    ...page,
    items: [...added, ...page.items].slice(0, NOTIFICATIONS_PAGE_SIZE),
    unread_count: page.unread_count + added.filter((n) => !n.read).length,
  };
}

/**
 * Оптимистичный счётчик до ответа сервера: непрочитанные, созданные позже самого нового учтённого
 * (`latest_unread_at`), — повтор кадров при переподключении (перекрытие 60 с) не увеличивает счётчик.
 */
export function bumpUnread(count: NotificationUnreadCount | undefined, fresh: readonly Notification[]): NotificationUnreadCount | undefined {
  if (!count) return count;
  const since = count.latest_unread_at ? Date.parse(count.latest_unread_at) : Number.NEGATIVE_INFINITY;
  const added = fresh.filter((n) => !n.read && Date.parse(n.created_at) > since);
  if (added.length === 0) return count;
  const latest = added.map((n) => n.created_at).sort((a, b) => Date.parse(a) - Date.parse(b)).at(-1) ?? count.latest_unread_at;
  return { unread_count: count.unread_count + added.length, latest_unread_at: latest };
}

/** Отметка в загруженной странице после успешного POST /notifications/mark-read. */
export function applyMarkRead(page: NotificationPage | undefined, body: NotificationMarkReadRequest, unreadCount: number, at: string): NotificationPage | undefined {
  if (!page) return page;
  let hit: (n: Notification, index: number) => boolean;
  if (body.notification_ids) {
    const ids = new Set(body.notification_ids);
    hit = (n) => ids.has(n.id);
  } else if (body.up_to) {
    // Страница отсортирована новыми сверху: «до уведомления включительно» — оно и всё ниже.
    const from = page.items.findIndex((n) => n.id === body.up_to);
    hit = (_n, i) => from >= 0 && i >= from;
  } else {
    hit = () => true;
  }
  return {
    ...page,
    unread_count: unreadCount,
    items: page.items.map((n, i) => (!n.read && hit(n, i) ? { ...n, read: true, read_at: at } : n)),
  };
}

function ingestNotifications(queryClient: QueryClient, workspaceId: string, fresh: readonly Notification[]): void {
  if (fresh.length === 0) return;
  queryClient.setQueryData<NotificationPage>(notificationKeys.list(workspaceId, NOTIFICATIONS_LIST_QUERY), (page) => prependNotifications(page, fresh));
  queryClient.setQueryData<NotificationUnreadCount>(notificationKeys.unread(workspaceId), (count) => bumpUnread(count, fresh));
  // Истина — сервер: счётчик перечитывается (дёшево, уведомления редки).
  void queryClient.invalidateQueries({ queryKey: notificationKeys.unread(workspaceId) });
}

export interface ShellNotifications {
  /** Загруженная страница (новые сверху); undefined — ещё не загружена (список грузится при открытии панели). */
  items: Notification[] | undefined;
  hasMore: boolean;
  listPending: boolean;
  listError: unknown;
  refetchList: () => void;
  /** Счётчик непрочитанных с сервера; null — неизвестен (нет права, загрузка, ошибка). */
  unread: number | null;
  /** Состояние потока в этой вкладке; `shared` — поток держит другая вкладка. */
  state: StreamState | 'shared';
  canUpdate: boolean;
  markRead: (ids: string[]) => void;
  markAllRead: () => void;
  marking: boolean;
  markError: unknown;
}

export function useShellNotifications(workspaceId: string | null, role: string | null | undefined, { listOpen }: { listOpen: boolean }): ShellNotifications {
  const queryClient = useQueryClient();
  const canRead = Boolean(workspaceId && can(role, 'notification:read'));
  const canUpdate = Boolean(workspaceId && can(role, 'notification:update'));
  const { leader } = useNotificationsLeader(workspaceId);
  const channelRef = useRef<BroadcastChannel | null>(null);

  const unreadQuery = useQuery({
    queryKey: notificationKeys.unread(workspaceId ?? ''),
    queryFn: ({ signal }) => notificationsApi.unreadCount(workspaceId ?? '', signal),
    enabled: canRead,
    staleTime: 30_000,
  });
  const listQuery = useQuery({
    queryKey: notificationKeys.list(workspaceId ?? '', NOTIFICATIONS_LIST_QUERY),
    queryFn: ({ signal }) => notificationsApi.list(workspaceId ?? '', NOTIFICATIONS_LIST_QUERY, signal),
    enabled: canRead && listOpen,
    staleTime: 30_000,
  });

  const ingest = useCallback(
    (fresh: StreamEvent[]) => {
      if (!workspaceId) return;
      const notes = fresh.map(notificationFromEvent).filter((n): n is Notification => n !== null);
      if (notes.length > 0) ingestNotifications(queryClient, workspaceId, notes);
      else if (fresh.some((e) => e.type === NOTIFICATION_CREATED)) void queryClient.invalidateQueries({ queryKey: notificationKeys.all(workspaceId) });
      if (fresh.some((e) => e.type.startsWith('approval.'))) void queryClient.invalidateQueries({ queryKey: approvalKeys.all(workspaceId) });
    },
    [queryClient, workspaceId],
  );

  useEffect(() => {
    if (!workspaceId || !hasCoordination()) return undefined;
    const channel = new BroadcastChannel(`aicd.workspace-events.${workspaceId}`);
    channelRef.current = channel;
    channel.onmessage = (e: MessageEvent<BusMessage>) => {
      if (e.data.events) ingest(e.data.events);
      if (e.data.notificationsChanged) void queryClient.invalidateQueries({ queryKey: notificationKeys.all(workspaceId) });
    };
    return () => {
      channel.close();
      channelRef.current = null;
    };
  }, [ingest, queryClient, workspaceId]);

  const spec = useMemo(() => (workspaceId && leader ? workspaceEventsSpec(workspaceId, WORKSPACE_NOTIFICATION_TYPES) : null), [workspaceId, leader]);
  const { state } = useEventStream(spec, {
    limit: 0,
    onEvents: (fresh) => {
      ingest(fresh);
      channelRef.current?.postMessage({ events: fresh } satisfies BusMessage);
    },
    onResync: () => {
      // 410: часть истории потока недоступна — перечитать REST.
      if (!workspaceId) return;
      void queryClient.invalidateQueries({ queryKey: approvalKeys.all(workspaceId) });
      void queryClient.invalidateQueries({ queryKey: notificationKeys.all(workspaceId) });
    },
  });

  const intent = useRef(new IdempotentIntent());
  const mutation = useMutation({
    mutationFn: (body: NotificationMarkReadRequest) => notificationsApi.markRead(workspaceId ?? '', body, intent.current.keyFor(body)),
    onSuccess: (result, body) => {
      intent.current.complete();
      if (!workspaceId) return;
      const at = new Date().toISOString();
      queryClient.setQueryData<NotificationUnreadCount>(notificationKeys.unread(workspaceId), (count) => ({
        unread_count: result.unread_count,
        latest_unread_at: result.unread_count === 0 ? null : (count?.latest_unread_at ?? null),
      }));
      queryClient.setQueryData<NotificationPage>(notificationKeys.list(workspaceId, NOTIFICATIONS_LIST_QUERY), (page) =>
        applyMarkRead(page, body, result.unread_count, at),
      );
      channelRef.current?.postMessage({ notificationsChanged: true } satisfies BusMessage);
    },
  });
  const { mutate } = mutation;

  const items = listQuery.data?.items;
  const markRead = useCallback(
    (ids: string[]) => {
      if (canUpdate && ids.length > 0) mutate({ notification_ids: ids.slice(0, 100) });
    },
    [canUpdate, mutate],
  );
  const markAllRead = useCallback(() => {
    if (!canUpdate) return;
    // «Прочитано всё, что видел»: до самого нового показанного включительно; без списка — все.
    const newest = items?.[0];
    mutate(newest ? { up_to: newest.id } : { all: true });
  }, [canUpdate, items, mutate]);

  const fromList = listQuery.data?.unread_count;
  const unread = unreadQuery.data?.unread_count ?? fromList ?? null;

  return {
    items,
    hasMore: Boolean(listQuery.data?.next_cursor),
    listPending: listQuery.isPending && listQuery.fetchStatus !== 'idle',
    listError: listQuery.error,
    refetchList: () => void listQuery.refetch(),
    unread: canRead ? unread : null,
    state: leader ? state : 'shared',
    canUpdate,
    markRead,
    markAllRead,
    marking: mutation.isPending,
    markError: mutation.error,
  };
}
