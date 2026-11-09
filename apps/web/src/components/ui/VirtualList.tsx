import { useVirtualizer } from '@tanstack/react-virtual';
import { useCallback, useEffect, useReducer, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { cx } from './cx';
import { Icon } from './Icon';

export interface VirtualListProps<T> {
  items: readonly T[];
  getKey: (item: T, index: number) => string;
  renderItem: (item: T, index: number) => ReactNode;
  /** Доступное имя списка (aria-label). */
  label: string;
  /** Оценка высоты строки, px (фактическая измеряется). */
  estimateSize?: number;
  overscan?: number;
  /**
   * list — список (listitem + aria-setsize/posinset); feed — лента (article, APG Feed: aria-busy
   * при подгрузке). Лента событий/CLI — feed.
   */
  kind?: 'list' | 'feed';
  /** Идёт подгрузка (aria-busy). */
  busy?: boolean;
  /**
   * Следовать за концом (логи, CLI): новые элементы прокручивают вниз, только если пользователь
   * уже у конца; иначе — кнопка «Перейти к последнему» (позиция чтения не сдвигается).
   */
  followOutput?: boolean;
  /** Класс высоты/рамки контейнера прокрутки (по умолчанию h-96). */
  className?: string;
  /** Начальная высота окна (jsdom/SSR, где размеры не измеряются). */
  initialHeight?: number;
}

/**
 * Виртуализированный список на @tanstack/react-virtual (план §4.3/§5.9): в DOM только видимые
 * строки, но каждая сообщает позицию (aria-posinset/aria-setsize), поэтому скринридер знает размер.
 * Клавиатура: контейнер в порядке Tab; ↑/↓ — предыдущий/следующий элемент, Home/End — первый/
 * последний, PageUp/PageDown — на экран; фокус переходит на элемент (tabindex=-1), список
 * прокручивается к нему. Поиск по содержимому — через фильтр API (Ctrl+F не видит скрытые строки).
 */
/** Ключ видимого окна: эффект фокуса повторяется, когда нужная строка появилась в DOM. */
function virtualItemsKey(v: { getVirtualItems: () => { index: number }[] }): string {
  const items = v.getVirtualItems();
  return `${items[0]?.index ?? -1}:${items.at(-1)?.index ?? -1}`;
}

export function VirtualList<T>({
  items,
  getKey,
  renderItem,
  label,
  estimateSize = 40,
  overscan = 8,
  kind = 'list',
  busy = false,
  followOutput = false,
  className,
  initialHeight = 384,
}: VirtualListProps<T>) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(0);
  const [atEnd, setAtEnd] = useState(true);
  const pendingFocus = useRef<number | null>(null);
  // Перерисовка после scrollToIndex, даже если активный индекс не изменился (фокус из контейнера).
  const [focusTick, requestFocus] = useReducer((x: number) => x + 1, 0);
  const count = items.length;

  // React Compiler не используется; возвращаемые функции виртуализатора не мемоизируются вручную.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => estimateSize,
    overscan,
    initialRect: { width: 0, height: initialHeight },
    getItemKey: (index) => getKey(items[index]!, index),
  });

  // Следование за концом: только если пользователь уже был у конца списка.
  const prevCount = useRef(count);
  useEffect(() => {
    if (followOutput && atEnd && count > prevCount.current && count > 0) virtualizer.scrollToIndex(count - 1, { align: 'end' });
    prevCount.current = count;
  }, [count, followOutput, atEnd, virtualizer]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setAtEnd(el.scrollHeight - el.scrollTop - el.clientHeight < 8);
  }, []);

  const focusIndex = useCallback(
    (index: number) => {
      if (count === 0) return;
      const next = Math.max(0, Math.min(count - 1, index));
      setActive(next);
      pendingFocus.current = next;
      virtualizer.scrollToIndex(next, { align: 'auto' });
      requestFocus();
    },
    [count, virtualizer],
  );

  const windowKey = virtualItemsKey(virtualizer);
  // Фокус на элемент — после того, как он попал в DOM (focusTick — запрос фокуса).
  useEffect(() => {
    const index = pendingFocus.current;
    if (index === null) return;
    const el = scrollRef.current?.querySelector<HTMLElement>(`[data-index="${index}"]`);
    if (el) {
      pendingFocus.current = null;
      el.focus({ preventScroll: true });
    }
  }, [focusTick, windowKey]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const visibleCount = Math.max(1, Math.floor((scrollRef.current?.clientHeight || initialHeight) / estimateSize) - 1);
    const fromContainer = e.target === scrollRef.current;
    const current = fromContainer ? active - (e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0) : active;
    const keys: Record<string, number> = {
      ArrowDown: current + 1,
      ArrowUp: current - 1,
      Home: 0,
      End: count - 1,
      PageDown: current + visibleCount,
      PageUp: current - visibleCount,
    };
    const target = keys[e.key];
    if (target === undefined || count === 0) return;
    e.preventDefault();
    focusIndex(target);
  };

  const itemRole = kind === 'feed' ? 'article' : 'listitem';
  const virtualItems = virtualizer.getVirtualItems();

  return (
    <div className="relative">
      {/*
        Контейнер прокрутки фокусируем (axe scrollable-region-focusable) и обрабатывает стрелки —
        клавиатурная модель APG Feed/списка; интерактивной роли у списка нет, отсюда исключение.
      */}
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div
        ref={scrollRef}
        role={kind}
        aria-label={label}
        aria-busy={busy || undefined}
        tabIndex={0} // eslint-disable-line jsx-a11y/no-noninteractive-tabindex
        onKeyDown={onKeyDown}
        onFocus={(e) => {
          const index = (e.target as HTMLElement).dataset.index;
          if (index !== undefined) setActive(Number(index));
        }}
        onScroll={onScroll}
        className={cx('overflow-y-auto overscroll-contain rounded-lg border border-border bg-surface', className ?? 'h-96')}
        data-testid="virtual-list"
      >
        <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
          {virtualItems.map((row) => (
            <div
              key={row.key}
              ref={virtualizer.measureElement}
              data-index={row.index}
              role={itemRole}
              aria-posinset={row.index + 1}
              aria-setsize={count}
              tabIndex={-1}
              className="absolute left-0 top-0 w-full border-b border-border px-3 py-2 text-sm focus-visible:z-10 focus-visible:outline-offset-[-2px]"
              style={{ transform: `translateY(${row.start}px)` }}
            >
              {renderItem(items[row.index]!, row.index)}
            </div>
          ))}
        </div>
      </div>
      {followOutput && !atEnd && count > 0 ? (
        <button
          type="button"
          className="btn-secondary btn-sm absolute bottom-3 right-3 shadow-e2"
          onClick={() => {
            virtualizer.scrollToIndex(count - 1, { align: 'end' });
            setAtEnd(true);
          }}
        >
          <Icon name="chevronDown" className="h-4 w-4" />
          {t('ds.virtualList.jumpToEnd')}
        </button>
      ) : null}
    </div>
  );
}
