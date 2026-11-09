import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Group, Panel, Separator, useDefaultLayout, useGroupRef, type Layout, type LayoutStorage, type PanelProps } from 'react-resizable-panels';
import { cx } from './cx';
import { Icon } from './Icon';

/** Префикс ключей раскладок в localStorage (только удобство пользователя; сброс — resetLayout). */
export const LAYOUT_STORAGE_PREFIX = 'aicd.layout.';

/** localStorage с try/catch: в приватном режиме/при запрете хранилища раскладка просто не сохраняется. */
export const safeLayoutStorage: LayoutStorage = {
  getItem(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  setItem(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* хранилище недоступно — размеры действуют до перезагрузки */
    }
  },
};

export interface ResizableGroupControls {
  reset: () => void;
  /**
   * Изменяет размер панели на `delta` процентных пунктов за счёт соседней (группа из двух панелей) —
   * альтернатива перетаскиванию одним указателем без жеста перетаскивания (WCAG 2.5.7): кнопки или
   * пункты меню. Ограничения minSize/maxSize применяет библиотека (`setLayout` валидирует раскладку).
   */
  resize: (panelId: string, delta: number) => void;
}
const ResizableContext = createContext<ResizableGroupControls | null>(null);

/** Управление размерами панелей ближайшей ResizableGroup (сброс к значениям по умолчанию, шаг). */
export function useResizableGroup(): ResizableGroupControls {
  return useContext(ResizableContext) ?? { reset: () => undefined, resize: () => undefined };
}

/**
 * Группа изменяемых панелей (react-resizable-panels v4) с сохранением размеров в localStorage
 * под ключом `aicd.layout.<id>`. Перетаскивание (WCAG 2.5.7) дублируется клавиатурой на
 * разделителе (стрелки, Home/End, Enter — свернуть сворачиваемую панель), а для указателя без
 * перетаскивания — `useResizableGroup().resize/reset` (кнопки/меню экрана, например «Размеры панелей»
 * в Mission Control).
 */
export function ResizableGroup({
  id,
  orientation = 'horizontal',
  defaultLayout,
  children,
  className,
}: {
  /** Уникальный ключ раскладки (например, `mission-control`). */
  id: string;
  orientation?: 'horizontal' | 'vertical';
  /** Размеры по умолчанию (id панели → %), используются для сброса. */
  defaultLayout?: Layout;
  children: ReactNode;
  className?: string;
}) {
  const groupRef = useGroupRef();
  const storageId = `${LAYOUT_STORAGE_PREFIX}${id}`;
  const persisted = useDefaultLayout({ id: storageId, storage: safeLayoutStorage, onlySaveAfterUserInteractions: true });
  const reset = useCallback(() => {
    if (defaultLayout) groupRef.current?.setLayout(defaultLayout);
    try {
      for (let i = window.localStorage.length - 1; i >= 0; i -= 1) {
        const key = window.localStorage.key(i);
        if (key?.includes(storageId)) window.localStorage.removeItem(key);
      }
    } catch {
      /* ignore */
    }
  }, [defaultLayout, groupRef, storageId]);
  const resize = useCallback(
    (panelId: string, delta: number) => {
      const group = groupRef.current;
      if (!group) return;
      const layout = group.getLayout();
      const current = layout[panelId];
      const other = Object.keys(layout).find((key) => key !== panelId);
      if (current === undefined || other === undefined || Object.keys(layout).length !== 2) return;
      const next = Math.min(100, Math.max(0, current + delta));
      group.setLayout({ [panelId]: next, [other]: 100 - next });
    },
    [groupRef],
  );
  const value = useMemo(() => ({ reset, resize }), [reset, resize]);
  return (
    <ResizableContext.Provider value={value}>
      <Group
        id={id}
        groupRef={groupRef}
        orientation={orientation}
        defaultLayout={persisted.defaultLayout ?? defaultLayout}
        onLayoutChanged={persisted.onLayoutChanged}
        className={cx('h-full w-full', className)}
      >
        {children}
      </Group>
    </ResizableContext.Provider>
  );
}

export function ResizablePanel({ className, ...props }: PanelProps & { id: string }) {
  return <Panel className={cx('min-h-0 min-w-0', className)} {...props} />;
}

/**
 * Разделитель панелей: role=separator с aria-valuenow (библиотека), доступное имя, видимый фокус,
 * зона захвата ≥ 24 px (WCAG 2.5.8) при тонкой линии 1 px.
 */
export function ResizableHandle({
  before,
  after,
  orientation = 'horizontal',
  className,
}: {
  /** Подписи соседних панелей — для доступного имени («Навигатор / Лента»). */
  before: string;
  after: string;
  orientation?: 'horizontal' | 'vertical';
  className?: string;
}) {
  const { t } = useTranslation();
  const horizontal = orientation === 'horizontal';
  return (
    <Separator
      aria-label={t('ds.resizable.handle', { before, after })}
      className={cx(
        'group relative flex shrink-0 items-center justify-center outline-none',
        horizontal ? 'w-3 cursor-col-resize' : 'h-3 cursor-row-resize',
        'focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-focus',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cx(
          'absolute bg-border transition-colors duration-instant group-hover:bg-border-active group-data-[separator=active]:bg-accent group-focus-visible:bg-focus',
          horizontal ? 'inset-y-0 left-1/2 w-px -translate-x-1/2' : 'inset-x-0 top-1/2 h-px -translate-y-1/2',
        )}
      />
      <span
        aria-hidden="true"
        className="relative z-10 flex h-6 w-3 items-center justify-center rounded-sm border border-border-strong bg-surface-raised text-fg-subtle group-hover:text-fg"
      >
        <Icon name={horizontal ? 'gripVertical' : 'gripHorizontal'} className="h-3 w-3" />
      </span>
    </Separator>
  );
}
