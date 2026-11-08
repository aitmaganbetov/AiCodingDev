import * as RadixPopover from '@radix-ui/react-popover';
import type { ReactNode } from 'react';
import { cx } from './cx';
import { focusMovedElsewhere } from './focus';

/**
 * Немодальная всплывающая панель (Radix Popover): кнопка с aria-expanded/aria-controls,
 * фокус переходит в панель, Escape и клик снаружи закрывают и возвращают фокус.
 * Подходит для фильтров, списков ссылок (переключатель workspace), карточек деталей.
 */
export const Popover = RadixPopover.Root;
export const PopoverTrigger = RadixPopover.Trigger;
export const PopoverClose = RadixPopover.Close;

export function PopoverContent({
  children,
  align = 'start',
  label,
  className,
}: {
  children: ReactNode;
  align?: 'start' | 'center' | 'end';
  /** Доступное имя панели (role=dialog у Radix Popover). */
  label: string;
  className?: string;
}) {
  return (
    <RadixPopover.Portal>
      <RadixPopover.Content
        align={align}
        sideOffset={6}
        collisionPadding={8}
        aria-label={label}
        onCloseAutoFocus={(e) => {
          if (focusMovedElsewhere()) e.preventDefault();
        }}
        className={cx(
          'z-modal w-72 max-w-[calc(100vw-1rem)] rounded-md border border-border bg-surface-raised p-2 text-fg shadow-elevated animate-fade-in',
          className,
        )}
      >
        {children}
      </RadixPopover.Content>
    </RadixPopover.Portal>
  );
}
