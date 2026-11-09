import type { UseQueryResult } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { AnalyticsSummary } from '../../api/analytics';
import type { RunPage } from '../../api/runs';
import { Badge, Meter, Metric, RelativeTime, SeverityBadge, Sparkline, Tile } from '../../components/ui';
import { CHIP_STATE_ICON, CHIP_STATE_TONE } from '../../layout/providerStatus';
import { useProvidersStatus } from '../../layout/shellData';
import { formatDate } from '../../lib/format';
import {
  ACTIVE_RUNS_LIMIT,
  APPROVALS_LIMIT,
  DASHBOARD_WINDOW_DAYS,
  FINDINGS_LIMIT,
  approvalsSummary,
  findingsBySeverity,
  gateTotals,
  pageCount,
  projectsWithActiveRuns,
  providerHealthRows,
  readyProviders,
  runningAgents,
} from './model';
import { useActiveProjects, useOpenFindings, usePendingApprovalsList, useRunningTasks } from './data';
import { formatPercent } from './format';

type Analytics = UseQueryResult<AnalyticsSummary>;
type Window = { from: string; to: string };

function useWindowLabel(window: Window) {
  const { i18n } = useTranslation();
  // `to` — исключающая граница: подпись — последний включённый день.
  return {
    from: formatDate(window.from, i18n.language),
    to: formatDate(new Date(Date.parse(window.to) - 1).toISOString(), i18n.language),
  };
}

function TileLink({ to, children }: { to: string; children: string }) {
  return (
    <Link to={to} className="link inline-flex min-h-target items-center text-sm">
      {children}
    </Link>
  );
}

export function ProjectsTile({ workspaceId, activeRuns }: { workspaceId: string; activeRuns: UseQueryResult<RunPage> }) {
  const { t } = useTranslation();
  const q = useActiveProjects(workspaceId);
  const count = q.data ? pageCount(q.data) : null;
  const withRuns = activeRuns.data ? projectsWithActiveRuns(activeRuns.data.items) : null;
  return (
    <Tile
      id="kpi-projects"
      icon="folder"
      title={t('dashboard.kpi.projects.title')}
      loading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      source={t('dashboard.kpi.projects.source', { limit: ACTIVE_RUNS_LIMIT })}
    >
      <Metric
        testId="metric-projects"
        label={t('dashboard.kpi.projects.metric')}
        value={count?.value ?? null}
        more={count?.more}
        hint={
          withRuns !== null
            ? t('dashboard.kpi.projects.withRuns', { count: withRuns, more: activeRuns.data?.next_cursor ? '+' : '' })
            : undefined
        }
      />
      <TileLink to={`/w/${workspaceId}/projects`}>{t('dashboard.hero.projects')}</TileLink>
    </Tile>
  );
}

