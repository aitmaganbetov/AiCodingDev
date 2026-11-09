import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import type { AgentPermissions, AgentProfile } from '../../api/agents';
import type { WorkspaceTask } from '../../api/tasks';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState } from '../../components/states';
import {
  AgentAvatar,
  Badge,
  Callout,
  Icon,
  LoadMore,
  Metric,
  RelativeTime,
  StatusBadge,
  Table,
  Tile,
  Timestamp,
  type Column,
} from '../../components/ui';
import { AGENT_ROLE_META, isAgentRole, type AgentRole } from '../../lib/agentRoles';
import { formatDate } from '../../lib/format';
import { can } from '../../lib/permissions';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { DASHBOARD_WINDOW_DAYS } from '../dashboard/model';
import { formatPercent } from '../dashboard/format';
import { useActiveTasks, useAgentProfiles, useAgentsWindow, useProviders, useRoleAnalytics, useRoleHistory } from './data';
import { activityByRole, eligibilityRows, profilesByRole, roleState, roleStats, taskLinks, type EligibilityRow } from './model';
import { ProviderChipsList, RoleStateBadge, useProviderLabel } from './parts';

function Fact({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <>
      <dt className="text-fg-muted">{label}</dt>
      <dd className="min-w-0 break-words text-fg">{children}</dd>
    </>
  );
}

function PathList({ paths }: { paths: readonly string[] }) {
  const { t } = useTranslation();
  if (paths.length === 0) return <>{t('agents.detail.none')}</>;
  return (
    <ul className="flex flex-wrap gap-1">
      {paths.map((p) => (
        <li key={p}>
          <code className="rounded bg-surface-muted px-1 py-0.5 text-code-sm">{p}</code>
        </li>
      ))}
    </ul>
  );
}

function PermissionsFacts({ permissions }: { permissions: AgentPermissions }) {
  const { t } = useTranslation();
  const yesNo = (v: boolean) => (v ? t('agents.detail.yes') : t('agents.detail.no'));
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[minmax(10rem,auto),1fr]">
      <Fact label={t('agents.detail.writeScope')}>{t(`agents.enums.write_scope.${permissions.write_scope}`)}</Fact>
      <Fact label={t('agents.detail.network')}>{t(`agents.enums.network.${permissions.network}`)}</Fact>
      <Fact label={t('agents.detail.pathPolicy')}>
        <PathList paths={permissions.path_policy} />
      </Fact>
      <Fact label={t('agents.detail.pathDeny')}>
        <PathList paths={permissions.path_deny} />
      </Fact>
      <Fact label={t('agents.detail.taskPaths')}>{yesNo(permissions.task_paths_allowed)}</Fact>
      <Fact label={t('agents.detail.commentsOnly')}>
        <PathList paths={permissions.comments_only_paths} />
      </Fact>
    </dl>
  );
}

