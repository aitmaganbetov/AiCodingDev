/**
 * P8-07 Analytics (ТЗ §11, §16; FR-OBS-05, FR-CI-08): сводка метрик workspace за период.
 * Параметры (период, проект) — в URL; данные — GET /analytics/summary (агрегаты под RLS workspace).
 */
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useMemo, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router';
import { analyticsApi, analyticsKeys, type AnalyticsSummary } from '../../api/analytics';
import { projectKeys, projectsApi } from '../../api/projects';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { Card, Table, Timestamp, type Column } from '../../components/ui';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { BarList, RunsByDayChart } from './charts';
import {
  PERIODS,
  formatBytes,
  formatDuration,
  formatRate,
  paramsFromSearch,
  queryFromParams,
  searchFromParams,
  type AnalyticsParams,
  type Period,
} from './format';

type Summary = AnalyticsSummary;
type ModeRow = Summary['runs']['by_mode'][number];
type RoleRow = Summary['tasks']['by_role'][number];
type GateRow = Summary['gates'][number];
type ProviderRow = Summary['providers'][number];

function Kpi({ label, value, hint, testId }: { label: string; value: string; hint?: string; testId: string }) {
  return (
    <div className="card flex flex-col gap-1" data-testid={testId}>
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className="text-3xl font-semibold text-fg">{value}</dd>
      {hint ? <dd className="text-xs text-fg-muted">{hint}</dd> : null}
    </div>
  );
}

