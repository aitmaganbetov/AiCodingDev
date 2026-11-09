import type { UseQueryResult } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { AnalyticsSummary } from '../../api/analytics';
import {
  AgentBadge,
  DataUnavailable,
  Donut,
  Meter,
  Metric,
  Table,
  Tile,
  VerificationBadge,
  type Column,
  type DonutSegment,
} from '../../components/ui';
import type { VerificationState } from '../../lib/verification';
import { formatPercent } from './format';
import { DASHBOARD_WINDOW_DAYS, gateOutcomes, gateTotals } from './model';

type Analytics = UseQueryResult<AnalyticsSummary>;
type RoleRow = AnalyticsSummary['tasks']['by_role'][number];

/** Результаты агентов: только измеренные поля analytics (by_role, providers, review_loops) — без рейтингов. */
export function AgentPerformance({ analytics }: { analytics: Analytics }) {
  const { t } = useTranslation();
  const data = analytics.data;
  const noData = t('ds.metric.noData');
  const columns: Column<RoleRow>[] = [
    { id: 'role', header: t('dashboard.agents.col.role'), rowHeader: true, cell: (r) => <AgentBadge agentRole={r.role} />, sortValue: (r) => r.role },
    { id: 'attempts', header: t('dashboard.agents.col.attempts'), cell: (r) => r.attempts, sortValue: (r) => r.attempts, className: 'text-right tabular-nums' },
    { id: 'succeeded', header: t('dashboard.agents.col.succeeded'), cell: (r) => r.succeeded, sortValue: (r) => r.succeeded, className: 'text-right tabular-nums' },
    { id: 'failed', header: t('dashboard.agents.col.failed'), cell: (r) => r.failed, sortValue: (r) => r.failed, className: 'text-right tabular-nums' },
    { id: 'retries', header: t('dashboard.agents.col.retries'), cell: (r) => r.retries, sortValue: (r) => r.retries, className: 'text-right tabular-nums' },
    {
      id: 'verified',
      header: t('dashboard.agents.col.verified'),
      cell: (r) => formatPercent(r.verified_completion_rate) ?? noData,
      sortValue: (r) => r.verified_completion_rate,
      className: 'text-right tabular-nums',
    },
  ];
  return (
    <Tile
      id="dashboard-agents"
      icon="bot"
      headingLevel={2}
      title={t('dashboard.agents.title')}
      className="xl:col-span-6"
      loading={analytics.isPending}
      error={analytics.error}
      onRetry={() => void analytics.refetch()}
      source={t('dashboard.agents.noRatings')}
    >
      <p className="-mt-2 text-sm text-fg-muted">{t('dashboard.agents.subtitle', { days: DASHBOARD_WINDOW_DAYS })}</p>
      {data ? (
        <>
          <h3 className="text-sm font-semibold text-fg">{t('dashboard.agents.byRole')}</h3>
          {data.tasks.by_role.length === 0 ? (
            <p className="text-sm text-fg-muted">{t('dashboard.agents.noRoles')}</p>
          ) : (
            <Table
              caption={t('dashboard.agents.byRole')}
              columns={columns}
              rows={data.tasks.by_role}
              rowKey={(r) => r.role}
              density="compact"
              minWidthClass="min-w-[560px]"
              defaultSort={{ columnId: 'attempts', direction: 'desc' }}
            />
          )}
          <h3 className="text-sm font-semibold text-fg">{t('dashboard.agents.byProvider')}</h3>
          {data.providers.length === 0 ? (
            <p className="text-sm text-fg-muted">{t('dashboard.agents.noProviders')}</p>
          ) : (
            <ul className="flex flex-col gap-3" aria-label={t('dashboard.agents.byProvider')}>
              {data.providers.map((p) => (
                <li key={p.provider} className="flex flex-col gap-1" data-provider-usage={p.provider}>
                  <Meter
                    label={`${t(`shell.cli.name.${p.provider}`, { defaultValue: p.provider })} · ${t('dashboard.agents.providerSuccess')}`}
                    ratio={p.success_rate}
                    valueText={formatPercent(p.success_rate) ?? noData}
                  />
                  <span className="text-xs text-fg-muted">
                    {t('dashboard.agents.providerLine', { executions: p.executions, errors: p.errors, rateLimited: p.rate_limited, authRequired: p.auth_required })}
                  </span>
                  <span className="text-xs text-fg-muted">
                    {p.tokens.total === null
                      ? t('dashboard.agents.tokensUnknown')
                      : t('dashboard.agents.tokens', { total: p.tokens.total.toLocaleString('ru-RU'), reported: p.tokens.executions_reported })}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <h3 className="text-sm font-semibold text-fg">{t('dashboard.agents.reviewLoops')}</h3>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3" data-testid="review-loops">
            <Metric size="sm" label={t('dashboard.agents.reviewRequests')} value={data.review_loops.review_requests} />
            <Metric size="sm" label={t('dashboard.agents.fixRequests')} value={data.review_loops.fix_requests} />
            <Metric
              size="sm"
              label={t('dashboard.agents.chainsWithFixes')}
              value={`${data.review_loops.chains_with_fixes.toLocaleString('ru-RU')} / ${data.review_loops.review_chains.toLocaleString('ru-RU')}`}
            />
            <Metric
              size="sm"
              label={t('dashboard.agents.avgFixRounds')}
              value={data.review_loops.avg_fix_rounds === null ? null : data.review_loops.avg_fix_rounds.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}
            />
            <Metric size="sm" label={t('dashboard.agents.loopsExhausted')} value={data.review_loops.loops_exhausted} />
          </div>
          <DataUnavailable compact title={t('dashboard.agents.reviewByRoleTitle')} reason={t('dashboard.agents.reviewByRoleReason')} />
        </>
      ) : null}
    </Tile>
  );
}

const OUTCOME_STYLE: Record<VerificationState, { stroke: string; swatch: string }> = {
  PASS: { stroke: 'stroke-verify-pass', swatch: 'bg-verify-pass' },
  INSUFFICIENT_EVIDENCE: { stroke: 'stroke-verify-insufficient', swatch: 'bg-verify-insufficient' },
  FAIL: { stroke: 'stroke-verify-fail', swatch: 'bg-verify-fail' },
  BLOCKED: { stroke: 'stroke-verify-blocked', swatch: 'bg-verify-blocked' },
  NOT_RUN: { stroke: 'stroke-verify-not-run', swatch: 'bg-verify-not-run' },
};

/** Обзор качества: исходы гейтов по пяти состояниям (D-8) и доля PASS по типам гейтов. */
export function QualityOverview({ analytics }: { analytics: Analytics }) {
  const { t } = useTranslation();
  const data = analytics.data;
  const outcomes = data ? gateOutcomes(data.gates) : [];
  const totals = data ? gateTotals(data.gates) : null;
  const segments: DonutSegment[] = outcomes
    .filter((o): o is typeof o & { count: number } => o.count !== null)
    .map((o) => ({
      key: o.state,
      label: t(`ds.verification.${o.state}`),
      value: o.count,
      strokeClass: OUTCOME_STYLE[o.state].stroke,
      swatchClass: OUTCOME_STYLE[o.state].swatch,
      marker: <VerificationBadge result={{ state: o.state, source: o.source }} />,
    }));
  const summary = segments.map((s) => `${t(`ds.verification.${s.key}`)} — ${s.value}`).join(', ');
  return (
    <Tile
      id="dashboard-quality"
      icon="quality"
      headingLevel={2}
      title={t('dashboard.quality.title')}
      className="xl:col-span-6"
      loading={analytics.isPending}
      error={analytics.error}
      onRetry={() => void analytics.refetch()}
    >
      <p className="-mt-2 text-sm text-fg-muted">{t('dashboard.quality.subtitle', { days: DASHBOARD_WINDOW_DAYS })}</p>
      {data && totals ? (
        totals.total === 0 ? (
          <p className="rounded-lg border border-dashed border-border p-4 text-sm text-fg-muted" data-testid="quality-empty">
            {t('dashboard.quality.noGates')}
          </p>
        ) : (
          <>
            <h3 className="text-sm font-semibold text-fg">{t('dashboard.quality.outcomes')}</h3>
            <Donut
              testId="gate-outcomes"
              segments={segments}
              summary={t('dashboard.quality.donut', { summary })}
              legendLabel={t('dashboard.quality.outcomes')}
              center={
                <span className="flex flex-col leading-tight">
                  {totals.total.toLocaleString('ru-RU')}
                  <span className="text-xs font-normal text-fg-muted">{t('dashboard.quality.total')}</span>
                </span>
              }
            />
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm" data-outcome="PASS">
              <VerificationBadge result={{ state: 'PASS', source: 'PASS + VERIFIED evidence' }} />
              <span className="text-fg-muted">{t('dashboard.quality.notDerivable')}</span>
            </div>
            <p className="text-xs text-fg-muted">{t('dashboard.quality.passNote')}</p>
            <h3 className="text-sm font-semibold text-fg">{t('dashboard.quality.byGate')}</h3>
            <ul className="flex flex-col gap-3" aria-label={t('dashboard.quality.byGate')}>
              {data.gates.map((g) => (
                <li key={g.gate_type} data-gate-type={g.gate_type}>
                  <Meter
                    label={<span className="font-mono text-code-sm">{g.gate_type}</span>}
                    ratio={g.pass_rate}
                    valueText={t('dashboard.quality.gateLine', {
                      rate: formatPercent(g.pass_rate) ?? t('ds.metric.noData'),
                      pass: g.passed,
                      executed: g.passed + g.failed + g.errored,
                      skipped: g.skipped,
                    })}
                  />
                </li>
              ))}
            </ul>
          </>
        )
      ) : null}
    </Tile>
  );
}
