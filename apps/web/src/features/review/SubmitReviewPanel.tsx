/**
 * Отправка ревью (submitReview, ADR-0015 §3, API_DESIGN §5.6) — единственная точка воздействия комментариев на
 * исполнение: «Утвердить» = решение существующего PENDING approval run (какого — видно в форме; без approval
 * кнопки нет), «Запросить изменения» = поручение CHANGE_REQUEST (impact preview → подтверждение → доставка),
 * «Комментарий» = только запись ревью. Step-up и four-eyes решает сервер; 409 — run ушёл вперёд/завершён.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { Approval } from '../../api/approvals';
import { IdempotentIntent, isApiError } from '../../api/client';
import { directiveKeys } from '../../api/directives';
import {
  REVIEW_BODY_MAX,
  REVIEW_THREADS_MAX,
  reviewKeys,
  reviewsApi,
  type ReviewSubmission,
  type ReviewSubmitRequest,
  type ReviewThread,
  type ReviewVerdict,
} from '../../api/reviews';
import { runKeys, type Run, type TaskSummary } from '../../api/runs';
import { useStepUp } from '../../auth/StepUp';
import { FormAlert } from '../../components/form';
import { RUN_TERMINAL } from '../../components/StatusBadge';
import { Badge, Button, Callout, Dialog, ErrorState, LoadingState, Timestamp } from '../../components/ui';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { DirectiveTrackContent } from '../directives/DirectiveDialog';
import { anchorLabel, decidableApprovals, defaultSubmitThreads, verdictOptions } from './reviewModel';

const VERDICT_TONE = { APPROVE: 'success', REQUEST_CHANGES: 'warning', COMMENT: 'neutral' } as const;

function SubmissionItem({ s, workspaceId }: { s: ReviewSubmission; workspaceId: string }) {
  const { t } = useTranslation();
  return (
    <li className="flex flex-col gap-1 border-b border-border py-2 text-sm last:border-b-0" data-review-id={s.id}>
      <p className="flex flex-wrap items-center gap-2">
        <Badge tone={VERDICT_TONE[s.verdict]}>{t(`review.verdict.${s.verdict}`)}</Badge>
        <span className="text-fg-muted">
          {s.submitted_by.display_name} · <Timestamp value={s.created_at} />
        </span>
      </p>
      {s.body ? <p className="whitespace-pre-wrap break-words">{s.body}</p> : null}
      {s.approval_id ? (
        <p className="text-fg-muted">
          {t('review.submitted.approval')}{' '}
          <Link className="text-link underline" to={`/w/${workspaceId}/approvals/${s.approval_id}`}>
            {s.approval_status ? t(`approvals.enums.status.${s.approval_status}`, { defaultValue: s.approval_status }) : t('review.submitted.open')}
          </Link>
        </p>
      ) : null}
      {s.directive_id ? (
        <p className="text-fg-muted">
          {t('review.submitted.directive')}{' '}
          <Link className="text-link underline" to={`/w/${workspaceId}/mission-control?${new URLSearchParams({ run: s.run_id, view: 'directives' }).toString()}`}>
            {s.directive_status ? t(`directives.enums.status.${s.directive_status}`, { defaultValue: s.directive_status }) : t('review.submitted.open')}
          </Link>
        </p>
      ) : null}
      {s.thread_ids.length > 0 ? <p className="text-xs text-fg-muted">{t('review.submitted.threads', { count: s.thread_ids.length })}</p> : null}
    </li>
  );
}

function ApprovalSummary({ approval }: { approval: Approval }) {
  const { t } = useTranslation();
  return (
    <span className="flex flex-col gap-0.5">
      <span className="font-medium">{t(`approvals.enums.action.${approval.action_type}`, { defaultValue: approval.action_type })}</span>
      <span className="break-words text-fg-muted">{approval.summary}</span>
      <span className="text-xs text-fg-muted">
        {t('review.submit.risk', { risk: t(`quality.enums.risk.${approval.risk_level}`, { defaultValue: approval.risk_level }) })}
        {approval.can_decide ? '' : ` · ${t('review.submit.cannotDecideOne')}`}
      </span>
    </span>
  );
}

export function SubmitReviewPanel({
  run,
  role,
  workspaceId,
  scopeTask,
  threads,
  approvals,
}: {
  run: Run;
  role: string;
  workspaceId: string;
  /** Задача области ревью (цель REQUEST_CHANGES по умолчанию), иначе — run. */
  scopeTask: TaskSummary | null;
  /** Треды run в области ревью. */
  threads: readonly ReviewThread[];
  approvals: { isPending: boolean; isError: boolean; error: unknown; data: readonly Approval[] | undefined; refetch: () => unknown };
}) {
  const { t } = useTranslation();
  const id = useId();
  const queryClient = useQueryClient();
  const stepUp = useStepUp();
  const intent = useRef(new IdempotentIntent());
  const pending = approvals.data ?? [];
  const options = verdictOptions(role, pending);
  const firstAllowed = options.find((o) => o.blocked === null)?.verdict ?? 'COMMENT';
  const [verdict, setVerdict] = useState<ReviewVerdict>(firstAllowed);
  const decidable = decidableApprovals(pending);
  const [approvalId, setApprovalId] = useState('');
  const [target, setTarget] = useState<'task' | 'run'>(scopeTask ? 'task' : 'run');
  const openThreads = threads.filter((th) => th.status === 'OPEN');
  const [selected, setSelected] = useState<string[] | null>(null);
  const chosen = selected ?? defaultSubmitThreads(threads, REVIEW_THREADS_MAX);
  const [body, setBody] = useState('');
  const [preview, setPreview] = useState(true);
  const [errors, setErrors] = useState<string[]>([]);
  const [result, setResult] = useState<ReviewSubmission | null>(null);
  const [trackOpen, setTrackOpen] = useState(false);

  const submissions = useQuery({
    queryKey: reviewKeys.submissions(run.id),
    queryFn: ({ signal }) => reviewsApi.listSubmissions(run.id, { limit: 50 }, signal),
  });

  const submit = useMutation({
    mutationFn: (req: ReviewSubmitRequest) => stepUp.run(() => reviewsApi.submit(run.id, req, intent.current.keyFor(req))),
    onSuccess: (s) => {
      intent.current.complete();
      setResult(s);
      setBody('');
      setSelected(null);
      setErrors([]);
      void queryClient.invalidateQueries({ queryKey: reviewKeys.submissions(run.id) });
      void queryClient.invalidateQueries({ queryKey: reviewKeys.threadsOfRun(run.id) });
      void queryClient.invalidateQueries({ queryKey: directiveKeys.all(run.id) });
      void queryClient.invalidateQueries({ queryKey: runKeys.detail(run.id), exact: true });
      if (s.verdict === 'REQUEST_CHANGES' && s.directive_id) setTrackOpen(true);
    },
    onError: (e) => {
      setErrors([problemMessage(t, e)]);
      if (isApiError(e) && e.status === 409) void queryClient.invalidateQueries({ queryKey: runKeys.detail(run.id), exact: true });
      if (isApiError(e) && e.status === 403) void queryClient.invalidateQueries({ queryKey: directiveKeys.approvals(run.id) });
    },
  });

  const canSubmit = can(role, 'review:submit');
  const terminal = RUN_TERMINAL.includes(run.status);
  const blockedOf = (v: ReviewVerdict) => options.find((o) => o.verdict === v)?.blocked ?? 'role';
  const effectiveApproval = approvalId || (decidable.length === 1 ? (decidable[0]?.id ?? '') : '');
  const chosenApproval = pending.find((a) => a.id === effectiveApproval) ?? null;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const next: string[] = [];
    const text = body.trim();
    if (blockedOf(verdict) !== null) next.push(t('review.submit.errors.verdict'));
    if (verdict === 'COMMENT' && !text) next.push(t('review.submit.errors.body'));
    if (verdict === 'APPROVE' && !effectiveApproval) next.push(t('review.submit.errors.approval'));
    if (verdict === 'REQUEST_CHANGES' && !text && chosen.length === 0) next.push(t('review.submit.errors.changes'));
    setErrors(next);
    if (next.length > 0) return;
    const req: ReviewSubmitRequest = {
      verdict,
      expected_run_version: run.version,
      ...(text ? { body: text } : {}),
      ...(verdict !== 'APPROVE' && chosen.length > 0 ? { thread_ids: chosen } : {}),
      ...(verdict === 'APPROVE' ? { approval_id: effectiveApproval } : {}),
      ...(verdict === 'REQUEST_CHANGES' ? { requires_impact_preview: preview, ...(target === 'task' && scopeTask ? { task_id: scopeTask.id } : {}) } : {}),
    };
    submit.mutate(req);
  };

  const history = (
    <section aria-labelledby={`${id}-history`} className="flex flex-col gap-2">
      <h3 id={`${id}-history`} className="text-sm font-semibold">
        {t('review.submitted.title')}
      </h3>
      {submissions.isPending ? (
        <LoadingState inline label={t('review.submitted.loading')} />
      ) : submissions.isError ? (
        <ErrorState error={submissions.error} onRetry={() => void submissions.refetch()} />
      ) : submissions.data.items.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('review.submitted.empty')}</p>
      ) : (
        <ul aria-label={t('review.submitted.title')} data-testid="review-submissions">
          {submissions.data.items.map((s) => (
            <SubmissionItem key={s.id} s={s} workspaceId={workspaceId} />
          ))}
        </ul>
      )}
    </section>
  );

  if (!canSubmit) {
    return (
      <div className="flex flex-col gap-4">
        <Callout tone="neutral" title={t('review.submit.readOnlyTitle')}>
          {t('review.submit.readOnly')}
        </Callout>
        {history}
      </div>
    );
  }
  if (terminal) {
    return (
      <div className="flex flex-col gap-4">
        <Callout tone="neutral" title={t('review.submit.terminalTitle')}>
          {t('review.submit.terminal')}
        </Callout>
        {history}
      </div>
    );
  }

  const label = { COMMENT: t('review.submit.doComment'), APPROVE: t('review.submit.doApprove'), REQUEST_CHANGES: t('review.submit.doRequestChanges') }[verdict];
  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4" aria-label={t('review.submit.title')}>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-semibold">{t('review.submit.verdict')}</legend>
          {options.map((o) => {
            const rid = `${id}-verdict-${o.verdict}`;
            return (
              <div key={o.verdict} className="flex items-start gap-2">
                <input
                  type="radio"
                  id={rid}
                  name={`${id}-verdict`}
                  className="mt-1 h-4 w-4 accent-[rgb(var(--color-accent))]"
                  checked={verdict === o.verdict}
                  disabled={o.blocked !== null}
                  aria-describedby={`${rid}-desc`}
                  onChange={() => setVerdict(o.verdict)}
                />
                <div>
                  <label htmlFor={rid} className="text-sm font-medium">
                    {t(`review.verdict.${o.verdict}`)}
                  </label>
                  <p id={`${rid}-desc`} className="text-xs text-fg-muted">
                    {o.blocked ? t(`review.submit.blocked.${o.blocked}`) : t(`review.submit.hint.${o.verdict}`)}
                  </p>
                </div>
              </div>
            );
          })}
        </fieldset>

        {approvals.isPending ? (
          <LoadingState inline label={t('review.submit.approvalsLoading')} />
        ) : approvals.isError ? (
          <ErrorState error={approvals.error} onRetry={() => void approvals.refetch()} />
        ) : null}

        {verdict === 'APPROVE' ? (
          <div className="flex flex-col gap-2" data-testid="approve-target">
            {decidable.length > 1 ? (
              <>
                <label htmlFor={`${id}-approval`} className="text-sm font-medium">
                  {t('review.submit.approval')}
                </label>
                <select id={`${id}-approval`} className="select min-h-touch" value={approvalId} onChange={(e) => setApprovalId(e.target.value)}>
                  <option value="">{t('review.submit.chooseApproval')}</option>
                  {decidable.map((a) => (
                    <option key={a.id} value={a.id}>
                      {t(`approvals.enums.action.${a.action_type}`, { defaultValue: a.action_type })}: {a.summary}
                    </option>
                  ))}
                </select>
              </>
            ) : null}
            {chosenApproval ? (
              <div className="rounded-md border border-border bg-surface-muted p-3 text-sm">
                <p className="mb-1 text-xs font-semibold uppercase text-fg-muted">{t('review.submit.approvesWhat')}</p>
                <ApprovalSummary approval={chosenApproval} />
                <Link className="mt-1 inline-block text-xs text-link underline" to={`/w/${workspaceId}/approvals/${chosenApproval.id}`}>
                  {t('review.submit.openApproval')}
                </Link>
              </div>
            ) : null}
          </div>
        ) : null}

        {verdict === 'REQUEST_CHANGES' ? (
          <div className="flex flex-col gap-3" data-testid="request-changes-target">
            <Callout tone="info" title={t('review.submit.directiveTitle')}>
              {t('review.submit.directiveHint')}
            </Callout>
            <fieldset className="flex flex-col gap-1">
              <legend className="mb-1 text-sm font-medium">{t('review.submit.target')}</legend>
              {scopeTask ? (
                <div className="flex items-center gap-2">
                  <input
                    type="radio"
                    id={`${id}-target-task`}
                    name={`${id}-target`}
                    className="h-4 w-4 accent-[rgb(var(--color-accent))]"
                    checked={target === 'task'}
                    onChange={() => setTarget('task')}
                  />
                  <label htmlFor={`${id}-target-task`} className="break-words text-sm">
                    {t('review.submit.targetTask', { title: scopeTask.title })}
                  </label>
                </div>
              ) : null}
              <div className="flex items-center gap-2">
                <input
                  type="radio"
                  id={`${id}-target-run`}
                  name={`${id}-target`}
                  className="h-4 w-4 accent-[rgb(var(--color-accent))]"
                  checked={target === 'run'}
                  onChange={() => setTarget('run')}
                />
                <label htmlFor={`${id}-target-run`} className="text-sm">
                  {t('review.submit.targetRun')}
                </label>
              </div>
            </fieldset>
            <div className="flex items-start gap-2">
              <input
                id={`${id}-preview`}
                type="checkbox"
                className="checkbox mt-1"
                checked={preview}
                aria-describedby={`${id}-preview-hint`}
                onChange={(e) => setPreview(e.target.checked)}
              />
              <div>
                <label htmlFor={`${id}-preview`} className="text-sm">
                  {t('review.submit.preview')}
                </label>
                <p id={`${id}-preview-hint`} className="text-xs text-fg-muted">
                  {t('review.submit.previewHint')}
                </p>
              </div>
            </div>
          </div>
        ) : null}

        {verdict !== 'APPROVE' ? (
          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-sm font-medium">{t('review.submit.threads', { count: chosen.length })}</legend>
            {openThreads.length === 0 ? (
              <p className="text-xs text-fg-muted">{t('review.submit.noOpenThreads')}</p>
            ) : (
              openThreads.slice(0, REVIEW_THREADS_MAX).map((th) => (
                <div key={th.id} className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    id={`${id}-th-${th.id}`}
                    className="checkbox mt-1"
                    checked={chosen.includes(th.id)}
                    onChange={(e) => setSelected(e.target.checked ? [...chosen, th.id] : chosen.filter((x) => x !== th.id))}
                  />
                  <label htmlFor={`${id}-th-${th.id}`} className="min-w-0 break-all font-mono text-xs">
                    {anchorLabel(th)}
                  </label>
                </div>
              ))
            )}
          </fieldset>
        ) : null}

        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-body`} className="text-sm font-medium">
            {verdict === 'COMMENT' ? t('review.submit.bodyRequired') : t('review.submit.bodyOptional')}
          </label>
          <textarea id={`${id}-body`} className="input min-h-[5rem]" value={body} maxLength={REVIEW_BODY_MAX} onChange={(e) => setBody(e.target.value)} />
        </div>
        <p className="text-xs text-fg-muted">{t('review.submit.version', { version: run.version })}</p>
        <FormAlert messages={errors} />
        <div>
          <Button type="submit" variant={verdict === 'APPROVE' ? 'primary' : 'secondary'} loading={submit.isPending}>
            {label}
          </Button>
        </div>
        {stepUp.dialog}
      </form>
      {result ? (
        <p role="status" className="rounded-md border border-success-border bg-success-soft p-3 text-sm text-success-fg" data-testid="review-result">
          {t(`review.submit.done.${result.verdict}`)}
          {result.approval_status ? ` ${t('review.submit.doneApproval', { status: t(`approvals.enums.status.${result.approval_status}`, { defaultValue: result.approval_status }) })}` : ''}
          {result.directive_id ? (
            <>
              {' '}
              <Link className="underline" to={`/w/${workspaceId}/mission-control?${new URLSearchParams({ run: run.id, view: 'directives' }).toString()}`}>
                {t('review.submit.openDirective')}
              </Link>
            </>
          ) : null}
        </p>
      ) : null}
      {history}
      <Dialog open={trackOpen} onOpenChange={setTrackOpen}>
        {trackOpen && result?.directive_id ? (
          <DirectiveTrackContent
            runId={run.id}
            directiveId={result.directive_id}
            type="CHANGE_REQUEST"
            taskTitle={(taskId) => (scopeTask && scopeTask.id === taskId ? scopeTask.title : undefined)}
            taskHref={(taskId) => `/w/${workspaceId}/projects/${run.project_id}/runs/${run.id}/tasks/${taskId}`}
            approvalsHref={`/w/${workspaceId}/approvals`}
            canConfirm={can(role, 'directive:confirm')}
          />
        ) : null}
      </Dialog>
    </div>
  );
}
