import type { ReactNode } from 'react';
import { cx } from './cx';

interface CardProps {
  /** Заголовок карточки (h2 по умолчанию; уровень — по месту в иерархии страницы). */
  title?: ReactNode;
  headingLevel?: 2 | 3 | 4;
  /** Действия справа от заголовка. */
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** section с aria-labelledby, если есть заголовок. */
  as?: 'div' | 'section' | 'article';
  id?: string;
  /**
   * default — непрозрачная поверхность (данные, плитки); raised — приподнятая (e2);
   * glass — стекло (§4.7: только над плоским фоном, не над градиентом/изображением);
   * featured — тонкая градиентная рамка ключевого элемента (декор).
   */
  variant?: CardVariant;
  /** Внутренние отступы: md 16 px (по умолчанию), sm 12 px, none — без отступов (таблица во всю ширину). */
  padding?: 'none' | 'sm' | 'md';
}

export type CardVariant = 'default' | 'raised' | 'glass' | 'featured';

const VARIANT: Record<CardVariant, string> = {
  default: 'border border-border bg-surface shadow-e1',
  raised: 'border border-border bg-surface-raised shadow-e2',
  glass: 'glass shadow-e2',
  featured: 'ring-brand shadow-glow',
};
const PADDING = { none: 'p-0', sm: 'p-3', md: 'p-4' } as const;

export function Card({ title, headingLevel = 2, actions, children, className, as = 'div', id, variant = 'default', padding = 'md' }: CardProps) {
  const Tag = as;
  const H = `h${headingLevel}` as const;
  const headingId = id ? `${id}-title` : undefined;
  return (
    <Tag id={id} className={cx('rounded-lg', VARIANT[variant], PADDING[padding], className)}
      data-variant={variant === 'default' ? undefined : variant} aria-labelledby={title && headingId && as !== 'div' ? headingId : undefined}>
      {title || actions ? (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title ? (
            <H id={headingId} className="text-lg font-semibold text-fg">
              {title}
            </H>
          ) : (
            <span />
          )}
          {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </Tag>
  );
}
