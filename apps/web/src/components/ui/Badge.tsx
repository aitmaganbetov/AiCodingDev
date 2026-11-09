import type { ReactNode } from 'react';
import { cx } from './cx';
import { Icon, type IconName } from './Icon';

export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

/** Текст тона на его soft-фоне ≥ 4,5:1 в обеих темах (tokens.test.ts); цвет всегда дублируется текстом и иконкой. */
export const TONE_CLASS: Record<Tone, string> = {
  neutral: 'border-neutral-border bg-neutral-soft text-neutral-fg',
  info: 'border-info-border bg-info-soft text-info-fg',
  success: 'border-success-border bg-success-soft text-success-fg',
  warning: 'border-warning-border bg-warning-soft text-warning-fg',
  danger: 'border-danger-border bg-danger-soft text-danger-fg',
};

/** Иконка тона по умолчанию (§5.2): статус никогда не передаётся только цветом. */
export const TONE_ICON: Record<Tone, IconName> = {
  neutral: 'statusNeutral',
  info: 'info',
  success: 'statusSuccess',
  warning: 'alert',
  danger: 'statusFailed',
};

export function Badge({
  tone = 'neutral',
  icon,
  children,
  className,
  ...data
}: {
  tone?: Tone;
  /** Иконка слева (декоративная; смысл — в тексте). */
  icon?: IconName | undefined;
  children: ReactNode;
  className?: string;
} & Record<`data-${string}`, string>) {
  return (
    <span
      className={cx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-semibold', TONE_CLASS[tone], className)}
      {...data}
    >
      {icon ? <Icon name={icon} className="h-3.5 w-3.5 shrink-0" /> : null}
      {children}
    </span>
  );
}

/** Баннер (некритичное сообщение) в тоне статуса. Для ошибок с ролью alert — ErrorState. */
export function Callout({
  tone = 'info',
  title,
  children,
  className,
  icon,
}: {
  tone?: Tone;
  title?: ReactNode;
  children: ReactNode;
  className?: string;
  /** false — без иконки; по умолчанию — иконка тона. */
  icon?: IconName | false;
}) {
  const glyph = icon === false ? null : (icon ?? TONE_ICON[tone]);
  return (
    <div className={cx('flex gap-2 rounded-lg border p-3 text-sm', TONE_CLASS[tone], className)}>
      {glyph ? <Icon name={glyph} className="mt-0.5 h-4 w-4 shrink-0" /> : null}
      <div className="min-w-0 flex-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        <div className={title ? 'mt-1' : undefined}>{children}</div>
      </div>
    </div>
  );
}
