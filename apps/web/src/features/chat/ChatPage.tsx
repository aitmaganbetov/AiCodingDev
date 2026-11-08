/**
 * P7-06 Live Agent Chat: единая лента run (getRunChat) с потоками All Agents/Codex/Claude/Role/
 * Task/Approvals, фильтрами, поиском, timeline по дням, упоминаниями ролей и live-обновлением
 * по SSE (chat.message). P7-07: панель поручений (DirectivesPanel). Тексты агентов — только текст.
 */
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams, useSearchParams } from 'react-router';
import { chatApi, chatKeys, type ChatFilters } from '../../api/chat';
import { runKeys, type AgentRole } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import { useRunEvents } from '../../api/useRunEvents';
import { EmptyState, ErrorState, LoadingState, PageHeading } from '../../components/ui';
import { formatDateTime } from '../../lib/format';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { DirectivesPanel } from '../directives/DirectivesPanel';
import { RunNav } from '../runs/RunNav';
import { StreamStatus } from '../runs/StreamStatus';
import { useRunTasks } from '../runs/useRunTasks';
import { useRunDetail } from '../runs/useRunStatus';
import { ChatComposer } from './ChatComposer';
import { ChatFiltersPanel } from './ChatFilters';
import { ChatItemView } from './ChatItemView';
import {
  chatQuery,
  filtersComplete,
  filtersFromParams,
  liveItemFromEvent,
  matchesFilters,
  mergeTimeline,
  paramsFromFilters,
  type TimelineItem,
} from './chatModel';

const PAGE_SIZE = 50;
/** События, после которых лента/статусы в чате могли измениться (REST перечитывается пачкой). */
const CHAT_EVENTS = new Set(['chat.message', 'agent.message', 'directive.state_changed', 'directive.impact_ready', 'approval.requested', 'approval.decided']);
const RUN_EVENTS = new Set(['run.state_changed', 'run.completed']);
const REFRESH_DELAY_MS = 400;

function dayKey(iso: string): string {
  return iso.slice(0, 10);
}

