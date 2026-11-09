import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useSearchParams } from 'react-router';
import {
  APPROVAL_EVENT_TYPES,
  approvalKeys,
  approvalsApi,
  DECIDED_STATUSES,
  decisionReasonRequired,
  FOUR_EYES_ACTIONS,
  isApprovalExpired,
  type Approval,
  type ApprovalDecision,
  type ListApprovalsQuery,
} from '../../api/approvals';
import { IdempotentIntent, isApiError } from '../../api/client';
import { runKeys, runsApi } from '../../api/runs';
import { FormAlert, TextAreaField } from '../../components/form';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { Callout } from '../../components/ui/Badge';
import { Button } from '../../components/ui/Button';
import { CodeBlock } from '../../components/ui/CodeBlock';
import { Dialog, DialogClose, DialogContent } from '../../components/ui/Dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../components/ui/Tabs';
import { RelativeTime, Timestamp } from '../../components/ui/Time';
import { useToast } from '../../components/ui/Toast';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { useCurrentWorkspace } from '../../workspace';
import { LiveStatus, useRunInvalidation, useWorkspaceInvalidation } from '../quality/live';
import { Field, FieldList, runViewPath, SeverityBadge, TextLink, useWorkspaceRole, workspacePath } from '../quality/shared';
import { useStepUp } from '../../auth/StepUp';

export const APPROVALS_PAGE_SIZE = 25;
/** События потока workspace, меняющие список approvals (OQ-45 (c): вместо опроса раз в 30 с). */
export const APPROVAL_WORKSPACE_EVENTS = ['approval.requested', 'approval.decided'] as const;
export const DECISION_REASON_MAX = 4000;
const TABS = ['pending', 'decided'] as const;
type Tab = (typeof TABS)[number];

function approvalPath(workspaceId: string, id: string): string {
  return workspacePath(workspaceId, `approvals/${id}`);
}

function Requester({ approval }: { approval: Approval }) {
  const { t } = useTranslation();
  const r = approval.requested_by;
  const who = r.user?.display_name ?? (r.role ? t(`enums.agent_role.${r.role}`, { defaultValue: r.role }) : null);
  return (
    <span>
      {t(`approvals.enums.actor.${r.actor_type}`)}
      {who ? `: ${who}` : ''}
    </span>
  );
}

function Expiry({ approval }: { approval: Approval }) {
  const { t } = useTranslation();
  if (!approval.expires_at) return <span className="text-fg-muted">{t('approvals.noExpiry')}</span>;
  const expired = approval.status === 'PENDING' && isApprovalExpired(approval);
  return (
    <span className={expired ? 'font-semibold text-danger-text' : undefined} data-testid="approval-expiry">
      {expired ? `${t('approvals.expiredByClock')} · ` : ''}
      <RelativeTime value={approval.expires_at} />
    </span>
  );
}

function ApprovalItem({ approval, workspaceId }: { approval: Approval; workspaceId: string }) {
  const { t } = useTranslation();
  return (
    <li className="card flex flex-col gap-2" data-approval-id={approval.id} data-status={approval.status}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge kind="approval" status={approval.status} labelKey={`approvals.enums.status.${approval.status}`} />
        <span className="font-medium">{t(`approvals.enums.action.${approval.action_type}`, { defaultValue: approval.action_type })}</span>
        <SeverityBadge kind="risk" value={approval.risk_level} />
        {approval.status === 'PENDING' ? (
          <span className="text-fg-muted">{approval.can_decide ? t('approvals.canDecide') : t('approvals.cannotDecide')}</span>
        ) : null}
      </div>
      <p className="whitespace-pre-line break-words">{approval.summary}</p>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-fg-muted">
        <Requester approval={approval} />
        <span>
          {t('approvals.created')}: <Timestamp value={approval.created_at} />
        </span>
        {approval.status === 'PENDING' ? (
          <span>
            {t('approvals.expires')}: <Expiry approval={approval} />
          </span>
        ) : approval.decided_at ? (
          <span>
            {t('approvals.decidedAt')}: <Timestamp value={approval.decided_at} />
            {approval.decided_by ? ` · ${approval.decided_by.display_name}` : ''}
          </span>
        ) : null}
      </div>
      <TextLink to={approvalPath(workspaceId, approval.id)} srSuffix={approval.summary.slice(0, 80)}>
        {t('approvals.open')}
      </TextLink>
    </li>
  );
}