export function AgentsTile({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const q = useRunningTasks(workspaceId);
  const count = q.data ? pageCount(q.data) : null;
  const split = q.data ? runningAgents(q.data.items) : null;
  return (
    <Tile
      id="kpi-agents"
      icon="bot"
      title={t('dashboard.kpi.agents.title')}
      loading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      source={t('dashboard.kpi.agents.source')}
    >
      <Metric testId="metric-agents" label={t('dashboard.kpi.agents.metric')} value={count?.value ?? null} more={count?.more} />
      {split && split.byProvider.length > 0 ? (
        <ul aria-label={t('dashboard.kpi.agents.byProvider')} className="flex flex-wrap gap-2 text-xs">
          {split.byProvider.map((p) => (
            <li key={p.key} className="rounded-full border border-border bg-surface-muted px-2 py-0.5 text-fg-muted" data-provider={p.key}>
              {p.key === 'unknown' ? t('dashboard.kpi.agents.providerUnknown') : t(`shell.cli.name.${p.key}`)}: <span className="tabular-nums text-fg">{p.count}</span>
            </li>
          ))}
        </ul>
      ) : count?.value === 0 ? (
        <p className="text-sm text-fg-muted">{t('dashboard.kpi.agents.none')}</p>
      ) : null}
      <TileLink to={`/w/${workspaceId}/mission-control`}>{t('dashboard.hero.missionControl')}</TileLink>
    </Tile>
  );
}

export function CompletedTile({ analytics, window }: { analytics: Analytics; window: Window }) {
  const { t } = useTranslation();
  const w = useWindowLabel(window);
  const tasks = analytics.data?.tasks;
  return (
    <Tile
      id="kpi-completed"
      icon="listChecks"
      title={t('dashboard.kpi.completed.title')}
      loading={analytics.isPending}
      error={analytics.error}
      onRetry={() => void analytics.refetch()}
      source={t('dashboard.kpi.completed.source', w)}
    >
      <Metric
        testId="metric-completed"
        label={t('dashboard.kpi.completed.metric')}
        value={tasks?.succeeded ?? null}
        hint={tasks ? t('dashboard.kpi.completed.finished', { count: tasks.tasks_finished }) : undefined}
      />
      {tasks ? <p className="text-xs text-fg-muted">{t('dashboard.kpi.completed.failed', { count: tasks.failed })}</p> : null}
    </Tile>
  );
}

export function QualityTile({ analytics, window }: { analytics: Analytics; window: Window }) {
  const { t } = useTranslation();
  const w = useWindowLabel(window);
  const data = analytics.data;
  const gates = data ? gateTotals(data.gates) : null;
  return (
    <Tile
      id="kpi-quality"
      icon="quality"
      title={t('dashboard.kpi.quality.title')}
      loading={analytics.isPending}
      error={analytics.error}
      onRetry={() => void analytics.refetch()}
      source={t('dashboard.kpi.quality.source', w)}
    >
      <Metric
        testId="metric-verified-rate"
        label={t('dashboard.kpi.quality.verified')}
        value={data && data.tasks.verified_completion_rate !== null ? formatPercent(data.tasks.verified_completion_rate) : null}
        hint={t('dashboard.kpi.quality.verifiedHint')}
      />
      {gates ? (
        <Metric
          size="sm"
          testId="metric-gate-pass-status"
          label={t('dashboard.kpi.quality.gates')}
          value={gates.passStatusRate !== null ? formatPercent(gates.passStatusRate) : null}
          hint={t('dashboard.kpi.quality.gatesHint', { pass: gates.passStatus, executed: gates.passStatus + gates.failed + gates.errored })}
        />
      ) : null}
      <p className="text-xs text-fg-muted">{t('dashboard.kpi.quality.noScore')}</p>
    </Tile>
  );
}

export function SecurityTile({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const q = useOpenFindings(workspaceId);
  const count = q.data ? pageCount(q.data) : null;
  const rows = q.data ? findingsBySeverity(q.data) : [];
  return (
    <Tile
      id="kpi-security"
      icon="security"
      title={t('dashboard.kpi.security.title')}
      loading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      source={t('dashboard.kpi.security.source', { limit: FINDINGS_LIMIT })}
    >
      <Metric testId="metric-findings" label={t('dashboard.kpi.security.metric')} value={count?.value ?? null} more={count?.more} />
      {q.data && q.data.items.length > 0 ? (
        <ul aria-label={t('dashboard.kpi.security.bySeverity')} className="flex flex-col gap-1">
          {rows
            .filter((r) => r.count !== 0)
            .map((r) => (
              <li key={r.severity} className="flex items-center justify-between gap-2 text-sm" data-severity-count={r.severity}>
                <SeverityBadge severity={r.severity} />
                <span className="tabular-nums text-fg">
                  {r.count === null ? t('dashboard.kpi.security.notCounted') : r.more ? t('ds.metric.more', { value: r.count }) : r.count}
                </span>
              </li>
            ))}
        </ul>
      ) : null}
    </Tile>
  );
}

export function ApprovalsTile({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const q = usePendingApprovalsList(workspaceId);
  const s = q.data ? approvalsSummary(q.data) : null;
  return (
    <Tile
      id="kpi-approvals"
      icon="approval"
      title={t('dashboard.kpi.approvals.title')}
      loading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      source={t('dashboard.kpi.approvals.source', { limit: APPROVALS_LIMIT })}
    >
      <Metric
        testId="metric-approvals"
        label={t('dashboard.kpi.approvals.metric')}
        value={s?.total.value ?? null}
        more={s?.total.more}
        hint={s ? t('dashboard.kpi.approvals.mine', { count: s.mine }) : undefined}
      />
      {s && s.highRisk > 0 ? (
        <Badge tone="warning" icon="alert">
          {t('dashboard.kpi.approvals.highRisk', { count: s.highRisk })}
        </Badge>
      ) : null}
      <TileLink to={`/w/${workspaceId}/approvals`}>{t('dashboard.kpi.approvals.open')}</TileLink>
    </Tile>
  );
}

export function ProvidersTile({ workspaceId, role, analytics }: { workspaceId: string; role: string; analytics: Analytics }) {
  const { t } = useTranslation();
  const q = useProvidersStatus(workspaceId, role);
  const rows = q.data ? providerHealthRows(q.data.items, analytics.data?.providers) : [];
  return (
    <Tile
      id="kpi-providers"
      icon="terminal"
      title={t('dashboard.kpi.providers.title')}
      loading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      source={t('dashboard.kpi.providers.source', { days: DASHBOARD_WINDOW_DAYS })}
    >
      {rows.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('dashboard.kpi.providers.none')}</p>
      ) : (
        <>
          <Metric
            testId="metric-providers"
            label={t('dashboard.kpi.providers.metric')}
            value={t('dashboard.kpi.providers.ofTotal', { ready: readyProviders(rows), total: rows.length })}
          />
          <ul className="flex flex-col gap-2" aria-label={t('dashboard.kpi.providers.title')}>
            {rows.map((r) => (
              <li key={r.code} className="flex flex-col gap-1 text-sm" data-provider-row={r.code} data-state={r.state}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-fg">{t(`shell.cli.name.${r.code}`, { defaultValue: r.name })}</span>
                  <Badge tone={CHIP_STATE_TONE[r.state]} icon={CHIP_STATE_ICON[r.state]}>
                    {t(`shell.cli.state.${r.state}`)}
                  </Badge>
                </div>
                <span className="text-xs text-fg-muted">
                  {t('dashboard.kpi.providers.hosts', { count: r.hosts })} ·{' '}
                  {r.checkedAt ? (
                    <>
                      {t('dashboard.kpi.providers.checked')}{' '}
                      <RelativeTime value={r.checkedAt} />
                    </>
                  ) : (
                    t('dashboard.kpi.providers.neverChecked')
                  )}
                </span>
                <span className="text-xs text-fg-muted">
                  {analytics.isError
                    ? t('dashboard.kpi.providers.usageError')
                    : r.executions
                      ? t('dashboard.kpi.providers.success', { rate: formatPercent(r.successRate) ?? t('ds.metric.noData'), count: r.executions })
                      : analytics.data
                        ? t('dashboard.kpi.providers.noExecutions')
                        : null}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Tile>
  );
}

export function WorkflowSuccessTile({ analytics, window }: { analytics: Analytics; window: Window }) {
  const { t, i18n } = useTranslation();
  const w = useWindowLabel(window);
  const runs = analytics.data?.runs;
  const daily = analytics.data?.daily ?? [];
  const points = daily.map((d) => ({ label: formatDate(d.date, i18n.language), value: d.runs_completed }));
  return (
    <Tile
      id="kpi-workflows"
      icon="workflow"
      title={t('dashboard.kpi.workflows.title')}
      loading={analytics.isPending}
      error={analytics.error}
      onRetry={() => void analytics.refetch()}
      source={t('dashboard.kpi.workflows.source', w)}
    >
      <Metric
        testId="metric-run-success"
        label={t('dashboard.kpi.workflows.metric')}
        value={runs && runs.success_rate !== null ? formatPercent(runs.success_rate) : null}
        empty={t('dashboard.kpi.workflows.noFinished')}
      />
      {runs && runs.finished > 0 ? (
        <Meter
          label={t('dashboard.kpi.workflows.detail', { completed: runs.completed, finished: runs.finished })}
          ratio={runs.success_rate}
          valueText={formatPercent(runs.success_rate) ?? ''}
          tone="success"
        />
      ) : null}
      {points.length > 0 ? (
        <Sparkline
          testId="sparkline-runs"
          points={points}
          summary={t('dashboard.kpi.workflows.trend', {
            days: points.length,
            total: points.reduce((a, p) => a + (p.value ?? 0), 0),
            last: points.at(-1)?.value ?? 0,
          })}
          valueHeader={t('dashboard.kpi.workflows.trendValue')}
          labelHeader={t('dashboard.kpi.workflows.trendLabel')}
        />
      ) : null}
    </Tile>
  );
}
