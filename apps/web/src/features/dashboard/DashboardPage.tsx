import { useQuery } from '@tanstack/react-query';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { projectKeys, projectsApi, type Project } from '../../api/projects';
import { runKeys, runsApi, type RunSummary } from '../../api/runs';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { ButtonLink, Card, RelativeTime, Table, type Column } from '../../components/ui';
import { can } from '../../lib/permissions';
import { rememberWorkspace } from '../../lib/storage';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { summarizeRuns } from './summary';

/** Сколько последних runs workspace анализирует Dashboard (одна страница listRuns). */
export const DASHBOARD_RUNS_LIMIT = 50;
export const DASHBOARD_PROJECTS_LIMIT = 50;

function runHref(workspaceId: string, run: RunSummary): string {
  return `/w/${workspaceId}/projects/${run.project_id}/runs/${run.id}`;
}

function StatTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card flex flex-col gap-1">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className="text-3xl font-semibold text-fg">{value}</dd>
      {hint ? <dd className="text-xs text-fg-muted">{hint}</dd> : null}
    </div>
  );
}

function RunLine({ run, workspaceId, projectName }: { run: RunSummary; workspaceId: string; projectName: string | undefined }) {
  const { t } = useTranslation();
  const needsProvider = run.blocked_reason === 'PROVIDER_UNAVAILABLE' || run.blocked_reason === 'AUTH_REQUIRED';
  return (
    <li className="flex flex-col gap-1 border-t border-border py-3 first:border-t-0 first:pt-0" data-run-id={run.id}>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge kind="run" status={run.status} />
        {run.blocked_reason ? (
          <span className="text-sm text-fg-muted">{t(`enums.blocked_reason.${run.blocked_reason}`, { defaultValue: run.blocked_reason })}</span>
        ) : null}
        <span className="text-sm text-fg-muted">{projectName ?? t('dashboard.unknownProject')}</span>
      </div>
      <p className="line-clamp-2 break-words">{run.objective}</p>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
        <Link to={runHref(workspaceId, run)} className="font-medium text-link underline">
          {t('dashboard.openRun')}
          <span className="sr-only">: {run.objective.slice(0, 80)}</span>
        </Link>
        {run.status === 'AWAITING_APPROVAL' ? (
          <Link to={`/w/${workspaceId}/approvals`} className="font-medium text-link underline">
            {t('dashboard.toApprovals')}
          </Link>
        ) : null}
        {needsProvider ? (
          <Link to={`/w/${workspaceId}/providers`} className="font-medium text-link underline">
            {t('dashboard.toProviders')}
          </Link>
        ) : null}
      </div>
    </li>
  );
}

function RunList({
  runs,
  workspaceId,
  projects,
  label,
  empty,
}: {
  runs: RunSummary[];
  workspaceId: string;
  projects: Map<string, Project>;
  label: string;
  empty: string;
}) {
  if (runs.length === 0) return <p className="text-fg-muted">{empty}</p>;
  return (
    <ul aria-label={label}>
      {runs.map((r) => (
        <RunLine key={r.id} run={r} workspaceId={workspaceId} projectName={projects.get(r.project_id)?.name} />
      ))}
    </ul>
  );
}

