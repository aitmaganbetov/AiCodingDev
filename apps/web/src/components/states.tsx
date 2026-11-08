import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../api/client';
import { problemMessage } from '../lib/problem';
import { cx } from './ui/cx';
import { Icon, type IconName } from './ui/Icon';

/**
 * Состояния экранов (NFR-UX-01): загрузка, пусто, ошибка, нет прав, в разработке.
 * Каждый экран Phase 7 обязан показывать эти состояния, а не «пустой» белый лист.
 */

export function LoadingState({ label, inline = false }: { label?: string; inline?: boolean }) {
  const { t } = useTranslation();
  return (
    <div role="status" aria-live="polite" className={cx('flex items-center gap-3 text-fg-muted', inline ? 'py-1' : 'py-8')}>
      <span aria-hidden="true" className="h-5 w-5 animate-spin rounded-full border-2 border-border-strong border-t-accent" />
      <span>{label ?? t('state.loading')}</span>
    </div>
  );
}

/** Заглушка-скелет для области, контент которой ещё грузится (декоративна, aria-hidden). */
export function Skeleton({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div aria-hidden="true" className={cx('flex animate-pulse flex-col gap-2', className)}>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className={cx('h-4 rounded bg-surface-muted', i === lines - 1 ? 'w-2/3' : 'w-full')} />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  icon,
  headingLevel = 2,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: IconName;
  headingLevel?: 2 | 3;
}) {
  const H = `h${headingLevel}` as const;
  return (
    <div className="card flex flex-col items-start gap-3 py-8" data-testid="empty-state">
      {icon ? (
        <span className="rounded-full bg-surface-muted p-3 text-fg-muted">
          <Icon name={icon} className="h-6 w-6" />
        </span>
      ) : null}
      <H className="text-lg font-semibold">{title}</H>
      {description ? <p className="text-fg-muted">{description}</p> : null}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  const { t } = useTranslation();
  const requestId = isApiError(error) ? error.requestId : undefined;
  return (
    <div role="alert" className="rounded-lg border border-danger-border bg-danger-soft p-4 text-danger-fg" data-testid="error-state">
      <h2 className="font-semibold">{title ?? t('state.errorTitle')}</h2>
      <p className="mt-1">{problemMessage(t, error)}</p>
      {requestId ? <p className="mt-1 text-sm">{t('state.requestId', { id: requestId })}</p> : null}
      {onRetry ? (
        <button type="button" className="btn-secondary mt-3" onClick={onRetry}>
          {t('action.retry')}
        </button>
      ) : null}
    </div>
  );
}

/** Нет права (UI-проверка по роли или ответ 403 forbidden). */
export function ForbiddenState({ description, action }: { description?: string; action?: ReactNode }) {
  const { t } = useTranslation();
  return (
    <EmptyState icon="lock" title={t('state.forbiddenTitle')} description={description ?? t('state.forbiddenDescription')} action={action} />
  );
}

/** Экран Phase 7, который ещё не реализован (заглушка маршрута). */
export function UnderConstructionState({ section }: { section: string }) {
  const { t } = useTranslation();
  return (
    <EmptyState
      icon="construction"
      title={t('state.underConstructionTitle')}
      description={t('state.underConstructionDescription', { section })}
    />
  );
}
