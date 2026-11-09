import type { ReactNode } from 'react';

/** Клавиша/сочетание (семантический <kbd>). */
export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex min-w-[1.75rem] items-center justify-center rounded border border-border-strong border-b-2 bg-surface-muted px-1.5 py-0.5 font-mono text-code-sm font-semibold text-fg">
      {children}
    </kbd>
  );
}
