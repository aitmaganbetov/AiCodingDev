import * as RadixTooltip from '@radix-ui/react-tooltip';
import type { ReactElement, ReactNode } from 'react';

/**
 * Подсказка (Radix Tooltip): показывается при наведении и при фокусе с клавиатуры,
 * Escape скрывает (WCAG 1.4.13). Только для дополнительной информации — не прятать
 * в подсказку то, без чего нельзя выполнить действие. Провайдер — в AppProviders.
 */
export const TooltipProvider = RadixTooltip.Provider;

export function Tooltip({
  content,
  children,
  side = 'top',
}: {
  content: ReactNode;
  /** Один фокусируемый элемент (кнопка/ссылка), принимающий ref. */
  children: ReactElement;
  side?: 'top' | 'right' | 'bottom' | 'left';
}) {
  return (
    <RadixTooltip.Root>
      <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
      <RadixTooltip.Portal>
        <RadixTooltip.Content
          side={side}
          sideOffset={6}
          className="z-toast max-w-xs rounded-md bg-fg px-2 py-1 text-xs font-medium text-surface shadow-e2 animate-fade-in"
        >
          {content}
          <RadixTooltip.Arrow className="fill-fg" />
        </RadixTooltip.Content>
      </RadixTooltip.Portal>
    </RadixTooltip.Root>
  );
}
