import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router';
import { artifactDownloadHref, qualityApi, qualityKeys, type Evidence } from '../../api/quality';
import { PageHeading } from '../../components/PageHeading';
import { ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { Callout } from '../../components/ui/Badge';
import { CodeBlock } from '../../components/ui/CodeBlock';
import { Timestamp } from '../../components/ui/Time';
import { can } from '../../lib/permissions';
import { useCurrentWorkspace } from '../../workspace';
import { Field, FieldList, TextLink, useWorkspaceRole, workspacePath } from './shared';

/** AC-14 (ТЗ §14): evidence содержит commit, команду, exit code, время и checksum artifact. */
export function missingEvidenceFields(e: Evidence): string[] {
  const missing: string[] = [];
  if (!e.commit_sha) missing.push('commit');
  if (!e.command) missing.push('command');
  if (e.exit_code === null || e.exit_code === undefined) missing.push('exitCode');
  if (!e.started_at) missing.push('startedAt');
  if (!e.finished_at) missing.push('finishedAt');
  if (!e.environment || Object.keys(e.environment).length === 0) missing.push('environment');
  return missing;
}

function formatBytes(n: number, locale: string): string {
  return new Intl.NumberFormat(locale, { style: 'unit', unit: n >= 1024 * 1024 ? 'megabyte' : n >= 1024 ? 'kilobyte' : 'byte', maximumFractionDigits: 1 }).format(
    n >= 1024 * 1024 ? n / (1024 * 1024) : n >= 1024 ? n / 1024 : n,
  );
}

export function EvidencePage() {
  const { t, i18n } = useTranslation();
  const { evidenceId = '' } = useParams();
  const { workspaceId } = useCurrentWorkspace();
  const role = useWorkspaceRole();
  const evidence = useQuery({ queryKey: qualityKeys.evidence(evidenceId), queryFn: ({ signal }) => qualityApi.evidence(evidenceId, signal) });

  if (!workspaceId) return null;
  if (evidence.isPending || evidence.isError) {
    return (
      <section aria-labelledby="page-title">
        <PageHeading title={t('quality.evidence.pageTitle')} />
        {evidence.isPending ? <LoadingState /> : <ErrorState error={evidence.error} onRetry={() => void evidence.refetch()} />}
      </section>
    );
  }
  const e = evidence.data;
  const missing = missingEvidenceFields(e);
  const raw = e.artifact.kind === 'raw_cli_log';
  const canDownload = can(role, raw ? 'artifact:download_raw' : 'artifact:download');
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('quality.evidence.pageTitle')} />
      <div className="mb-4 flex flex-wrap items-center gap-3 text-sm">
        <span data-testid="evidence-verification">
          <StatusBadge kind="verification" status={e.verification_status} labelKey={`quality.enums.verification.${e.verification_status}`} />
        </span>
        <span className="font-mono text-xs">{e.kind}</span>
        <TextLink to={workspacePath(workspaceId, `quality/runs/${e.run_id}?tab=evidence`)}>{t('quality.finding.backToRun')}</TextLink>
      </div>
      {e.verification_status !== 'VERIFIED' ? (
        <Callout tone="warning" className="mb-4">
          {t('quality.evidence.notVerified')}
        </Callout>
      ) : null}
      {missing.length > 0 ? (
        <Callout tone="warning" title={t('quality.evidence.incompleteTitle')} className="mb-4">
          <p data-testid="evidence-missing">{missing.map((m) => t(`quality.evidence.${m}`)).join(', ')}</p>
        </Callout>
      ) : (
        <Callout tone="success" className="mb-4">
          <p data-testid="evidence-complete">{t('quality.evidence.complete')}</p>
        </Callout>
      )}
      <div className="card mb-6 flex flex-col gap-4">
        <h2 className="text-lg font-semibold">{t('quality.evidence.execution')}</h2>
        <FieldList label={t('quality.evidence.execution')}>
          <Field label={t('quality.evidence.producer')}>{e.producer}</Field>
          <Field label={t('quality.evidence.commit')} testId="evidence-commit">
            {e.commit_sha ? <code className="break-all font-mono text-xs">{e.commit_sha}</code> : null}
          </Field>
          <Field label={t('quality.evidence.exitCode')} testId="evidence-exit-code">
            {e.exit_code === null || e.exit_code === undefined ? null : String(e.exit_code)}
          </Field>
          <Field label={t('quality.evidence.startedAt')}>{e.started_at ? <Timestamp value={e.started_at} /> : null}</Field>
          <Field label={t('quality.evidence.finishedAt')}>{e.finished_at ? <Timestamp value={e.finished_at} /> : null}</Field>
          <Field label={t('quality.evidence.createdAt')}>
            <Timestamp value={e.created_at} />
          </Field>
        </FieldList>
        {e.command ? <CodeBlock code={e.command} label={t('quality.evidence.command')} wrap /> : null}
        {e.environment && Object.keys(e.environment).length > 0 ? (
          <CodeBlock code={JSON.stringify(e.environment, null, 2)} label={t('quality.evidence.environment')} language="json" maxHeightClass="max-h-64" />
        ) : null}
      </div>
      <div className="card mb-6 flex flex-col gap-4">
        <h2 className="text-lg font-semibold">{t('quality.evidence.artifact')}</h2>
        <FieldList label={t('quality.evidence.artifact')}>
          <Field label={t('quality.evidence.artifactKind')}>{e.artifact.kind}</Field>
          <Field label={t('quality.evidence.sha256')} testId="evidence-sha256">
            <code className="break-all font-mono text-xs">{e.artifact.sha256}</code>
          </Field>
          <Field label={t('quality.evidence.size')}>{formatBytes(e.artifact.size_bytes, i18n.language)}</Field>
          <Field label={t('quality.evidence.mime')}>{e.artifact.mime_type}</Field>
        </FieldList>
        {canDownload ? (
          <a className="btn-secondary self-start" href={artifactDownloadHref(e.artifact.id)}>
            {t('quality.evidence.download')}
          </a>
        ) : (
          <p className="text-sm text-fg-muted">{t('quality.evidence.downloadForbidden')}</p>
        )}
      </div>
      {e.links && e.links.length > 0 ? (
        <div className="card flex flex-col gap-2">
          <h2 className="text-lg font-semibold">{t('quality.evidence.links')}</h2>
          <ul className="flex flex-col gap-1 text-sm">
            {e.links.map((l) => (
              <li key={`${l.target_type}:${l.target_id}:${l.relation}`}>
                <span className="font-mono text-xs">{l.relation}</span> → {l.target_type} <code className="font-mono text-xs">{l.target_id}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
