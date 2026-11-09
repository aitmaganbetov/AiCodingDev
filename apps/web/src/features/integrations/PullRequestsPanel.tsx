/**
 * Pull requests проекта / запуска (P8-04, P8-05): список (`listPullRequests`), создание PR из завершённого
 * запуска (`requestPullRequest`, pull_request:create) и запрос merge (`requestPullRequestMerge`,
 * pull_request:merge) — merge выполняет воркер только после утверждения approval MERGE (four-eyes).
 * PR создаёт воркер асинхронно (202): строка появится в списке после push и создания PR на GitHub.
 */
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { IdempotentIntent, isApiError } from '../../api/client';
import { githubApi, githubKeys, type MergeMethod, type PullRequest, type PullRequestCreateRequest } from '../../api/github';
import { FormAlert, RadioGroup, TextField, focusFirstInvalid } from '../../components/form';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import {
  Badge,
  Button,
  Callout,
  Dialog,
  DialogClose,
  DialogContent,
  DialogTrigger,
  LoadMore,
  Table,
  type Column,
  type Tone,
} from '../../components/ui';
import { can } from '../../lib/permissions';
import { mapProblemToFields, problemMessage } from '../../lib/problem';

export const PULL_REQUESTS_PAGE_SIZE = 50;
const BRANCH_PATTERN = /^[A-Za-z0-9._/-]+$/;
const TITLE_MAX = 256;
const MERGE_METHODS: readonly MergeMethod[] = ['merge', 'squash', 'rebase'];
const STATE_TONE: Record<PullRequest['state'], Tone> = { OPEN: 'info', MERGED: 'success', CLOSED: 'neutral' };

interface Notice {
  text: string;
  approvalId?: string | undefined;
  tone: 'success' | 'warning';
}

/** approval_id из расширения problem (403 policy_denied: изменения CI-конфигурации требуют MANUAL_REVIEW). */
export function problemApprovalId(error: unknown): string | undefined {
  if (!isApiError(error) || !error.problem) return undefined;
  const value = (error.problem as Record<string, unknown>).approval_id;
  return typeof value === 'string' ? value : undefined;
}

export function validatePullRequest(input: { title: string; base: string }, t: (k: string, o?: Record<string, unknown>) => string) {
  const errors: Partial<Record<'title' | 'base_branch', string>> = {};
  if (input.title.length > TITLE_MAX) errors.title = t('validation.maxLength', { max: TITLE_MAX });
  if (input.base && (input.base.length > 255 || !BRANCH_PATTERN.test(input.base))) errors.base_branch = t('integrations.pr.branchInvalid');
  return errors;
}

