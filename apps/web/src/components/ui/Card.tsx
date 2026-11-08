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
}

export function Card({ title, headingLevel = 2, actions, children, className, as = 'div', id }: CardProps) {
  const Tag = as;
  const H = `h${headingLevel}` as const;
  const headingId = id ? `${id}-title` : undefined;
  return (
    <Tag id={id} className={cx('card', className)} aria-labelledby={title && headingId && as !== 'div' ? headingId : undefined}>
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
