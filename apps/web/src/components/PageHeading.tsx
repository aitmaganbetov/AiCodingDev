import { useEffect, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation } from 'react-router';

interface Props {
  title: string;
  children?: ReactNode;
}

/**
 * Номер «экрана»: растёт при каждой смене pathname (включая смену workspace — он в пути). Смена
 * query-параметров (вкладка, фильтр, пагинация) — состояние того же экрана. Модульное состояние, а не
 * состояние компонента: PageHeading перемонтируется внутри экрана (загрузка → данные → ошибка), и
 * перенос фокуса должен выполняться один раз на переход, а не на каждое монтирование.
 */
let screen: { seq: number; pathname: string | null } = { seq: 0, pathname: null };
/** Экран, для которого фокус уже перенесён на h1. */
let focusedSeq = -1;

function trackScreen(pathname: string): number {
  if (screen.pathname !== pathname) screen = { seq: screen.seq + 1, pathname };
  return screen.seq;
}

/**
 * Отмечает смену экрана в оболочке (AppLayout), даже если на промежуточном экране нет PageHeading:
 * иначе возврат P → R → P выглядел бы как «тот же экран» и фокус не переносился бы.
 */
export function useScreenChangeTracker(): void {
  trackScreen(useLocation().pathname);
}

const EDITABLE = 'input, textarea, select, [contenteditable=""], [contenteditable="true"]';

/** Сброс модульного состояния (unit-тесты: каждый тест — новое приложение). */
export function resetPageHeadingFocus(): void {
  screen = { seq: 0, pathname: null };
  focusedSeq = -1;
}

/**
 * Заголовок страницы: обновляет document.title и после перехода на другой экран переводит
 * фокус на h1, чтобы клавиатура и скринридер начинали с нового содержимого (WCAG 2.4.3).
 *
 * Фокус переносится один раз на переход (смену pathname), а не при каждом монтировании: экраны с
 * ленивыми данными перемонтируют заголовок (загрузка → данные), и повторный перенос отбирал бы фокус
 * у поля, в котором пользователь уже печатает (поиск в верхней панели, фильтр, форма). Повторно
 * фокус возвращается на h1, только если он потерян (элемент с фокусом удалён — фокус на body).
 * Не отбирается фокус у открытого меню/диалога/списка (Radix закрыл бы его) и у поля ввода в
 * основной области, где пользователь уже начал работу с новым экраном.
 */
export function PageHeading({ title, children }: Props) {
  const { t } = useTranslation();
  const ref = useRef<HTMLHeadingElement>(null);
  const { pathname } = useLocation();
  const seq = trackScreen(pathname);

  useEffect(() => {
    document.title = t('app.documentTitle', { page: title });
  }, [t, title]);

  useEffect(() => {
    const active = document.activeElement;
    const lost = !(active instanceof HTMLElement) || active === document.body || !active.isConnected;
    if (!lost) {
      if (focusedSeq === seq) return;
      if (active.closest('[role="menu"], [role="dialog"], [role="listbox"]')) return;
      if (active.matches(EDITABLE) && active.closest('main')) return;
    }
    ref.current?.focus();
    focusedSeq = seq;
  }, [seq]);

  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
      <h1 id="page-title" ref={ref} tabIndex={-1} className="text-2xl font-semibold text-fg">
        {title}
      </h1>
      {children}
    </div>
  );
}
