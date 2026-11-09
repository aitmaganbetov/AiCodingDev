import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { AgentProfile } from '../../api/agents';
import { PageHeading } from '../../components/PageHeading';
import { AgentAvatar, Button, Callout, Icon, Table, cx, type Column } from '../../components/ui';
import { AGENT_ROLE_META, AGENT_ROLE_ORDER, type AgentRole } from '../../lib/agentRoles';
import { formatDate } from '../../lib/format';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { DASHBOARD_WINDOW_DAYS } from '../dashboard/model';
import { formatPercent } from '../dashboard/format';
import { useInPageSearchParams } from '../runs/useInPageSearchParams';
import { useActiveTasks, useAgentProfiles, useAgentsWindow, useRoleAnalytics } from './data';
import {
  ACTIVE_TASKS_LIMIT,
  CARD_TASKS_SHOWN,
  activeRoleCount,
  activityByRole,
  profilesByRole,
  readStoredView,
  resolveView,
  roleState,
  roleStats,
  storeView,
  type AgentsView,
  type RoleActivity,
  type RoleState,
  type RoleStats,
} from './model';
import { CurrentTasks, ProfileFacts, ProviderChipsList, RoleResults, RoleStateBadge } from './parts';

interface RoleRow {
  role: AgentRole;
  profile: AgentProfile | undefined;
  activity: RoleActivity | undefined;
  state: RoleState;
  stats: RoleStats | null;
}

interface Status {
  profilesPending: boolean;
  profilesError: boolean;
  analyticsPending: boolean;
  analyticsError: boolean;
}

function RoleCard({ row, workspaceId, status }: { row: RoleRow; workspaceId: string; status: Status }) {
  const { t } = useTranslation();
  const meta = AGENT_ROLE_META[row.role];
  const name = t(`enums.agent_role.${row.role}`);
  const headingId = `agent-${row.role}-title`;
  return (
    <li className="min-w-0">
      <article
        aria-labelledby={headingId}
        data-role={row.role}
        data-state={row.state}
        className={cx(
          'flex h-full min-w-0 flex-col gap-3 rounded-xl border bg-surface p-4 shadow-e1',
          'transition-transform duration-instant ease-standard hover:border-border-strong focus-within:border-border-active motion-safe:hover:-translate-y-px',
          row.state === 'active' ? 'border-border-active' : 'border-border',
        )}
      >
        <header className="flex min-w-0 items-start gap-3">
          <AgentAvatar agentRole={row.role} />
          <div className="min-w-0 flex-1">
            <h2 id={headingId} className="break-words text-base font-semibold text-fg">
              {name}
            </h2>
            <p className="flex flex-wrap items-center gap-x-2 text-xs text-fg-muted">
              <span className="font-mono font-semibold">{meta.code}</span>
              <span>{t(`agents.family.${meta.family}`)}</span>
            </p>
          </div>
        </header>
        <div className="flex flex-col gap-1">
          <RoleStateBadge state={row.state} activity={row.activity} />
          <p className="text-xs text-fg-subtle">{t('agents.state.hint')}</p>
        </div>
        <section aria-label={t('agents.card.specialization')} className="flex flex-col gap-2">
          <p className="text-sm text-fg">
            {t(`agents.mission.${row.role}`)} <span className="text-xs text-fg-subtle">({t('agents.card.missionSource')})</span>
          </p>
          {row.profile ? (
            <ProfileFacts profile={row.profile} />
          ) : status.profilesPending ? (
            <span aria-hidden="true" className="skeleton block h-10 w-full" />
          ) : (
            <p className="text-xs text-fg-muted">{t('agents.card.profileMissing')}</p>
          )}
        </section>
        <div className="flex flex-col gap-1">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">{t('agents.card.currentTasks')}</h3>
          {row.state === 'loading' ? (
            <span aria-hidden="true" className="skeleton block h-5 w-full" />
          ) : row.state === 'unknown' && !row.activity ? (
            <p className="text-sm text-fg-muted">{t('agents.notMeasured')}</p>
          ) : (
            <CurrentTasks workspaceId={workspaceId} activity={row.activity} limit={CARD_TASKS_SHOWN} />
          )}
        </div>
        <div className="flex flex-col gap-1">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">{t('agents.card.results', { days: DASHBOARD_WINDOW_DAYS })}</h3>
          <RoleResults stats={row.stats} pending={status.analyticsPending} error={status.analyticsError} />
        </div>
        <div className="flex flex-col gap-1">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-muted">{t('agents.card.providers')}</h3>
          {row.profile ? (
            <ProviderChipsList providers={row.profile.eligible_providers} emptyText={t('agents.card.noProviders')} />
          ) : (
            <p className="text-sm text-fg-muted">{t('agents.notMeasured')}</p>
          )}
        </div>
        <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
          <span className="text-xs text-fg-muted">
            {t('agents.card.template')}: <span className="font-mono">{row.profile ? `v${row.profile.template_version}` : '—'}</span>
          </span>
          <Link to={`/w/${workspaceId}/agents/${row.role}`} className="link inline-flex min-h-target items-center gap-1 text-sm font-medium">
            {t('agents.card.details')}
            <span className="sr-only">: {name}</span>
            <Icon name="chevronRight" className="h-4 w-4" />
          </Link>
        </div>
      </article>
    </li>
  );
}

