import { useEffect, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation } from 'react-router';

interface Props {
  title: string;
  children?: ReactNode;
}

/**
 * Заголовок страницы: обновляет document.title и после навигации переводит фокус
 * на h1, чтобы клавиатура и скринридер начинали с нового содержимого (WCAG 2.4.3).
 */
export function PageHeading({ title, children }: Props) {
  const { t } = useTranslation();
  const ref = useRef<HTMLHeadingElement>(null);
  // key меняется при каждой навигации — в том числе между workspace на том же маршруте.
  const { key: locationKey } = useLocation();

  useEffect(() => {
    document.title = t('app.documentTitle', { page: title });
  }, [t, title]);

  useEffect(() => {
    ref.current?.focus();
  }, [locationKey]);

  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
      <h1 id="page-title" ref={ref} tabIndex={-1} className="text-2xl font-semibold text-fg">
        {title}
      </h1>
      {children}
    </div>
  );
}
