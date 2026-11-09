import * as RadixTabs from '@radix-ui/react-tabs';
import type { ReactNode } from 'react';
import { cx } from './cx';

/**
 * Вкладки (Radix Tabs, паттерн APG Tabs): стрелки переключают вкладки, Tab переходит в панель.
 * Если вкладка должна иметь свой URL (Run: граф/чат/задачи) — используйте маршруты и
 * NavLink, а Tabs — для вкладок внутри одного экрана.
 */
export const Tabs = RadixTabs.Root;

export function TabsList({
  label,
  children,
  className,
  variant = 'line',
}: {
  label: string;
  children: ReactNode;
  className?: string;
  /** line — подчёркивание (по умолчанию); pills — сегменты (панели Mission Control, фильтры вида). */
  variant?: 'line' | 'pills';
}) {
  return (
    <RadixTabs.List
      aria-label={label}
      data-variant={variant}
      className={cx(
        'group mb-4 flex flex-wrap gap-1',
        variant === 'line' ? 'border-b border-border' : 'w-fit rounded-lg border border-border bg-canvas-secondary p-1',
        className,
      )}
    >
      {children}
    </RadixTabs.List>
  );
}

export function TabsTrigger({ value, children, disabled }: { value: string; children: ReactNode; disabled?: boolean }) {
  return (
    <RadixTabs.Trigger
      value={value}
      disabled={disabled}
      className="inline-flex min-h-touch items-center gap-2 px-3 text-sm font-medium text-fg-muted transition-colors duration-instant hover:text-fg disabled:text-fg-disabled data-[state=active]:text-fg group-data-[variant=line]:-mb-px group-data-[variant=line]:border-b-2 group-data-[variant=line]:border-transparent group-data-[variant=line]:data-[state=active]:border-accent group-data-[variant=pills]:min-h-[36px] group-data-[variant=pills]:rounded-md group-data-[variant=pills]:data-[state=active]:bg-surface-raised group-data-[variant=pills]:data-[state=active]:shadow-e1"
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
