import { useMemo, useState, type HTMLAttributes, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { cx } from './cx';
import { Icon } from './Icon';

export type SortDirection = 'asc' | 'desc';
export interface SortState {
  columnId: string;
  direction: SortDirection;
}

export interface Column<T> {
  id: string;
  /** Текст заголовка (используется и в подписи сортировки). */
  header: string;
  cell: (row: T) => ReactNode;
  /** Значение для клиентской сортировки; наличие делает столбец сортируемым. */
  sortValue?: (row: T) => string | number | null | undefined;
  /** Сортируемый на сервере столбец (с `onSortChange`), если sortValue не задан. */
  sortable?: boolean;
  /** Ячейка — заголовок строки (th scope=row). */
  rowHeader?: boolean;
  className?: string;
}

interface TableProps<T> {
  /** Доступное имя таблицы (caption); по умолчанию визуально скрыто. */
  caption: string;
  captionVisible?: boolean;
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  /** Управляемая сортировка (серверная: строки уже отсортированы). */
  sort?: SortState | null;
  onSortChange?: (sort: SortState) => void;
  /** Начальная клиентская сортировка (неуправляемый режим). */
  defaultSort?: SortState | null;
  /** Что показать вместо tbody без строк. */
  empty?: ReactNode;
  rowProps?: (row: T) => HTMLAttributes<HTMLTableRowElement> & Record<`data-${string}`, string | number | undefined>;
  /** Минимальная ширина таблицы (Tailwind-класс); уже — горизонтальная прокрутка контейнера. */
  minWidthClass?: string;
  className?: string;
}

const collator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' });

function compare(a: string | number | null | undefined, b: string | number | null | undefined): number {
  if (a === b) return 0;
  if (a === null || a === undefined) return 1; // пустые — в конец
  if (b === null || b === undefined) return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return collator.compare(String(a), String(b));
}

/**
 * Таблица данных с сортируемыми заголовками (APG Sortable Table): кнопка в th,
 * aria-sort на отсортированном столбце, состояние сортировки озвучивается в caption.
 * Клиентская сортировка — по `sortValue`; серверная — `sort` + `onSortChange`.
 */
export function Table<T>({
  caption,
  captionVisible = false,
  columns,
  rows,
  rowKey,
  sort: controlledSort,
  onSortChange,
  defaultSort = null,
  empty,
  rowProps,
  minWidthClass = 'min-w-[40rem]',
  className,
}: TableProps<T>) {
  const { t } = useTranslation();
  const [localSort, setLocalSort] = useState<SortState | null>(defaultSort);
  const controlled = onSortChange !== undefined;
  const sort = controlled ? (controlledSort ?? null) : localSort;

  const sorted = useMemo(() => {
    if (controlled || !sort) return rows;
    const col = columns.find((c) => c.id === sort.columnId);
    if (!col?.sortValue) return rows;
    const factor = sort.direction === 'asc' ? 1 : -1;
    const value = col.sortValue;
    return [...rows].sort((a, b) => factor * compare(value(a), value(b)));
  }, [rows, columns, sort, controlled]);

  const toggle = (col: Column<T>) => {
    const next: SortState = {
      columnId: col.id,
      direction: sort?.columnId === col.id && sort.direction === 'asc' ? 'desc' : 'asc',
    };
    if (controlled) onSortChange(next);
    else setLocalSort(next);
  };

  const sortedCol = sort ? columns.find((c) => c.id === sort.columnId) : undefined;
  const directionLabel = (d: SortDirection) => (d === 'asc' ? t('ui.sortAscending') : t('ui.sortDescending'));

  return (
    <div className={cx('overflow-x-auto rounded-lg border border-border bg-surface', className)}>
      <table className={cx('w-full text-left text-sm', minWidthClass)}>
        <caption className={captionVisible ? 'px-3 py-2 text-left font-semibold' : 'sr-only'}>
          {caption}
          {sortedCol && sort ? (
            <span className="sr-only">
              {' '}
              ({t('ui.sortedBy', { column: sortedCol.header, direction: directionLabel(sort.direction) })})
            </span>
          ) : null}
        </caption>
        <thead className="bg-surface-muted text-fg-muted">
          <tr>
            {columns.map((col) => {
              const sortable = Boolean(col.sortValue) || (controlled && col.sortable);
              const active = sort?.columnId === col.id;
              return (
                <th
                  key={col.id}
                  scope="col"
                  aria-sort={active && sort ? (sort.direction === 'asc' ? 'ascending' : 'descending') : undefined}
                  className={cx('px-3 py-2 font-semibold', col.className)}
                >
                  {sortable ? (
                    <button
                      type="button"
                      onClick={() => toggle(col)}
                      className="-mx-2 inline-flex min-h-[2.25rem] items-center gap-1 rounded px-2 font-semibold hover:bg-surface hover:text-fg"
                    >
                      {col.header}
                      <Icon
                        name={active && sort ? (sort.direction === 'asc' ? 'sortAsc' : 'sortDesc') : 'sortNone'}
                        className={cx('h-4 w-4', !active && 'opacity-50')}
                      />
                    </button>
                  ) : (
                    col.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-3 py-6 text-center text-fg-muted">
                {empty ?? t('ui.tableEmpty')}
              </td>
            </tr>
          ) : (
            sorted.map((row) => (
              <tr key={rowKey(row)} className="border-t border-border align-top" {...rowProps?.(row)}>
                {columns.map((col) =>
                  col.rowHeader ? (
                    <th key={col.id} scope="row" className={cx('px-3 py-2 font-medium', col.className)}>
                      {col.cell(row)}
                    </th>
                  ) : (
                    <td key={col.id} className={cx('px-3 py-2', col.className)}>
                      {col.cell(row)}
                    </td>
                  ),
                )}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
