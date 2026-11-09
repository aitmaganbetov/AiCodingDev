import { useInfiniteQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { chatApi, chatKeys, type ChatFilters } from '../../api/chat';
import type { RunEvent } from '../../api/runEvents';
import { chatQuery, filtersComplete, liveItemFromEvent, matchesFilters, mergeTimeline, type TimelineItem } from './chatModel';

export const CHAT_PAGE_SIZE = 50;
/** События, после которых лента/статусы в чате могли измениться (REST перечитывается пачкой). */
export const CHAT_EVENTS: ReadonlySet<string> = new Set([
  'chat.message',
  'agent.message',
  'directive.state_changed',
  'directive.impact_ready',
  'approval.requested',
  'approval.decided',
]);

/**
 * Лента Live Agent Chat (P7-06): getRunChat (cursor, новые сверху) + live `chat.message` из общего
 * потока run, хронологически и без дублей (chatModel.mergeTimeline). Фильтры — ключ кэша.
 */
export function useChatFeed(runId: string, filters: ChatFilters, liveEvents: readonly RunEvent[], enabled = true) {
  const complete = filtersComplete(filters);
  const feed = useInfiniteQuery({
    queryKey: chatKeys.feed(runId, filters),
    queryFn: ({ pageParam, signal }) => chatApi.feed(runId, chatQuery(filters, pageParam, CHAT_PAGE_SIZE), signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: Boolean(runId) && enabled && complete,
  });
  const restItems = useMemo(() => feed.data?.pages.flatMap((p) => p.items) ?? [], [feed.data]);
  const threads = feed.data?.pages[0]?.threads ?? [];
  const live = useMemo(
    () => liveEvents.map(liveItemFromEvent).filter((x): x is TimelineItem => x !== null && matchesFilters(x, filters)),
    [liveEvents, filters],
  );
  const timeline = useMemo(() => (complete ? mergeTimeline(restItems, live) : []), [complete, restItems, live]);
  return { feed, complete, restItems, threads, timeline };
}