function ProfileSection({ profile }: { profile: AgentProfile }) {
  const { t } = useTranslation();
  const yesNo = (v: boolean) => (v ? t('agents.detail.yes') : t('agents.detail.no'));
  const list = (items: readonly string[], render: (v: string) => string = (v) => v) =>
    items.length === 0 ? (
      t('agents.detail.none')
    ) : (
      <ul className="flex flex-wrap gap-1">
        {items.map((v) => (
          <li key={v}>
            <Badge tone="neutral">{render(v)}</Badge>
          </li>
        ))}
      </ul>
    );
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="mb-2 text-sm font-semibold text-fg">{t('agents.detail.permissions')}</h3>
        <PermissionsFacts permissions={profile.permissions} />
      </div>
      {profile.prototype_permissions ? (
        <div>
          <h3 className="mb-2 text-sm font-semibold text-fg">{t('agents.detail.prototype')}</h3>
          <PermissionsFacts permissions={profile.prototype_permissions} />
        </div>
      ) : null}
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[minmax(10rem,auto),1fr]">
        <Fact label={t('agents.detail.tools')}>{list(profile.allowed_tools)}</Fact>
        <Fact label={t('agents.detail.capabilities')}>{list(profile.required_capabilities)}</Fact>
        <Fact label={t('agents.detail.acpSend')}>{list(profile.acp_send, (v) => t(`agents.enums.acp.${v}`, { defaultValue: v }))}</Fact>
        <Fact label={t('agents.detail.complexity')}>
          <span className="font-mono">{t('agents.detail.complexityValue', { min: profile.complexity_band.min, max: profile.complexity_band.max })}</span>
        </Fact>
        <Fact label={t('agents.detail.independentCheck')}>
          {profile.independent_check_from ? t('agents.detail.independentFrom', { level: profile.independent_check_from }) : t('agents.detail.independentNone')}
        </Fact>
        <Fact label={t('agents.detail.mustDiffer')}>{yesNo(profile.must_differ_from_author)}</Fact>
        <Fact label={t('agents.detail.fixRequest')}>{yesNo(profile.can_send_fix_request)}</Fact>
        <Fact label={t('agents.detail.templateVersion')}>
          <span className="font-mono">
            v{profile.template_version} · {profile.template_ref}
          </span>
        </Fact>
        <Fact label={t('agents.detail.templateSha')}>
          <code className="break-all font-mono text-code-sm" data-testid="template-sha">
            {profile.template_sha256}
          </code>
        </Fact>
        <Fact label={t('agents.detail.catalog')}>
          {profile.catalog ? (
            <>
              {t('agents.detail.catalogValue', {
                version: profile.catalog.template_version,
                state: profile.catalog.enabled ? t('agents.detail.catalogEnabled') : t('agents.detail.catalogDisabled'),
              })}{' '}
              <Timestamp value={profile.catalog.updated_at} />
            </>
          ) : (
            t('agents.detail.catalogMissing')
          )}
        </Fact>
      </dl>
    </div>
  );
}

/** Провайдеры × роль: список (без горизонтальной прокрутки на узких экранах), итог — с сервера. */
function EligibilityList({ rows, name }: { rows: EligibilityRow[]; name: string }) {
  const { t } = useTranslation();
  if (rows.length === 0) return <p className="text-sm text-fg-muted">{t('agents.detail.providersEmpty')}</p>;
  const fact = (label: string, v: boolean) => (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd>
        <Badge tone={v ? 'success' : 'neutral'} icon={v ? 'check' : 'statusNeutral'}>
          {v ? t('agents.detail.yes') : t('agents.detail.no')}
        </Badge>
      </dd>
    </div>
  );
  return (
    <ul aria-label={t('agents.detail.providersCaption', { name })} className="flex flex-col divide-y divide-border rounded-lg border border-border">
      {rows.map((r) => (
        <li key={r.code} className="flex min-w-0 flex-col gap-2 p-3" data-provider={r.code}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium text-fg">{r.displayName}</span>
            {r.eligible === null ? (
              <span className="text-sm text-fg-muted">{t('agents.notMeasured')}</span>
            ) : (
              <Badge tone={r.eligible ? 'success' : 'warning'} icon={r.eligible ? 'statusSuccess' : 'alert'} data-eligible={String(r.eligible)}>
                {t('agents.detail.colEligible')}: {r.eligible ? t('agents.detail.eligible') : t('agents.detail.notEligible')}
              </Badge>
            )}
          </div>
          <dl className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {fact(t('agents.detail.colPlatform'), r.enabledGlobally)}
            {fact(t('agents.detail.colWorkspace'), r.enabledInWorkspace)}
            {fact(t('agents.detail.colAllowed'), r.allowed)}
          </dl>
        </li>
      ))}
    </ul>
  );
}

