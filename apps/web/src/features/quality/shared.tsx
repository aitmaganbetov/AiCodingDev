/**
 * Общие элементы экранов Evidence & Quality, Standards & Compliance и Approvals (P7-10, P7-11).
 * Тексты находок, вывода инструментов, обоснований — недоверенные данные: только как текст.
 */
import { useInfiniteQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { runsApi, type RunSummary } from '../../api/runs';
import { Badge, type Tone } from '../../components/ui/Badge';
import { LoadMore } from '../../components/LoadMore';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { Timestamp } from '../../components/ui/Time';
import { useCurrentWorkspace } from '../../workspace';

export const RECENT_RUNS_PAGE_SIZE = 20;

/** Роль пользователя в текущем workspace (null — нет членства). */
export function useWorkspaceRole(): string | null {
  return useCurrentWorkspace().membership?.role ?? null;
}

export function workspacePath(workspaceId: string, path: string): string {
  return `/w/${workspaceId}/${path}`;
}

/** Ссылка на Run view (маршрут области runs; только URL). */
export function runViewPath(workspaceId: string, projectId: string, runId: string): string {
  return workspacePath(workspaceId, `projects/${projectId}/runs/${runId}`);
}

const SEVERITY_TONE: Record<string, Tone> = { critical: 'danger', high: 'danger', medium: 'warning', low: 'info', info: 'neutral' };

/** Severity находки / уровень риска: тон + текст (цвет всегда дублируется словом). */
export function SeverityBadge({ value, kind = 'severity' }: { value: string; kind?: 'severity' | 'risk' }) {
  const { t } = useTranslation();
  return (
    <Badge tone={SEVERITY_TONE[value] ?? 'neutral'} data-severity={value}>
      {t(`quality.enums.${kind}.${value}`, { defaultValue: value })}
    </Badge>
  );
}

const GATE_TONE: Record<string, Tone> = {
  PASS: 'success',
  FAIL: 'danger',
  ERROR: 'danger',
  SKIPPED: 'warning',
  NOT_APPLICABLE: 'neutral',
  PENDING: 'info',
  RUNNING: 'info',
};

/** Статус gate / verdict: SKIPPED и ERROR — никогда не «зелёные» (ТЗ §14: SKIPPED ≠ PASS). */
export function GateStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  return (
    <Badge tone={GATE_TONE[status] ?? 'neutral'} data-status={status}>
      {t(`quality.enums.gate_status.${status}`, { defaultValue: status })}
    </Badge>
  );
}

/** Хэш/commit: короткий префикс на экране, полный — для скринридера и копирования из title. */
export function Hash({ value, length = 12 }: { value: string; length?: number }) {
  if (value.length <= length) return <code className="font-mono text-xs">{value}</code>;
  return (
    <code className="font-mono text-xs" title={value}>
      <span aria-hidden="true">{value.slice(0, length)}…</span>
      <span className="sr-only">{value}</span>
    </code>
  );
}

/** Строка «термин — значение» для списков определений; пустое значение — «не указано». */
export function Field({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  const { t } = useTranslation();
  const empty = children === null || children === undefined || children === '';
  return (
    <>
      <dt className="text-fg-muted">{label}</dt>
      <dd className="min-w-0 break-words" data-testid={testId}>
        {empty ? <span className="text-fg-muted">{t('quality.notRecorded')}</span> : children}
      </dd>
    </>
  );
}

export function FieldList({ children, label }: { children: ReactNode; label?: string }) {
  return (
    <dl aria-label={label} className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[minmax(10rem,auto),1fr]">
      {children}
    </dl>
  );
}

function RunPickerItem({ run, actions }: { run: RunSummary; actions: (run: RunSummary) => ReactNode }) {
  const { t } = useTranslation();
  return (
    <li className="card flex flex-col gap-2" data-run-id={run.id}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge kind="run" status={run.status} />
        <span className="text-fg-muted">{t(`enums.run_mode.${run.mode}`, { defaultValue: run.mode })}</span>
        <span className="text-fg-muted">
          <Timestamp value={run.created_at} />
        </span>
      </div>
      <p className="line-clamp-2 whitespace-pre-line break-words">{run.objective}</p>
      <div className="flex flex-wrap gap-4">{actions(run)}</div>
    </li>
  );
}

/** Последние запуски workspace (GET /runs) со ссылками на экран run (качество, соответствие). */
export function RecentRuns({ workspaceId, label, actions }: { workspaceId: string; label: string; actions: (run: RunSummary) => ReactNode }) {
  const { t } = useTranslation();
  const query = useInfiniteQuery({
    queryKey: ['runs', workspaceId, 'recent'],
    queryFn: ({ pageParam, signal }) =>
      runsApi.list(workspaceId, { limit: RECENT_RUNS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  if (query.isPending) return <LoadingState label={t('quality.runs.loading')} />;
  if (query.isError && items.length === 0) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (items.length === 0) return <EmptyState headingLevel={3} title={t('quality.runs.emptyTitle')} description={t('quality.runs.emptyDescription')} />;
  return (
    <>
      <ul className="flex flex-col gap-3" aria-label={label}>
        {items.map((r) => (
          <RunPickerItem key={r.id} run={r} actions={actions} />
        ))}
      </ul>
      <LoadMore
        shown={items.length}
        hasNextPage={query.hasNextPage}
        isFetchingNextPage={query.isFetchingNextPage}
        nextPageError={query.isFetchNextPageError ? query.error : null}
        onLoadMore={() => void query.fetchNextPage()}
      />
    </>
  );
}

export function TextLink({ to, children, srSuffix }: { to: string; children: ReactNode; srSuffix?: string }) {
  return (
    <Link to={to} className="font-medium text-link underline">
      {children}
      {srSuffix ? <span className="sr-only">: {srSuffix}</span> : null}
    </Link>
  );
}
