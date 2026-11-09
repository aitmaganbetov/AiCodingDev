/**
 * Live Agent Chat Mission Control (F4, план §3.3, C-2): вид = тип содержимого (вкладки), потоки ТЗ
 * (All Agents/Codex/Claude/Role/Task/Approvals) — фильтры «Беседы». Пять видов: беседа агентов
 * (getRunChat + SSE), вывод CLI (`cli.output`, виртуализация, моноширинный), системные события
 * (журнал run целиком или по группам типов), поручения (DirectivesPanel), evidence (listRunEvidence).
 * Содержимое агентов/CLI — недоверенный текст: только как текст, без HTML/markdown; скрытых рассуждений
 * API не отдаёт и UI их не показывает.
 */
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { memo, useCallback, useId, useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { chatKeys, type ChatFilters } from '../../api/chat';
import { qualityApi, qualityKeys, type RunEvidenceQuery } from '../../api/quality';
import type { AgentRole, Run, TaskSummary } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import { artifactDownloadPath } from '../../api/tasks';
import {
  AgentAvatar,
  Badge,
  Callout,
  EmptyState,
  ErrorState,
  Icon,
  LoadMore,
  LoadingState,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Timestamp,
  VirtualList,
  cx,
  type IconName,
} from '../../components/ui';
import { formatDateTime } from '../../lib/format';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { ChatComposer } from '../chat/ChatComposer';
import { ChatFiltersPanel } from '../chat/ChatFilters';
import { ChatItemView } from '../chat/ChatItemView';
import { AGENT_ROLES, type TimelineItem } from '../chat/chatModel';
import { useChatFeed } from '../chat/useChatFeed';
import { DirectivesPanel } from '../directives/DirectivesPanel';
import { describeEvent } from '../runs/describeEvent';
import { providerLabel, roleLabel } from '../runs/labels';
import { StreamStatus } from '../runs/StreamStatus';
import { cliLines, filterEvents, type CliLine } from './feedModel';
import {
  EVENT_GROUPS,
  EVIDENCE_STATUSES,
  MC_VIEWS,
  SINCE_OPTIONS,
  sinceCutoff,
  type EventGroup,
  type FeedFilters,
  type McView,
} from './mcParams';
import { useNow } from './useNow';
import { MC_EVENT_LIMIT, type MissionControlRun } from './useRunData';

/** Выше этого числа элементов лента и журнал виртуализируются (в DOM — только видимые строки). */
export const VIRTUALIZE_FROM = 400;

const VIEW_ICON: Record<McView, IconName> = {
  conversation: 'message',
  cli: 'terminal',
  system: 'activity',
  directives: 'command',
  evidence: 'verifyPass',
};

function dayKey(iso: string): string {
  return iso.slice(0, 10);
}

/** Общая строка фильтров видов CLI/Система/Evidence: роль, задача, период (+ свои поля вида). */
function FilterBar({
  filters,
  onChange,
  tasks,
  children,
  label,
}: {
  filters: FeedFilters;
  onChange: (next: FeedFilters) => void;
  tasks: readonly TaskSummary[];
  children?: ReactNode;
  label: string;
}) {
  const { t } = useTranslation();
  const id = useId();
  const active = Boolean(filters.role || filters.task || filters.since || filters.q || filters.groups || filters.evidenceStatus);
  return (
    <div role="group" aria-label={label} className="flex flex-wrap items-end gap-2" data-testid="feed-filters">
      <div className="flex min-w-[9rem] flex-1 flex-col gap-1 sm:flex-none">
        <label htmlFor={`${id}-role`} className="text-xs font-medium text-fg-muted">
          {t('missionControl.filters.role')}
        </label>
        <select
          id={`${id}-role`}
          className="select min-h-target text-sm"
          value={filters.role ?? ''}
          onChange={(e) => onChange({ ...filters, role: (e.target.value || undefined) as AgentRole | undefined })}
        >
          <option value="">{t('missionControl.filters.allRoles')}</option>
          {AGENT_ROLES.map((r) => (
            <option key={r} value={r}>
              {roleLabel(t, r)}
            </option>
          ))}
        </select>
      </div>
      <div className="flex min-w-[9rem] flex-1 flex-col gap-1 sm:flex-none">
        <label htmlFor={`${id}-task`} className="text-xs font-medium text-fg-muted">
          {t('missionControl.filters.task')}
        </label>
        <select
          id={`${id}-task`}
          className="select min-h-target max-w-[16rem] text-sm"
          value={filters.task ?? ''}
          onChange={(e) => onChange({ ...filters, task: e.target.value || undefined })}
        >
          <option value="">{t('missionControl.filters.allTasks')}</option>
          {tasks.map((x) => (
            <option key={x.id} value={x.id}>
              {x.title}
            </option>
          ))}
        </select>
      </div>
      <div className="flex min-w-[8rem] flex-1 flex-col gap-1 sm:flex-none">
        <label htmlFor={`${id}-since`} className="text-xs font-medium text-fg-muted">
          {t('missionControl.filters.since')}
        </label>
        <select
          id={`${id}-since`}
          className="select min-h-target text-sm"
          value={filters.since ?? ''}
          onChange={(e) => onChange({ ...filters, since: (e.target.value || undefined) as FeedFilters['since'] })}
        >
          <option value="">{t('missionControl.filters.sinceAll')}</option>
          {SINCE_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {t(`missionControl.filters.sinceOption.${s}`)}
            </option>
          ))}
        </select>
      </div>
      {children}
      {active ? (
        <button type="button" className="btn-ghost btn-sm" onClick={() => onChange({})}>
          {t('missionControl.filters.reset')}
        </button>
      ) : null}
    </div>
  );
}