function HistoryTable({ workspaceId, tasks, name }: { workspaceId: string; tasks: WorkspaceTask[]; name: string }) {
  const { t } = useTranslation();
  const providerLabel = useProviderLabel();
  const columns: Column<WorkspaceTask>[] = [
    { id: 'task', header: t('agents.detail.colTask'), rowHeader: true, cell: (task) => <span className="break-words">{task.title}</span> },
    { id: 'status', header: t('agents.detail.colStatus'), cell: (task) => <StatusBadge kind="task" status={task.status} /> },
    {
      id: 'provider',
      header: t('agents.detail.colProviderAttempt'),
      cell: (task) => {
        const p = task.current_attempt?.provider ?? task.current_provider;
        return p ? providerLabel(p) : t('agents.detail.noProvider');
      },
    },
    { id: 'updated', header: t('agents.detail.colUpdated'), cell: (task) => <RelativeTime value={task.updated_at} /> },
    {
      id: 'links',
      header: t('agents.detail.colLinks'),
      cell: (task) => {
        const links = taskLinks(workspaceId, task);
        return (
          <span className="flex flex-wrap gap-x-3">
            <Link to={links.detail} className="link inline-block min-h-target leading-6">
              {t('agents.detail.openTask')}
              <span className="sr-only">: {task.title}</span>
            </Link>
            <Link to={links.mission} className="link inline-block min-h-target leading-6">
              {t('agents.detail.openMission')}
              <span className="sr-only">: {task.title}</span>
            </Link>
          </span>
        );
      },
    },
  ];
  return (
    <Table
      caption={t('agents.detail.historyCaption', { name })}
      columns={columns}
      rows={tasks}
      rowKey={(task) => task.id}
      rowProps={(task) => ({ 'data-task-id': task.id })}
      empty={t('agents.detail.historyEmpty')}
      minWidthClass="min-w-[44rem]"
      density="compact"
    />
  );
}

