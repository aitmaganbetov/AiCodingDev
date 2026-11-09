import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { cx } from './cx';
import { Icon } from './Icon';

/**
 * «Нет источника данных» (принцип 2 плана редизайна, API GAP §3.10): виджет без реального
 * источника показывает причину и ссылку на описание вместо числа. Никогда не подставляет нули,
 * прочерки-«0» или демо-данные. `operation` — недостающая операция API (например, `GET /system/health`).
 */
export function DataUnavailable({
  title,
  reason,
  operation,
  docsHref,
  headingLevel = 3,
  compact = false,
  className,
}: {
  title?: ReactNode;
  reason: ReactNode;
  operation?: string;
  /** Ссылка на описание GAP / фазы backend (документация проекта). */
  docsHref?: string;
  headingLevel?: 2 | 3 | 4;
  compact?: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  const H = `h${headingLevel}` as const;
  return (
    <div
      className={cx('flex items-start gap-3 rounded-lg border border-dashed border-input bg-surface text-sm', compact ? 'p-3' : 'p-4', className)}
      data-testid="data-unavailable"
      data-operation={operation}
    >
      <span className="rounded-md bg-neutral-soft p-2 text-neutral-fg">
        <Icon name="unplug" className="h-5 w-5" />
      </span>
      <div className="min-w-0 flex-1">
        <H className="font-semibold text-fg">{title ?? t('ds.dataUnavailable.title')}</H>
        <p className="mt-1 text-fg-muted">{reason}</p>
        {operation ? (
          <p className="mt-1 text-fg-muted">
            {t('ds.dataUnavailable.requires', { operation: '' })}
            <code className="rounded bg-surface-muted px-1 py-0.5 text-code-sm text-fg">{operation}</code>
          </p>
        ) : null}
        {!compact ? <p className="mt-1 text-xs text-fg-subtle">{t('ds.dataUnavailable.note')}</p> : null}
        {docsHref ? (
          <a className="link mt-2 inline-flex items-center gap-1" href={docsHref} target="_blank" rel="noreferrer noopener">
            {t('ds.dataUnavailable.docs')}
            <Icon name="externalLink" className="h-4 w-4" />
          </a>
        ) : null}
      </div>
    </div>
  );
}
