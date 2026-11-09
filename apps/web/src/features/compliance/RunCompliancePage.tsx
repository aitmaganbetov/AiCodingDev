import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router';
import { IdempotentIntent, isApiError } from '../../api/client';
import {
  canReviewCompliance,
  complianceApi,
  complianceKeys,
  COMPLIANCE_STATUSES,
  isComplianceReviewable,
  type ComplianceRow,
  type ComplianceStatus,
} from '../../api/compliance';
import { qualityApi, qualityKeys, type Evidence } from '../../api/quality';
import { runKeys, runsApi } from '../../api/runs';
import { FormAlert, RadioGroup, TextAreaField } from '../../components/form';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { ErrorState, LoadingState } from '../../components/states';
import { RUN_TERMINAL, StatusBadge } from '../../components/StatusBadge';
import { Badge, Callout } from '../../components/ui/Badge';
import { Button } from '../../components/ui/Button';
import { Dialog, DialogClose, DialogContent } from '../../components/ui/Dialog';
import { Table, type Column } from '../../components/ui/Table';
import { Timestamp } from '../../components/ui/Time';
import { useToast } from '../../components/ui/Toast';
import { VerificationBadge } from '../../components/ui/VerificationBadge';
import { Icon } from '../../components/ui/Icon';
import { mapProblemToFields } from '../../lib/problem';
import { useCurrentWorkspace } from '../../workspace';
import { LiveStatus, useRunInvalidation } from '../quality/live';
import { ReportPanel } from '../quality/ReportPanel';
import { Hash, runViewPath, TextLink, useWorkspaceRole, workspacePath } from '../quality/shared';
import { useRunEvidenceStatus } from '../quality/useRunEvidenceStatus';
import { ComplianceDisclaimer } from './ComplianceDisclaimer';
import { evidenceGap, filterRows, rowVerification, type EvidenceGap } from './model';

export const COMPLIANCE_PAGE_SIZE = 100;
export const NOTES_MAX = 4000;
export const EVIDENCE_MAX = 20;
export const EVIDENCE_PAGE = 100;
const COMPLIANCE_EVENT_TYPES = ['gate.completed', 'run.completed', 'run.state_changed'] as const;

type ReviewField = 'status' | 'notes' | 'evidence_ids';

