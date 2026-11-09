/**
 * Diff выбранного файла: Monaco side-by-side / inline (лениво, из локального пакета) или текстовый вид с
 * комментариями под строками. В режимах Monaco якорь нового комментария — выделение строк в редакторе
 * (клавиатура/мышь), обсуждения файла — списком под редактором; маркеры тредов и находок — в поле глифов.
 */
import { lazy, Suspense, useMemo, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type { Finding } from '../../api/findings';
import type { ReviewSide, ReviewThread } from '../../api/reviews';
import { Badge, Button, Callout, LoadingState, cx } from '../../components/ui';
import type { MonacoLineMarker, MonacoLineSelection } from '../tasks/MonacoDiff';
import { languageForPath, type DiffFile } from '../tasks/diffModel';
import { DiffTextView } from './DiffTextView';
import type { DiffRow, FileThreads } from './reviewModel';

const MonacoDiff = lazy(() => import('../tasks/MonacoDiff'));

export type DiffMode = 'split' | 'inline' | 'text';

export function DiffPane({
  file,
  modelKey,
  rows,
  threads,
  findingsByLine,
  mode,
  modes,
  onMode,
  canComment,
  commentBlocked,
  draftKey,
  form,
  renderThread,
  onComment,
  selection,
  onSelection,
  reveal,
  toolbar,
}: {
  file: DiffFile;
  modelKey: string;
  rows: readonly DiffRow[];
  threads: FileThreads;
  findingsByLine: ReadonlyMap<number, readonly Finding[]>;
  mode: DiffMode;
  modes: readonly DiffMode[];
  onMode: (mode: DiffMode) => void;
  canComment: boolean;
  /** Почему комментировать нельзя (роль, нет репозитория/commit) — текстом; null — можно или не нужно. */
  commentBlocked: ReactNode;
  draftKey: string | null;
  form: ReactNode;
  renderThread: (threadId: string) => ReactNode;
  onComment: (side: ReviewSide, startLine: number, endLine?: number) => void;
  selection: MonacoLineSelection | null;
  onSelection: (s: MonacoLineSelection) => void;
  reveal: { side: ReviewSide; line: number; nonce: number } | null;
  toolbar: ReactNode;
}) {
  const { t } = useTranslation();
  const markers = useMemo<MonacoLineMarker[]>(() => {
    const out: MonacoLineMarker[] = [];
    for (const list of threads.byRow.values()) for (const th of list) out.push({ side: th.anchor.side, line: th.anchor.end_line, kind: 'thread' });
    for (const th of threads.unplaced) out.push({ side: th.anchor.side, line: th.anchor.end_line, kind: 'thread' });
    for (const line of findingsByLine.keys()) out.push({ side: 'head', line, kind: 'finding' });
    return out;
  }, [threads, findingsByLine]);

  const placedIds = new Set([...threads.byRow.values()].flat().map((th) => th.id));
  const listed: ReviewThread[] = mode === 'text' ? [...threads.unplaced, ...threads.otherCommit] : threads.all;
  const sideText = (side: ReviewSide) => t(`review.side.${side}`);

  return (
    <section aria-labelledby="review-file-title" className="flex min-w-0 flex-col gap-3" data-testid="review-diff">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="review-file-title" className="min-w-0 break-all font-mono text-sm font-semibold" data-testid="review-current-file">
          {file.status === 'renamed' && file.oldPath ? `${file.oldPath} → ${file.path}` : file.path}
        </h2>
        <Badge tone="success">+{file.additions}</Badge>
        <Badge tone="danger">−{file.deletions}</Badge>
        <Badge tone="neutral">{t(`tasks.diff.status.${file.status}`)}</Badge>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label={t('review.diff.modeLabel')} className="inline-flex rounded-md border border-border">
          {modes.map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={mode === m}
              onClick={() => onMode(m)}
              className={cx(
                'min-h-touch px-3 text-sm font-medium first:rounded-l-md last:rounded-r-md',
                mode === m ? 'bg-accent text-accent-fg' : 'bg-surface text-fg hover:bg-surface-muted',
              )}
            >
              {t(`review.diff.mode.${m}`)}
            </button>
          ))}
        </div>
        {toolbar}
      </div>
      {commentBlocked}
      {file.binary ? (
        <p className="text-sm text-fg-muted">{t('tasks.diff.binary')}</p>
      ) : mode === 'text' ? (
        <DiffTextView
          path={file.path}
          rows={rows}
          threads={threads}
          findingsByLine={findingsByLine}
          canComment={canComment}
          onComment={(side, line) => onComment(side, line)}
          draftKey={draftKey}
          form={form}
          renderThread={renderThread}
        />
      ) : (
        <>
          <p className="text-xs text-fg-muted">{t('review.diff.monacoHint')}</p>
          {canComment ? (
            <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="monaco-selection">
              <span className="text-fg-muted">
                {selection
                  ? t('review.diff.selection', { start: selection.startLine, end: selection.endLine, side: sideText(selection.side) })
                  : t('review.diff.noSelection')}
              </span>
              <Button size="sm" icon="message" disabled={!selection} onClick={() => selection && onComment(selection.side, selection.startLine, selection.endLine)}>
                {t('review.diff.commentSelection')}
              </Button>
            </div>
          ) : null}
          <Suspense fallback={<LoadingState label={t('tasks.diff.editorLoading')} />}>
            <MonacoDiff
              original={file.original}
              modified={file.modified}
              language={languageForPath(file.path)}
              modelPath={modelKey}
              ariaLabel={t('tasks.diff.editorLabel', { path: file.path })}
              sideBySide={mode === 'split'}
              markers={markers}
              reveal={reveal}
              onSelectionChange={onSelection}
            />
          </Suspense>
          {draftKey && mode !== 'text' ? form : null}
        </>
      )}
      {listed.length > 0 ? (
        <section aria-labelledby="review-file-threads" className="flex flex-col gap-2">
          <h3 id="review-file-threads" className="text-sm font-semibold">
            {mode === 'text' ? t('review.diff.otherThreads', { count: listed.length }) : t('review.diff.fileThreads', { count: listed.length })}
          </h3>
          {mode === 'text' && threads.otherCommit.length > 0 ? <p className="text-xs text-fg-muted">{t('review.diff.otherCommitHint')}</p> : null}
          <div className="flex flex-col gap-2">
            {listed.map((th) => (
              <div key={th.id}>{renderThread(th.id)}</div>
            ))}
          </div>
        </section>
      ) : mode !== 'text' && placedIds.size === 0 ? (
        <p className="text-sm text-fg-muted">{t('review.diff.noFileThreads')}</p>
      ) : null}
      {mode === 'text' && threads.all.length === 0 && !draftKey ? <p className="text-sm text-fg-muted">{t('review.diff.noFileThreads')}</p> : null}
    </section>
  );
}

export function CommentBlocked({ reason }: { reason: string }) {
  const { t } = useTranslation();
  return (
    <Callout tone="neutral" title={t('review.diff.commentsUnavailable')}>
      {reason}
    </Callout>
  );
}
