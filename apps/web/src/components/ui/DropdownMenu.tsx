import * as RadixMenu from '@radix-ui/react-dropdown-menu';
import { forwardRef, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import { cx } from './cx';
import { Icon } from './Icon';

/**
 * Меню действий (Radix DropdownMenu, паттерн APG Menu Button): стрелки/Home/End/поиск по
 * первой букве, Enter/Space выбирают, Escape закрывает и возвращает фокус на кнопку.
 * Для навигации по ссылкам без «команд» — Popover со списком ссылок.
 */
export function Menu(props: ComponentPropsWithoutRef<typeof RadixMenu.Root>) {
  // Немодальное меню: модальный режим Radix ставит aria-hidden на весь #root, оставляя
  // в нём фокусируемые элементы (axe aria-hidden-focus). APG Menu Button модальности не требует.
  return <RadixMenu.Root modal={false} {...props} />;
}
export const MenuTrigger = RadixMenu.Trigger;
export const MenuGroup = RadixMenu.Group;
export const MenuRadioGroup = RadixMenu.RadioGroup;

export function MenuContent({ children, align = 'end', className }: { children: ReactNode; align?: 'start' | 'center' | 'end'; className?: string }) {
  return (
    <RadixMenu.Portal>
      <RadixMenu.Content
        align={align}
        sideOffset={6}
        collisionPadding={8}
        className={cx(
          'z-modal min-w-[12rem] max-w-[calc(100vw-1rem)] rounded-md border border-border bg-surface-raised p-1 text-fg shadow-elevated animate-fade-in',
          className,
        )}
      >
        {children}
      </RadixMenu.Content>
    </RadixMenu.Portal>
  );
}

const itemClass =
  'flex min-h-touch cursor-default select-none items-center gap-2 rounded px-3 text-sm outline-none data-[disabled]:opacity-60 data-[highlighted]:bg-accent-soft data-[highlighted]:text-accent-soft-fg';

export const MenuItem = forwardRef<HTMLDivElement, ComponentPropsWithoutRef<typeof RadixMenu.Item>>(function MenuItem(
  { className, ...rest },
  ref,
) {
  return <RadixMenu.Item ref={ref} className={cx(itemClass, className)} {...rest} />;
});

export function MenuRadioItem({ value, children }: { value: string; children: ReactNode }) {
  return (
    <RadixMenu.RadioItem value={value} className={cx(itemClass, 'pl-8 relative')}>
      <RadixMenu.ItemIndicator className="absolute left-2 inline-flex">
        <Icon name="check" className="h-4 w-4" />
      </RadixMenu.ItemIndicator>
      {children}
    </RadixMenu.RadioItem>
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <RadixMenu.Label className="px-3 py-1.5 text-xs font-semibold text-fg-muted">{children}</RadixMenu.Label>;
}

export function MenuSeparator() {
  return <RadixMenu.Separator className="my-1 h-px bg-border" />;
}
