import * as RadixTabs from '@radix-ui/react-tabs';
import type { ReactNode } from 'react';
import { cx } from './cx';

/**
 * Вкладки (Radix Tabs, паттерн APG Tabs): стрелки переключают вкладки, Tab переходит в панель.
 * Если вкладка должна иметь свой URL (Run: граф/чат/задачи) — используйте маршруты и
 * NavLink, а Tabs — для вкладок внутри одного экрана.
 */
export const Tabs = RadixTabs.Root;

export function TabsList({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <RadixTabs.List aria-label={label} className={cx('mb-4 flex flex-wrap gap-1 border-b border-border', className)}>
      {children}
    </RadixTabs.List>
  );
}

export function TabsTrigger({ value, children, disabled }: { value: string; children: ReactNode; disabled?: boolean }) {
  return (
    <RadixTabs.Trigger
      value={value}
      disabled={disabled}
      className="-mb-px inline-flex min-h-touch items-center border-b-2 border-transparent px-3 text-sm font-medium text-fg-muted hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg disabled:opacity-60"
    >
      {children}
    </RadixTabs.Trigger>
  );
}

export function TabsContent({ value, children, className }: { value: string; children: ReactNode; className?: string }) {
  return (
    <RadixTabs.Content value={value} className={className}>
      {children}
    </RadixTabs.Content>
  );
}
