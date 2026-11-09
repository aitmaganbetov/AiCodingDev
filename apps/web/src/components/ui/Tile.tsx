import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../../api/client';
import { problemMessage } from '../../lib/problem';
import { cx } from './cx';
import { Icon, type IconName } from './Icon';
import { SkeletonBlock } from './Skeleton';

/**
 * Плитка bento-сетки (план §5.4: r-lg 14, e1, высоты кратны 8). Каждая плитка — отдельный
 * `section` с заголовком (h2/h3) и своим состоянием: загрузка (скелет, aria-busy), ошибка
 * (сообщение по problem+json + «Повторить» — соседние плитки при этом работают) или данные.
 * `source` — подпись источника/окна/выборки (обязательна для чисел плиток Dashboard, F3).
 * Микро-взаимодействия: рамка и тень на hover/focus-within меняются сразу, анимируется только
 * сдвиг на 1 px (transform, 120 мс `--dur-instant`; при prefers-reduced-motion — без сдвига, §5.5). Стекло на плитках не используется (§4.7).
 */
export function Tile({
  id,
  title,
  icon,
  headingLevel = 3,
  actions,
  source,
  loading = false,
  error,
  onRetry,
  className,
  children,
  featured = false,
  testId,
}: {
  id: string;
  title: ReactNode;
  icon?: IconName;
  headingLevel?: 2 | 3;
  actions?: ReactNode;
  /** Источник, окно и выборка значения (видимый текст). */
  source?: ReactNode;
  loading?: boolean;
  /** Ошибка загрузки (ApiError и т. п.) — вместо содержимого. */
  error?: unknown;
  onRetry?: () => void;
  className?: string;
  children?: ReactNode;
  /** Ключевая плитка: тонкая градиентная рамка (декор, §5.1). */
  featured?: boolean;
  testId?: string;
}) {
  const { t } = useTranslation();
  const H = `h${headingLevel}` as const;
  const headingId = `${id}-title`;
  const hasError = error !== undefined && error !== null;
  return (
    <section
      id={id}
      aria-labelledby={headingId}
      aria-busy={loading || undefined}
      data-testid={testId ?? id}
      data-state={loading ? 'loading' : hasError ? 'error' : 'ready'}
      className={cx(
        'group flex min-w-0 flex-col gap-3 rounded-xl border bg-surface p-4 shadow-e1',
        'transition-transform duration-instant ease-standard',
        'hover:border-border-strong hover:shadow-e2 focus-within:border-border-active motion-safe:hover:-translate-y-px',
        featured ? 'border-transparent ring-brand' : 'border-border',
        className,
      )}
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        <H id={headingId} className="flex min-w-0 items-center gap-2 text-base font-semibold text-fg">
          {icon ? (
            <span className="inline-flex shrink-0 rounded-md bg-accent-soft p-1.5 text-accent-soft-fg">
              <Icon name={icon} className="h-4 w-4" />
            </span>
          ) : null}
          <span className="min-w-0 break-words">{title}</span>
        </H>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {loading ? (
        <div className="flex flex-col gap-2" data-testid="tile-skeleton">
          <span className="sr-only">{t('ds.tile.loading')}</span>
          <SkeletonBlock className="h-8 w-24" />
          <SkeletonBlock className="h-4 w-full" />
        </div>
      ) : hasError ? (
        <TileError error={error} onRetry={onRetry} />
      ) : (
        <div className="flex min-w-0 flex-1 flex-col gap-3">{children}</div>
      )}
      {source && !loading && !hasError ? <p className="mt-auto text-xs text-fg-subtle">{source}</p> : null}
    </section>
  );
}

/** Ошибка внутри плитки: компактно, с текстом причины, request id и повтором. */
export function TileError({ error, onRetry }: { error: unknown; onRetry?: (() => void) | undefined }) {
  const { t } = useTranslation();
  const requestId = isApiError(error) ? error.requestId : undefined;
  return (
    <div role="alert" className="flex flex-col items-start gap-2 rounded-lg border border-danger-border bg-danger-soft p-3 text-sm text-danger-fg" data-testid="tile-error">
      <p className="flex items-start gap-2 font-semibold">
        <Icon name="statusFailed" className="mt-0.5 h-4 w-4 shrink-0" />
        {t('ds.tile.errorTitle')}
      </p>
      <p>{problemMessage(t, error)}</p>
      {requestId ? <p className="text-xs">{t('state.requestId', { id: requestId })}</p> : null}
      {onRetry ? (
        <button type="button" className="btn-secondary btn-sm" onClick={onRetry}>
          <Icon name="refresh" className="h-4 w-4" />
          {t('action.retry')}
        </button>
      ) : null}
    </div>
  );
}

/**
 * Число плитки (§5.3 metric: tabular-nums 28/1.1). `value = null` — нет данных (никогда не «0»):
 * показывается `empty` (по умолчанию «нет данных»). `more` — выборка неполная (есть next_cursor) → «N+».
 * `label` — что именно измерено (видимо); `hint` — пояснение под числом.
 */
export function Metric({
  value,
  label,
  more = false,
  unit,
  hint,
  empty,
  size = 'lg',
  testId,
}: {
  value: number | string | null | undefined;
  label: ReactNode;
  more?: boolean;
  unit?: string;
  hint?: ReactNode;
  empty?: ReactNode;
  size?: 'lg' | 'sm';
  testId?: string;
}) {
  const { t } = useTranslation();
  const missing = value === null || value === undefined;
  const text = missing ? null : typeof value === 'number' ? value.toLocaleString('ru-RU') : value;
  return (
    <div className="flex min-w-0 flex-col gap-0.5" data-testid={testId} data-value={missing ? 'none' : String(value)}>
      <span className="text-sm text-fg-muted">{label}</span>
      {missing ? (
        <span className={cx('font-semibold text-fg-muted', size === 'lg' ? 'text-lg' : 'text-sm')}>{empty ?? t('ds.metric.noData')}</span>
      ) : (
        <span className={cx('tabular-nums text-fg', size === 'lg' ? 'text-metric' : 'text-xl font-semibold')}>
          {more ? t('ds.metric.more', { value: text }) : text}
          {unit ? <span className="ml-1 text-base font-medium text-fg-muted">{unit}</span> : null}
        </span>
      )}
      {hint ? <span className="text-xs text-fg-muted">{hint}</span> : null}
    </div>
  );
}
