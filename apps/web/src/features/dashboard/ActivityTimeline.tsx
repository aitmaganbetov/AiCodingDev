import { useMemo, useRef, useState, type UIEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { StreamEvent, StreamState } from '../../api/sse';
import type { GateExecutionStatus } from '../../api/quality';
import { AgentBadge, Icon, RelativeTime, Tile, TileError, VerificationBadge, VirtualList, cx, type IconName } from '../../components/ui';
import { gateVerificationState } from '../../lib/verification';
import { describeEvent } from '../../pages/RunPage';
import { useActivity } from './data';
import {
  ACTIVITY_LIMIT,
  ALL_CATEGORIES,
  DEFAULT_CATEGORIES,
  eventCategory,
  fromActivity,
  fromStream,
  mergeTimeline,
  str,
  type ActivityCategory,
  type TimelineItem,
} from './model';

const CATEGORY_ICON: Record<ActivityCategory, IconName> = {
  runs: 'play',
  tasks: 'listChecks',
  agents: 'bot',
  approvals: 'approval',
  quality: 'quality',
  directives: 'command',
  chat: 'message',
  cli: 'terminal',
};

function EventRow({ item, workspaceId, runProjects }: { item: TimelineItem; workspaceId: string; runProjects: Map<string, string> }) {
  const { t } = useTranslation();
  const category = eventCategory(item.type);
  const text = describeEvent(t, { event_id: item.id, run_id: item.runId, seq: 0, type: item.type, timestamp: item.occurredAt, schema_version: '1.0', payload: item.payload }, () => undefined);
  const role = str(item.payload, 'agent_role');
  const projectId = runProjects.get(item.runId);
  const gate =
    item.type === 'gate.completed'
      ? gateVerificationState({ status: (str(item.payload, 'status') ?? 'PENDING') as GateExecutionStatus, evidence_id: str(item.payload, 'evidence_id') })
      : null;
  return (
    <div className="flex min-w-0 gap-3" data-event-type={item.type} data-event-id={item.id} data-live={item.live || undefined}>
      <span aria-hidden="true" className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-surface-muted text-fg-muted">
        <Icon name={category ? CATEGORY_ICON[category] : 'activity'} className="h-4 w-4" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-semibold text-fg">{t(`event.type.${item.type}`, { defaultValue: item.type })}</span>
          {gate ? <VerificationBadge result={gate} /> : null}
          {role && item.type !== 'agent.message' ? <AgentBadge agentRole={role} size="sm" showName={false} provider={str(item.payload, 'provider') ? t(`shell.cli.name.${str(item.payload, 'provider')}`, { defaultValue: str(item.payload, 'provider') ?? '' }) : undefined} /> : null}
          {item.live ? (
            <span className="rounded-full border border-info-border bg-info-soft px-2 py-0.5 text-xs font-semibold text-info-fg">{t('dashboard.activity.live')}</span>
          ) : null}
          <span className="ml-auto text-xs text-fg-muted">
            <RelativeTime value={item.occurredAt} />
          </span>
        </div>
        {text ? <p className="line-clamp-2 break-words text-fg-muted">{text}</p> : null}
        {projectId ? (
          <Link to={`/w/${workspaceId}/projects/${projectId}/runs/${item.runId}`} className="link w-fit text-xs">
            {t('dashboard.activity.openRun')}
          </Link>
        ) : (
          <Link to={`/w/${workspaceId}/mission-control?run=${encodeURIComponent(item.runId)}`} className="link w-fit text-xs">
            {t('dashboard.activity.openRun')}
          </Link>
        )}
      </div>
    </div>
  );
}

/**
 * Лента активности AI (план §3.2, B1 GET /activity + поток workspace): последние события журнала
 * run_event_log, фильтры по типам (параметр event_type), новые события потока — сверху.
 * Позиция чтения не сдвигается (§5.5): если пользователь прокрутил ленту, новые события ждут
 * кнопки «Показать N новых»; у начала ленты — появляются сразу. Объявление — вежливое и агрегированное.
 */
export function ActivityTimeline({
  workspaceId,
  liveEvents,
  liveState,
  onReconnect,
  runProjects,
}: {
  workspaceId: string;
  liveEvents: readonly StreamEvent[];
  liveState: StreamState;
  onReconnect: () => void;
  runProjects: Map<string, string>;
}) {
  const { t } = useTranslation();
  const [categories, setCategories] = useState<ActivityCategory[]>([...DEFAULT_CATEGORIES]);
  const q = useActivity(workspaceId, categories);
  const feedRef = useRef<HTMLDivElement>(null);
  const liveItems = useMemo(() => liveEvents.map(fromStream), [liveEvents]);
  // Пока пользователь читает ленту ниже начала, число показанных событий потока «заморожено»;
  // остальные ждут кнопки. У начала ленты (null) новые события появляются сразу.
  const [frozen, setFrozen] = useState<number | null>(null);
  const onScrollCapture = (e: UIEvent<HTMLDivElement>) => {
    const el = e.target as HTMLElement;
    if (el.dataset.testid !== 'virtual-list') return;
    if (el.scrollTop < 8) setFrozen(null);
    else setFrozen((f) => f ?? liveItems.length);
  };
  const shown = frozen ?? liveItems.length;
  const pageItems = useMemo(() => (q.data?.items ?? []).map(fromActivity), [q.data]);
  const pendingLive = mergeTimeline(liveItems.slice(shown), [], categories).filter((i) => !pageItems.some((e) => e.id === i.id));
  const items = useMemo(() => mergeTimeline(liveItems.slice(0, shown), pageItems, categories), [liveItems, shown, pageItems, categories]);

  const toggle = (c: ActivityCategory) => setCategories((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : ALL_CATEGORIES.filter((x) => x === c || prev.includes(x))));
  const allOn = ALL_CATEGORIES.every((c) => categories.includes(c));

  return (
    <Tile
      id="dashboard-activity"
      icon="activity"
      headingLevel={2}
      title={t('dashboard.activity.title')}
      className="xl:col-span-7"
      source={t('dashboard.activity.sample', { limit: ACTIVITY_LIMIT })}
      actions={<LiveIndicator state={liveState} onReconnect={onReconnect} />}
    >
      <p className="-mt-2 text-sm text-fg-muted">{t('dashboard.activity.subtitle')}</p>
      <div role="group" aria-label={t('dashboard.activity.filters')} className="flex flex-wrap gap-2">
        <FilterChip pressed={allOn} onClick={() => setCategories(allOn ? [...DEFAULT_CATEGORIES] : [...ALL_CATEGORIES])}>
          {t('dashboard.activity.selectAll')}
        </FilterChip>
        {ALL_CATEGORIES.map((c) => (
          <FilterChip key={c} pressed={categories.includes(c)} onClick={() => toggle(c)} icon={CATEGORY_ICON[c]}>
            {t(`dashboard.activity.category.${c}`)}
          </FilterChip>
        ))}
      </div>
      <p role="status" aria-live="polite" className="sr-only">
        {pendingLive.length > 0 ? t('dashboard.activity.announce', { count: pendingLive.length }) : ''}
      </p>
      {pendingLive.length > 0 ? (
        <button
          type="button"
          className="btn-secondary btn-sm w-fit"
          onClick={() => {
            setFrozen(null);
            feedRef.current?.querySelector<HTMLElement>('[data-testid="virtual-list"]')?.scrollTo({ top: 0 });
          }}
        >
          <Icon name="chevronUp" className="h-4 w-4" />
          {t('dashboard.activity.newEvents', { count: pendingLive.length })}
        </button>
      ) : null}
      {categories.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('dashboard.activity.noFilter')}</p>
      ) : q.isPending ? (
        <div aria-busy="true" className="flex flex-col gap-2" data-testid="activity-skeleton">
          <span className="sr-only">{t('ds.tile.loading')}</span>
          {[0, 1, 2, 3].map((i) => (
            <div key={i} aria-hidden="true" className="skeleton h-12 w-full" />
          ))}
        </div>
      ) : q.isError ? (
        <TileError error={q.error} onRetry={() => void q.refetch()} />
      ) : items.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border p-4 text-sm text-fg-muted" data-testid="activity-empty">
          {t('dashboard.activity.empty')}
        </p>
      ) : (
        <div ref={feedRef} onScrollCapture={onScrollCapture}>
          <VirtualList
            kind="feed"
            label={t('dashboard.activity.feed')}
            items={items}
            getKey={(i) => i.id}
            estimateSize={76}
            className="h-[26rem]"
            initialHeight={416}
            renderItem={(item) => <EventRow item={item} workspaceId={workspaceId} runProjects={runProjects} />}
          />
        </div>
      )}
    </Tile>
  );
}