function ReviewDialog({ runId, row, onClose }: { runId: string; row: ComplianceRow; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const formRef = useRef<HTMLFormElement>(null);
  const [status, setStatus] = useState<'PASS' | 'FAIL'>('PASS');
  const [notes, setNotes] = useState('');
  const [selected, setSelected] = useState<string[]>(row.evidence_id ? [row.evidence_id] : []);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<ReviewField, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);
  // Кандидаты — все evidence этого run (listRunEvidence, OQ-45 (c)), постранично.
  const report = useInfiniteQuery({
    queryKey: qualityKeys.runEvidence(runId, { limit: EVIDENCE_PAGE }),
    queryFn: ({ pageParam, signal }) => qualityApi.runEvidence(runId, { limit: EVIDENCE_PAGE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const candidates: Evidence[] = useMemo(() => report.data?.pages.flatMap((p) => p.items) ?? [], [report.data]);
  const byId = useMemo(() => new Map(candidates.map((e) => [e.id, e])), [candidates]);

  const review = useMutation({
    mutationFn: (body: { status: 'PASS' | 'FAIL'; notes: string; evidence_ids: string[] }) =>
      complianceApi.review(runId, row.control_id, body, intent.current.keyFor({ control: row.control_id, ...body })),
    onSuccess: (updated) => {
      intent.current.complete();
      void queryClient.invalidateQueries({ queryKey: complianceKeys.all(runId) });
      toast({ title: t('compliance.review.done', { control: updated.control_code, status: t(`compliance.enums.status.${updated.status}`) }), tone: 'success' });
      onClose();
    },
    onError: (error) => {
      if (isApiError(error) && error.status === 409) void queryClient.invalidateQueries({ queryKey: complianceKeys.all(runId) });
      const mapped = mapProblemToFields(t, error, ['status', 'notes', 'evidence_ids'] as const);
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
    },
  });

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : prev.length >= EVIDENCE_MAX ? prev : [...prev, id]));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const errors: Partial<Record<ReviewField, string>> = {};
    const trimmed = notes.trim();
    if (!trimmed) errors.notes = t('validation.required');
    else if (trimmed.length > NOTES_MAX) errors.notes = t('validation.maxLength', { max: NOTES_MAX });
    if (selected.length === 0) errors.evidence_ids = t('compliance.review.evidenceRequired');
    else if (status === 'PASS' && byId.get(selected[0] ?? '')?.verification_status !== 'VERIFIED') {
      errors.evidence_ids = t('compliance.review.passNeedsVerified');
    }
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"], input[type="checkbox"]')?.focus());
      return;
    }
    review.mutate({ status, notes: trimmed, evidence_ids: selected });
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="lg" title={t('compliance.review.title', { control: row.control_code })} description={t('compliance.review.description')}>
        <form ref={formRef} onSubmit={submit} noValidate className="flex flex-col gap-4">
          <FormAlert messages={formErrors} />
          <RadioGroup
            name="compliance-verdict"
            legend={t('compliance.review.verdict')}
            value={status}
            onChange={setStatus}
            error={fieldErrors.status}
            options={[
              { value: 'PASS', label: t('compliance.enums.status.PASS'), description: t('compliance.review.passHint') },
              { value: 'FAIL', label: t('compliance.enums.status.FAIL') },
            ]}
          />
          <fieldset className="flex flex-col gap-2" aria-describedby={fieldErrors.evidence_ids ? 'review-evidence-error' : 'review-evidence-hint'} aria-invalid={fieldErrors.evidence_ids ? true : undefined}>
            <legend className="mb-1 text-sm font-medium">{t('compliance.review.evidence')}</legend>
            <p id="review-evidence-hint" className="text-sm text-fg-muted">
              {t('compliance.review.evidenceHint')}
            </p>
            {report.isPending ? (
              <LoadingState inline label={t('compliance.review.evidenceLoading')} />
            ) : report.isError && candidates.length === 0 ? (
              <ErrorState error={report.error} onRetry={() => void report.refetch()} />
            ) : candidates.length === 0 ? (
              <p className="text-sm text-fg-muted">{t('compliance.review.noEvidence')}</p>
            ) : (
              <ul className="flex max-h-64 flex-col gap-2 overflow-y-auto">
                {candidates.map((ev) => {
                  const id = `review-ev-${ev.id}`;
                  const order = selected.indexOf(ev.id);
                  return (
                    <li key={ev.id} className="flex items-start gap-2">
                      <input
                        id={id}
                        type="checkbox"
                        className="mt-1 h-4 w-4"
                        checked={order >= 0}
                        onChange={() => toggle(ev.id)}
                        aria-invalid={fieldErrors.evidence_ids ? true : undefined}
                      />
                      <label htmlFor={id} className="text-sm">
                        <span className="font-mono text-xs">{ev.kind}</span> · {ev.producer} ·{' '}
                        {t(`quality.enums.verification.${ev.verification_status}`)} · sha256 <Hash value={ev.artifact.sha256} />
                        {order === 0 ? <span className="ml-1 font-semibold">({t('compliance.review.primary')})</span> : null}
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
            {report.hasNextPage || report.isFetchNextPageError ? (
              <LoadMore
                shown={candidates.length}
                hasNextPage={report.hasNextPage}
                isFetchingNextPage={report.isFetchingNextPage}
                nextPageError={report.isFetchNextPageError ? report.error : null}
                onLoadMore={() => void report.fetchNextPage()}
              />
            ) : null}
            {fieldErrors.evidence_ids ? (
              <p id="review-evidence-error" className="text-sm font-medium text-danger-text">
                {fieldErrors.evidence_ids}
              </p>
            ) : null}
          </fieldset>
          <TextAreaField
            id="compliance-notes"
            label={t('compliance.review.notes')}
            hint={t('compliance.review.notesHint')}
            error={fieldErrors.notes}
            value={notes}
            maxLength={NOTES_MAX}
            required
            onChange={(e) => setNotes(e.target.value)}
          />
          <div className="flex flex-wrap justify-end gap-3">
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button type="submit" variant="primary" loading={review.isPending}>
              {t('compliance.review.submit')}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function Matrix({ runId, workspaceId, terminal }: { runId: string; workspaceId: string; terminal: boolean }) {
  const { t } = useTranslation();
  const role = useWorkspaceRole();
  const [status, setStatus] = useState<ComplianceStatus | ''>('');
  const [standard, setStandard] = useState('');
  const [reviewRow, setReviewRow] = useState<ComplianceRow | null>(null);
  const [controlQuery, setControlQuery] = useState('');
  const [missingOnly, setMissingOnly] = useState(false);
  const evidence = useRunEvidenceStatus(runId);
  const query = { ...(status ? { status } : {}), ...(standard ? { standard_code: standard } : {}) };
  const matrix = useInfiniteQuery({
    queryKey: complianceKeys.run(runId, query),
    queryFn: ({ pageParam, signal }) =>
      complianceApi.run(runId, { ...query, limit: COMPLIANCE_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const first = matrix.data?.pages[0];
  const rows = useMemo(() => matrix.data?.pages.flatMap((p) => p.items) ?? [], [matrix.data]);
  const standards = useMemo(() => [...new Set(rows.map((r) => r.standard_code))].sort(), [rows]);
  const statusOf = evidence.statusOf;
  const gapOf = (r: ComplianceRow): EvidenceGap => evidenceGap(r, r.evidence_id ? statusOf(r.evidence_id) : undefined);
  const visible = useMemo(() => filterRows(rows, { q: controlQuery, missingOnly }, statusOf), [rows, controlQuery, missingOnly, statusOf]);
  const gaps = useMemo(() => {
    let missing = 0;
    let unverified = 0;
    for (const r of rows) {
      const g = evidenceGap(r, r.evidence_id ? statusOf(r.evidence_id) : undefined);
      if (g === 'missing') missing += 1;
      if (g === 'unverified') unverified += 1;
    }
    return { missing, unverified };
  }, [rows, statusOf]);
  const mayReview = canReviewCompliance(role) && !terminal;

  const columns: Column<ComplianceRow>[] = [
    {
      id: 'standard',
      header: t('compliance.col.standard'),
      cell: (r) => (
        <span>
          {r.standard_code} <span className="text-fg-muted">({r.edition})</span>
        </span>
      ),
      sortValue: (r) => r.standard_code,
    },
    {
      id: 'control',
      header: t('compliance.col.control'),
      rowHeader: true,
      cell: (r) => (
        <span>
          <span className="font-mono text-xs">{r.control_code}</span> <span className="text-fg-muted">§ {r.clause_ref}</span>
        </span>
      ),
      sortValue: (r) => r.control_code,
    },
    {
      id: 'applicability',
      header: t('compliance.col.applicability'),
      cell: (r) => t(`compliance.enums.applicability.${r.applicability}`, { defaultValue: r.applicability }),
    },
    { id: 'role', header: t('compliance.col.role'), cell: (r) => (r.agent_role ? t(`enums.agent_role.${r.agent_role}`, { defaultValue: r.agent_role }) : '—') },
    {
      id: 'check',
      header: t('compliance.col.check'),
      cell: (r) => (
        <span>
          {r.verification_method ? t(`compliance.enums.method.${r.verification_method}`) : '—'}
          {r.gate_type ? <span className="block font-mono text-xs text-fg-muted">{r.gate_type}</span> : null}
        </span>
      ),
    },
    {
      id: 'evidence',
      header: t('compliance.col.evidence'),
      cell: (r) => {
        const gap = gapOf(r);
        return (
          <div className="flex flex-col items-start gap-1" data-evidence-gap={gap}>
            {r.evidence_id ? (
              <TextLink to={workspacePath(workspaceId, `quality/evidence/${r.evidence_id}`)} srSuffix={r.control_code}>
                {t('quality.gate.openEvidence')}
              </TextLink>
            ) : gap === 'missing' ? null : (
              <span className="text-fg-muted">{t('quality.gate.noEvidence')}</span>
            )}
            {gap === 'missing' ? (
              <Badge tone="warning" icon="verifyInsufficient">
                {t('compliance.evidence.missing')}
              </Badge>
            ) : gap === 'unverified' ? (
              <Badge tone="warning" icon="verifyInsufficient">
                {t('compliance.evidence.unverified', { status: t(`quality.enums.verification.${statusOf(r.evidence_id ?? '') ?? 'PENDING'}`) })}
              </Badge>
            ) : gap === 'unknown' ? (
              <span className="text-xs text-fg-muted">{t('compliance.evidence.unknown')}</span>
            ) : null}
            {r.gate_execution_id ? <span className="font-mono text-xs text-fg-muted">{t('compliance.evidence.gateExecution', { id: r.gate_execution_id.slice(0, 8) })}</span> : null}
          </div>
        );
      },
      sortValue: (r) => gapOf(r),
    },
    {
      id: 'state',
      header: t('compliance.col.state'),
      cell: (r) => <VerificationBadge result={rowVerification(r, r.evidence_id ? statusOf(r.evidence_id) : undefined)} />,
      sortValue: (r) => rowVerification(r, r.evidence_id ? statusOf(r.evidence_id) : undefined).state,
    },
    {
      id: 'verdict',
      header: t('compliance.col.verdict'),
      cell: (r) => (
        <div className="flex flex-col items-start gap-1">
          <StatusBadge kind="compliance" status={r.status} labelKey={`compliance.enums.status.${r.status}`} />
          {r.blocking ? <span className="text-xs font-semibold text-danger-text">{t('compliance.blocking')}</span> : null}
          {r.reviewed_by ? <span className="text-xs text-fg-muted">{t('compliance.reviewedBy', { name: r.reviewed_by.display_name })}</span> : null}
          {r.checked_at ? (
            <span className="text-xs text-fg-muted">
              <Timestamp value={r.checked_at} />
            </span>
          ) : null}
          {r.notes ? <span className="whitespace-pre-line break-words text-xs">{r.notes}</span> : null}
        </div>
      ),
      sortValue: (r) => r.status,
    },
    {
      id: 'actions',
      header: t('compliance.col.actions'),
      cell: (r) =>
        mayReview && isComplianceReviewable(r) ? (
          <Button size="sm" onClick={() => setReviewRow(r)}>
            {t('compliance.review.open')}
            <span className="sr-only">: {r.control_code}</span>
          </Button>
        ) : (
          <span className="text-xs text-fg-muted">—</span>
        ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      {first?.disclaimer ? (
        <Callout tone="info">
          <p data-testid="compliance-disclaimer">{first.disclaimer}</p>
        </Callout>
      ) : null}
      {!matrix.isPending && rows.length > 0 ? (
        <div
          className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface p-3 text-sm"
          data-testid="evidence-gaps"
          data-missing={gaps.missing}
          data-unverified={gaps.unverified}
        >
          <Icon name={gaps.missing + gaps.unverified > 0 ? 'verifyInsufficient' : 'verifyPass'} className="h-4 w-4 shrink-0" />
          <span>
            {gaps.missing + gaps.unverified > 0
              ? t('compliance.evidence.gapsSummary', { missing: gaps.missing, unverified: gaps.unverified })
              : t('compliance.evidence.noGaps')}
          </span>
          {!evidence.complete && !evidence.query.isPending ? <span className="text-xs text-fg-muted">{t('compliance.evidence.partial')}</span> : null}
        </div>
      ) : null}
      {first?.summary ? (
        <ul aria-label={t('compliance.summary')} className="flex flex-wrap gap-3" data-testid="compliance-summary">
          {COMPLIANCE_STATUSES.map((s) => (
            <li key={s} className="flex items-center gap-1 text-sm">
              <StatusBadge kind="compliance" status={s} labelKey={`compliance.enums.status.${s}`} />
              <span>{first.summary?.[s] ?? 0}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <div role="group" aria-label={t('compliance.filters')} className="flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="compliance-status" className="text-sm font-medium">
            {t('compliance.col.verdict')}
          </label>
          <select id="compliance-status" className="input" value={status} onChange={(e) => setStatus(e.target.value as ComplianceStatus | '')}>
            <option value="">{t('quality.finding.any')}</option>
            {COMPLIANCE_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`compliance.enums.status.${s}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="compliance-standard" className="text-sm font-medium">
            {t('compliance.col.standard')}
          </label>
          <select id="compliance-standard" className="input" value={standard} onChange={(e) => setStandard(e.target.value)}>
            <option value="">{t('quality.finding.any')}</option>
            {[...new Set([...standards, ...(standard ? [standard] : [])])].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="compliance-control" className="text-sm font-medium">
            {t('compliance.filterControl')}
          </label>
          <input id="compliance-control" type="search" className="input" value={controlQuery} onChange={(e) => setControlQuery(e.target.value)} />
        </div>
        <button
          type="button"
          className="btn-secondary aria-pressed:border-border-active aria-pressed:bg-accent-soft aria-pressed:text-accent-soft-fg"
          aria-pressed={missingOnly}
          onClick={() => setMissingOnly((v) => !v)}
        >
          {missingOnly ? <Icon name="check" className="h-4 w-4" /> : null}
          {t('compliance.evidence.missingOnly')}
        </button>
      </div>
      {terminal ? <p className="text-sm text-fg-muted">{t('compliance.review.terminal')}</p> : null}
      {!canReviewCompliance(role) ? <p className="text-sm text-fg-muted">{t('compliance.review.noRole')}</p> : null}
      {matrix.isPending ? (
        <LoadingState label={t('compliance.loading')} />
      ) : matrix.isError && rows.length === 0 ? (
        <ErrorState error={matrix.error} onRetry={() => void matrix.refetch()} />
      ) : (
        <>
          <Table
            caption={t('compliance.caption')}
            columns={columns}
            rows={visible}
            rowKey={(r) => r.control_id}
            rowProps={(r) => {
              const gap = gapOf(r);
              return {
                'data-control': r.control_code,
                'data-status': r.status,
                'data-evidence-gap': gap,
                ...(gap === 'missing' || gap === 'unverified' ? { className: 'border-t border-l-4 border-border border-l-warning align-top' } : {}),
              };
            }}
            empty={t('compliance.empty')}
            minWidthClass="min-w-[76rem]"
          />
          <LoadMore
            shown={rows.length}
            hasNextPage={matrix.hasNextPage}
            isFetchingNextPage={matrix.isFetchingNextPage}
            nextPageError={matrix.isFetchNextPageError ? matrix.error : null}
            onLoadMore={() => void matrix.fetchNextPage()}
          />
        </>
      )}
      {reviewRow ? <ReviewDialog runId={runId} row={reviewRow} onClose={() => setReviewRow(null)} /> : null}
    </div>
  );
}

export function RunCompliancePage() {
  const { t } = useTranslation();
  const { runId = '' } = useParams();
  const { workspaceId } = useCurrentWorkspace();
  const run = useQuery({ queryKey: runKeys.detail(runId), queryFn: ({ signal }) => runsApi.get(runId, signal) });
  const terminal = RUN_TERMINAL.includes(run.data?.status ?? '');
  const keys = useMemo(() => [complianceKeys.all(runId), runKeys.detail(runId)], [runId]);
  const live = useRunInvalidation(run.data && !terminal ? runId : null, COMPLIANCE_EVENT_TYPES, keys, () => terminal);
  if (!workspaceId) return null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('compliance.runTitle')}>
        <LiveStatus state={live.state} onReconnect={live.reconnect} />
      </PageHeading>
      {run.data ? (
        <div className="mb-4 flex flex-col gap-2">
          <p className="whitespace-pre-line break-words text-fg-muted">{run.data.objective}</p>
          <div className="flex flex-wrap items-center gap-4 text-sm">
            <StatusBadge kind="run" status={run.data.status} />
            <TextLink to={runViewPath(workspaceId, run.data.project_id, runId)}>{t('quality.openRun')}</TextLink>
            <TextLink to={workspacePath(workspaceId, `quality/runs/${runId}`)}>{t('compliance.openQuality')}</TextLink>
            <TextLink to={workspacePath(workspaceId, `compliance/projects/${run.data.project_id}/controls`)}>{t('compliance.openControls')}</TextLink>
          </div>
        </div>
      ) : null}
      <ComplianceDisclaimer className="mb-4" />
      <p className="mb-4 text-sm text-fg-muted">{t('compliance.trace')}</p>
      <Matrix runId={runId} workspaceId={workspaceId} terminal={terminal} />
      <section aria-labelledby="compliance-reports" className="mt-8 flex flex-col gap-3">
        <h2 id="compliance-reports" className="text-lg font-semibold">
          {t('compliance.reports.title')}
        </h2>
        <ReportPanel runId={runId} />
      </section>
    </section>
  );
}
