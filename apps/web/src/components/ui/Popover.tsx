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
  returnFocus,
}: {
  children: ReactNode;
  /**
   * Возвращать ли фокус на кнопку при закрытии (по умолчанию — да, если фокус не ушёл сам).
   * Панели-навигаторы возвращают `false` после перехода по ссылке: фокус переносит новый экран
   * (PageHeading → h1), а запоздалый возврат на кнопку отнял бы его.
   */
  returnFocus?: () => boolean;
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
          if (focusMovedElsewhere() || (returnFocus && !returnFocus())) e.preventDefault();
        }}
        className={cx(
          'glass z-modal w-72 max-w-[calc(100vw-1rem)] rounded-lg p-2 text-fg shadow-e2 animate-fade-in',
          className,
        )}
      >
        {children}
      </RadixPopover.Content>
    </RadixPopover.Portal>
  );
}