function CreatePullRequestDialog({ runId, projectId, onNotice }: { runId: string; projectId: string; onNotice: (n: Notice) => void }) {
  const { t } = useTranslation();
  const uid = useId();
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const intent = useRef(new IdempotentIntent());
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [base, setBase] = useState('');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<'title' | 'base_branch', string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const create = useMutation({
    mutationFn: (body: PullRequestCreateRequest) => githubApi.requestPullRequest(runId, body, intent.current.keyFor(body)),
    onSuccess: async (job) => {
      intent.current.complete();
      setOpen(false);
      onNotice({ tone: 'success', text: t('integrations.pr.queued', { repo: job.repository_full_name, branch: job.head_branch, base: job.base_branch }) });
      await queryClient.invalidateQueries({ queryKey: githubKeys.pullRequestsAll(projectId) });
    },
    onError: (error) => {
      const approvalId = problemApprovalId(error);
      if (approvalId) {
        setOpen(false);
        onNotice({ tone: 'warning', text: t('integrations.pr.ciApproval'), approvalId });
        return;
      }
      const mapped = mapProblemToFields(t, error, ['title', 'base_branch'] as const);
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
    },
  });
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const input = { title: title.trim(), base: base.trim() };
    const errors = validatePullRequest(input, t);
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    create.mutate({ ...(input.title ? { title: input.title } : {}), ...(input.base ? { base_branch: input.base } : {}) });
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setFieldErrors({});
          setFormErrors([]);
        }
      }}
    >
      <DialogTrigger asChild>
        <Button variant="primary" icon="layers">
          {t('integrations.pr.create')}
        </Button>
      </DialogTrigger>
      <DialogContent title={t('integrations.pr.createTitle')} description={t('integrations.pr.createDescription')}>
        <form ref={formRef} noValidate onSubmit={onSubmit} className="flex flex-col gap-4">
          <FormAlert messages={formErrors} />
          <TextField
            id={`${uid}-title`}
            label={t('integrations.pr.titleField')}
            hint={t('integrations.pr.titleHint')}
            maxLength={TITLE_MAX}
            autoComplete="off"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            error={fieldErrors.title}
          />
          <TextField
            id={`${uid}-base`}
            label={t('integrations.pr.baseBranch')}
            hint={t('integrations.pr.baseBranchHint')}
            maxLength={255}
            autoComplete="off"
            value={base}
            onChange={(e) => setBase(e.target.value)}
            error={fieldErrors.base_branch}
          />
          <div className="flex flex-wrap justify-end gap-3">
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button type="submit" variant="primary" loading={create.isPending}>
              {t('integrations.pr.submit')}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function MergeRequestDialog({ projectId, pr, onNotice }: { projectId: string; pr: PullRequest; onNotice: (n: Notice) => void }) {
  const { t } = useTranslation();
  const intent = useRef(new IdempotentIntent());
  const [open, setOpen] = useState(false);
  const [method, setMethod] = useState<MergeMethod>('merge');
  const [errors, setErrors] = useState<string[]>([]);
  const merge = useMutation({
    mutationFn: (m: MergeMethod) => githubApi.requestMerge(projectId, pr.id, { merge_method: m }, intent.current.keyFor({ pr: pr.id, m })),
    onSuccess: (accepted) => {
      intent.current.complete();
      setOpen(false);
      onNotice({ tone: 'success', text: t('integrations.pr.mergeRequested', { number: pr.pr_number }), approvalId: accepted.approval_id });
    },
    onError: (error) => setErrors([problemMessage(t, error)]),
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setErrors([]);
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" aria-label={t('integrations.pr.mergeOf', { number: pr.pr_number })}>
          {t('integrations.pr.merge')}
        </Button>
      </DialogTrigger>
      <DialogContent
        title={t('integrations.pr.mergeTitle', { number: pr.pr_number })}
        description={t('integrations.pr.mergeDescription')}
        footer={
          <>
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button variant="primary" loading={merge.isPending} onClick={() => merge.mutate(method)}>
              {t('integrations.pr.mergeSubmit')}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <FormAlert messages={errors} />
          <RadioGroup
            name={`merge-method-${pr.id}`}
            legend={t('integrations.pr.mergeMethod')}
            value={method}
            onChange={setMethod}
            options={MERGE_METHODS.map((m) => ({ value: m, label: t(`integrations.pr.enums.method.${m}`) }))}
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function PullRequestsPanel({
  workspaceId,
  projectId,
  role,
  run,
  headingLevel = 2,
}: {
  workspaceId: string;
  projectId: string;
  role: string;
  /** Панель запуска: фильтр по run и создание PR (только COMPLETED). */
  run?: { id: string; status: string } | undefined;
  headingLevel?: 2 | 3;
}) {
  const { t } = useTranslation();
  const uid = useId();
  const [notice, setNotice] = useState<Notice | null>(null);
  const query = useInfiniteQuery({
    queryKey: githubKeys.pullRequests(projectId, run?.id),
    queryFn: ({ pageParam, signal }) =>
      githubApi.pullRequests(
        projectId,
        { limit: PULL_REQUESTS_PAGE_SIZE, ...(run ? { run_id: run.id } : {}), ...(pageParam ? { cursor: pageParam } : {}) },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  const canMerge = can(role, 'pull_request:merge');
  const canCreate = Boolean(run) && can(role, 'pull_request:create');
  const Heading = headingLevel === 2 ? 'h2' : 'h3';

  const columns: Column<PullRequest>[] = [
    {
      id: 'number',
      header: t('integrations.pr.number'),
      rowHeader: true,
      cell: (pr) => (
        <a href={pr.url} target="_blank" rel="noopener noreferrer" className="text-link underline" aria-label={t('integrations.pr.openOnGitHub', { number: pr.pr_number })}>
          #{pr.pr_number}
        </a>
      ),
      sortValue: (pr) => pr.pr_number,
    },
    { id: 'repo', header: t('integrations.pr.repository'), cell: (pr) => <span className="break-all">{pr.repository_full_name}</span> },
    {
      id: 'branches',
      header: t('integrations.pr.branches'),
      cell: (pr) => (
        <span className="break-all font-mono text-xs">
          {pr.head_branch} → {pr.base_branch}
        </span>
      ),
    },
    {
      id: 'state',
      header: t('integrations.pr.state'),
      cell: (pr) => <Badge tone={STATE_TONE[pr.state] ?? 'neutral'}>{t(`integrations.pr.enums.state.${pr.state}`, { defaultValue: pr.state })}</Badge>,
      sortValue: (pr) => pr.state,
    },
  ];
  if (!run) {
    columns.push({
      id: 'run',
      header: t('integrations.pr.run'),
      cell: (pr) =>
        pr.run_id ? (
          <Link className="text-link underline" to={`/w/${workspaceId}/projects/${projectId}/runs/${pr.run_id}`}>
            {pr.run_id.slice(-8)}
          </Link>
        ) : null,
    });
  }
  if (canMerge) {
    columns.push({
      id: 'actions',
      header: t('integrations.github.actions'),
      cell: (pr) => (pr.state === 'OPEN' ? <MergeRequestDialog projectId={projectId} pr={pr} onNotice={setNotice} /> : null),
    });
  }

  let body;
  if (query.isPending) body = <LoadingState label={t('integrations.pr.loading')} />;
  else if (query.isError && items.length === 0) body = <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  else if (items.length === 0) {
    body = <EmptyState headingLevel={3} icon="layers" title={t('integrations.pr.emptyTitle')} description={t('integrations.pr.emptyDescription')} />;
  } else {
    body = (
      <>
        <Table caption={t('integrations.pr.listLabel')} columns={columns} rows={items} rowKey={(pr) => pr.id} />
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

  return (
    <section aria-labelledby={`${uid}-title`} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Heading id={`${uid}-title`} className="text-lg font-semibold">
          {t('integrations.pr.title')}
        </Heading>
        {canCreate && run?.status === 'COMPLETED' ? <CreatePullRequestDialog runId={run.id} projectId={projectId} onNotice={setNotice} /> : null}
      </div>
      {canCreate && run && run.status !== 'COMPLETED' ? <p className="text-sm text-fg-muted">{t('integrations.pr.createOnlyCompleted')}</p> : null}
      {notice ? (
        notice.tone === 'success' ? (
          <p role="status" className="rounded-md border border-success-border bg-success-soft p-3 text-sm text-success-fg">
            {notice.text}{' '}
            {notice.approvalId ? (
              <Link className="font-medium underline" to={`/w/${workspaceId}/approvals/${notice.approvalId}`}>
                {t('integrations.pr.openApproval')}
              </Link>
            ) : null}
          </p>
        ) : (
          <Callout tone="warning">
            <span role="status">
              {notice.text}{' '}
              {notice.approvalId ? (
                <Link className="font-medium underline" to={`/w/${workspaceId}/approvals/${notice.approvalId}`}>
                  {t('integrations.pr.openApproval')}
                </Link>
              ) : null}
            </span>
          </Callout>
        )
      ) : null}
      {body}
    </section>
  );
}