export function ChatPage() {
  const { t, i18n } = useTranslation();
  const { projectId = '', runId = '' } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  // Фильтры живут в URL (ссылкой можно поделиться); локальная копия применяется сразу, пока
  // навигация роутера (transition) не зафиксировала новый URL, — контролы не «откатываются».
  const paramsKey = params.toString();
  const [local, setLocal] = useState(() => ({ seen: paramsKey, filters: filtersFromParams(params) }));
  let filters = local.filters;
  if (local.seen !== paramsKey) {
    filters = filtersFromParams(params);
    setLocal({ seen: paramsKey, filters });
  }
  const complete = filtersComplete(filters);
  const { run, isTerminal } = useRunDetail(runId, Boolean(membership));
  const { tasks } = useRunTasks(runId, { all: true, enabled: Boolean(membership) });

  const feed = useInfiniteQuery({
    queryKey: chatKeys.feed(runId, filters),
    queryFn: ({ pageParam, signal }) => chatApi.feed(runId, chatQuery(filters, pageParam, PAGE_SIZE), signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: Boolean(runId && membership) && complete,
  });

  // Пачка событий → одна отложенная инвалидация (replay сотен событий не порождает сотни запросов).
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);
  const onEvents = useCallback(
    (events: RunEvent[]) => {
      if (events.some((e) => RUN_EVENTS.has(e.type))) void queryClient.invalidateQueries({ queryKey: runKeys.detail(runId), exact: true });
      if (events.some((e) => e.type === 'task.state_changed')) void queryClient.invalidateQueries({ queryKey: runKeys.tasks(runId) });
      if (!events.some((e) => CHAT_EVENTS.has(e.type)) || timer.current) return;
      timer.current = setTimeout(() => {
        timer.current = null;
        void queryClient.invalidateQueries({ queryKey: chatKeys.all(runId) });
      }, REFRESH_DELAY_MS);
    },
    [queryClient, runId],
  );
  const onResync = useCallback(() => void queryClient.invalidateQueries({ queryKey: chatKeys.all(runId) }), [queryClient, runId]);
  const stream = useRunEvents(runId, { onEvents, onResync, isTerminal });

  const restItems = useMemo(() => feed.data?.pages.flatMap((p) => p.items) ?? [], [feed.data]);
  const threads = feed.data?.pages[0]?.threads ?? [];
  const live = useMemo(
    () => stream.events.map(liveItemFromEvent).filter((x): x is TimelineItem => x !== null && matchesFilters(x, filters)),
    [stream.events, filters],
  );
  const timeline = useMemo(() => (complete ? mergeTimeline(restItems, live) : []), [complete, restItems, live]);

  const titles = useMemo(() => new Map(tasks.map((x) => [x.id, x.title])), [tasks]);
  const taskTitle = useCallback((id: string) => titles.get(id), [titles]);
  const taskHref = useCallback(
    (taskId: string, tab?: string) =>
      `/w/${workspaceId ?? ''}/projects/${projectId}/runs/${runId}/tasks/${taskId}${tab && tab !== 'overview' ? `?tab=${tab}` : ''}`,
    [workspaceId, projectId, runId],
  );

  const setFilters = useCallback(
    (next: ChatFilters) => {
      setLocal({ seen: paramsKey, filters: next });
      setParams(paramsFromFilters(next), { replace: true });
    },
    [setParams, paramsKey],
  );
  const onMention = useCallback((role: AgentRole) => setFilters({ stream: 'role', role }), [setFilters]);

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const role = membership.role;

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('chat.title')}>
        <Link to={`/w/${workspaceId}/projects/${projectId}/runs`} className="btn-secondary">
          {t('runs.backToRuns')}
        </Link>
      </PageHeading>
      <RunNav workspaceId={workspaceId} projectId={projectId} runId={runId} />
      {run.isError ? (
        <ErrorState error={run.error} onRetry={() => void run.refetch()} />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[16rem,minmax(0,1fr)] 2xl:grid-cols-[16rem,minmax(0,1fr),26rem]">
          <aside aria-label={t('chat.filters.title')}>
            <ChatFiltersPanel filters={filters} onChange={setFilters} tasks={tasks} threads={threads} />
          </aside>

          <div className="flex min-w-0 flex-col gap-4">
            <StreamStatus state={stream.state} error={stream.error} onReconnect={stream.reconnect} />
            <section aria-labelledby="chat-timeline-title" className="flex flex-col gap-3">
              <h2 id="chat-timeline-title" className="text-lg font-semibold">
                {t(`chat.enums.stream.${filters.stream}`)}
              </h2>
              {!complete ? (
                <p className="text-fg-muted" data-testid="chat-incomplete">
                  {filters.stream === 'role' ? t('chat.chooseRoleFirst') : t('chat.chooseTaskFirst')}
                </p>
              ) : feed.isPending ? (
                <LoadingState label={t('chat.loading')} />
              ) : feed.isError && restItems.length === 0 ? (
                <ErrorState error={feed.error} onRetry={() => void feed.refetch()} />
              ) : (
                <>
                  {feed.hasNextPage || feed.isFetchNextPageError ? (
                    <div className="flex flex-col items-start gap-2">
                      {feed.isFetchNextPageError ? (
                        <p role="alert" className="text-sm font-medium text-danger-text">
                          {problemMessage(t, feed.error)}
                        </p>
                      ) : null}
                      {feed.hasNextPage ? (
                        <button type="button" className="btn-secondary" disabled={feed.isFetchingNextPage} onClick={() => void feed.fetchNextPage()}>
                          {feed.isFetchingNextPage ? t('state.loading') : t('chat.loadEarlier')}
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                  {timeline.length === 0 ? (
                    <EmptyState
                      headingLevel={3}
                      title={t('chat.emptyTitle')}
                      description={filters.q || filters.item_type || filters.thread_id ? t('chat.emptyFiltered') : t('chat.emptyDescription')}
                    />
                  ) : (
                    <div role="log" aria-label={t('chat.timelineLabel')} aria-live="polite" aria-relevant="additions">
                      <ol className="flex flex-col divide-y divide-border rounded-lg border border-border bg-surface" data-testid="chat-timeline">
                        {timeline.map((item, i) => {
                          const prev = timeline[i - 1];
                          const separator = !prev || dayKey(prev.created_at) !== dayKey(item.created_at);
                          return (
                            <li key={item.id}>
                              {separator ? (
                                <p className="bg-surface-muted px-3 py-1 text-xs font-semibold text-fg-muted">
                                  {formatDateTime(item.created_at, i18n.language, 'date')}
                                </p>
                              ) : null}
                              <ChatItemView item={item} taskTitle={taskTitle} taskHref={taskHref} onMention={onMention} />
                            </li>
                          );
                        })}
                      </ol>
                    </div>
                  )}
                </>
              )}
            </section>
            {can(role, 'chat:write') ? (
              <ChatComposer
                runId={runId}
                threadId={filters.thread_id}
                onPosted={() => void queryClient.invalidateQueries({ queryKey: chatKeys.all(runId) })}
              />
            ) : (
              <p className="text-sm text-fg-muted">{t('chat.readOnly')}</p>
            )}
          </div>

          <div className="lg:col-span-2 2xl:col-span-1">
            {run.isPending ? (
              <LoadingState label={t('run.loading')} />
            ) : (
              <DirectivesPanel
                run={run.data}
                role={role}
                workspaceId={workspaceId}
                tasks={tasks}
                taskHref={taskHref}
                approvalsHref={`/w/${workspaceId}/approvals`}
                isTerminal={isTerminal}
              />
            )}
          </div>
        </div>
      )}
    </section>
  );
}

export default ChatPage;
