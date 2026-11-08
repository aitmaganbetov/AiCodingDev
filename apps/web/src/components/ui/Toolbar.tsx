import { useLayoutEffect, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { cx } from './cx';

const ITEMS = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [data-toolbar-item]';

/**
 * Панель инструментов (APG Toolbar): одна точка табуляции, стрелки ←/→, Home/End
 * перемещают фокус между элементами (roving tabindex). Для фильтров и действий над списком.
 * Поля ввода внутри панели оставляют стрелки себе — в панель их лучше не класть,
 * поля фильтров размещайте рядом (обычная форма).
 */
export function Toolbar({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  const items = () => Array.from(ref.current?.querySelectorAll<HTMLElement>(ITEMS) ?? []);

  // Ровно один элемент с tabIndex=0: последний активный или первый.
  useLayoutEffect(() => {
    const list = items();
    if (list.length === 0) return;
    const current = list.find((el) => el.tabIndex === 0 && el.dataset.rovingActive === 'true') ?? list[0];
    for (const el of list) {
      el.tabIndex = el === current ? 0 : -1;
      el.dataset.rovingActive = el === current ? 'true' : 'false';
    }
  });

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const list = items();
    const index = list.indexOf(document.activeElement as HTMLElement);
    if (index < 0) return;
    let next = index;
    if (e.key === 'ArrowRight') next = (index + 1) % list.length;
    else if (e.key === 'ArrowLeft') next = (index - 1 + list.length) % list.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = list.length - 1;
    else return;
    e.preventDefault();
    for (const el of list) {
      el.tabIndex = -1;
      el.dataset.rovingActive = 'false';
    }
    const target = list[next] as HTMLElement;
    target.tabIndex = 0;
    target.dataset.rovingActive = 'true';
    target.focus();
  };

  return (
    <div ref={ref} role="toolbar" aria-label={label} aria-orientation="horizontal" onKeyDown={onKeyDown} className={cx('flex flex-wrap items-center gap-2', className)}>
      {children}
    </div>
  );
}
