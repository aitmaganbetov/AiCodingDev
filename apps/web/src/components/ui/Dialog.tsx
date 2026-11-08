import * as RadixDialog from '@radix-ui/react-dialog';
import { useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { cx } from './cx';
import { focusMovedElsewhere } from './focus';
import { Icon } from './Icon';

/**
 * Модальный диалог (Radix Dialog): role=dialog + aria-modal, ловушка фокуса, Escape и
 * клик по подложке закрывают, фокус возвращается на кнопку-триггер (WAI-ARIA APG).
 */
export const Dialog = RadixDialog.Root;
export const DialogTrigger = RadixDialog.Trigger;
export const DialogClose = RadixDialog.Close;

interface DialogContentProps {
  /** Видимый заголовок — доступное имя диалога. */
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Нижняя панель действий. */
  footer?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  /** `drawer-left` — выезжающая панель (мобильная навигация). */
  variant?: 'center' | 'drawer-left';
  className?: string;
}

const SIZE = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-3xl' } as const;

export function DialogContent({ title, description, children, footer, size = 'md', variant = 'center', className }: DialogContentProps) {
  const { t } = useTranslation();
  // Диалог, открытый не через DialogTrigger (сочетание клавиш, программно), после закрытия
  // возвращает фокус туда, где он был до открытия (WCAG 2.4.3).
  const opener = useRef<Element | null>(null);
  return (
    <RadixDialog.Portal>
      <RadixDialog.Overlay className="fixed inset-0 z-overlay bg-overlay/50 animate-fade-in" />
      <RadixDialog.Content
        {...(description ? {} : { 'aria-describedby': undefined })}
        onOpenAutoFocus={() => {
          // Вызывается до переноса фокуса внутрь: activeElement — ещё элемент, открывший диалог.
          opener.current = document.activeElement;
        }}
        onCloseAutoFocus={(e) => {
          if (focusMovedElsewhere()) {
            e.preventDefault();
            return;
          }
          const el = opener.current;
          if (el instanceof HTMLElement && el !== document.body && el.isConnected) {
            e.preventDefault();
            el.focus();
          }
        }}
        className={cx(
          'fixed z-modal flex flex-col border border-border bg-surface-raised text-fg shadow-elevated focus:outline-none',
          variant === 'center'
            ? cx('left-1/2 top-1/2 max-h-[85vh] w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 rounded-lg', SIZE[size])
            : 'inset-y-0 left-0 w-[min(20rem,85vw)] rounded-r-lg',
          className,
        )}
      >
        <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <RadixDialog.Title className="text-lg font-semibold">{title}</RadixDialog.Title>
            {description ? <RadixDialog.Description className="mt-1 text-sm text-fg-muted">{description}</RadixDialog.Description> : null}
          </div>
          <RadixDialog.Close className="btn-ghost -mr-2 min-w-touch px-2" aria-label={t('action.close')}>
            <Icon name="close" />
          </RadixDialog.Close>
        </div>
        <div className="flex-1 overflow-y-auto px-4 py-3">{children}</div>
        {footer ? <div className="flex flex-wrap justify-end gap-3 border-t border-border px-4 py-3">{footer}</div> : null}
      </RadixDialog.Content>
    </RadixDialog.Portal>
  );
}
