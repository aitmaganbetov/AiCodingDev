import type { ReactNode } from 'react';
import { cx } from './cx';

export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

/** Контраст текста ≥ 7:1 на фоне бейджа в обеих темах (tokens.css); цвет всегда дублируется текстом. */
export const TONE_CLASS: Record<Tone, string> = {
  neutral: 'border-neutral-border bg-neutral-soft text-neutral-fg',
  info: 'border-info-border bg-info-soft text-info-fg',
  success: 'border-success-border bg-success-soft text-success-fg',
  warning: 'border-warning-border bg-warning-soft text-warning-fg',
  danger: 'border-danger-border bg-danger-soft text-danger-fg',
};

export function Badge({ tone = 'neutral', children, className, ...data }: { tone?: Tone; children: ReactNode; className?: string } & Record<`data-${string}`, string>) {
  return (
    <span className={cx('inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-semibold', TONE_CLASS[tone], className)} {...data}>
      {children}
    </span>
  );
}

/** Баннер (некритичное сообщение) в тоне статуса. Для ошибок с ролью alert — ErrorState. */
export function Callout({ tone = 'info', title, children, className }: { tone?: Tone; title?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cx('rounded-md border p-3 text-sm', TONE_CLASS[tone], className)}>
      {title ? <p className="font-semibold">{title}</p> : null}
      <div className={title ? 'mt-1' : undefined}>{children}</div>
    </div>
  );
}
