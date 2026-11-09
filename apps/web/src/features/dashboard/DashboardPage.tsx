import { useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { PageHeading } from '../../components/PageHeading';
import { ButtonLink, Card } from '../../components/ui';
import { formatDate } from '../../lib/format';
import { can } from '../../lib/permissions';
import { rememberWorkspace } from '../../lib/storage';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { ActiveWorkflows } from './ActiveWorkflows';
import { ActivityTimeline } from './ActivityTimeline';
import { AgentPerformance, QualityOverview } from './AnalyticsSections';
import { useActiveProjects, useActiveRuns, useAnalytics, useDashboardLive, useDashboardWindow } from './data';
import { AgentsTile, ApprovalsTile, CompletedTile, ProjectsTile, ProvidersTile, QualityTile, SecurityTile, WorkflowSuccessTile } from './KpiTiles';
import { DASHBOARD_WINDOW_DAYS } from './model';
import { SystemHealthPanel } from './SystemHealthPanel';

function Hero({
  workspaceId,
  workspaceName,
  role,
  window,
  noProjects,
  canCreateProject,
}: {
  workspaceId: string;
  workspaceName: string;
  role: string;
  window: { from: string; to: string };
  noProjects: boolean;
  canCreateProject: boolean;
}) {
  const { t, i18n } = useTranslation();
  const lastDay = new Date(Date.parse(window.to) - 1).toISOString();
  return (
    <Card variant="featured" className="rounded-xl p-5 md:p-6" id="dashboard-hero">
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            {/* Строки бренда — как у владельца (английский); lang для экранных дикторов (WCAG 3.1.2). */}
            <p lang="en" className="text-2xl font-semibold text-fg md:text-display" data-testid="hero-title">
              {t('dashboard.hero.title')}
            </p>
            <p lang="en" className="text-base text-fg-muted">
              {t('dashboard.hero.subtitle')}
            </p>
          </div>
        </div>
        <p className="text-sm text-fg-muted">
          {t('dashboard.hero.workspace', { workspace: workspaceName, role: t(`enums.workspace_role.${role}`, { defaultValue: role }) })}
        </p>
        <p className="text-xs text-fg-muted">
          {t('dashboard.hero.window', { days: DASHBOARD_WINDOW_DAYS, from: formatDate(window.from, i18n.language), to: formatDate(lastDay, i18n.language) })}
        </p>
        {noProjects ? (
          <div className="flex flex-col items-start gap-2 rounded-lg border border-border bg-surface p-4" data-testid="empty-state">
            <h2 className="text-lg font-semibold text-fg">{t('dashboard.hero.noProjectsTitle')}</h2>
            <p className="text-sm text-fg-muted">{canCreateProject ? t('dashboard.hero.noProjectsDescription') : t('dashboard.hero.noProjectsReadOnly')}</p>
            {canCreateProject ? (
              <ButtonLink to={`/w/${workspaceId}/projects/new`} variant="primary" icon="plus">
                {t('dashboard.hero.createProject')}
              </ButtonLink>
            ) : null}
          </div>
        ) : null}
        <nav aria-label={t('dashboard.hero.actions')} className="flex flex-wrap gap-2">
          <ButtonLink to={`/w/${workspaceId}/mission-control`} icon="missionControl" size="sm">
            {t('dashboard.hero.missionControl')}
          </ButtonLink>
          <ButtonLink to={`/w/${workspaceId}/projects`} icon="folder" size="sm" variant="outline">
            {t('dashboard.hero.projects')}
          </ButtonLink>
          <ButtonLink to={`/w/${workspaceId}/approvals`} icon="approval" size="sm" variant="outline">
            {t('dashboard.hero.approvals')}
          </ButtonLink>
          <ButtonLink to={`/w/${workspaceId}/analytics`} icon="analytics" size="sm" variant="outline">
            {t('dashboard.hero.analytics')}
          </ButtonLink>
        </nav>
      </div>
    </Card>
  );
}

function DashboardContent({ workspaceId, workspaceName, role }: { workspaceId: string; workspaceName: string; role: string }) {
  const { t } = useTranslation();
  const window = useDashboardWindow();
  const analytics = useAnalytics(workspaceId, window);
  const activeRuns = useActiveRuns(workspaceId);
  const projects = useActiveProjects(workspaceId);
  const live = useDashboardLive(workspaceId);
  const projectNames = useMemo(() => new Map((projects.data?.items ?? []).map((p) => [p.id, p.name])), [projects.data]);
  const runProjects = useMemo(() => new Map((activeRuns.data?.items ?? []).map((r) => [r.id, r.project_id])), [activeRuns.data]);

  return (
    <div className="flex flex-col gap-4 xl:gap-5">
      <Hero
        workspaceId={workspaceId}
        workspaceName={workspaceName}
        role={role}
        window={window}
        noProjects={projects.data?.items.length === 0}
        canCreateProject={can(role, 'project:create')}
      />
      <section aria-labelledby="dashboard-kpi-title">
        <h2 id="dashboard-kpi-title" className="sr-only">
          {t('dashboard.kpi.label')}
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4 xl:gap-5" data-testid="kpi-grid">
          <ProjectsTile workspaceId={workspaceId} activeRuns={activeRuns} />
          <AgentsTile workspaceId={workspaceId} />
          <CompletedTile analytics={analytics} window={window} />
          <QualityTile analytics={analytics} window={window} />
          <SecurityTile workspaceId={workspaceId} />
          <ApprovalsTile workspaceId={workspaceId} />
          <ProvidersTile workspaceId={workspaceId} role={role} analytics={analytics} />
          <WorkflowSuccessTile analytics={analytics} window={window} />
        </div>
      </section>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-5">
        <ActivityTimeline workspaceId={workspaceId} liveEvents={live.events} liveState={live.state} onReconnect={live.reconnect} runProjects={runProjects} />
        <ActiveWorkflows workspaceId={workspaceId} runs={activeRuns} projectNames={projectNames} />
        <AgentPerformance analytics={analytics} />
        <QualityOverview analytics={analytics} />
        {can(role, 'system:health') ? <SystemHealthPanel workspaceId={workspaceId} role={role} /> : null}
      </div>
    </div>
  );
}

/**
 * Bento Dashboard (F3, план §3.2) — стартовый экран workspace: hero, 8 KPI-плиток из реальных
 * операций (listProjects, GET /tasks, analytics, GET /findings, listApprovals, listProviders),
 * лента активности (GET /activity + поток workspace), активные процессы, результаты агентов,
 * обзор качества (D-8) и состояние системы (B2, только owner/admin). Каждое число подписано
 * источником/окном/выборкой; нет данных ≠ 0; интегральной «оценки качества» нет (D-6).
 */
export default function DashboardPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();

  useEffect(() => {
    if (membership) rememberWorkspace(membership.workspace_id);
  }, [membership]);

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('dashboard.pageTitle', { workspace: membership.name })} />
      <DashboardContent key={workspaceId} workspaceId={workspaceId} workspaceName={membership.name} role={membership.role} />
    </section>
  );
}