function RoleDetail({ workspaceId, role, memberRole }: { workspaceId: string; role: AgentRole; memberRole: string }) {
  const { t, i18n } = useTranslation();
  const name = t(`enums.agent_role.${role}`);
  const meta = AGENT_ROLE_META[role];
  const window = useAgentsWindow();
  const profiles = useAgentProfiles(workspaceId);
  const active = useActiveTasks(workspaceId, role);
  const analytics = useRoleAnalytics(workspaceId, window);
  const providers = useProviders(workspaceId);
  const history = useRoleHistory(workspaceId, role);
  const profile = profilesByRole(profiles.data?.items).get(role);
  const activity = activityByRole(active.data)?.get(role);
  const state = roleState(activity, { pending: active.isPending, error: active.isError });
  const stats = roleStats(analytics.data, role);
  const historyItems = history.data?.pages.flatMap((p) => p.items) ?? [];
  const canEditProviders = can(memberRole, 'provider:settings');
  const windowText = { days: DASHBOARD_WINDOW_DAYS, from: formatDate(window.from, i18n.language), to: formatDate(window.to, i18n.language) };

  return (
    <section aria-labelledby="page-title" data-role={role}>
      <Link to={`/w/${workspaceId}/agents`} className="link mb-2 inline-flex min-h-target items-center gap-1 text-sm">
        <Icon name="chevronLeft" className="h-4 w-4" />
        {t('agents.detail.back')}
      </Link>
      <div className="flex items-start gap-3">
        <AgentAvatar agentRole={role} className="mt-1" />
        <div className="min-w-0 flex-1">
          <PageHeading title={t('agents.detail.title', { name })}>
            <span className="font-mono text-sm font-semibold text-fg-muted">{meta.code}</span>
          </PageHeading>
        </div>
      </div>
      <Callout tone="info" className="mb-6">
        {t('agents.intro')}
      </Callout>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        <Tile
          id="agent-status"
          headingLevel={2}
          icon="activity"
          title={t('agents.detail.status')}
          className="xl:col-span-6"
          error={active.isError ? active.error : undefined}
          onRetry={() => void active.refetch()}
          source={t('agents.state.hint')}
        >
          <RoleStateBadge state={state} activity={activity} />
          <h3 className="text-sm font-semibold text-fg">{t('agents.detail.activeTasks')}</h3>
          {state === 'loading' ? (
            <span aria-hidden="true" className="skeleton block h-10 w-full" />
          ) : activity && activity.tasks.length > 0 ? (
            <ul aria-label={t('agents.detail.activeTasksCaption')} className="flex flex-col gap-2 text-sm">
              {activity.tasks.map((task) => (
                <li key={task.id} className="flex min-w-0 flex-wrap items-center gap-2" data-task-id={task.id}>
                  <StatusBadge kind="task" status={task.status} />
                  <Link to={taskLinks(workspaceId, task).mission} className="link min-w-0 break-words">
                    {task.title}
                  </Link>
                  {task.current_attempt?.started_at ? (
                    <span className="text-xs text-fg-muted">
                      <RelativeTime value={task.current_attempt.started_at} />
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-fg-muted">{t('agents.card.noCurrentTasks')}</p>
          )}
        </Tile>
        <Tile
          id="agent-results"
          headingLevel={2}
          icon="analytics"
          title={t('agents.detail.results')}
          className="xl:col-span-6"
          loading={analytics.isPending}
          error={analytics.isError ? analytics.error : undefined}
          onRetry={() => void analytics.refetch()}
          source={t('agents.detail.resultsSource', windowText)}
        >
          {stats ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Metric size="sm" value={stats.attempts} label={t('agents.card.attempts')} />
              <Metric size="sm" value={stats.succeeded} label={t('agents.card.succeeded')} />
              <Metric size="sm" value={stats.failed} label={t('agents.detail.failed')} />
              <Metric size="sm" value={stats.retries} label={t('agents.detail.retries')} />
              <Metric size="sm" value={formatPercent(stats.verified_completion_rate)} label={t('agents.card.verified')} testId="role-verified" />
            </div>
          ) : (
            <p className="text-sm text-fg-muted" data-testid="role-no-attempts">
              {t('agents.card.noAttempts')}
            </p>
          )}
        </Tile>
        <Tile
          id="agent-profile"
          headingLevel={2}
          icon="lock"
          title={t('agents.detail.profile')}
          className="xl:col-span-7"
          loading={profiles.isPending}
          error={profiles.isError ? profiles.error : undefined}
          onRetry={() => void profiles.refetch()}
          source={t('agents.detail.profileSource')}
        >
          <div>
            <h3 className="mb-1 text-sm font-semibold text-fg">{t('agents.detail.mission')}</h3>
            <p className="text-sm text-fg">{t(`agents.mission.${role}`)}</p>
          </div>
          {profile ? <ProfileSection profile={profile} /> : <p className="text-sm text-fg-muted">{t('agents.card.profileMissing')}</p>}
        </Tile>
        <Tile
          id="agent-providers"
          headingLevel={2}
          icon="terminal"
          title={t('agents.detail.providers')}
          className="xl:col-span-5"
          loading={providers.isPending}
          error={providers.isError ? providers.error : undefined}
          onRetry={() => void providers.refetch()}
          source={t('agents.detail.providersSource')}
        >
          {profile ? <ProviderChipsList providers={profile.eligible_providers} emptyText={t('agents.card.noProviders')} /> : null}
          <EligibilityList rows={eligibilityRows(role, providers.data?.items ?? [], profile)} name={name} />
          <Link to={`/w/${workspaceId}/settings/providers`} className="link inline-flex min-h-target w-fit items-center gap-1 text-sm">
            {canEditProviders ? t('agents.detail.toProvidersEdit') : t('agents.detail.toProviders')}
            <Icon name="chevronRight" className="h-4 w-4" />
          </Link>
        </Tile>
        <Tile
          id="agent-history"
          headingLevel={2}
          icon="history"
          title={t('agents.detail.history')}
          className="xl:col-span-12"
          loading={history.isPending}
          error={history.isError && historyItems.length === 0 ? history.error : undefined}
          onRetry={() => void history.refetch()}
          source={t('agents.detail.historySource')}
        >
          <HistoryTable workspaceId={workspaceId} tasks={historyItems} name={name} />
          <LoadMore
            shown={historyItems.length}
            hasNextPage={history.hasNextPage}
            isFetchingNextPage={history.isFetchingNextPage}
            nextPageError={history.isFetchNextPageError ? history.error : null}
            onLoadMore={() => void history.fetchNextPage()}
          />
        </Tile>
      </div>
    </section>
  );
}

/** F5: карточка роли агента — профиль (B7), активность, результаты, провайдеры, история задач. */
export function AgentRolePage() {
  const { t } = useTranslation();
  const { role } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  if (!isAgentRole(role)) {
    return (
      <section aria-labelledby="page-title">
        <PageHeading title={t('agents.title')} />
        <EmptyState icon="bot" title={t('agents.detail.notFound', { role: role ?? '' })} action={<Link to={`/w/${workspaceId}/agents`} className="link">{t('agents.detail.back')}</Link>} />
      </section>
    );
  }
  return <RoleDetail key={role} workspaceId={workspaceId} role={role} memberRole={membership.role} />;
}
