import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { Link, type LinkProps } from 'react-router';
import { cx } from './cx';
import { Icon, type IconName } from './Icon';
import { Tooltip } from './Tooltip';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'md' | 'sm';

const VARIANT: Record<ButtonVariant, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  ghost: 'btn-ghost',
  danger: 'btn-danger',
};

export function buttonClass({
  variant = 'secondary',
  size = 'md',
  className,
}: { variant?: ButtonVariant; size?: ButtonSize; className?: string | undefined } = {}): string {
  return cx(VARIANT[variant], size === 'sm' && 'btn-sm', className);
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Идёт операция: кнопка недоступна, показывается индикатор, aria-busy. */
  loading?: boolean;
  icon?: IconName;
}

/** Кнопка дизайн-системы. Видимый текст обязателен; для кнопки-иконки — IconButton. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant, size, loading = false, icon, className, type = 'button', disabled, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={buttonClass({ variant, size, className })}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? (
        <span aria-hidden="true" className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
      ) : icon ? (
        <Icon name={icon} className="h-4 w-4" />
      ) : null}
      {children}
    </button>
  );
});

export interface ButtonLinkProps extends LinkProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: IconName;
  children: ReactNode;
}

/** Ссылка, оформленная как кнопка (навигация — всегда ссылка, а не кнопка). */
export function ButtonLink({ variant, size, icon, className, children, ...rest }: ButtonLinkProps) {
  return (
    <Link className={buttonClass({ variant, size, className: typeof className === 'string' ? className : undefined })} {...rest}>
      {icon ? <Icon name={icon} className="h-4 w-4" /> : null}
      {children}
    </Link>
  );
}

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  icon: IconName;
  /** Доступное имя и текст подсказки (WCAG 4.1.2): у кнопки-иконки нет видимого текста. */
  label: string;
  variant?: ButtonVariant;
  /** false — без всплывающей подсказки (например, внутри других всплывающих слоёв). */
  tooltip?: boolean;
}

/** Кнопка-иконка 44×44 с aria-label и подсказкой (Radix Tooltip, видна при наведении и фокусе). */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { icon, label, variant = 'ghost', tooltip = true, className, type = 'button', ...rest },
  ref,
) {
  const button = (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      className={cx(VARIANT[variant], 'min-w-touch px-2', className)}
      {...rest}
    >
      <Icon name={icon} />
    </button>
  );
  return tooltip ? <Tooltip content={label}>{button}</Tooltip> : button;
});