/* ───────────────────────────── Беседа агентов ───────────────────────────── */

function ConversationView({
  run,
  role,
  data,
  filters,
  onFilters,
  feedFilters,
  onFeedFilters,
  taskHref,
  onSelectTask,
}: {
  run: Run;
  role: string;
  data: MissionControlRun;
  filters: ChatFilters;
  onFilters: (f: ChatFilters) => void;
  feedFilters: FeedFilters;
  onFeedFilters: (f: FeedFilters) => void;
  taskHref: (taskId: string, tab?: string) => string;
  onSelectTask: (taskId: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [showFilters, setShowFilters] = useState(false);
  const { feed, complete, restItems, threads, timeline } = useChatFeed(run.id, filters, data.stream.events);
  const now = useNow(Boolean(feedFilters.since));
  const cutoff = sinceCutoff(feedFilters.since, now);
  const visible = useMemo(() => (cutoff === null ? timeline : timeline.filter((i) => Date.parse(i.created_at) >= cutoff)), [timeline, cutoff]);
  const titles = useMemo(() => new Map(data.tasks.map((x) => [x.id, x.title])), [data.tasks]);
  const taskTitle = useCallback((id: string) => titles.get(id), [titles]);
  const onMention = useCallback((r: AgentRole) => onFilters({ stream: 'role', role: r }), [onFilters]);
  const filtered = filters.stream !== 'all_agents' || filters.q || filters.item_type || filters.thread_id || feedFilters.since;
  const panelId = useId();

  const renderItem = (item: TimelineItem, i: number, list: readonly TimelineItem[]) => {
    const prev = list[i - 1];
    const separator = !prev || dayKey(prev.created_at) !== dayKey(item.created_at);
    return (
      <>
        {separator ? <p className="bg-surface-muted px-3 py-1 text-xs font-semibold text-fg-muted">{formatDateTime(item.created_at, i18n.language, 'date')}</p> : null}
        <ChatItemView item={item} taskTitle={taskTitle} taskHref={taskHref} onMention={onMention} />
        {item.task_id ? (
          <div className="px-3 pb-2">
            <button type="button" className="link text-xs" onClick={() => onSelectTask(item.task_id ?? '')}>
              {t('missionControl.inspectTask')}
            </button>
          </div>
        ) : null}
      </>
    );
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn-secondary btn-sm" aria-expanded={showFilters} aria-controls={panelId} onClick={() => setShowFilters((v) => !v)}>
          <Icon name="search" className="h-4 w-4" />
          {t('missionControl.filters.toggle')}
        </button>
        <span className="text-sm text-fg-muted" data-testid="chat-stream-label">
          {t(`chat.enums.stream.${filters.stream}`)}
          {filters.stream === 'role' && filters.role ? ` · ${roleLabel(t, filters.role)}` : ''}
          {filters.stream === 'task' && filters.task_id ? ` · ${taskTitle(filters.task_id) ?? filters.task_id.slice(0, 8)}` : ''}
        </span>
      </div>
      <div id={panelId} hidden={!showFilters} className="rounded-lg border border-border bg-surface p-3">
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_12rem]">
          <ChatFiltersPanel filters={filters} onChange={onFilters} tasks={data.tasks} threads={threads} />
          <div className="flex flex-col gap-1">
            <label htmlFor={`${panelId}-since`} className="text-sm font-medium">
              {t('missionControl.filters.since')}
            </label>
            <select
              id={`${panelId}-since`}
              className="select min-h-touch"
              value={feedFilters.since ?? ''}
              onChange={(e) => onFeedFilters({ ...feedFilters, since: (e.target.value || undefined) as FeedFilters['since'] })}
            >
              <option value="">{t('missionControl.filters.sinceAll')}</option>
              {SINCE_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {t(`missionControl.filters.sinceOption.${s}`)}
                </option>
              ))}
            </select>
            <p className="text-xs text-fg-muted">{t('missionControl.filters.sinceLoaded')}</p>
          </div>
        </div>
      </div>
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
                <button type="button" className="btn-secondary btn-sm" disabled={feed.isFetchingNextPage} onClick={() => void feed.fetchNextPage()}>
                  {feed.isFetchingNextPage ? t('state.loading') : t('chat.loadEarlier')}
                </button>
              ) : null}
            </div>
          ) : null}
          {visible.length === 0 ? (
            <EmptyState headingLevel={3} title={t('chat.emptyTitle')} description={filtered ? t('chat.emptyFiltered') : t('chat.emptyDescription')} />
          ) : visible.length > VIRTUALIZE_FROM ? (
            <div data-testid="chat-timeline" data-virtual="true">
              <VirtualList
                items={visible}
                getKey={(item) => item.id}
                renderItem={(item, i) => renderItem(item, i, visible)}
                label={t('chat.timelineLabel')}
                kind="feed"
                estimateSize={120}
                followOutput
                className="h-[min(60vh,40rem)]"
              />
            </div>
          ) : (
            <div role="log" aria-label={t('chat.timelineLabel')} aria-live="polite" aria-relevant="additions">
              <ol className="flex flex-col divide-y divide-border rounded-lg border border-border bg-surface" data-testid="chat-timeline">
                {visible.map((item, i) => (
                  <li key={item.id}>{renderItem(item, i, visible)}</li>
                ))}
              </ol>
            </div>
          )}
        </>
      )}
      {can(role, 'chat:write') ? (
        <details className="rounded-lg border border-border bg-surface">
          <summary className="min-h-touch cursor-pointer px-3 py-2 text-sm font-medium">{t('missionControl.comment')}</summary>
          <div className="p-3">
            <ChatComposer
              runId={run.id}
              threadId={filters.thread_id}
              onPosted={() => void queryClient.invalidateQueries({ queryKey: chatKeys.all(run.id) })}
            />
          </div>
        </details>
      ) : (
        <p className="text-sm text-fg-muted">{t('chat.readOnly')}</p>
      )}
    </div>
  );
}

