import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate, useParams } from 'react-router';
import { api, IdempotentIntent, isApiError, type RunCreateRequest, type RunMode, type RunSummary } from '../api/client';
import { FormAlert, TextAreaField, focusFirstInvalid } from '../components/form';
import { LoadMore } from '../components/LoadMore';
import { PageHeading } from '../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../components/states';
import { StatusBadge } from '../components/StatusBadge';
import { formatDate } from '../lib/format';
import { can } from '../lib/permissions';
import { mapProblemToFields } from '../lib/problem';
import { RUN_OBJECTIVE_MAX } from '../lib/validation';
import { useCurrentWorkspace } from '../workspace';
import { WorkspaceNotFound } from './NotFoundPage';

export const RUNS_PAGE_SIZE = 20;
const RUN_MODES = [
  'DEVELOP',
  'CREATE',
  'DEBUG',
  'AUDIT',
  'MODERNIZE',
  'SECURITY',
  'OPTIMIZE',
  'TEST',
  'MAINTAIN',
  'DOCUMENT',
  'REVIEW',
] as const satisfies readonly RunMode[];

export function runsQueryKey(workspaceId: string, projectId: string) {
  return ['runs', workspaceId, projectId] as const;
}

export function runPath(workspaceId: string, projectId: string, runId: string): string {
  return `/w/${workspaceId}/projects/${projectId}/runs/${runId}`;
}

function RunItem({ run, workspaceId }: { run: RunSummary; workspaceId: string }) {
  const { t, i18n } = useTranslation();
  return (
    <li className="card flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge kind="run" status={run.status} />
        <span className="text-sm text-slate-700">{t(`enums.run_mode.${run.mode}`, { defaultValue: run.mode })}</span>
        <span className="text-sm text-slate-700">
          {t('runs.createdAt')}: {formatDate(run.created_at, i18n.language)}
        </span>
      </div>
      <p className="line-clamp-3 whitespace-pre-line break-words">{run.objective}</p>
      <Link to={runPath(workspaceId, run.project_id, run.id)} className="self-start font-medium text-blue-800 underline">
        {t('runs.open')}
        <span className="sr-only">: {run.objective.slice(0, 80)}</span>
      </Link>
    </li>
  );
}

function RunsList({ workspaceId, projectId, canCreate }: { workspaceId: string; projectId: string; canCreate: boolean }) {
  const { t } = useTranslation();
  const query = useInfiniteQuery({
    queryKey: runsQueryKey(workspaceId, projectId),
    queryFn: ({ pageParam, signal }) =>
      api.listRuns(
        workspaceId,
        { project_id: projectId, limit: RUNS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];

  if (query.isPending) return <LoadingState label={t('runs.loading')} />;
  if (query.isError && items.length === 0) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (items.length === 0) {
    return (
      <EmptyState
        title={t('runs.emptyTitle')}
        description={canCreate ? t('runs.emptyDescription') : t('runs.emptyDescriptionReadOnly')}
      />
    );
  }
  return (
    <>
      <ul className="flex flex-col gap-3" aria-label={t('runs.listLabel')}>
        {items.map((r) => (
          <RunItem key={r.id} run={r} workspaceId={workspaceId} />
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

function StartRunForm({ workspaceId, projectId }: { workspaceId: string; projectId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const intent = useRef(new IdempotentIntent());
  const [mode, setMode] = useState<RunMode>('DEVELOP');
  const [objective, setObjective] = useState('');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<'objective' | 'mode', string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const create = useMutation({
    mutationFn: (body: RunCreateRequest) => api.createRun(body, intent.current.keyFor(body)),
    onSuccess: async (created) => {
      intent.current.complete();
      await queryClient.invalidateQueries({ queryKey: runsQueryKey(workspaceId, projectId) });
      navigate(runPath(workspaceId, projectId, created.run_id));
    },
    onError: (error) => {
      // 404: проект удалён или чужой (createRun: workspace определяется по project_id).
      if (isApiError(error) && error.status === 404) {
        setFieldErrors({});
        setFormErrors([t('runs.projectNotFound')]);
        return;
      }
      const mapped = mapProblemToFields(t, error, ['objective', 'mode'] as const);
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = objective.trim();
    const errors: Partial<Record<'objective', string>> = {};
    if (!trimmed) errors.objective = t('validation.required');
    else if (trimmed.length > RUN_OBJECTIVE_MAX) errors.objective = t('validation.maxLength', { max: RUN_OBJECTIVE_MAX });
    setFieldErrors(errors);
    setFormErrors([]);
    if (errors.objective) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    create.mutate({ project_id: projectId, mode, objective: trimmed, approval_policy: 'risk_based' });
  };

  return (
    <form ref={formRef} noValidate onSubmit={onSubmit} className="card mb-6 flex flex-col gap-4" aria-labelledby="start-run-title">
      <h2 id="start-run-title" className="text-lg font-semibold">
        {t('runs.startTitle')}
      </h2>
      <FormAlert messages={formErrors} />
      <div className="flex flex-col gap-1">
        <label htmlFor="run-mode" className="text-sm font-medium text-slate-900">
          {t('runs.mode')}
        </label>
        <select
          id="run-mode"
          className="input"
          value={mode}
          onChange={(e) => setMode(e.target.value as RunMode)}
          aria-invalid={Boolean(fieldErrors.mode)}
        >
          {RUN_MODES.map((m) => (
            <option key={m} value={m}>
              {t(`enums.run_mode.${m}`)}
            </option>
          ))}
        </select>
      </div>
      <TextAreaField
        id="run-objective"
        label={t('runs.objective')}
        name="objective"
        required
        maxLength={RUN_OBJECTIVE_MAX}
        value={objective}
        onChange={(e) => setObjective(e.target.value)}
        hint={t('runs.objectiveHint', { max: RUN_OBJECTIVE_MAX })}
        error={fieldErrors.objective}
      />
      <div>
        <button type="submit" className="btn-primary" disabled={create.isPending}>
          {create.isPending ? t('runs.starting') : t('runs.start')}
        </button>
      </div>
    </form>
  );
}

export function ProjectRunsPage() {
  const { t } = useTranslation();
  const { projectId = '' } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: ({ signal }) => api.getProject(projectId, signal),
    enabled: Boolean(projectId && membership),
  });

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const canCreate = can(membership.role, 'run:create');
  const title = project.data ? t('runs.title', { project: project.data.name }) : t('runs.titleFallback');

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={title}>
        <Link to={`/w/${workspaceId}/projects`} className="btn-secondary">
          {t('runs.backToProjects')}
        </Link>
      </PageHeading>
      {project.isError ? <ErrorState error={project.error} onRetry={() => void project.refetch()} /> : null}
      {canCreate ? <StartRunForm workspaceId={workspaceId} projectId={projectId} /> : null}
      <RunsList key={projectId} workspaceId={workspaceId} projectId={projectId} canCreate={canCreate} />
    </section>
  );
}
