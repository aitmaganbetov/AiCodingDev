import { useEffect, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation } from 'react-router';

interface Props {
  title: string;
  children?: ReactNode;
}

/**
 * Заголовок страницы: обновляет document.title и после перехода на другой экран переводит
 * фокус на h1, чтобы клавиатура и скринридер начинали с нового содержимого (WCAG 2.4.3).
 * Фокус переносится только при смене pathname (включая смену workspace — он в пути): смена
 * query-параметров (вкладка, фильтр, пагинация) — состояние того же экрана, и фокус остаётся
 * на элементе управления, которым пользователь её вызвал.
 */
export function PageHeading({ title, children }: Props) {
  const { t } = useTranslation();
  const ref = useRef<HTMLHeadingElement>(null);
  const { pathname } = useLocation();

  useEffect(() => {
    document.title = t('app.documentTitle', { page: title });
  }, [t, title]);

  useEffect(() => {
    // Ленивый экран монтируется позже перехода: не отбираем фокус у уже открытого меню/диалога
    // (Radix закрыл бы его при потере фокуса), иначе — фокус на h1.
    const active = document.activeElement;
    if (active instanceof HTMLElement && active.closest('[role="menu"], [role="dialog"], [role="listbox"]')) return;
    ref.current?.focus();
  }, [pathname]);

  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
      <h1 id="page-title" ref={ref} tabIndex={-1} className="text-2xl font-semibold text-fg">
        {title}
      </h1>
      {children}
    </div>
  );
}
