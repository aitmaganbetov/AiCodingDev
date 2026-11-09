/**
 * «Гейты качества» уровня workspace (F7, план §3.7, §5.2; D-6, D-8): исходы gates за окно analytics
 * в пяти состояниях — «Пройдено» только PASS с VERIFIED evidence (`passed_verified`), PASS без него —
 * «Недостаточно доказательств», FAIL, ERROR → «Заблокировано», SKIPPED → «Не запускалось»; группы
 * проверок (lint … security) и типы gates; переход к отчёту качества каждого запуска.
 * Интегральной оценки качества нет (D-6).
 */
import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router';
import { analyticsApi, analyticsKeys, type AnalyticsSummary } from '../../api/analytics';
import { projectsApi } from '../../api/projects';
import { PageHeading } from '../../components/PageHeading';
import { Donut, type DonutSegment } from '../../components/ui/Charts';
import { Table, type Column } from '../../components/ui/Table';
import { Tile } from '../../components/ui/Tile';
import { Timestamp } from '../../components/ui/Time';
import { VerificationBadge } from '../../components/ui/VerificationBadge';
import { gateAggregateOutcomes, VERIFICATION_STATES, type GateAggregateCounts, type VerificationState } from '../../lib/verification';
import { useCurrentWorkspace } from '../../workspace';
import { categoryAggregates, gateTypeVerifiedRate, qualityWindow, type CategoryAggregate } from './gatesModel';
import { RecentRuns, TextLink, workspacePath } from './shared';

export const QUALITY_WINDOWS = [7, 30, 90] as const;
const DEFAULT_WINDOW = 30;
const PROJECTS_LIMIT = 100;

const OUTCOME_STYLE: Record<VerificationState, { stroke: string; swatch: string }> = {
  PASS: { stroke: 'stroke-verify-pass', swatch: 'bg-verify-pass' },
  INSUFFICIENT_EVIDENCE: { stroke: 'stroke-verify-insufficient', swatch: 'bg-verify-insufficient' },
  FAIL: { stroke: 'stroke-verify-fail', swatch: 'bg-verify-fail' },
  BLOCKED: { stroke: 'stroke-verify-blocked', swatch: 'bg-verify-blocked' },
  NOT_RUN: { stroke: 'stroke-verify-not-run', swatch: 'bg-verify-not-run' },
};

type GateStats = AnalyticsSummary['gates'][number];

function countsOf(g: GateStats): GateAggregateCounts {
  return { passed_verified: g.passed_verified, passed_unverified: g.passed_unverified, failed: g.failed, errored: g.errored, skipped: g.skipped };
}

function stateCount(counts: GateAggregateCounts, state: VerificationState): number {
  return gateAggregateOutcomes(counts).find((o) => o.state === state)?.count ?? 0;
}

function percent(ratio: number | null): string | null {
  return ratio === null ? null : `${(ratio * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} %`;
}

/** Колонки пяти состояний (заголовок — метка состояния; значение — число, у ячейки data-state). */
function stateColumns<T>(counts: (row: T) => GateAggregateCounts, t: (k: string) => string): Column<T>[] {
  return VERIFICATION_STATES.map((state) => ({
    id: state,
    header: t(`ds.verification.${state}`),
    cell: (row: T) => (
      <span className="tabular-nums" data-state={state}>
        {stateCount(counts(row), state).toLocaleString('ru-RU')}
      </span>
    ),
    sortValue: (row: T) => stateCount(counts(row), state),
  }));
}

