/**
 * Тред inline-комментариев (ADR-0015): комментарии (только текст — недоверенный ввод людей), ответ, правка своего
 * комментария с историей правок, resolve/unresolve (If-Match — версия треда). Комментарии агентам не доставляются.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { Link } from 'react-router';
import { IdempotentIntent, isApiError } from '../../api/client';
import { REVIEW_BODY_MAX, reviewKeys, reviewsApi, type ReviewComment, type ReviewThread } from '../../api/reviews';
import { FormAlert } from '../../components/form';
import { Badge, Button, LoadingState, Timestamp, cx } from '../../components/ui';
import { problemMessage } from '../../lib/problem';
import { anchorLabel } from './reviewModel';

/** Ctrl/⌘+Enter отправляет форму комментария. */
export function submitOnModEnter(e: KeyboardEvent<HTMLTextAreaElement>) {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    e.currentTarget.form?.requestSubmit();
  }
}

export function errorText(t: TFunction, error: unknown): string {
  if (isApiError(error) && error.status === 412) return t('review.thread.conflict');
  return problemMessage(t, error);
}

function Revisions({ comment }: { comment: ReviewComment }) {
  const { t } = useTranslation();
  const query = useQuery({ queryKey: reviewKeys.revisions(comment.id), queryFn: ({ signal }) => reviewsApi.revisions(comment.id, signal) });
  if (query.isPending) return <LoadingState inline label={t('review.comment.historyLoading')} />;
  if (query.isError) return <FormAlert messages={[problemMessage(t, query.error)]} />;
  const items = query.data.items;
  if (items.length === 0) return <p className="text-xs text-fg-muted">{t('review.comment.historyEmpty')}</p>;
  return (
    <ol className="flex flex-col gap-2 border-l-2 border-border pl-3" aria-label={t('review.comment.historyLabel')} data-testid="comment-history">
      {items.map((r) => (
        <li key={r.revision} className="text-xs">
          <p className="text-fg-muted">
            {t('review.comment.revision', { revision: r.revision })} · <Timestamp value={r.authored_at} />
          </p>
          <p className="whitespace-pre-wrap break-words text-fg">{r.body}</p>
        </li>
      ))}
    </ol>
  );
}