/* ───────────────────────────── Вывод CLI ───────────────────────────── */

/** Строка CLI мемоизирована: прокрутка перерисовывает окно списка, а не содержимое уже видимых строк. */
const CliLineView = memo(function CliLineView({ line, taskTitle }: { line: CliLine; taskTitle: (id: string) => string | undefined }) {
  const { t } = useTranslation();
  const markers = [
    line.truncated ? t('missionControl.cli.truncated') : null,
    line.redactions > 0 ? t('missionControl.cli.redactions', { count: line.redactions }) : null,
    line.dropped > 0 ? t('event.cliDropped', { count: line.dropped }) : null,
  ].filter(Boolean);
  return (
    <div className="flex flex-col gap-0.5 font-mono text-code-sm" data-testid="cli-line" data-seq={line.seq} data-stream={line.stream ?? undefined}>
      <div className="flex flex-wrap items-center gap-x-2 text-fg-muted">
        <span>#{line.seq}</span>
        <Timestamp value={line.timestamp} format="time" />
        <span className="inline-flex items-center gap-1">
          <AgentAvatar agentRole={line.role} size="sm" className="h-4 w-4" />
          {roleLabel(t, line.role)}
        </span>
        {line.provider ? <span>{providerLabel(t, line.provider)}</span> : null}
        {line.stream ? <span className={line.stream === 'stderr' ? 'font-semibold text-danger-text' : undefined}>{line.stream}</span> : null}
        {line.eventType !== 'output' ? <span>{t(`missionControl.cli.event.${line.eventType}`, { defaultValue: line.eventType })}</span> : null}
        {line.taskId ? <span className="truncate">{taskTitle(line.taskId) ?? line.taskId.slice(0, 8)}</span> : null}
      </div>
      {line.text ? <p className="whitespace-pre-wrap break-words text-fg">{line.text}</p> : null}
      {markers.length > 0 ? (
        <p className="text-fg-muted" data-testid="cli-markers">
          {markers.join(' · ')}
        </p>
      ) : null}
    </div>
  );
});

