import { useQuery } from '@tanstack/react-query';
import { lazy, Suspense, useId, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MAX_INLINE_ARTIFACT_BYTES, artifactDownloadPath, taskKeys, tasksApi, type Evidence } from '../../api/tasks';
import { Badge, CodeBlock, EmptyState, ErrorState, LoadingState, cx } from '../../components/ui';
import { languageForPath, parseUnifiedDiff } from './diffModel';

const MonacoDiff = lazy(() => import('./MonacoDiff'));

type Mode = 'split' | 'inline' | 'text';

function formatBytes(n: number, locale: string): string {
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(v)} ${units[i]}`;
}

function DiffArtifact({ evidence, canDownload }: { evidence: Evidence; canDownload: boolean }) {
  const { t, i18n } = useTranslation();
  const id = useId();
  const artifact = evidence.artifact;
  const tooLarge = artifact.size_bytes > MAX_INLINE_ARTIFACT_BYTES;
  const text = useQuery({
    queryKey: taskKeys.artifact(artifact.id),
    queryFn: ({ signal }) => tasksApi.artifactText(artifact.id, signal),
    enabled: !tooLarge && canDownload,
    staleTime: Infinity,
  });
  const files = useMemo(() => (text.data ? parseUnifiedDiff(text.data) : []), [text.data]);
  const [selected, setSelected] = useState(0);
  const [mode, setMode] = useState<Mode>('split');
  const file = files[Math.min(selected, Math.max(0, files.length - 1))];

  return (
    <section aria-labelledby={`${id}-title`} className="flex flex-col gap-3" data-testid="diff-artifact" data-artifact-id={artifact.id}>
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 id={`${id}-title`} className="font-semibold">
          {t('tasks.diff.artifactTitle', { commit: evidence.commit_sha?.slice(0, 12) ?? t('tasks.diff.noCommit') })}
        </h3>
        <span className="text-sm text-fg-muted">{formatBytes(artifact.size_bytes, i18n.language)}</span>
        {canDownload ? (
          <a className="link text-sm" href={artifactDownloadPath(artifact.id)} download>
            {t('tasks.diff.download')}
          </a>
        ) : null}
      </div>
      {!canDownload ? (
        <p className="text-sm text-fg-muted">{t('tasks.diff.noDownloadPermission')}</p>
      ) : tooLarge ? (
        <p className="text-sm text-fg-muted">{t('tasks.diff.tooLarge', { size: formatBytes(MAX_INLINE_ARTIFACT_BYTES, i18n.language) })}</p>
      ) : text.isPending ? (
        <LoadingState label={t('tasks.diff.loading')} />
      ) : text.isError ? (
        <ErrorState error={text.error} onRetry={() => void text.refetch()} title={t('tasks.diff.loadError')} />
      ) : files.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('tasks.diff.unparsable')}</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[16rem,minmax(0,1fr)]">
          <nav aria-label={t('tasks.diff.filesLabel')}>
            <ul className="flex flex-col gap-1" data-testid="diff-files">
              {files.map((f, i) => (
                <li key={`${f.path}-${i}`}>
                  <button
                    type="button"
                    aria-current={i === selected ? 'true' : undefined}
                    onClick={() => setSelected(i)}
                    className={cx(
                      'flex min-h-touch w-full flex-col items-start rounded-md border px-2 py-1 text-left text-sm',
                      i === selected ? 'border-accent bg-accent-soft text-accent-soft-fg' : 'border-border bg-surface hover:bg-surface-muted',
                    )}
                  >
                    <span className="break-all font-mono text-xs">{f.path}</span>
                    <span className="text-xs">
                      {t(`tasks.diff.status.${f.status}`)} · {t('tasks.diff.stats', { add: f.additions, del: f.deletions })}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          {file ? (
            <div className="flex min-w-0 flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <p className="break-all font-mono text-sm font-semibold" data-testid="diff-current-file">
                  {file.status === 'renamed' && file.oldPath ? `${file.oldPath} → ${file.path}` : file.path}
                </p>
                <Badge tone="success">+{file.additions}</Badge>
                <Badge tone="danger">−{file.deletions}</Badge>
              </div>
              <div role="group" aria-label={t('tasks.diff.modeLabel')} className="inline-flex self-start rounded-md border border-border">
                {(['split', 'inline', 'text'] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    aria-pressed={mode === m}
                    onClick={() => setMode(m)}
                    className={cx(
                      'min-h-touch px-3 text-sm font-medium first:rounded-l-md last:rounded-r-md',
                      mode === m ? 'bg-accent text-accent-fg' : 'bg-surface text-fg hover:bg-surface-muted',
                    )}
                  >
                    {t(`tasks.diff.mode.${m}`)}
                  </button>
                ))}
              </div>
              {file.binary ? (
                <p className="text-sm text-fg-muted">{t('tasks.diff.binary')}</p>
              ) : mode === 'text' ? (
                <CodeBlock code={file.raw} label={file.path} language="diff" maxHeightClass="max-h-[60vh]" />
              ) : (
                <>
                  <p className="text-xs text-fg-muted">{t('tasks.diff.a11yHint')}</p>
                  <Suspense fallback={<LoadingState label={t('tasks.diff.editorLoading')} />}>
                    <MonacoDiff
                      original={file.original}
                      modified={file.modified}
                      language={languageForPath(file.path)}
                      modelPath={`${artifact.id}/${selected}/${file.path}`}
                      ariaLabel={t('tasks.diff.editorLabel', { path: file.path })}
                      sideBySide={mode === 'split'}
                    />
                  </Suspense>
                </>
              )}
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

/** Diff viewer задачи: артефакты git_diff из evidence задачи; Monaco (лениво) или текстовый вид. */
export function DiffViewer({ evidence, canDownload }: { evidence: readonly Evidence[]; canDownload: boolean }) {
  const { t } = useTranslation();
  const diffs = evidence.filter((e) => e.artifact.kind === 'git_diff');
  if (diffs.length === 0) return <EmptyState headingLevel={3} title={t('tasks.diff.emptyTitle')} description={t('tasks.diff.emptyDescription')} />;
  return (
    <div className="flex flex-col gap-6">
      {diffs.map((e) => (
        <DiffArtifact key={e.id} evidence={e} canDownload={canDownload} />
      ))}
    </div>
  );
}
