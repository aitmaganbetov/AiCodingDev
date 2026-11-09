/** Общие части экрана Code Intelligence (F8): состояние «нет индекса», commit, статус задания индексации. */
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { IndexingJobStatus, IndexSnapshotRef } from '../../api/codeIntel';
import { Badge, Icon, Timestamp, type Tone } from '../../components/ui';

export function Sha({ sha, length = 12 }: { sha: string | null | undefined; length?: number }) {
  const { t } = useTranslation();
  if (!sha) return <span className="text-fg-muted">{t('codeIntel.unknown')}</span>;
  return (
    <code className="font-mono text-code-sm" title={sha}>
      {sha.slice(0, length)}
    </code>
  );
}

export const JOB_TONE: Record<IndexingJobStatus, Tone> = {
  QUEUED: 'neutral',
  RUNNING: 'info',
  SUCCEEDED: 'success',
  FAILED: 'danger',
  CANCELLED: 'warning',
};

export function JobStatusBadge({ status }: { status: IndexingJobStatus }) {
  const { t } = useTranslation();
  return (
    <Badge tone={JOB_TONE[status]} data-status={status}>
      {t(`codeIntel.jobStatus.${status}`, { defaultValue: status })}
    </Badge>
  );
}

/** Снимок, к которому относится ответ B3 (commit, число файлов/символов, когда построен). */
export function SnapshotLine({ snapshot }: { snapshot: IndexSnapshotRef }) {
  const { t, i18n } = useTranslation();
  const nf = new Intl.NumberFormat(i18n.language);
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-fg-muted" data-testid="snapshot-line">
      <span>{t('codeIntel.snapshot.label')}</span>
      <Sha sha={snapshot.commit_sha} />
      {snapshot.file_count !== null ? <span>· {t('codeIntel.snapshot.files', { count: snapshot.file_count, n: nf.format(snapshot.file_count) })}</span> : null}
      {snapshot.symbol_count !== null ? <span>· {t('codeIntel.snapshot.symbols', { count: snapshot.symbol_count, n: nf.format(snapshot.symbol_count) })}</span> : null}
      {snapshot.completed_at ? (
        <span>
          · {t('codeIntel.snapshot.built')} <Timestamp value={snapshot.completed_at} />
        </span>
      ) : null}
    </p>
  );
}

/**
 * Индекса нет (нет READY-снимка): что это значит и как его получить — без нулей и демо-данных.
 * `commit` — запрошен снимок конкретного commit, которого нет (404).
 */
export function NoIndexState({
  workspaceId,
  projectId,
  indexingHref,
  commit,
  latestHref,
}: {
  workspaceId: string;
  projectId: string;
  indexingHref: string;
  commit?: string | undefined;
  latestHref?: string | undefined;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-dashed border-input bg-surface p-4 text-sm" data-testid="no-index">
      <div className="flex items-start gap-3">
        <span className="rounded-md bg-neutral-soft p-2 text-neutral-fg">
          <Icon name="layers" className="h-5 w-5" />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold text-fg">{commit ? t('codeIntel.noIndex.commitTitle', { sha: commit.slice(0, 12) }) : t('codeIntel.noIndex.title')}</h3>
          <p className="mt-1 text-fg-muted">{commit ? t('codeIntel.noIndex.commitText') : t('codeIntel.noIndex.text')}</p>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-fg">
            <li>{t('codeIntel.noIndex.step1')}</li>
            <li>{t('codeIntel.noIndex.step2')}</li>
            <li>{t('codeIntel.noIndex.step3')}</li>
          </ol>
        </div>
      </div>
      <div className="flex flex-wrap gap-3">
        {latestHref ? (
          <Link className="link" to={latestHref}>
            {t('codeIntel.noIndex.toLatest')}
          </Link>
        ) : null}
        <Link className="link" to={indexingHref}>
          {t('codeIntel.noIndex.toJobs')}
        </Link>
        <Link className="link" to={`/w/${workspaceId}/projects/${projectId}?tab=repositories`}>
          {t('codeIntel.noIndex.toRepositories')}
        </Link>
      </div>
    </div>
  );
}
