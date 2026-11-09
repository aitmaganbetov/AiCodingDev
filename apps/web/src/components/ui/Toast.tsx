import * as RadixToast from '@radix-ui/react-toast';
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { TONE_CLASS, TONE_ICON, type Tone } from './Badge';
import { cx } from './cx';
import { Icon } from './Icon';

/**
 * Уведомления (Radix Toast): озвучиваются через live region (foreground — assertive,
 * background — polite), не перехватывают фокус; F8 переводит фокус в область уведомлений;
 * таймер останавливается при наведении/фокусе (WCAG 2.2.1). Для результата действия,
 * который нужен для продолжения работы, используйте текст на странице, а не toast.
 */
export interface ToastInput {
  title: string;
  description?: string;
  tone?: Tone;
  /** мс; по умолчанию 6000. */
  durationMs?: number;
  /** foreground — важное (assertive), background — фоновое (polite, по умолчанию). */
  priority?: 'foreground' | 'background';
}

interface ToastItem extends ToastInput {
  id: number;
  open: boolean;
}

const ToastContext = createContext<((toast: ToastInput) => void) | null>(null);

/** Показать уведомление: `const toast = useToast(); toast({ title: t('…'), tone: 'success' })`. */
export function useToast(): (toast: ToastInput) => void {
  return useContext(ToastContext) ?? noop;
}

function noop(): void {}

export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);
  const show = useCallback((toast: ToastInput) => {
    const id = nextId.current++;
    setItems((prev) => [...prev.slice(-4), { ...toast, id, open: true }]);
  }, []);
  const value = useMemo(() => show, [show]);

  return (
    <ToastContext.Provider value={value}>
      <RadixToast.Provider swipeDirection="right" label={t('ui.toastLabel')}>
        {children}
        {items.map((item) => (
          <RadixToast.Root
            key={item.id}
            open={item.open}
            type={item.priority ?? 'background'}
            duration={item.durationMs ?? 6000}
            onOpenChange={(open) => {
              if (!open) setItems((prev) => prev.filter((x) => x.id !== item.id));
            }}
            className={cx('flex items-start gap-3 rounded-lg border p-3 shadow-e2', TONE_CLASS[item.tone ?? 'neutral'])}
            data-testid="toast"
          >
            <Icon name={TONE_ICON[item.tone ?? 'neutral']} className="mt-0.5 h-4 w-4 shrink-0" />
            <div className="flex-1">
              <RadixToast.Title className="text-sm font-semibold">{item.title}</RadixToast.Title>
              {item.description ? <RadixToast.Description className="mt-1 text-sm">{item.description}</RadixToast.Description> : null}
            </div>
            <RadixToast.Close className="btn-ghost btn-sm -mr-1 -mt-1 min-w-touch px-2 text-current" aria-label={t('ui.dismiss')}>
              <Icon name="close" className="h-4 w-4" />
            </RadixToast.Close>
          </RadixToast.Root>
        ))}
        <RadixToast.Viewport
          label={t('ui.toastRegion', { hotkey: '{hotkey}' })}
          className="fixed bottom-0 right-0 z-toast m-0 flex w-full max-w-sm list-none flex-col gap-2 p-4 outline-none"
        />
      </RadixToast.Provider>
    </ToastContext.Provider>
  );
}
