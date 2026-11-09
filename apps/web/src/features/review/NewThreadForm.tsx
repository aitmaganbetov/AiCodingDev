/**
 * Новый тред inline-комментария (createReviewThread): якорь — commit просматриваемого diff, сторона, файл и
 * диапазон строк. Комментарий агентам НЕ доставляется (ADR-0015 §4): воздействие — только «Запросить изменения».
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent } from '../../api/client';
import { REVIEW_BODY_MAX, reviewKeys, reviewsApi, type ReviewSide, type ReviewThread, type ReviewThreadCreateRequest } from '../../api/reviews';
import { FormAlert } from '../../components/form';
import { Button } from '../../components/ui';
import { validateThreadDraft, type AnchorDraft, type AnchorError } from './reviewModel';
import { errorText, submitOnModEnter } from './ThreadCard';

export interface ThreadTarget {
  projectId: string;
  repositoryId: string;
  runId: string;
  taskId: string | null;
  path: string;
  headSha: string;
  baseSha: string | null;
}

export function NewThreadForm({
  target,
  initial,
  onDone,
  onCancel,
}: {
  target: ThreadTarget;
  initial: AnchorDraft;
  onDone: (thread: ReviewThread) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const [side, setSide] = useState<ReviewSide>(initial.side);
  const [start, setStart] = useState(String(initial.startLine));
  const [end, setEnd] = useState(String(initial.endLine));
  const [body, setBody] = useState('');
  const [errors, setErrors] = useState<AnchorError[]>([]);
  const [serverError, setServerError] = useState<string | null>(null);

  // Новая строка выбрана, пока форма открыта (клик по другой строке, выделение в Monaco) — якорь следует за ней,
  // введённый текст сохраняется (корректировка состояния при смене пропса — во время рендера).
  const initialKey = `${initial.side}:${initial.startLine}:${initial.endLine}`;
  const [seenInitial, setSeenInitial] = useState(initialKey);
  if (seenInitial !== initialKey) {
    setSeenInitial(initialKey);
    setSide(initial.side);
    setStart(String(initial.startLine));
    setEnd(String(initial.endLine));
  }
  useEffect(() => {
    bodyRef.current?.focus();
  }, []);

  const create = useMutation({
    mutationFn: (req: ReviewThreadCreateRequest) => reviewsApi.createThread(target.projectId, target.repositoryId, req, intent.current.keyFor(req)),
    onSuccess: async (thread) => {
      intent.current.complete();
      await queryClient.invalidateQueries({ queryKey: reviewKeys.threadsOfRun(target.runId) });
      onDone(thread);
    },
    onError: (e) => setServerError(errorText(t, e)),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const draft: AnchorDraft = { side, startLine: Number(start), endLine: Number(end) };
    const next = validateThreadDraft(draft, body, target.baseSha);
    setErrors(next);
    setServerError(null);
    if (next.length > 0) {
      const field = next.some((x) => x === 'body' || x === 'bodyTooLong') && next.length === 1 ? 'body' : next.includes('baseUnknown') ? 'side' : 'start';
      document.getElementById(`${id}-${field}`)?.focus();
      return;
    }
    create.mutate({
      anchor: {
        commit_sha: target.headSha,
        ...(target.baseSha ? { base_commit_sha: target.baseSha } : {}),
        side,
        file_path: target.path,
        start_line: draft.startLine,
        end_line: draft.endLine,
      },
      body: body.trim(),
      run_id: target.runId,
      ...(target.taskId ? { task_id: target.taskId } : {}),
    });
  };

  const lineError = errors.find((x) => x === 'lines' || x === 'range' || x === 'tooLong');
  const bodyError = errors.find((x) => x === 'body' || x === 'bodyTooLong');
  return (
    <form
      onSubmit={onSubmit}
      noValidate
      className="flex flex-col gap-3 rounded-md border border-border-active bg-surface-raised p-3 text-sm"
      aria-label={t('review.form.title', { path: target.path })}
      data-testid="new-thread-form"
    >
      <p className="font-semibold text-fg">{t('review.form.title', { path: target.path })}</p>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-side`} className="text-xs font-medium">
            {t('review.form.side')}
          </label>
          <select
            id={`${id}-side`}
            className="select min-h-target"
            value={side}
            aria-invalid={errors.includes('baseUnknown') || undefined}
            aria-describedby={errors.includes('baseUnknown') ? `${id}-side-error` : undefined}
            onChange={(e) => setSide(e.target.value as ReviewSide)}
          >
            <option value="head">{t('review.side.head')}</option>
            <option value="base" disabled={!target.baseSha}>
              {t('review.side.base')}
            </option>
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-start`} className="text-xs font-medium">
            {t('review.form.startLine')}
          </label>
          <input
            id={`${id}-start`}
            className="input min-h-target w-24"
            inputMode="numeric"
            value={start}
            aria-invalid={Boolean(lineError) || undefined}
            aria-describedby={lineError ? `${id}-lines-error` : undefined}
            onChange={(e) => setStart(e.target.value.replace(/\D/g, ''))}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-end`} className="text-xs font-medium">
            {t('review.form.endLine')}
          </label>
          <input
            id={`${id}-end`}
            className="input min-h-target w-24"
            inputMode="numeric"
            value={end}
            aria-invalid={Boolean(lineError) || undefined}
            aria-describedby={lineError ? `${id}-lines-error` : undefined}
            onChange={(e) => setEnd(e.target.value.replace(/\D/g, ''))}
          />
        </div>
      </div>
      {lineError ? (
        <p id={`${id}-lines-error`} className="text-xs font-medium text-danger-text">
          {t(`review.form.errors.${lineError}`)}
        </p>
      ) : null}
      {errors.includes('baseUnknown') ? (
        <p id={`${id}-side-error`} className="text-xs font-medium text-danger-text">
          {t('review.form.errors.baseUnknown')}
        </p>
      ) : null}
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-body`} className="text-xs font-medium">
          {t('review.form.body')}
        </label>
        <textarea
          ref={bodyRef}
          id={`${id}-body`}
          className="input min-h-[5rem]"
          value={body}
          maxLength={REVIEW_BODY_MAX}
          aria-invalid={Boolean(bodyError) || undefined}
          aria-describedby={`${id}-body-hint${bodyError ? ` ${id}-body-error` : ''}`}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={submitOnModEnter}
        />
        <p id={`${id}-body-hint`} className="text-xs text-fg-muted">
          {t('review.form.hint')}
        </p>
        {bodyError ? (
          <p id={`${id}-body-error`} className="text-xs font-medium text-danger-text">
            {t(`review.form.errors.${bodyError}`)}
          </p>
        ) : null}
      </div>
      {serverError ? <FormAlert messages={[serverError]} /> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" size="sm" loading={create.isPending}>
          {t('review.form.submit')}
        </Button>
        <Button size="sm" onClick={onCancel}>
          {t('action.cancel')}
        </Button>
      </div>
    </form>
  );
}
