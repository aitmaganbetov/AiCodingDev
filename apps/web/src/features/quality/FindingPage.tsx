import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router';
import { findingKeys, findingsApi } from '../../api/findings';
import { runKeys, runsApi } from '../../api/runs';
import { PageHeading } from '../../components/PageHeading';
import { ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { Callout } from '../../components/ui/Badge';
import { Timestamp } from '../../components/ui/Time';
import { useCurrentWorkspace } from '../../workspace';
import { FindingTransitionActions } from './FindingTransitions';
import { Field, FieldList, Hash, SeverityBadge, TextLink, workspacePath } from './shared';

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
      <FindingTransitionActions finding={f} profile={run.data?.quality_profile} />
    </section>
  );
}