function Outcomes({ data, days }: { data: AnalyticsSummary; days: number }) {
  const { t } = useTranslation();
  const total: GateAggregateCounts = { passed_verified: 0, passed_unverified: 0, failed: 0, errored: 0, skipped: 0 };
  for (const g of data.gates) for (const k of Object.keys(total) as (keyof GateAggregateCounts)[]) total[k] += g[k];
  const executions = data.gates.reduce((a, g) => a + g.total, 0);
  const outcomes = gateAggregateOutcomes(total);
  const segments: DonutSegment[] = outcomes.map((o) => ({
    key: o.state,
    label: t(`ds.verification.${o.state}`),
    value: o.count,
    strokeClass: OUTCOME_STYLE[o.state].stroke,
    swatchClass: OUTCOME_STYLE[o.state].swatch,
    marker: <VerificationBadge result={{ state: o.state, source: o.source }} />,
  }));
  if (executions === 0) {
    return (
      <p className="rounded-lg border border-dashed border-border p-4 text-sm text-fg-muted" data-testid="quality-no-gates">
        {t('quality.overview.noGates', { days })}
      </p>
    );
  }
  return (
    <>
      <Donut
        testId="quality-outcomes"
        segments={segments}
        summary={t('quality.overview.donut', { summary: segments.map((s) => `${t(`ds.verification.${s.key}`)} — ${s.value}`).join(', ') })}
        legendLabel={t('quality.overview.outcomes')}
        center={
          <span className="flex flex-col leading-tight">
            {executions.toLocaleString('ru-RU')}
            <span className="text-xs font-normal text-fg-muted">{t('quality.overview.executions')}</span>
          </span>
        }
      />
      <p className="text-xs text-fg-muted">{t('quality.overview.d8')}</p>
    </>
  );
}

function CategoryTable({ aggregates }: { aggregates: CategoryAggregate[] }) {
  const { t } = useTranslation();
  const columns: Column<CategoryAggregate>[] = [
    {
      id: 'category',
      header: t('quality.gates.category'),
      rowHeader: true,
      cell: (c) => (
        <span className="flex flex-col">
          <span className="font-medium">{t(`quality.gates.categories.${c.category}`)}</span>
          <span className="break-words font-mono text-xs text-fg-muted">{c.gateTypes.length > 0 ? c.gateTypes.join(', ') : t('quality.overview.noExecutions')}</span>
        </span>
      ),
    },
    ...stateColumns<CategoryAggregate>((c) => c.counts, t),
    {
      id: 'rate',
      header: t('quality.overview.verifiedRate'),
      cell: (c) => <span className="tabular-nums">{percent(c.verifiedRate) ?? t('ds.metric.noData')}</span>,
      sortValue: (c) => c.verifiedRate,
    },
  ];
  return (
    <Table
      caption={t('quality.overview.byCategory')}
      columns={columns}
      rows={aggregates}
      rowKey={(c) => c.category}
      rowProps={(c) => ({ 'data-category': c.category, 'data-executions': String(c.total) })}
      empty={t('quality.overview.noExecutions')}
      minWidthClass="min-w-[52rem]"
      density="compact"
    />
  );
}

function GateTypeTable({ gates }: { gates: GateStats[] }) {
  const { t } = useTranslation();
  const columns: Column<GateStats>[] = [
    { id: 'type', header: t('quality.gate.type'), rowHeader: true, cell: (g) => <span className="font-mono text-xs">{g.gate_type}</span>, sortValue: (g) => g.gate_type },
    { id: 'total', header: t('quality.overview.executions'), cell: (g) => <span className="tabular-nums">{g.total.toLocaleString('ru-RU')}</span>, sortValue: (g) => g.total },
    ...stateColumns<GateStats>(countsOf, t),
    {
      id: 'rate',
      header: t('quality.overview.verifiedRate'),
      cell: (g) => <span className="tabular-nums">{percent(gateTypeVerifiedRate(g)) ?? t('ds.metric.noData')}</span>,
      sortValue: (g) => gateTypeVerifiedRate(g),
    },
  ];
  return (
    <Table
      caption={t('quality.overview.byType')}
      columns={columns}
      rows={gates}
      rowKey={(g) => g.gate_type}
      rowProps={(g) => ({ 'data-gate-type': g.gate_type })}
      empty={t('quality.overview.noExecutions')}
      minWidthClass="min-w-[56rem]"
      density="compact"
    />
  );
}

function ProjectFilter({ workspaceId, value, onChange }: { workspaceId: string; value: string; onChange: (v: string) => void }) {
  const { t } = useTranslation();
  const projects = useQuery({
    queryKey: ['projects', workspaceId, 'quality-filter'],
    queryFn: ({ signal }) => projectsApi.list(workspaceId, { limit: PROJECTS_LIMIT }, signal),
  });
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor="quality-project" className="text-sm font-medium">
        {t('quality.overview.project')}
      </label>
      <select id="quality-project" className="select" value={value} onChange={(e) => onChange(e.target.value)} disabled={projects.isPending}>
        <option value="">{t('quality.overview.allProjects')}</option>
        {(projects.data?.items ?? []).map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
        {value && projects.data && !projects.data.items.some((p) => p.id === value) ? <option value={value}>{value}</option> : null}
      </select>
      {projects.isError ? <span className="text-xs text-danger-text">{t('quality.overview.projectsError')}</span> : null}
    </div>
  );
}