function FilterChip({ pressed, onClick, icon, children }: { pressed: boolean; onClick: () => void; icon?: IconName; children: string }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cx(
        'inline-flex min-h-target items-center gap-1 rounded-full border px-3 py-1 text-xs font-medium',
        'transition-transform duration-instant ease-standard motion-safe:active:scale-95',
        pressed ? 'border-border-active bg-accent-soft text-accent-soft-fg' : 'border-input bg-surface text-fg-muted hover:bg-surface-muted hover:text-fg',
      )}
    >
      {icon ? <Icon name={pressed ? 'check' : icon} className="h-3.5 w-3.5" /> : null}
      {children}
    </button>
  );
}

export function LiveIndicator({ state, onReconnect }: { state: StreamState; onReconnect: () => void }) {
  const { t } = useTranslation();
  const tone = state === 'open' ? 'text-success-fg' : state === 'failed' || state === 'offline' ? 'text-danger-fg' : 'text-fg-muted';
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className={cx('inline-flex items-center gap-1 text-xs font-medium', tone)} data-testid="live-state" data-state={state}>
        <span aria-hidden="true" className={cx('inline-block h-2 w-2 rounded-full', state === 'open' ? 'bg-success' : state === 'failed' || state === 'offline' ? 'bg-danger' : 'bg-fg-muted')} />
        {t(`dashboard.live.${state}`)}
      </span>
      {state === 'failed' ? (
        <button type="button" className="btn-secondary btn-sm" onClick={onReconnect}>
          {t('dashboard.live.reconnect')}
        </button>
      ) : null}
    </span>
  );
}
