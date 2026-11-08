import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useLocation } from 'react-router';

interface Props {
  /** Содержимое кнопки (видимый текст — он же доступное имя). */
  label: ReactNode;
  buttonClassName?: string;
  panelClassName?: string;
  children: ReactNode;
}

/**
 * Disclosure-паттерн (WAI-ARIA APG): кнопка с aria-expanded управляет панелью.
 * Escape закрывает и возвращает фокус на кнопку; клик снаружи и навигация закрывают.
 */
export function Disclosure({ label, buttonClassName, panelClassName, children }: Props) {
  const location = useLocation();
  // Панель открыта только на том пути, где её открыли: навигация закрывает её.
  const [openedAt, setOpenedAt] = useState<string | null>(null);
  const open = openedAt === location.pathname;
  const setOpen = (value: boolean | ((prev: boolean) => boolean)) => {
    const next = typeof value === 'function' ? value(open) : value;
    setOpenedAt(next ? location.pathname : null);
  };
  const panelId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onOutside = (e: Event) => {
      if (rootRef.current && e.target instanceof Node && !rootRef.current.contains(e.target)) setOpenedAt(null);
    };
    const onPointer = onOutside;
    const onFocus = onOutside;
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('focusin', onFocus);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('focusin', onFocus);
    };
  }, [open]);

  const close = () => {
    setOpen(false);
    buttonRef.current?.focus();
  };

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- Escape обрабатывается на контейнере, интерактивны только кнопка и ссылки внутри
    <div
      ref={rootRef}
      className="relative"
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.stopPropagation();
          close();
        }
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        className={buttonClassName ?? 'btn-secondary'}
        onClick={() => setOpen((v) => !v)}
      >
        {label}
        <span aria-hidden="true">▾</span>
      </button>
      <div
        id={panelId}
        hidden={!open}
        className={
          panelClassName ??
          'absolute right-0 z-20 mt-2 w-72 max-w-[calc(100vw-2rem)] rounded-md border border-slate-200 bg-white p-2 shadow-lg'
        }
      >
        {open ? children : null}
      </div>
    </div>
  );
}