function CommentView({ comment, canWrite, threadRunId }: { comment: ReviewComment; canWrite: boolean; threadRunId: string | null }) {
  const { t } = useTranslation();
  const id = useId();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(comment.body);
  const [showHistory, setShowHistory] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  const edit = useMutation({
    mutationFn: (body: string) => reviewsApi.editComment(comment.id, { body }, comment.revision),
    onSuccess: async () => {
      setEditing(false);
      setError(null);
      await queryClient.invalidateQueries({ queryKey: reviewKeys.threadsOfRun(threadRunId ?? '') });
      void queryClient.invalidateQueries({ queryKey: reviewKeys.revisions(comment.id) });
      requestAnimationFrame(() => editButton.current?.focus());
    },
    onError: (e) => {
      setError(errorText(t, e));
      if (isApiError(e) && e.status === 412) void queryClient.invalidateQueries({ queryKey: reviewKeys.threadsOfRun(threadRunId ?? '') });
    },
  });
  const edited = comment.revision > 1 || comment.edited_at !== null;
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const body = draft.trim();
    if (!body) return setError(t('review.form.bodyRequired'));
    if (body === comment.body) return setEditing(false);
    edit.mutate(body);
  };
  return (
    <li className="flex flex-col gap-1" data-comment-id={comment.id}>
      <p className="flex flex-wrap items-center gap-x-2 text-xs text-fg-muted">
        <span className="font-semibold text-fg">{comment.author.display_name}</span>
        <Timestamp value={comment.created_at} />
        {edited ? (
          <span data-testid="comment-edited">
            {t('review.comment.edited')}
            {comment.edited_at ? (
              <>
                {' '}
                <Timestamp value={comment.edited_at} />
              </>
            ) : null}
          </span>
        ) : null}
      </p>
      {editing ? (
        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-2" aria-label={t('review.comment.editLabel')}>
          <label htmlFor={`${id}-edit`} className="sr-only">
            {t('review.comment.editLabel')}
          </label>
          <textarea
            id={`${id}-edit`}
            className="input min-h-[5rem]"
            value={draft}
            maxLength={REVIEW_BODY_MAX}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={submitOnModEnter}
            // Поле правки появляется по нажатию «Изменить» — фокус сразу в нём.
            // eslint-disable-next-line jsx-a11y/no-autofocus
            autoFocus
          />
          {error ? <FormAlert messages={[error]} /> : null}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" size="sm" variant="primary" loading={edit.isPending}>
              {t('review.comment.save')}
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setEditing(false);
                setDraft(comment.body);
                setError(null);
                requestAnimationFrame(() => editButton.current?.focus());
              }}
            >
              {t('action.cancel')}
            </Button>
          </div>
        </form>
      ) : (
        // Тело комментария — недоверенный текст: только как текст (React экранирует), без markdown/ссылок.
        <p className="whitespace-pre-wrap break-words text-sm text-fg" data-testid="comment-body">
          {comment.body}
        </p>
      )}
      {!editing ? (
        <div className="flex flex-wrap gap-2">
          {comment.can_edit && canWrite ? (
            <Button
              ref={editButton}
              size="sm"
              variant="ghost"
              onClick={() => {
                setDraft(comment.body);
                setEditing(true);
              }}
            >
              {t('review.comment.edit')}
            </Button>
          ) : null}
          {edited ? (
            <Button size="sm" variant="ghost" aria-expanded={showHistory} onClick={() => setShowHistory((v) => !v)}>
              {showHistory ? t('review.comment.historyHide') : t('review.comment.history')}
            </Button>
          ) : null}
        </div>
      ) : null}
      {showHistory ? <Revisions comment={comment} /> : null}
    </li>
  );
}

