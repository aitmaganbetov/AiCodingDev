import type { UseQueryResult } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { Run, RunPage } from '../../api/runs';
import { Badge, Meter, RelativeTime, StatusBadge, Tile, TileError } from '../../components/ui';
import { useRunDetails } from './data';
import { ACTIVE_RUNS_LIMIT, WORKFLOW_DETAILS_LIMIT, runProgress } from './model';

function Progress({ detail }: { detail: UseQueryResult<Run> | undefined }) {
  const { t } = useTranslation();
  if (!detail || detail.isPending) return <div aria-hidden="true" className="skeleton h-6 w-full" />;
  if (detail.isError) return <p className="text-xs text-danger-text">{t('dashboard.workflows.detailsError')}</p>;
  const p = runProgress(detail.data);
  if (!p) return <p className="text-xs text-fg-muted">{t('dashboard.workflows.noPlan')}</p>;
  return (
    <div className="flex flex-col gap-1">
      <Meter
        testId="run-progress"
        label={t('dashboard.workflows.progress')}
        ratio={p.ratio}
        valueText={t('dashboard.workflows.progressValue', { verified: p.verified, total: p.total })}
        tone="success"
      />
      <p className="text-xs text-fg-muted">{t('dashboard.workflows.progressHint', { inProgress: p.inProgress, failed: p.failed, blocked: p.blocked })}</p>
      {detail.data.pending_approvals ? (
        <Badge tone="warning" icon="approval" className="w-fit">
          {t('dashboard.workflows.pendingApprovals', { count: detail.data.pending_approvals })}
        </Badge>
      ) : null}
    </div>
  );
}

/** Активные процессы: незавершённые runs (одна страница listRuns) + task_counts первых N (getRun). */
export function ActiveWorkflows({
  workspaceId,
  runs,
  projectNames,
}: {
  workspaceId: string;
  runs: UseQueryResult<RunPage>;
  projectNames: Map<string, string>;
}) {
  const { t } = useTranslation();
  const items = runs.data?.items ?? [];
  const shown = items.slice(0, WORKFLOW_DETAILS_LIMIT);
  const details = useRunDetails(shown.map((r) => r.id));
  const rest = items.length - shown.length;
  return (
    <Tile
      id="dashboard-workflows"
      icon="workflow"
      headingLevel={2}
      title={t('dashboard.workflows.title')}
      className="xl:col-span-5"
      loading={runs.isPending}
      source={runs.data ? t('dashboard.workflows.source', { shown: shown.length, limit: ACTIVE_RUNS_LIMIT }) : undefined}
    >
      <p className="-mt-2 text-sm text-fg-muted">{t('dashboard.workflows.subtitle')}</p>
      {runs.isError ? (
        <TileError error={runs.error} onRetry={() => void runs.refetch()} />
      ) : shown.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border p-4 text-sm text-fg-muted" data-testid="workflows-empty">
          {t('dashboard.workflows.empty')}
        </p>
      ) : (
        <ul aria-label={t('dashboard.workflows.list')} className="flex flex-col divide-y divide-border">
          {shown.map((run, i) => (
            <li key={run.id} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0" data-run-id={run.id}>
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <StatusBadge kind="run" status={run.status} />
                {run.blocked_reason ? (
                  <span className="text-xs text-fg-muted">{t(`enums.blocked_reason.${run.blocked_reason}`, { defaultValue: run.blocked_reason })}</span>
                ) : null}
                <span className="ml-auto text-xs text-fg-muted">
                  {t('dashboard.workflows.started')} <RelativeTime value={run.created_at} />
                </span>
              </div>
              <Link to={`/w/${workspaceId}/projects/${run.project_id}/runs/${run.id}`} className="link line-clamp-2 break-words font-medium">
                {run.objective}
              </Link>
              <span className="text-xs text-fg-muted">
                {t('dashboard.workflows.project')}: {projectNames.get(run.project_id) ?? t('dashboard.workflows.unknownProject')}
              </span>
              <Progress detail={details[i]} />
            </li>
          ))}
        </ul>
      )}
      {rest > 0 || runs.data?.next_cursor ? (
        <p className="text-sm text-fg-muted">{t('dashboard.workflows.more', { count: rest, plus: runs.data?.next_cursor ? '+' : '' })}</p>
      ) : null}
      <Link to={`/w/${workspaceId}/runs`} className="link inline-flex min-h-target w-fit items-center text-sm">
        {t('dashboard.workflows.allRuns')}
      </Link>
    </Tile>
  );
}