function RoleTable({ rows, workspaceId, status }: { rows: RoleRow[]; workspaceId: string; status: Status }) {
  const { t } = useTranslation();
  const pct = (s: RoleStats | null) => formatPercent(s?.verified_completion_rate);
  const statsCell = (s: RoleStats | null, pick: (s: RoleStats) => number) =>
    status.analyticsPending ? '…' : status.analyticsError || !s ? t('agents.notMeasured') : pick(s);
  const columns: Column<RoleRow>[] = [
    {
      id: 'role',
      header: t('agents.col.role'),
      rowHeader: true,
      sortValue: (r) => AGENT_ROLE_ORDER.indexOf(r.role),
      cell: (r) => (
        <Link to={`/w/${workspaceId}/agents/${r.role}`} className="link inline-flex min-h-target items-center gap-2">
          <AgentAvatar agentRole={r.role} size="sm" />
          <span className="font-mono text-code-sm font-semibold text-fg-muted">{AGENT_ROLE_META[r.role].code}</span>
          <span>{t(`enums.agent_role.${r.role}`)}</span>
        </Link>
      ),
    },
    { id: 'state', header: t('agents.col.state'), cell: (r) => <RoleStateBadge state={r.state} activity={r.activity} /> },
    {
      id: 'attempts',
      header: t('agents.col.attempts'),
      className: 'text-right tabular-nums',
      sortValue: (r) => r.stats?.attempts ?? -1,
      cell: (r) => statsCell(r.stats, (s) => s.attempts),
    },
    {
      id: 'succeeded',
      header: t('agents.col.succeeded'),
      className: 'text-right tabular-nums',
      sortValue: (r) => r.stats?.succeeded ?? -1,
      cell: (r) => statsCell(r.stats, (s) => s.succeeded),
    },
    {
      id: 'verified',
      header: t('agents.col.verified'),
      className: 'text-right tabular-nums',
      sortValue: (r) => r.stats?.verified_completion_rate ?? -1,
      cell: (r) => (status.analyticsPending ? '…' : (pct(r.stats) ?? t('agents.notMeasured'))),
    },
    {
      id: 'providers',
      header: t('agents.col.providers'),
      cell: (r) =>
        r.profile ? (
          <ProviderChipsList providers={r.profile.eligible_providers} emptyText={t('agents.card.noProviders')} />
        ) : status.profilesPending ? (
          '…'
        ) : (
          t('agents.notMeasured')
        ),
    },
    { id: 'template', header: t('agents.col.template'), cell: (r) => <span className="font-mono">{r.profile ? `v${r.profile.template_version}` : '—'}</span> },
  ];
  return (
    <Table
      caption={t('agents.col.caption', { days: DASHBOARD_WINDOW_DAYS })}
      columns={columns}
      rows={rows}
      rowKey={(r) => r.role}
      rowProps={(r) => ({ 'data-role': r.role, 'data-state': r.state })}
      minWidthClass="min-w-[56rem]"
      density="compact"
    />
  );
}