function Facts({ items, label }: { items: [string, ReactNode][]; label: string }) {
  return (
    <dl aria-label={label} className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
      {items.map(([k, v]) => (
        <div key={k} className="flex flex-col">
          <dt className="text-sm text-fg-muted">{k}</dt>
          <dd className="font-medium text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function Filters({ period, projectId, onChange }: { period: Period; projectId: string | null; onChange: (patch: Partial<AnalyticsParams>) => void }) {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  const projects = useQuery({
    queryKey: projectKeys.summary(workspaceId ?? ''),
    queryFn: ({ signal }) => projectsApi.list(workspaceId ?? '', { limit: 100 }, signal),
    enabled: Boolean(workspaceId),
  });
  return (
    <form className="card mb-6 grid gap-4 sm:grid-cols-2" aria-label={t('analytics.filters')} onSubmit={(e) => e.preventDefault()}>
      <div className="flex flex-col gap-1">
        <label htmlFor="analytics-period" className="text-sm font-medium text-fg">
          {t('analytics.period')}
        </label>
        <select id="analytics-period" className="input" value={period} onChange={(e) => onChange({ period: e.target.value as Period })}>
          {PERIODS.map((p) => (
            <option key={p} value={p}>
              {t(`analytics.periods.${p}`)}
            </option>
          ))}
        </select>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="analytics-project" className="text-sm font-medium text-fg">
          {t('analytics.project')}
        </label>
        <select
          id="analytics-project"
          className="input"
          value={projectId ?? ''}
          onChange={(e) => onChange({ projectId: e.target.value || null })}
          disabled={!projects.data}
        >
          <option value="">{t('analytics.allProjects')}</option>
          {projects.data?.items.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>
    </form>
  );
}

function SummaryView({ data }: { data: Summary }) {
  const { t } = useTranslation();
  const { runs, tasks, approvals, context } = data;
  const modeColumns: Column<ModeRow>[] = [
    { id: 'mode', header: t('analytics.modes.mode'), rowHeader: true, cell: (m) => t(`enums.run_mode.${m.mode}`, { defaultValue: m.mode }), sortValue: (m) => m.mode },
    { id: 'total', header: t('analytics.modes.total'), cell: (m) => m.total, sortValue: (m) => m.total },
    { id: 'completed', header: t('analytics.modes.completed'), cell: (m) => m.completed, sortValue: (m) => m.completed },
    { id: 'rate', header: t('analytics.modes.successRate'), cell: (m) => formatRate(t, m.success_rate), sortValue: (m) => m.success_rate },
    { id: 'duration', header: t('analytics.modes.avgDuration'), cell: (m) => formatDuration(t, m.avg_duration_seconds), sortValue: (m) => m.avg_duration_seconds },
  ];
  const roleColumns: Column<RoleRow>[] = [
    { id: 'role', header: t('analytics.tasks.role'), rowHeader: true, cell: (r) => t(`enums.agent_role.${r.role}`, { defaultValue: r.role }), sortValue: (r) => r.role },
    { id: 'attempts', header: t('analytics.tasks.attempts'), cell: (r) => r.attempts, sortValue: (r) => r.attempts },
    { id: 'succeeded', header: t('analytics.tasks.succeeded'), cell: (r) => r.succeeded, sortValue: (r) => r.succeeded },
    { id: 'failed', header: t('analytics.tasks.failed'), cell: (r) => r.failed, sortValue: (r) => r.failed },
    { id: 'retries', header: t('analytics.tasks.retries'), cell: (r) => r.retries, sortValue: (r) => r.retries },
    { id: 'vcr', header: t('analytics.tasks.vcr'), cell: (r) => formatRate(t, r.verified_completion_rate), sortValue: (r) => r.verified_completion_rate },
  ];
  const gateColumns: Column<GateRow>[] = [
    { id: 'type', header: t('analytics.gates.type'), rowHeader: true, cell: (g) => g.gate_type, sortValue: (g) => g.gate_type },
    { id: 'total', header: t('analytics.gates.total'), cell: (g) => g.total, sortValue: (g) => g.total },
    { id: 'passed', header: t('analytics.gates.passed'), cell: (g) => g.passed, sortValue: (g) => g.passed },
    { id: 'failed', header: t('analytics.gates.failed'), cell: (g) => g.failed, sortValue: (g) => g.failed },
    { id: 'errored', header: t('analytics.gates.errored'), cell: (g) => g.errored, sortValue: (g) => g.errored },
    { id: 'skipped', header: t('analytics.gates.skipped'), cell: (g) => g.skipped, sortValue: (g) => g.skipped },
    { id: 'rate', header: t('analytics.gates.passRate'), cell: (g) => formatRate(t, g.pass_rate), sortValue: (g) => g.pass_rate },
  ];
  const tokens = (p: ProviderRow): string => {
    if (p.tokens.total === null) return t('analytics.providers.tokensUnknown');
    const value = p.tokens.total.toLocaleString('ru-RU');
    return p.tokens.source === 'mixed' ? t('analytics.providers.tokensMixed', { value }) : value;
  };
  const providerColumns: Column<ProviderRow>[] = [
    { id: 'provider', header: t('analytics.providers.provider'), rowHeader: true, cell: (p) => p.provider, sortValue: (p) => p.provider },
    { id: 'executions', header: t('analytics.providers.executions'), cell: (p) => p.executions, sortValue: (p) => p.executions },
    { id: 'rate', header: t('analytics.providers.successRate'), cell: (p) => formatRate(t, p.success_rate), sortValue: (p) => p.success_rate },
    {
      id: 'limits',
      header: t('analytics.providers.limits'),
      cell: (p) => t('analytics.providers.limitsValue', { rate: p.rate_limited, auth: p.auth_required }),
      sortValue: (p) => p.rate_limited + p.auth_required,
    },
    { id: 'errors', header: t('analytics.providers.errors'), cell: (p) => p.errors, sortValue: (p) => p.errors },
    { id: 'duration', header: t('analytics.providers.avgDuration'), cell: (p) => formatDuration(t, p.avg_duration_seconds), sortValue: (p) => p.avg_duration_seconds },
    { id: 'tokens', header: t('analytics.providers.tokens'), cell: tokens, sortValue: (p) => p.tokens.total },
  ];
  const messageRows = Object.entries(data.agent_messages.by_type).map(([type, count]) => ({ type, count }));

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <section aria-labelledby="analytics-kpi">
        <h2 id="analytics-kpi" className="sr-only">
          {t('analytics.kpi.title')}
        </h2>
        <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <Kpi
            testId="kpi-success"
            label={t('analytics.kpi.successRate')}
            value={formatRate(t, runs.success_rate)}
            hint={t('analytics.kpi.successRateHint', { completed: runs.completed, finished: runs.finished })}
          />
          <Kpi
            testId="kpi-vcr"
            label={t('analytics.kpi.vcr')}
            value={formatRate(t, tasks.verified_completion_rate)}
            hint={t('analytics.kpi.vcrHint', { succeeded: tasks.succeeded })}
          />
          <Kpi testId="kpi-retries" label={t('analytics.kpi.retries')} value={String(tasks.retries)} hint={t('analytics.kpi.retriesHint', { attempts: tasks.attempts })} />
          <Kpi
            testId="kpi-approval"
            label={t('analytics.kpi.approvalWait')}
            value={formatDuration(t, approvals.wait_seconds.p50)}
            hint={t('analytics.kpi.approvalWaitHint', { p90: formatDuration(t, approvals.wait_seconds.p90) })}
          />
          <Kpi
            testId="kpi-usefulness"
            label={t('analytics.kpi.usefulness')}
            value={formatRate(t, context.usefulness.rate)}
            hint={t('analytics.kpi.usefulnessHint', { referenced: context.usefulness.fragments_referenced, total: context.usefulness.fragments_total })}
          />
        </dl>
      </section>

      <Card as="section" className="min-w-0" id="analytics-daily" title={t('analytics.daily.title')}>
        <p className="mb-3 text-sm text-fg-muted">
          {t('analytics.runs.duration')}: {t('analytics.runs.p50', { value: formatDuration(t, runs.duration_seconds.p50) })},{' '}
          {t('analytics.runs.p90', { value: formatDuration(t, runs.duration_seconds.p90) })}
        </p>
        <RunsByDayChart days={data.daily} />
      </Card>

      <div className="grid min-w-0 gap-6 xl:grid-cols-2">
        <Card as="section" className="min-w-0" id="analytics-modes" title={t('analytics.modes.title')}>
          <Table caption={t('analytics.modes.caption')} columns={modeColumns} rows={runs.by_mode} rowKey={(m) => m.mode} minWidthClass="min-w-[520px]" />
        </Card>
        <Card as="section" className="min-w-0" id="analytics-approvals" title={t('analytics.approvals.title')}>
          <Facts
            label={t('analytics.approvals.title')}
            items={[
              [t('analytics.approvals.requested'), approvals.requested],
              [t('analytics.approvals.pending'), approvals.pending],
              [t('analytics.approvals.waitP50'), formatDuration(t, approvals.wait_seconds.p50)],
              [t('analytics.approvals.waitP90'), formatDuration(t, approvals.wait_seconds.p90)],
            ]}
          />
        </Card>
      </div>

      <Card as="section" className="min-w-0" id="analytics-tasks" title={t('analytics.tasks.title')}>
        <p className="mb-3 text-sm text-fg-muted">
          {t('analytics.tasks.latency', { p50: formatDuration(t, tasks.latency_seconds.p50), p90: formatDuration(t, tasks.latency_seconds.p90) })}
        </p>
        <Table caption={t('analytics.tasks.caption')} columns={roleColumns} rows={tasks.by_role} rowKey={(r) => r.role} minWidthClass="min-w-[640px]" />
      </Card>

      <Card as="section" className="min-w-0" id="analytics-gates" title={t('analytics.gates.title')}>
        {data.gates.length === 0 ? (
          <p className="text-fg-muted">{t('analytics.gates.empty')}</p>
        ) : (
          <div className="flex flex-col gap-4">
            <BarList
              label={t('analytics.gates.barsLabel')}
              bars={data.gates.map((g) => ({ key: g.gate_type, label: g.gate_type, value: g.pass_rate, text: formatRate(t, g.pass_rate) }))}
            />
            <Table caption={t('analytics.gates.caption')} columns={gateColumns} rows={data.gates} rowKey={(g) => g.gate_type} minWidthClass="min-w-[640px]" />
          </div>
        )}
      </Card>

      <Card as="section" className="min-w-0" id="analytics-providers" title={t('analytics.providers.title')}>
        {data.providers.length === 0 ? (
          <p className="text-fg-muted">{t('analytics.providers.empty')}</p>
        ) : (
          <Table caption={t('analytics.providers.caption')} columns={providerColumns} rows={data.providers} rowKey={(p) => p.provider} minWidthClass="min-w-[760px]" />
        )}
      </Card>

      <div className="grid min-w-0 gap-6 xl:grid-cols-2">
        <Card as="section" className="min-w-0" id="analytics-collaboration" title={t('analytics.collaboration.title')}>
          <Facts
            label={t('analytics.collaboration.title')}
            items={[
              [t('analytics.collaboration.reviewRequests'), data.review_loops.review_requests],
              [t('analytics.collaboration.fixRequests'), data.review_loops.fix_requests],
              [t('analytics.collaboration.chains'), data.review_loops.review_chains],
              [t('analytics.collaboration.avgFixRounds'), data.review_loops.avg_fix_rounds ?? t('analytics.noData')],
              [t('analytics.collaboration.maxFixRounds'), data.review_loops.max_fix_rounds],
              [t('analytics.collaboration.exhausted'), data.review_loops.loops_exhausted],
              [t('analytics.collaboration.messages'), data.agent_messages.total],
            ]}
          />
          {messageRows.length > 0 ? (
            <div className="mt-4">
              <Table
                caption={t('analytics.collaboration.messagesCaption')}
                columns={[
                  { id: 'type', header: t('analytics.collaboration.type'), rowHeader: true, cell: (r) => r.type, sortValue: (r) => r.type },
                  { id: 'count', header: t('analytics.collaboration.count'), cell: (r) => r.count, sortValue: (r) => r.count },
                ]}
                rows={messageRows}
                rowKey={(r) => r.type}
              />
            </div>
          ) : null}
        </Card>
        <Card as="section" className="min-w-0" id="analytics-context" title={t('analytics.context.title')}>
          <Facts
            label={t('analytics.context.title')}
            items={[
              [t('analytics.context.packs'), context.packs],
              [t('analytics.context.avgSize'), formatBytes(t, context.avg_size_bytes)],
              [t('analytics.context.staleRatio'), formatRate(t, context.stale_fragment_ratio)],
              [t('analytics.context.usefulness'), formatRate(t, context.usefulness.rate)],
            ]}
          />
          <p className="mt-3 text-sm text-fg-muted">{t('analytics.context.usefulnessNote', { count: context.usefulness.packs_evaluated })}</p>
          {context.usefulness.truncated ? <p className="mt-1 text-sm text-fg-muted">{t('analytics.context.truncated')}</p> : null}
        </Card>
      </div>
    </div>
  );
}

export function AnalyticsPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [search, setSearch] = useSearchParams();
  const params = paramsFromSearch(search);
  // Окно фиксируется на час (ключ кэша стабилен при повторных рендерах).
  const query = useMemo(() => queryFromParams(params), [params.period, params.projectId]); // eslint-disable-line react-hooks/exhaustive-deps
  const summary = useQuery({
    queryKey: analyticsKeys.summary(workspaceId ?? '', query),
    queryFn: ({ signal }) => analyticsApi.summary(workspaceId ?? '', query, signal),
    enabled: Boolean(workspaceId && membership),
    placeholderData: keepPreviousData,
  });
  if (!workspaceId || !membership) return <WorkspaceNotFound />;

  // От текущего URL документа, а не от значений последнего рендера и не от `prev` setSearchParams:
  // навигация react-router идёт в transition, и history уже обновлён, а рендер с новыми параметрами —
  // ещё нет. Быстрые последовательные изменения (период → проект) не теряют предыдущее значение.
  const onChange = (patch: Partial<AnalyticsParams>) =>
    setSearch(() => searchFromParams({ ...paramsFromSearch(new URLSearchParams(window.location.search)), ...patch }));

  let body: ReactNode;
  if (summary.isPending) body = <LoadingState label={t('analytics.loading')} />;
  else if (summary.isError) body = <ErrorState error={summary.error} onRetry={() => void summary.refetch()} title={t('analytics.error')} />;
  else if (summary.data.runs.total === 0 && summary.data.providers.length === 0)
    body = <EmptyState icon="layers" title={t('analytics.emptyTitle')} description={t('analytics.emptyDescription')} />;
  else body = <SummaryView data={summary.data} />;

  return (
    <>
      <PageHeading title={t('analytics.title')} />
      <p className="mb-4 max-w-3xl text-fg-muted">{t('analytics.intro')}</p>
      <Filters period={params.period} projectId={params.projectId} onChange={onChange} />
      {summary.data ? (
        <p className="mb-4 text-sm text-fg-muted" data-testid="analytics-window">
          {t('analytics.window')} <Timestamp value={summary.data.window.from} /> — <Timestamp value={summary.data.window.to} />
        </p>
      ) : null}
      <div aria-busy={summary.isFetching}>
        {body}
      </div>
    </>
  );
}