function CliView({ data, filters, onFilters }: { data: MissionControlRun; filters: FeedFilters; onFilters: (f: FeedFilters) => void }) {
  const { t } = useTranslation();
  const id = useId();
  const [q, setQ] = useState(filters.q ?? '');
  const now = useNow(Boolean(filters.since));
  const lines = useMemo(() => cliLines(data.stream.events, filters, sinceCutoff(filters.since, now)), [data.stream.events, filters, now]);
  const titles = useMemo(() => new Map(data.tasks.map((x) => [x.id, x.title])), [data.tasks]);
  const taskTitle = useCallback((tid: string) => titles.get(tid), [titles]);
  return (
    <div className="flex flex-col gap-3">
      <FilterBar filters={filters} onChange={onFilters} tasks={data.tasks} label={t('missionControl.filters.label')}>
        <form
          className="flex min-w-[10rem] flex-1 flex-col gap-1"
          role="search"
          aria-label={t('missionControl.cli.search')}
          onSubmit={(e) => {
            e.preventDefault();
            onFilters({ ...filters, q: q.trim() || undefined });
          }}
        >
          <label htmlFor={`${id}-q`} className="text-xs font-medium text-fg-muted">
            {t('missionControl.cli.search')}
          </label>
          <input id={`${id}-q`} type="search" className="input min-h-target text-sm" value={q} maxLength={200} onChange={(e) => setQ(e.target.value)} />
        </form>
      </FilterBar>
      <p className="text-xs text-fg-muted" data-testid="cli-count">
        {t('missionControl.cli.count', { count: lines.length })} {t('missionControl.cli.note')}
      </p>
      {lines.length === 0 ? (
        <EmptyState headingLevel={3} title={t('missionControl.cli.empty')} description={t('missionControl.cli.emptyHint')} />
      ) : (
        <VirtualList
          items={lines}
          getKey={(l) => l.id}
          renderItem={(l) => <CliLineView line={l} taskTitle={taskTitle} />}
          label={t('missionControl.cli.label')}
          kind="feed"
          estimateSize={52}
          followOutput
          className="h-[min(60vh,40rem)] bg-canvas-secondary"
        />
      )}
    </div>
  );
}

/* ───────────────────────────── Системные события ───────────────────────────── */

