import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../api/client';
import { problemMessage } from '../lib/problem';

export function LoadingState({ label }: { label?: string }) {
  const { t } = useTranslation();
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-3 py-8 text-slate-700">
      <span aria-hidden="true" className="h-5 w-5 animate-spin rounded-full border-2 border-slate-300 border-t-blue-700" />
      <span>{label ?? t('state.loading')}</span>
    </div>
  );
}

export function EmptyState({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="card flex flex-col items-start gap-3 py-8" data-testid="empty-state">
      <h2 className="text-lg font-semibold">{title}</h2>
      {description ? <p className="text-slate-700">{description}</p> : null}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  const { t } = useTranslation();
  const requestId = isApiError(error) ? error.requestId : undefined;
  return (
    <div role="alert" className="rounded-lg border border-red-300 bg-red-50 p-4 text-red-900" data-testid="error-state">
      <h2 className="font-semibold">{title ?? t('state.errorTitle')}</h2>
      <p className="mt-1">{problemMessage(t, error)}</p>
      {requestId ? <p className="mt-1 text-sm text-red-900">{t('state.requestId', { id: requestId })}</p> : null}
      {onRetry ? (
        <button type="button" className="btn-secondary mt-3" onClick={onRetry}>
          {t('action.retry')}
        </button>
      ) : null}
    </div>
  );
}
