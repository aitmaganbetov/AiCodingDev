import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router';
import { IdempotentIntent, isApiError } from '../../api/client';
import {
  acceptedRiskBlockedByProfile,
  allowedFindingTransitions,
  canTransitionFinding,
  findingKeys,
  findingsApi,
  type Finding,
  type FindingTargetStatus,
} from '../../api/findings';
import { qualityKeys } from '../../api/quality';
import { runKeys, runsApi } from '../../api/runs';
import { FormAlert, TextAreaField } from '../../components/form';
import { PageHeading } from '../../components/PageHeading';
import { ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { Callout } from '../../components/ui/Badge';
import { Button } from '../../components/ui/Button';
import { Dialog, DialogClose, DialogContent } from '../../components/ui/Dialog';
import { Timestamp } from '../../components/ui/Time';
import { useToast } from '../../components/ui/Toast';
import { problemMessage } from '../../lib/problem';
import { useCurrentWorkspace } from '../../workspace';
import { Field, FieldList, Hash, SeverityBadge, TextLink, useWorkspaceRole, workspacePath } from './shared';

export const REASON_MAX = 4000;

function TransitionDialog({
  finding,
  to,
  open,
  onOpenChange,
}: {
  finding: Finding;
  to: FindingTargetStatus;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [reason, setReason] = useState('');
  const [fieldError, setFieldError] = useState<string | undefined>();
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const version = finding.version ?? 0;

  const mutation = useMutation({
    mutationFn: (body: { to_status: FindingTargetStatus; reason: string }) =>
      findingsApi.transition(finding.id, body, intent.current.keyFor({ id: finding.id, version, ...body }), version),
    onSuccess: (updated) => {
      intent.current.complete();
      queryClient.setQueryData(findingKeys.detail(finding.id), updated);
      void queryClient.invalidateQueries({ queryKey: findingKeys.all(finding.run_id ?? '') });
      void queryClient.invalidateQueries({ queryKey: qualityKeys.run(finding.run_id ?? '') });
      toast({ title: t('quality.transition.done', { status: t(`quality.enums.finding_status.${updated.status}`) }), tone: 'success' });
      onOpenChange(false);
    },
    onError: (error) => {
      // 409 (FSM ушла вперёд) / 412 (версия устарела): показать причину и перечитать находку.
      if (isApiError(error) && (error.status === 409 || error.status === 412)) {
        void queryClient.invalidateQueries({ queryKey: findingKeys.detail(finding.id) });
      }
      const fe = isApiError(error) ? error.errors.find((e) => e.pointer === '/reason') : undefined;
      setFieldError(fe ? t(`fieldError.${fe.code}`, { defaultValue: fe.message ?? t('fieldError.invalid') }) : undefined);
      setFormErrors(fe ? [] : [problemMessage(t, error)]);
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = reason.trim();
    if (!trimmed) {
      setFieldError(t('validation.required'));
      return;
    }
    if (trimmed.length > REASON_MAX) {
      setFieldError(t('validation.maxLength', { max: REASON_MAX }));
      return;
    }
    setFieldError(undefined);
    setFormErrors([]);
    mutation.mutate({ to_status: to, reason: trimmed });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={t('quality.transition.dialogTitle', { status: t(`quality.enums.finding_status.${to}`) })}
        description={t(`quality.transition.hint.${to}`)}
      >
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <FormAlert messages={formErrors} />
          <TextAreaField
            id="finding-reason"
            label={t('quality.transition.reason')}
            hint={t('quality.transition.reasonHint')}
            error={fieldError}
            value={reason}
            maxLength={REASON_MAX}
            required
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="flex flex-wrap justify-end gap-3">
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button type="submit" variant={to === 'ACCEPTED_RISK' ? 'danger' : 'primary'} loading={mutation.isPending}>
              {t(`quality.transition.to.${to}`)}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function TransitionActions({ finding, profile }: { finding: Finding; profile: string | undefined }) {
  const { t } = useTranslation();
  const role = useWorkspaceRole();
  const [target, setTarget] = useState<FindingTargetStatus | null>(null);
  const allowed = allowedFindingTransitions(finding.status);

  if (allowed.length === 0) return <p className="text-sm text-fg-muted">{t('quality.transition.engineOnly')}</p>;
  const permitted = allowed.filter((to) => canTransitionFinding(role, to));
  if (permitted.length === 0) return <p className="text-sm text-fg-muted">{t('quality.transition.noRole')}</p>;
  if (finding.version === undefined) return <p className="text-sm text-fg-muted">{t('quality.transition.noVersion')}</p>;

  return (
    <div className="flex flex-col gap-2">
      <div role="group" aria-label={t('quality.transition.group')} className="flex flex-wrap gap-3">
        {permitted.map((to) => {
          const blocked = to === 'ACCEPTED_RISK' && acceptedRiskBlockedByProfile(finding.severity, profile);
          return (
            <Button
              key={to}
              variant={to === 'ACCEPTED_RISK' ? 'danger' : 'secondary'}
              disabled={blocked}
              aria-describedby={blocked ? 'accepted-risk-blocked' : undefined}
              onClick={() => setTarget(to)}
            >
              {t(`quality.transition.to.${to}`)}
            </Button>
          );
        })}
      </div>
      {permitted.includes('ACCEPTED_RISK') && acceptedRiskBlockedByProfile(finding.severity, profile) ? (
        <p id="accepted-risk-blocked" className="text-sm text-fg-muted">
          {t('quality.transition.profileBlocked')}
        </p>
      ) : null}
      {allowed.length > permitted.length ? <p className="text-sm text-fg-muted">{t('quality.transition.someNeedAdmin')}</p> : null}
      {target ? <TransitionDialog key={target} finding={finding} to={target} open onOpenChange={(o) => !o && setTarget(null)} /> : null}
    </div>
  );
}

export function FindingPage() {
  const { t } = useTranslation();
  const { findingId = '' } = useParams();
  const { workspaceId } = useCurrentWorkspace();
  const finding = useQuery({ queryKey: findingKeys.detail(findingId), queryFn: ({ signal }) => findingsApi.get(findingId, signal) });
  const runId = finding.data?.run_id;
  const run = useQuery({
    queryKey: runKeys.detail(runId ?? ''),
    queryFn: ({ signal }) => runsApi.get(runId ?? '', signal),
    enabled: Boolean(runId),
  });

  if (!workspaceId) return null;
  if (finding.isPending) {
    return (
      <section aria-labelledby="page-title">
        <PageHeading title={t('quality.finding.pageTitle')} />
        <LoadingState />
      </section>
    );
  }
  if (finding.isError) {
    return (
      <section aria-labelledby="page-title">
        <PageHeading title={t('quality.finding.pageTitle')} />
        <ErrorState error={finding.error} onRetry={() => void finding.refetch()} />
      </section>
    );
  }
  const f = finding.data;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('quality.finding.pageTitle')} />
      <div className="card mb-6 flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <SeverityBadge value={f.severity} />
          <span data-testid="finding-status">
            <StatusBadge kind="finding" status={f.status} labelKey={`quality.enums.finding_status.${f.status}`} />
          </span>
        </div>
        {/* Текст находки — недоверенные данные (вывод инструмента / агента): только как текст. */}
        <h2 className="whitespace-pre-line break-words text-lg font-semibold" data-testid="finding-title">
          {f.title ?? f.rule_id}
        </h2>
        <FieldList>
          <Field label={t('quality.finding.rule')}>
            <span className="break-all font-mono text-xs">{f.rule_id}</span>
          </Field>
          <Field label={t('quality.finding.cwe')}>{f.cwe_id}</Field>
          <Field label={t('quality.finding.source')}>{f.source ? t(`quality.enums.finding_source.${f.source}`, { defaultValue: f.source }) : null}</Field>
          <Field label={t('quality.finding.location')}>
            {f.file_path ? <span className="break-all font-mono text-xs">{f.line ? `${f.file_path}:${f.line}` : f.file_path}</span> : null}
          </Field>
          <Field label={t('quality.finding.controls')}>{f.control_refs?.length ? f.control_refs.join(', ') : null}</Field>
          <Field label={t('quality.finding.fixedIn')}>{f.fixed_in_commit_sha ? <Hash value={f.fixed_in_commit_sha} length={40} /> : null}</Field>
          <Field label={t('quality.finding.evidence')}>
            {f.evidence_id ? <TextLink to={workspacePath(workspaceId, `quality/evidence/${f.evidence_id}`)}>{t('quality.gate.openEvidence')}</TextLink> : null}
          </Field>
          <Field label={t('quality.finding.createdAt')}>{f.created_at ? <Timestamp value={f.created_at} /> : null}</Field>
          <Field label={t('quality.finding.resolvedBy')}>{f.resolved_by?.display_name}</Field>
          <Field label={t('quality.finding.resolutionReason')}>
            {f.resolution_reason ? <span className="whitespace-pre-line">{f.resolution_reason}</span> : null}
          </Field>
          {f.run_id ? (
            <Field label={t('quality.finding.run')}>
              <TextLink to={workspacePath(workspaceId, `quality/runs/${f.run_id}?tab=findings`)}>{t('quality.finding.backToRun')}</TextLink>
            </Field>
          ) : null}
        </FieldList>
      </div>
      <h2 className="mb-3 text-lg font-semibold">{t('quality.transition.title')}</h2>
      {run.isError ? (
        <Callout tone="warning" className="mb-3">
          {t('quality.transition.profileUnknown')}
        </Callout>
      ) : null}
      <TransitionActions finding={f} profile={run.data?.quality_profile} />
    </section>
  );
}