function EventRow({ e, taskTitle }: { e: RunEvent; taskTitle: (id: string) => string | undefined }) {
  const { t } = useTranslation();
  const text = describeEvent(t, e, taskTitle);
  return (
    <>
      <div className="flex flex-wrap items-baseline gap-x-2 text-fg-muted">
        <span className="font-mono text-xs">{t('event.seq', { seq: e.seq })}</span>
        {e.timestamp ? <Timestamp value={e.timestamp} format="time" /> : null}
        <span className="font-semibold text-fg">{t(`event.type.${e.type}`, { defaultValue: e.type })}</span>
      </div>
      {text ? <p className={cx('whitespace-pre-line break-words', e.type === 'cli.output' && 'font-mono text-xs')}>{text}</p> : null}
    </>
  );
}

function SystemView({ data, filters, onFilters }: { data: MissionControlRun; filters: FeedFilters; onFilters: (f: FeedFilters) => void }) {
  const { t } = useTranslation();
  const now = useNow(Boolean(filters.since));
  const events = useMemo(() => filterEvents(data.stream.events, filters, sinceCutoff(filters.since, now)), [data.stream.events, filters, now]);
  const titles = useMemo(() => new Map(data.tasks.map((x) => [x.id, x.title])), [data.tasks]);
  const taskTitle = useCallback((id: string) => titles.get(id), [titles]);
  const groups = filters.groups ?? [];
  const toggle = (g: EventGroup | null) => {
    if (g === null) return onFilters({ ...filters, groups: undefined });
    const set = new Set(groups);
    if (set.has(g)) set.delete(g);
    else set.add(g);
    const list = EVENT_GROUPS.filter((x) => set.has(x));
    return onFilters({ ...filters, groups: list.length > 0 ? list : undefined });
  };
  return (
    <section aria-labelledby="run-feed-title" className="flex flex-col gap-3">
      <h3 id="run-feed-title" className="sr-only">
        {t('run.feed')}
      </h3>
      <div role="group" aria-label={t('missionControl.system.groups')} className="flex flex-wrap gap-1">
        <button type="button" className={cx('btn-sm', groups.length === 0 ? 'btn-primary' : 'btn-secondary')} aria-pressed={groups.length === 0} onClick={() => toggle(null)}>
          {t('missionControl.system.all')}
        </button>
        {EVENT_GROUPS.map((g) => (
          <button key={g} type="button" className={cx('btn-sm', groups.includes(g) ? 'btn-primary' : 'btn-secondary')} aria-pressed={groups.includes(g)} onClick={() => toggle(g)}>
            {t(`missionControl.system.group.${g}`)}
          </button>
        ))}
      </div>
      <FilterBar filters={filters} onChange={onFilters} tasks={data.tasks} label={t('missionControl.filters.label')} />
      {data.resynced ? <Callout tone="warning">{t('run.resynced')}</Callout> : null}
      {data.stream.events.length >= MC_EVENT_LIMIT ? <p className="text-xs text-fg-muted">{t('missionControl.system.limit', { count: data.stream.events.length })}</p> : null}
      {events.length === 0 ? (
        <p className="text-fg-muted">{data.stream.events.length === 0 ? t('run.feedEmpty') : t('missionControl.system.emptyFiltered')}</p>
      ) : events.length > VIRTUALIZE_FROM ? (
        <VirtualList
          items={events}
          getKey={(e) => e.event_id}
          renderItem={(e) => <EventRow e={e} taskTitle={taskTitle} />}
          label={t('run.feedLabel')}
          estimateSize={56}
          followOutput
          className="h-[min(60vh,40rem)]"
        />
      ) : (
        <ol aria-label={t('run.feedLabel')} className="flex flex-col divide-y divide-border rounded-lg border border-border bg-surface">
          {events.map((e) => (
            <li key={e.event_id} className="flex flex-col gap-0.5 px-3 py-2 text-sm" data-seq={e.seq} data-event-id={e.event_id} data-type={e.type}>
              <EventRow e={e} taskTitle={taskTitle} />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/* ───────────────────────────── Evidence ───────────────────────────── */

const EVIDENCE_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
  VERIFIED: 'success',
  PENDING: 'neutral',
  UNVERIFIED: 'warning',
  REJECTED: 'danger',
};

function EvidenceView({
  workspaceId,
  runId,
  role,
  tasks,
  filters,
  onFilters,
  onSelectTask,
}: {
  workspaceId: string;
  runId: string;
  role: string;
  tasks: readonly TaskSummary[];
  filters: FeedFilters;
  onFilters: (f: FeedFilters) => void;
  onSelectTask: (id: string) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const allowed = can(role, 'evidence:read');
  const query: RunEvidenceQuery = {
    limit: 50,
    ...(filters.evidenceStatus ? { verification_status: filters.evidenceStatus } : {}),
    ...(filters.task ? { task_id: filters.task } : {}),
  };
  const list = useInfiniteQuery({
    queryKey: qualityKeys.runEvidence(runId, query),
    queryFn: ({ pageParam, signal }) => qualityApi.runEvidence(runId, { ...query, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: allowed,
  });
  const now = useNow(Boolean(filters.since));
  const cutoff = sinceCutoff(filters.since, now);
  const items = useMemo(
    () =>
      (list.data?.pages.flatMap((p) => p.items) ?? []).filter(
        (e) => (cutoff === null || Date.parse(e.created_at) >= cutoff) && (!filters.role || tasks.find((x) => x.id === e.task_id)?.agent_role === filters.role),
      ),
    [list.data, cutoff, filters.role, tasks],
  );
  const titles = new Map(tasks.map((x) => [x.id, x.title]));
  if (!allowed) return <p className="text-fg-muted">{t('state.forbiddenDescription')}</p>;
  return (
    <div className="flex flex-col gap-3">
      <FilterBar filters={filters} onChange={onFilters} tasks={tasks} label={t('missionControl.filters.label')}>
        <div className="flex min-w-[9rem] flex-1 flex-col gap-1 sm:flex-none">
          <label htmlFor={`${id}-status`} className="text-xs font-medium text-fg-muted">
            {t('missionControl.evidence.status')}
          </label>
          <select
            id={`${id}-status`}
            className="select min-h-target text-sm"
            value={filters.evidenceStatus ?? ''}
            onChange={(e) => onFilters({ ...filters, evidenceStatus: (e.target.value || undefined) as FeedFilters['evidenceStatus'] })}
          >
            <option value="">{t('missionControl.evidence.allStatuses')}</option>
            {EVIDENCE_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`tasks.enums.verification.${s}`, { defaultValue: s })}
              </option>
            ))}
          </select>
        </div>
      </FilterBar>
      {list.isPending ? (
        <LoadingState label={t('tasks.evidence.loading')} />
      ) : list.isError && items.length === 0 ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState headingLevel={3} title={t('tasks.evidence.empty')} description={t('missionControl.evidence.emptyHint')} />
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-lg border border-border bg-surface" aria-label={t('missionControl.evidence.label')} data-testid="mc-evidence">
          {items.map((e) => (
            <li key={e.id} className="flex flex-col gap-1 px-3 py-2 text-sm" data-evidence-id={e.id} data-verification={e.verification_status}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{t(`tasks.enums.artifact.${e.artifact.kind}`, { defaultValue: e.kind })}</span>
                <Badge tone={EVIDENCE_TONE[e.verification_status] ?? 'neutral'}>
                  {t(`tasks.enums.verification.${e.verification_status}`, { defaultValue: e.verification_status })}
                </Badge>
                <Timestamp value={e.created_at} className="text-xs text-fg-muted" />
              </div>
              <p className="text-xs text-fg-muted">
                {t('tasks.evidence.producer')}: <span className="break-words">{e.producer}</span>
                {e.commit_sha ? <span className="ml-2 font-mono">{e.commit_sha.slice(0, 12)}</span> : null}
                {e.exit_code !== null && e.exit_code !== undefined ? <span className="ml-2">exit {e.exit_code}</span> : null}
              </p>
              <div className="flex flex-wrap gap-3 text-xs">
                {e.task_id ? (
                  <button type="button" className="link" onClick={() => onSelectTask(e.task_id ?? '')}>
                    {t('missionControl.evidence.task', { task: titles.get(e.task_id) ?? e.task_id.slice(0, 8) })}
                  </button>
                ) : null}
                <Link to={`/w/${workspaceId}/quality/evidence/${e.id}`} className="link">
                  {t('missionControl.evidence.open')}
                </Link>
                {can(role, 'artifact:download') && (e.artifact.kind !== 'raw_cli_log' || can(role, 'artifact:download_raw')) ? (
                  <a className="link" href={artifactDownloadPath(e.artifact.id)} download>
                    {t('tasks.evidence.download')}
                  </a>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
      {list.hasNextPage || list.isFetchNextPageError ? (
        <LoadMore
          shown={items.length}
          hasNextPage={list.hasNextPage}
          isFetchingNextPage={list.isFetchingNextPage}
          nextPageError={list.isFetchNextPageError ? list.error : null}
          onLoadMore={() => void list.fetchNextPage()}
        />
      ) : null}
    </div>
  );
}

/* ───────────────────────────── Панель ───────────────────────────── */

export interface FeedPanelProps {
  workspaceId: string;
  role: string;
  run: Run;
  data: MissionControlRun;
  view: McView;
  onView: (v: McView) => void;
  chatFilters: ChatFilters;
  onChatFilters: (f: ChatFilters) => void;
  feedFilters: FeedFilters;
  onFeedFilters: (f: FeedFilters) => void;
  taskHref: (taskId: string, tab?: string) => string;
  onSelectTask: (taskId: string) => void;
}

export function FeedPanel(props: FeedPanelProps) {
  const { t } = useTranslation();
  const { workspaceId, role, run, data, view, onView, feedFilters, onFeedFilters, taskHref, onSelectTask } = props;
  return (
    <section aria-labelledby="mc-feed-title" className="flex min-h-0 flex-col gap-2" data-testid="mc-feed">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="mc-feed-title" className="text-base font-semibold text-fg">
          {t('missionControl.feed.title')}
        </h2>
        <StreamStatus state={data.stream.state} error={data.stream.error} onReconnect={data.stream.reconnect} />
      </div>
      <Tabs value={view} onValueChange={(v) => onView(v as McView)} className="flex min-h-0 flex-col">
        <TabsList label={t('missionControl.feed.views')} variant="pills" className="mb-2 max-w-full">
          {MC_VIEWS.map((v) => (
            <TabsTrigger key={v} value={v}>
              <Icon name={VIEW_ICON[v]} className="h-4 w-4" />
              {t(`missionControl.view.${v}`)}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="conversation" className="min-h-0">
          <ConversationView
            run={run}
            role={role}
            data={data}
            filters={props.chatFilters}
            onFilters={props.onChatFilters}
            feedFilters={feedFilters}
            onFeedFilters={onFeedFilters}
            taskHref={taskHref}
            onSelectTask={onSelectTask}
          />
        </TabsContent>
        <TabsContent value="cli" className="min-h-0">
          <CliView data={data} filters={feedFilters} onFilters={onFeedFilters} />
        </TabsContent>
        <TabsContent value="system" className="min-h-0">
          <SystemView data={data} filters={feedFilters} onFilters={onFeedFilters} />
        </TabsContent>
        <TabsContent value="directives" className="min-h-0">
          <DirectivesPanel
            run={run}
            role={role}
            workspaceId={workspaceId}
            tasks={data.tasks}
            taskHref={taskHref}
            approvalsHref={`/w/${workspaceId}/approvals?run_id=${encodeURIComponent(run.id)}`}
            isTerminal={data.isTerminal}
          />
        </TabsContent>
        <TabsContent value="evidence" className="min-h-0">
          <EvidenceView
            workspaceId={workspaceId}
            runId={run.id}
            role={role}
            tasks={data.tasks}
            filters={feedFilters}
            onFilters={onFeedFilters}
            onSelectTask={onSelectTask}
          />
        </TabsContent>
      </Tabs>
    </section>
  );
}