export function ThreadCard({
  thread,
  canWrite,
  findingHref,
  highlighted = false,
}: {
  thread: ReviewThread;
  canWrite: boolean;
  findingHref?: string | undefined;
  highlighted?: boolean;
}) {
  const { t } = useTranslation();
  const id = useId();
  const queryClient = useQueryClient();
  const [reply, setReply] = useState('');
  const [error, setError] = useState<string | null>(null);
  const replyIntent = useRef(new IdempotentIntent());
  const statusIntent = useRef(new IdempotentIntent());
  const invalidate = () => queryClient.invalidateQueries({ queryKey: reviewKeys.threadsOfRun(thread.run_id ?? '') });

  const replyMutation = useMutation({
    mutationFn: (body: string) => reviewsApi.reply(thread.id, { body }, replyIntent.current.keyFor({ thread: thread.id, body })),
    onSuccess: async () => {
      replyIntent.current.complete();
      setReply('');
      setError(null);
      await invalidate();
    },
    onError: (e) => setError(errorText(t, e)),
  });
  const status = useMutation({
    mutationFn: (action: 'resolve' | 'unresolve') => {
      const key = statusIntent.current.keyFor({ thread: thread.id, action, version: thread.version });
      return action === 'resolve' ? reviewsApi.resolve(thread.id, thread.version, key) : reviewsApi.unresolve(thread.id, thread.version, key);
    },
    onSuccess: async () => {
      statusIntent.current.complete();
      setError(null);
      await invalidate();
    },
    onError: (e) => {
      setError(errorText(t, e));
      if (isApiError(e) && (e.status === 412 || e.status === 409)) void invalidate();
    },
  });

  const onReply = (e: FormEvent) => {
    e.preventDefault();
    const body = reply.trim();
    if (!body) return setError(t('review.form.bodyRequired'));
    replyMutation.mutate(body);
  };
  const resolved = thread.status === 'RESOLVED';
  const sideLabel = t(`review.side.${thread.anchor.side}`);

  return (
    <article
      id={`thread-${thread.id}`}
      tabIndex={-1}
      aria-labelledby={`${id}-title`}
      data-thread-id={thread.id}
      data-status={thread.status}
      data-outdated={thread.outdated === null ? 'unknown' : String(thread.outdated)}
      className={cx(
        'flex min-w-0 flex-col gap-2 rounded-md border bg-surface p-3 text-sm focus-visible:outline-none',
        highlighted ? 'border-border-active ring-2 ring-focus' : resolved ? 'border-border opacity-90' : 'border-highlight',
      )}
    >
      <header className="flex flex-wrap items-center gap-2">
        <h4 id={`${id}-title`} className="break-all font-mono text-xs font-semibold">
          {anchorLabel(thread)} <span className="font-sans font-normal text-fg-muted">({sideLabel})</span>
        </h4>
        <Badge tone={resolved ? 'success' : 'warning'}>{t(`review.status.${thread.status}`)}</Badge>
        {thread.outdated === true ? (
          <Badge tone="neutral" data-testid="thread-outdated">
            {t('review.thread.outdated')}
          </Badge>
        ) : thread.outdated === null ? (
          <span className="text-xs text-fg-muted">{t('review.thread.outdatedUnknown')}</span>
        ) : null}
        <code className="text-xs text-fg-muted" title={thread.anchor.commit_sha}>
          @{thread.anchor.commit_sha.slice(0, 7)}
        </code>
        {thread.finding_id && findingHref ? (
          <Link to={findingHref} className="text-xs text-link underline">
            {t('review.thread.finding')}
          </Link>
        ) : null}
      </header>
      {thread.outdated === true && thread.current_commit_sha ? (
        <p className="text-xs text-fg-muted">{t('review.thread.outdatedHint', { commit: thread.current_commit_sha.slice(0, 7) })}</p>
      ) : null}
      <ol className="flex flex-col gap-3" aria-label={t('review.thread.commentsLabel', { count: thread.comment_count })}>
        {thread.comments.map((c) => (
          <CommentView key={c.id} comment={c} canWrite={canWrite} threadRunId={thread.run_id} />
        ))}
      </ol>
      {resolved && thread.resolved_by ? (
        <p className="text-xs text-fg-muted">
          {t('review.thread.resolvedBy', { name: thread.resolved_by.display_name })}
          {thread.resolved_at ? (
            <>
              {' '}
              <Timestamp value={thread.resolved_at} />
            </>
          ) : null}
        </p>
      ) : null}
      {error ? <FormAlert messages={[error]} /> : null}
      {canWrite ? (
        <div className="flex flex-col gap-2 border-t border-border pt-2">
          <form onSubmit={onReply} noValidate className="flex flex-col gap-2">
            <label htmlFor={`${id}-reply`} className="text-xs font-medium text-fg">
              {t('review.thread.reply')}
            </label>
            <textarea
              id={`${id}-reply`}
              className="input min-h-[3.5rem]"
              value={reply}
              maxLength={REVIEW_BODY_MAX}
              aria-describedby={`${id}-reply-hint`}
              onChange={(e) => setReply(e.target.value)}
              onKeyDown={submitOnModEnter}
            />
            <p id={`${id}-reply-hint`} className="text-xs text-fg-muted">
              {t('review.form.notDelivered')}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button type="submit" size="sm" variant="secondary" loading={replyMutation.isPending}>
                {t('review.thread.replySubmit')}
              </Button>
              <Button size="sm" variant="ghost" loading={status.isPending} onClick={() => status.mutate(resolved ? 'unresolve' : 'resolve')}>
                {resolved ? t('review.thread.unresolve') : t('review.thread.resolve')}
              </Button>
            </div>
          </form>
        </div>
      ) : null}
    </article>
  );
}