function DashboardContent({ workspaceId, canCreateProject }: { workspaceId: string; canCreateProject: boolean }) {
  const { t } = useTranslation();
  const projectsQuery = useQuery({
    queryKey: projectKeys.summary(workspaceId),
    queryFn: ({ signal }) => projectsApi.list(workspaceId, { limit: DASHBOARD_PROJECTS_LIMIT }, signal),
  });
  const runsQuery = useQuery({
    queryKey: [...runKeys.list(workspaceId), 'dashboard'] as const,
    queryFn: ({ signal }) => runsApi.list(workspaceId, { limit: DASHBOARD_RUNS_LIMIT }, signal),
  });

  if (projectsQuery.isPending || runsQuery.isPending) return <LoadingState label={t('dashboard.loading')} />;
  if (projectsQuery.isError) return <ErrorState error={projectsQuery.error} onRetry={() => void projectsQuery.refetch()} />;

  const projects = new Map(projectsQuery.data.items.map((p) => [p.id, p]));
  const projectCount = projectsQuery.data.next_cursor
    ? t('dashboard.countMore', { count: projectsQuery.data.items.length })
    : String(projectsQuery.data.items.length);

  if (projectsQuery.data.items.length === 0) {
    return (
      <EmptyState
        icon="folder"
        title={t('dashboard.noProjectsTitle')}
        description={canCreateProject ? t('dashboard.noProjectsDescription') : t('dashboard.noProjectsReadOnly')}
        action={
          canCreateProject ? (
            <ButtonLink to={`/w/${workspaceId}/projects/new`} variant="primary">
              {t('dashboard.createProject')}
            </ButtonLink>
          ) : undefined
        }
      />
    );
  }

  if (runsQuery.isError) {
    return (
      <>
        <dl className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label={t('dashboard.stat.projects')} value={projectCount} />
        </dl>
        <ErrorState error={runsQuery.error} onRetry={() => void runsQuery.refetch()} title={t('dashboard.runsError')} />
      </>
    );
  }

  const summary = summarizeRuns(runsQuery.data.items);
  const sample = runsQuery.data.items.length;
  const sampleHint = t('dashboard.sampleHint', { count: sample });
  const columns: Column<RunSummary>[] = [
    {
      id: 'objective',
      header: t('dashboard.col.objective'),
      rowHeader: true,
      cell: (r) => (
        <Link to={runHref(workspaceId, r)} className="line-clamp-2 break-words text-link underline">
          {r.objective}
        </Link>
      ),
      sortValue: (r) => r.objective,
    },
    { id: 'project', header: t('dashboard.col.project'), cell: (r) => projects.get(r.project_id)?.name ?? '—', sortValue: (r) => projects.get(r.project_id)?.name },
    { id: 'mode', header: t('dashboard.col.mode'), cell: (r) => t(`enums.run_mode.${r.mode}`, { defaultValue: r.mode }), sortValue: (r) => r.mode },
    { id: 'status', header: t('dashboard.col.status'), cell: (r) => <StatusBadge kind="run" status={r.status} />, sortValue: (r) => r.status },
    { id: 'created', header: t('dashboard.col.created'), cell: (r) => <RelativeTime value={r.created_at} />, sortValue: (r) => r.created_at },
  ];

  return (
    <div className="flex flex-col gap-6">
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label={t('dashboard.stat.projects')} value={projectCount} />
        <StatTile label={t('dashboard.stat.active')} value={String(summary.active.length)} hint={sampleHint} />
        <StatTile label={t('dashboard.stat.attention')} value={String(summary.attention.length)} hint={sampleHint} />
        <StatTile label={t('dashboard.stat.failed')} value={String(summary.failed)} hint={sampleHint} />
      </dl>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card as="section" id="dashboard-attention" title={t('dashboard.attentionTitle')}>
          <RunList
            runs={summary.attention}
            workspaceId={workspaceId}
            projects={projects}
            label={t('dashboard.attentionTitle')}
            empty={t('dashboard.attentionEmpty')}
          />
        </Card>
        <Card as="section" id="dashboard-active" title={t('dashboard.activeTitle')}>
          <RunList
            runs={summary.active}
            workspaceId={workspaceId}
            projects={projects}
            label={t('dashboard.activeTitle')}
            empty={t('dashboard.activeEmpty')}
          />
        </Card>
      </div>

      <section aria-labelledby="dashboard-recent-title">
        <h2 id="dashboard-recent-title" className="mb-3 text-lg font-semibold">
          {t('dashboard.recentTitle')}
        </h2>
        <Table
          caption={t('dashboard.recentTitle')}
          columns={columns}
          rows={summary.recent}
          rowKey={(r) => r.id}
          empty={t('dashboard.recentEmpty')}
        />
      </section>

      <nav aria-label={t('dashboard.quickLinks')} className="flex flex-wrap gap-3">
        <ButtonLink to={`/w/${workspaceId}/projects`} icon="folder">
          {t('dashboard.link.projects')}
        </ButtonLink>
        <ButtonLink to={`/w/${workspaceId}/approvals`} icon="approval">
          {t('dashboard.link.approvals')}
        </ButtonLink>
        <ButtonLink to={`/w/${workspaceId}/providers`} icon="terminal">
          {t('dashboard.link.providers')}
        </ButtonLink>
      </nav>
    </div>
  );
}

/** P7-03 Dashboard: сводка workspace — проекты, идущие runs, runs, требующие внимания, последние runs. */
export function DashboardPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();

  useEffect(() => {
    if (membership) rememberWorkspace(membership.workspace_id);
  }, [membership]);

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('dashboard.pageTitle', { workspace: membership.name })} />
      <DashboardContent key={workspaceId} workspaceId={workspaceId} canCreateProject={can(membership.role, 'project:create')} />
    </section>
  );
}
