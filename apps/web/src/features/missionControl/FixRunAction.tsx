/**
 * «Передать ИИ на исправление» (POST /runs/{id}/fix): для run в статусе FAILED создаёт run-исправление.
 * Цель нового run составляет платформа из проваленных проверок и открытых находок исходного; здесь —
 * только необязательное уточнение пользователя. 409 (нечего исправлять: отказ вызван инфраструктурой
 * проверок) показывается текстом ошибки API.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { IdempotentIntent } from '../../api/client';
import { runKeys, runsApi, type Run } from '../../api/runs';
import { problemMessage } from '../../lib/problem';

const NOTE_MAX = 2000;

export function FixRunAction({ workspaceId, run }: { workspaceId: string; run: Run }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState('');
  const fix = useMutation({
    mutationFn: () => {
      const trimmed = note.trim();
      return runsApi.fix(run.id, trimmed ? { note: trimmed } : {}, intent.current.keyFor({ run: run.id, fix: trimmed }));
    },
    onSuccess: () => {
      intent.current.complete();
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: runKeys.list(workspaceId) });
    },
  });
  if (run.status !== 'FAILED') return null;
  return (
    <section aria-label={t('run.fix.title')} className="flex flex-col gap-2 rounded-md border border-border p-3" data-testid="run-fix">
      <p className="text-sm text-fg-muted">{t('run.fix.hint')}</p>
      {fix.isSuccess ? (
        <p role="status" className="text-sm text-fg">
          {t('run.fix.created')}{' '}
          <Link className="link" to={`/w/${workspaceId}/mission-control?run=${encodeURIComponent(fix.data.run_id)}`} data-testid="run-fix-link">
            {t('run.fix.open')}
          </Link>
        </p>
      ) : open ? (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            fix.mutate();
          }}
        >
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-fg-muted">{t('run.fix.note')}</span>
            <textarea className="input min-h-20" maxLength={NOTE_MAX} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="flex flex-wrap gap-2">
            <button type="submit" className="btn-primary btn-sm" disabled={fix.isPending}>
              {fix.isPending ? t('run.fix.sending') : t('run.fix.confirm')}
            </button>
            <button type="button" className="btn-secondary btn-sm" disabled={fix.isPending} onClick={() => setOpen(false)}>
              {t('run.cancelNo')}
            </button>
          </div>
        </form>
      ) : (
        <div>
          <button type="button" className="btn-primary btn-sm" onClick={() => setOpen(true)}>
            {t('run.fix.action')}
          </button>
        </div>
      )}
      {fix.isError ? (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {problemMessage(t, fix.error)}
        </p>
      ) : null}
    </section>
  );
}