function ApprovalList({ workspaceId, tab, mine, runId }: { workspaceId: string; tab: Tab; mine: boolean; runId: string | null }) {
  const { t } = useTranslation();
  const query: ListApprovalsQuery = {
    status: tab === 'pending' ? ['PENDING'] : [...DECIDED_STATUSES],
    ...(mine && tab === 'pending' ? { decidable_by_me: true } : {}),
    ...(runId ? { run_id: runId } : {}),
  };
  const list = useInfiniteQuery({
    queryKey: approvalKeys.list(workspaceId, query),
    queryFn: ({ pageParam, signal }) =>
      approvalsApi.list(workspaceId, { ...query, limit: APPROVALS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // Обновления: с фильтром run — поток run, без него — поток уведомлений workspace (ApprovalsPage).
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];
  if (list.isPending) return <LoadingState label={t('approvals.loading')} />;
  if (list.isError && items.length === 0) return <ErrorState error={list.error} onRetry={() => void list.refetch()} />;
  if (items.length === 0) {
    return (
      <EmptyState
        headingLevel={3}
        icon="approval"
        title={tab === 'pending' ? t('approvals.emptyPending') : t('approvals.emptyDecided')}
        description={tab === 'pending' ? t('approvals.emptyPendingDescription') : undefined}
      />
    );
  }
  return (
    <>
      <ul className="flex flex-col gap-3" aria-label={t(`approvals.list.${tab}`)}>
        {items.map((a) => (
          <ApprovalItem key={a.id} approval={a} workspaceId={workspaceId} />
        ))}
      </ul>
      <LoadMore
        shown={items.length}
        hasNextPage={list.hasNextPage}
        isFetchingNextPage={list.isFetchingNextPage}
        nextPageError={list.isFetchNextPageError ? list.error : null}
        onLoadMore={() => void list.fetchNextPage()}
      />
    </>
  );
}

export function ApprovalsPage() {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  const [params, setParams] = useSearchParams();
  const tabParam = params.get('tab');
  const tab: Tab = TABS.includes(tabParam as Tab) ? (tabParam as Tab) : 'pending';
  const mine = params.get('mine') === '1';
  const runId = params.get('run_id');
  const keys = useMemo(() => [approvalKeys.all(workspaceId ?? '')], [workspaceId]);
  const runLive = useRunInvalidation(runId, APPROVAL_EVENT_TYPES, keys);
  const workspaceLive = useWorkspaceInvalidation(runId ? null : workspaceId, APPROVAL_WORKSPACE_EVENTS, keys);
  const live = runId ? runLive : workspaceLive;
  const update = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  if (!workspaceId) return null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('approvals.title')}>
        <LiveStatus state={live.state} onReconnect={live.reconnect} />
      </PageHeading>
      <p className="mb-4 text-fg-muted">{runId ? t('approvals.descriptionRun') : t('approvals.description')}</p>
      <div className="mb-4 flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <input
            id="approvals-mine"
            type="checkbox"
            className="h-4 w-4"
            checked={mine}
            disabled={tab !== 'pending'}
            onChange={(e) => update('mine', e.target.checked ? '1' : null)}
          />
          <label htmlFor="approvals-mine" className="text-sm">
            {t('approvals.onlyMine')}
          </label>
        </div>
        {runId ? (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span>{t('approvals.filteredByRun')}</span>
            <Button size="sm" variant="ghost" onClick={() => update('run_id', null)}>
              {t('approvals.clearRun')}
            </Button>
          </div>
        ) : null}
      </div>
      <Tabs value={tab} onValueChange={(v) => update('tab', v)}>
        <TabsList label={t('approvals.tabsLabel')}>
          {TABS.map((v) => (
            <TabsTrigger key={v} value={v}>
              {t(`approvals.tabs.${v}`)}
            </TabsTrigger>
          ))}
        </TabsList>
        {TABS.map((v) => (
          <TabsContent key={v} value={v}>
            {v === tab ? <ApprovalList workspaceId={workspaceId} tab={v} mine={mine} runId={runId} /> : null}
          </TabsContent>
        ))}
      </Tabs>
    </section>
  );
}

function DecisionDialog({ approval, decision, onClose }: { approval: Approval; decision: ApprovalDecision; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { workspaceId } = useCurrentWorkspace();
  const intent = useRef(new IdempotentIntent());
  const [reason, setReason] = useState('');
  const [fieldError, setFieldError] = useState<string | undefined>();
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const required = decisionReasonRequired(decision, approval.risk_level);

  // Утверждение критического действия (four-eyes) — со свежим вводом пароля (SC-IAM-08): диалог step-up и повтор.
  const stepUp = useStepUp();
  const decide = useMutation({
    mutationFn: (body: { decision: ApprovalDecision; reason?: string }) =>
      stepUp.run(() =>
        approvalsApi.decide(approval.id, body, intent.current.keyFor({ id: approval.id, version: approval.version, ...body }), approval.version),
      ),
    onSuccess: (updated) => {
      intent.current.complete();
      queryClient.setQueryData(approvalKeys.detail(approval.id), updated);
      void queryClient.invalidateQueries({ queryKey: approvalKeys.all(workspaceId ?? '') });
      if (updated.run_id) void queryClient.invalidateQueries({ queryKey: runKeys.detail(updated.run_id) });
      toast({ title: t(`approvals.decision.done.${updated.status}`, { defaultValue: t('approvals.decision.doneDefault') }), tone: 'success' });
      onClose();
    },
    onError: (error) => {
      // 409 (уже решено / истекло) и 412 (версия изменилась): перечитать подтверждение.
      if (isApiError(error) && (error.status === 409 || error.status === 412)) {
        void queryClient.invalidateQueries({ queryKey: approvalKeys.detail(approval.id) });
      }
      const fe = isApiError(error) ? error.errors.find((e) => e.pointer === '/reason') : undefined;
      setFieldError(fe ? t(`fieldError.${fe.code}`, { defaultValue: fe.message ?? t('fieldError.invalid') }) : undefined);
      setFormErrors(fe ? [] : [problemMessage(t, error)]);
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = reason.trim();
    if (required && !trimmed) {
      setFieldError(t('approvals.decision.reasonRequired'));
      document.getElementById('decision-reason')?.focus();
      return;
    }
    if (trimmed.length > DECISION_REASON_MAX) {
      setFieldError(t('validation.maxLength', { max: DECISION_REASON_MAX }));
      return;
    }
    setFieldError(undefined);
    setFormErrors([]);
    decide.mutate({ decision, ...(trimmed ? { reason: trimmed } : {}) });
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={t(`approvals.decision.title.${decision}`)}
        description={t(`approvals.decision.confirm.${decision}`, {
          action: t(`approvals.enums.action.${approval.action_type}`, { defaultValue: approval.action_type }),
        })}
      >
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <FormAlert messages={formErrors} />
          <p className="whitespace-pre-line break-words rounded-md border border-border p-3 text-sm">{approval.summary}</p>
          <TextAreaField
            id="decision-reason"
            label={required ? t('approvals.decision.reason') : t('approvals.decision.reasonOptional')}
            hint={required ? t('approvals.decision.reasonHintRequired') : t('approvals.decision.reasonHint')}
            error={fieldError}
            value={reason}
            maxLength={DECISION_REASON_MAX}
            required={required}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="flex flex-wrap justify-end gap-3">
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button type="submit" variant={decision === 'APPROVED' ? 'primary' : 'danger'} loading={decide.isPending}>
              {t(`approvals.decision.submit.${decision}`)}
            </Button>
          </div>
        </form>
        {stepUp.dialog}
      </DialogContent>
    </Dialog>
  );
}

function DecisionPanel({ approval }: { approval: Approval }) {
  const { t } = useTranslation();
  const role = useWorkspaceRole();
  const [decision, setDecision] = useState<ApprovalDecision | null>(null);

  if (approval.status !== 'PENDING') {
    return (
      <FieldList label={t('approvals.decisionTitle')}>
        <Field label={t('approvals.status')}>
          <StatusBadge kind="approval" status={approval.status} labelKey={`approvals.enums.status.${approval.status}`} />
        </Field>
        <Field label={t('approvals.decidedBy')}>{approval.decided_by?.display_name}</Field>
        <Field label={t('approvals.decidedAt')}>{approval.decided_at ? <Timestamp value={approval.decided_at} /> : null}</Field>
        <Field label={t('approvals.decisionReason')}>
          {approval.decision_reason ? <span className="whitespace-pre-line">{approval.decision_reason}</span> : null}
        </Field>
      </FieldList>
    );
  }
  if (!can(role, 'approval:decide')) return <p className="text-sm text-fg-muted">{t('approvals.readOnlyRole')}</p>;
  if (!approval.can_decide) {
    return (
      <Callout tone="warning" title={t('approvals.cannotDecideTitle')}>
        <p data-testid="cannot-decide">
          {FOUR_EYES_ACTIONS.includes(approval.action_type) ? t('approvals.cannotDecideFourEyes') : t('approvals.cannotDecideRole')}
        </p>
      </Callout>
    );
  }
  const expired = isApprovalExpired(approval);
  return (
    <div className="flex flex-col gap-3">
      {expired ? <Callout tone="warning">{t('approvals.expiredHint')}</Callout> : null}
      <div role="group" aria-label={t('approvals.decisionTitle')} className="flex flex-wrap gap-3">
        <Button variant="primary" icon="check" disabled={expired} onClick={() => setDecision('APPROVED')}>
          {t('approvals.approve')}
        </Button>
        <Button variant="danger" disabled={expired} onClick={() => setDecision('REJECTED')}>
          {t('approvals.reject')}
        </Button>
      </div>
      {decision ? <DecisionDialog key={decision} approval={approval} decision={decision} onClose={() => setDecision(null)} /> : null}
    </div>
  );
}

export function ApprovalDetailPage() {
  const { t } = useTranslation();
  const { approvalId = '' } = useParams();
  const { workspaceId } = useCurrentWorkspace();
  const approval = useQuery({ queryKey: approvalKeys.detail(approvalId), queryFn: ({ signal }) => approvalsApi.get(approvalId, signal) });
  const runId = approval.data?.run_id ?? null;
  const run = useQuery({ queryKey: runKeys.detail(runId ?? ''), queryFn: ({ signal }) => runsApi.get(runId ?? '', signal), enabled: Boolean(runId) });
  const keys = useMemo(
    () => [approvalKeys.detail(approvalId), approvalKeys.all(workspaceId ?? ''), ...(runId ? [runKeys.detail(runId)] : [])],
    [approvalId, workspaceId, runId],
  );
  const live = useRunInvalidation(runId, APPROVAL_EVENT_TYPES, keys);

  if (!workspaceId) return null;
  if (approval.isPending || approval.isError) {
    return (
      <section aria-labelledby="page-title">
        <PageHeading title={t('approvals.detailTitle')} />
        {approval.isPending ? <LoadingState /> : <ErrorState error={approval.error} onRetry={() => void approval.refetch()} />}
      </section>
    );
  }
  const a = approval.data;
  const context = a.context && Object.keys(a.context).length > 0 ? JSON.stringify(a.context, null, 2) : null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('approvals.detailTitle')}>{runId ? <LiveStatus state={live.state} onReconnect={live.reconnect} /> : null}</PageHeading>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span data-testid="approval-status">
          <StatusBadge kind="approval" status={a.status} labelKey={`approvals.enums.status.${a.status}`} />
        </span>
        <span className="font-medium">{t(`approvals.enums.action.${a.action_type}`, { defaultValue: a.action_type })}</span>
        <SeverityBadge kind="risk" value={a.risk_level} />
      </div>
      <div className="card mb-6 flex flex-col gap-4">
        <h2 className="text-lg font-semibold">{t('approvals.subject')}</h2>
        {/* summary/context пишет система или агент — недоверенный текст, без разметки. */}
        <p className="whitespace-pre-line break-words" data-testid="approval-summary">
          {a.summary}
        </p>
        <FieldList>
          <Field label={t('approvals.actionType')}>{t(`approvals.enums.actionHint.${a.action_type}`, { defaultValue: a.action_type })}</Field>
          <Field label={t('approvals.risk')}>
            <SeverityBadge kind="risk" value={a.risk_level} />
          </Field>
          <Field label={t('approvals.requestedBy')}>
            <Requester approval={a} />
          </Field>
          <Field label={t('approvals.created')}>
            <Timestamp value={a.created_at} />
          </Field>
          <Field label={t('approvals.expires')}>{a.expires_at ? <Expiry approval={a} /> : t('approvals.noExpiry')}</Field>
          <Field label={t('approvals.run')}>
            {runId ? (
              run.data ? (
                <span className="flex flex-wrap items-center gap-2">
                  <StatusBadge kind="run" status={run.data.status} />
                  <TextLink to={runViewPath(workspaceId, run.data.project_id, runId)}>{t('quality.openRun')}</TextLink>
                  <TextLink to={workspacePath(workspaceId, `approvals?run_id=${runId}`)}>{t('approvals.runApprovals')}</TextLink>
                </span>
              ) : (
                <code className="font-mono text-xs">{runId}</code>
              )
            ) : (
              t('approvals.noRun')
            )}
          </Field>
          {a.task_id ? (
            <Field label={t('approvals.task')}>
              <code className="font-mono text-xs">{a.task_id}</code>
            </Field>
          ) : null}
        </FieldList>
        {context ? <CodeBlock code={context} label={t('approvals.context')} language="json" maxHeightClass="max-h-72" /> : null}
        {FOUR_EYES_ACTIONS.includes(a.action_type) ? <p className="text-sm text-fg-muted">{t('approvals.fourEyesNote')}</p> : null}
      </div>
      <h2 className="mb-3 text-lg font-semibold">{t('approvals.decisionTitle')}</h2>
      <DecisionPanel approval={a} />
    </section>
  );
}