function ViewToggle({ view, onChange }: { view: AgentsView; onChange: (v: AgentsView) => void }) {
  const { t } = useTranslation();
  return (
    <div role="group" aria-label={t('agents.views.label')} className="inline-flex rounded-md border border-border bg-surface p-0.5">
      {(['grid', 'list'] as const).map((v) => (
        <Button
          key={v}
          variant={view === v ? 'secondary' : 'ghost'}
          size="sm"
          icon={v === 'grid' ? 'viewGrid' : 'viewList'}
          aria-pressed={view === v}
          onClick={() => onChange(v)}
        >
          {t(`agents.views.${v}`)}
        </Button>
      ))}
    </div>
  );
}

/**
 * F5 «Агенты» (план §3.4, §5.7; D-2): каталог 12 ролей AgentRole — профиль (B7), активность по
 * выполняемым задачам (B1), результаты analytics by_role, провайдеры. Вид — сетка/список (URL `view`,
 * выбор сохраняется на устройстве).
 */
export function AgentsPage() {
  const { t, i18n } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const ws = workspaceId ?? '';
  const [params, setParams] = useInPageSearchParams();
  const view = resolveView(params.get('view'), readStoredView());
  const window = useAgentsWindow();
  const profiles = useAgentProfiles(ws);
  const active = useActiveTasks(ws);
  const analytics = useRoleAnalytics(ws, window);
  if (!workspaceId || !membership) return <WorkspaceNotFound />;

  const byRole = profilesByRole(profiles.data?.items);
  const activity = activityByRole(active.data);
  const rows: RoleRow[] = AGENT_ROLE_ORDER.map((role) => ({
    role,
    profile: byRole.get(role),
    activity: activity?.get(role),
    state: roleState(activity?.get(role), { pending: active.isPending, error: active.isError }),
    stats: roleStats(analytics.data, role),
  }));
  const status: Status = {
    profilesPending: profiles.isPending,
    profilesError: profiles.isError,
    analyticsPending: analytics.isPending,
    analyticsError: analytics.isError,
  };
  const activeCount = activeRoleCount(activity);
  const setView = (v: AgentsView) => {
    storeView(v);
    const next = new URLSearchParams(params);
    next.set('view', v);
    setParams(next);
  };
  const errors: { key: string; text: string; retry: () => void }[] = [];
  if (profiles.isError) errors.push({ key: 'profiles', text: t('agents.summary.profilesError'), retry: () => void profiles.refetch() });
  if (active.isError) errors.push({ key: 'tasks', text: t('agents.summary.tasksError'), retry: () => void active.refetch() });
  if (analytics.isError) errors.push({ key: 'analytics', text: t('agents.summary.analyticsError'), retry: () => void analytics.refetch() });

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('agents.title')}>
        <ViewToggle view={view} onChange={setView} />
      </PageHeading>
      <Callout tone="info" className="mb-4">
        {t('agents.intro')}
      </Callout>
      <div className="mb-4 flex flex-col gap-1">
        <p className="text-sm font-medium text-fg" role="status" data-testid="agents-active-summary">
          {activeCount
            ? activeCount.more
              ? t('agents.summary.activeMore', { count: activeCount.count })
              : t('agents.summary.active', { count: activeCount.count })
            : null}
        </p>
        <p className="text-xs text-fg-subtle">
          {t('agents.summary.source', {
            limit: ACTIVE_TASKS_LIMIT,
            days: DASHBOARD_WINDOW_DAYS,
            from: formatDate(window.from, i18n.language),
            to: formatDate(window.to, i18n.language),
          })}
        </p>
      </div>
      {errors.length > 0 ? (
        <div role="alert" className="mb-4 flex flex-col gap-2" data-testid="agents-errors">
          {errors.map((e) => (
            <Callout key={e.key} tone="danger">
              <span className="mr-2">{e.text}</span>
              <Button size="sm" icon="refresh" onClick={e.retry}>
                {t('action.retry')}
              </Button>
            </Callout>
          ))}
        </div>
      ) : null}
      {view === 'grid' ? (
        <ul aria-label={t('agents.catalogLabel')} className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3 3xl:grid-cols-4" data-view="grid">
          {rows.map((row) => (
            <RoleCard key={row.role} row={row} workspaceId={workspaceId} status={status} />
          ))}
        </ul>
      ) : (
        <div data-view="list">
          <RoleTable rows={rows} workspaceId={workspaceId} status={status} />
        </div>
      )}
    </section>
  );
}