export function QualityOverviewPage() {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  const [params, setParams] = useSearchParams();
  const projectId = params.get('project') ?? '';
  const daysParam = Number(params.get('days'));
  const days = (QUALITY_WINDOWS as readonly number[]).includes(daysParam) ? daysParam : DEFAULT_WINDOW;
  const range = useMemo(() => qualityWindow(days), [days]);
  const query = { ...range, ...(projectId ? { project_id: projectId } : {}) };
  const analytics = useQuery({
    queryKey: analyticsKeys.summary(workspaceId ?? '', query),
    queryFn: ({ signal }) => analyticsApi.summary(workspaceId ?? '', query, signal),
    enabled: Boolean(workspaceId),
  });
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };
  const aggregates = useMemo(() => (analytics.data ? categoryAggregates(analytics.data.gates) : []), [analytics.data]);

  if (!workspaceId) return null;
  const source = analytics.data ? (
    <>
      {t('quality.overview.source')} <Timestamp value={analytics.data.window.from} /> — <Timestamp value={analytics.data.window.to} />
    </>
  ) : null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('quality.title')} />
      <p className="mb-4 max-w-prose text-fg-muted">{t('quality.indexDescription')}</p>
      <div role="group" aria-label={t('quality.overview.filters')} className="mb-6 flex flex-wrap items-end gap-4">
        <ProjectFilter workspaceId={workspaceId} value={projectId} onChange={(v) => setParam('project', v)} />
        <div className="flex flex-col gap-1">
          <label htmlFor="quality-window" className="text-sm font-medium">
            {t('quality.overview.window')}
          </label>
          <select id="quality-window" className="select" value={days} onChange={(e) => setParam('days', e.target.value === String(DEFAULT_WINDOW) ? '' : e.target.value)}>
            {QUALITY_WINDOWS.map((d) => (
              <option key={d} value={d}>
                {t('quality.overview.days', { count: d })}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        <Tile
          id="quality-outcomes-tile"
          icon="quality"
          headingLevel={2}
          title={t('quality.overview.outcomes')}
          className="xl:col-span-5"
          loading={analytics.isPending}
          error={analytics.error}
          onRetry={() => void analytics.refetch()}
          source={source}
        >
          {analytics.data ? <Outcomes data={analytics.data} days={days} /> : null}
        </Tile>
        <Tile
          id="quality-categories-tile"
          icon="listChecks"
          headingLevel={2}
          title={t('quality.overview.byCategory')}
          className="xl:col-span-7"
          loading={analytics.isPending}
          error={analytics.error}
          onRetry={() => void analytics.refetch()}
          source={source}
        >
          {analytics.data ? <CategoryTable aggregates={aggregates} /> : null}
        </Tile>
        <Tile
          id="quality-types-tile"
          headingLevel={2}
          title={t('quality.overview.byType')}
          className="xl:col-span-12"
          loading={analytics.isPending}
          error={analytics.error}
          onRetry={() => void analytics.refetch()}
          source={source}
        >
          {analytics.data ? <GateTypeTable gates={analytics.data.gates} /> : null}
        </Tile>
      </div>
      <h2 className="mb-3 mt-8 text-lg font-semibold">{t('quality.runs.title')}</h2>
      <p className="mb-3 text-sm text-fg-muted">{t('quality.overview.runsHint')}</p>
      <RecentRuns
        workspaceId={workspaceId}
        label={t('quality.runs.listLabel')}
        actions={(r) => (
          <>
            <TextLink to={workspacePath(workspaceId, `quality/runs/${r.id}`)} srSuffix={r.objective.slice(0, 80)}>
              {t('quality.runs.openQuality')}
            </TextLink>
            <TextLink to={workspacePath(workspaceId, `compliance/runs/${r.id}`)} srSuffix={r.objective.slice(0, 80)}>
              {t('quality.openCompliance')}
            </TextLink>
            <TextLink to={workspacePath(workspaceId, `security?run=${r.id}`)} srSuffix={r.objective.slice(0, 80)}>
              {t('quality.runs.openSecurity')}
            </TextLink>
          </>
        )}
      />
    </section>
  );
}

