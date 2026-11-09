import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { IconButton } from '../components/ui/Button';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '../components/ui/DropdownMenu';
import { cx } from '../components/ui/cx';
import { Icon } from '../components/ui/Icon';
import { CONTEXT_WIDTH, MQ_WIDE, clampContextWidth, useContextOpenPref, useContextWidth, useMediaQuery } from '../lib/layoutPrefs';

/**
 * Правая контекстная панель оболочки (F2, план §2.1, §5.8). Экран включает её сам — компонентом
 * `<ContextPanel id title>…</ContextPanel>` (slot через portal); без него панели и кнопки нет.
 * Содержимое по умолчанию — «Нет выбранного контекста»; экраны наполняют её в следующих фазах
 * (F4 Agent Inspector и т. п.). Открытость сохраняется по экрану, ширина — общая (localStorage).
 * ≥ 1280 px панель занимает место в раскладке (push) и меняет ширину разделителем (мышь, клавиатура,
 * кнопка сброса — WCAG 2.5.7); уже — накладывается поверх (фокус переносится внутрь и возвращается).
 */

interface Registration {
  id: string;
  title: string;
}

interface ContextPanelState {
  registration: Registration | null;
  register: (r: Registration) => () => void;
  target: HTMLElement | null;
  setTarget: (el: HTMLElement | null) => void;
}

const Ctx = createContext<ContextPanelState | null>(null);

export const CONTEXT_PANEL_ID = 'context-panel';

export function ContextPanelProvider({ children }: { children: ReactNode }) {
  const [registration, setRegistration] = useState<Registration | null>(null);
  const [target, setTarget] = useState<HTMLElement | null>(null);
  const register = useCallback((r: Registration) => {
    setRegistration(r);
    return () => setRegistration((cur) => (cur?.id === r.id ? null : cur));
  }, []);
  const value = useMemo(() => ({ registration, register, target, setTarget }), [registration, register, target]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Состояние «нет выбранного контекста» (по умолчанию). */
export function NoContextSelected() {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-start gap-2 py-6 text-sm text-fg-muted" data-testid="context-empty">
      <span className="rounded-full bg-surface-muted p-2">
        <Icon name="info" className="h-5 w-5" />
      </span>
      <p className="font-medium text-fg">{t('shell.context.emptyTitle')}</p>
      <p>{t('shell.context.emptyDescription')}</p>
    </div>
  );
}

/**
 * Slot экрана: регистрирует панель (заголовок) и рендерит содержимое в неё, когда она открыта.
 * `children` отсутствует/`null` — «Нет выбранного контекста».
 */
export function ContextPanel({ id, title, children }: { id: string; title: string; children?: ReactNode }) {
  const ctx = useContext(Ctx);
  const register = ctx?.register;
  useEffect(() => register?.({ id, title }), [register, id, title]);
  if (!ctx?.target) return null;
  const empty = children === undefined || children === null || children === false;
  return createPortal(empty ? <NoContextSelected /> : children, ctx.target);
}

function useContextPanelState() {
  const ctx = useContext(Ctx);
  const wide = useMediaQuery(MQ_WIDE, true);
  const [pref, setPref] = useContextOpenPref(ctx?.registration?.id ?? null);
  // Без явного выбора: открыта на широком экране (push), закрыта при наложении.
  const open = Boolean(ctx?.registration) && (pref ?? wide);
  return { ctx, wide, open, setOpen: setPref };
}

/** Кнопка верхней панели (видна только на экранах, включивших панель). */
export function ContextPanelToggle() {
  const { t } = useTranslation();
  const { ctx, open, setOpen } = useContextPanelState();
  if (!ctx?.registration) return null;
  return (
    <IconButton
      icon={open ? 'panelRightClose' : 'panelRightOpen'}
      label={open ? t('shell.context.hide') : t('shell.context.show')}
      aria-expanded={open}
      aria-controls={CONTEXT_PANEL_ID}
      data-context-toggle=""
      onClick={() => {
        setOpen(!open);
        if (!open) requestAnimationFrame(() => document.getElementById(`${CONTEXT_PANEL_ID}-title`)?.focus());
      }}
    />
  );
}

/** Панель в раскладке оболочки (после main). */
export function ShellContextPanel() {
  const { t } = useTranslation();
  const { ctx, wide, open, setOpen } = useContextPanelState();
  const [width, setWidth] = useContextWidth();
  const [dragWidth, setDragWidth] = useState<number | null>(null);
  const drag = useRef<{ x: number; width: number } | null>(null);
  const setTarget = ctx?.setTarget;
  const bodyRef = useCallback((el: HTMLDivElement | null) => setTarget?.(el), [setTarget]);

  const close = useCallback(() => {
    setOpen(false);
    requestAnimationFrame(() => (document.querySelector('[data-context-toggle]') as HTMLElement | null)?.focus());
  }, [setOpen]);

  // Наложение (< 1280 px): Escape закрывает и возвращает фокус на кнопку. Фокус, ушедший в основную
  // область (Tab, клик), закрывает наложение: иначе сфокусированный элемент мог бы оказаться под
  // панелью (WCAG 2.4.11 Focus Not Obscured); фокус остаётся там, куда его перевёл пользователь.
  useEffect(() => {
    if (!open || wide) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented) close();
    };
    const onFocusIn = (e: FocusEvent) => {
      if (e.target instanceof Element && e.target.closest('#main')) setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [open, wide, close, setOpen]);

  if (!ctx?.registration || !open) return null;
  const current = dragWidth ?? width;

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, width: current };
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    setDragWidth(clampContextWidth(drag.current.width + (drag.current.x - e.clientX)));
  };
  const onPointerUp = () => {
    if (!drag.current) return;
    drag.current = null;
    if (dragWidth !== null) setWidth(dragWidth);
    setDragWidth(null);
  };
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? CONTEXT_WIDTH.step * 4 : CONTEXT_WIDTH.step;
    const next =
      e.key === 'ArrowLeft'
        ? current + step
        : e.key === 'ArrowRight'
          ? current - step
          : e.key === 'Home'
            ? CONTEXT_WIDTH.min
            : e.key === 'End'
              ? CONTEXT_WIDTH.max
              : null;
    if (e.key === 'Enter') {
      e.preventDefault();
      close();
      return;
    }
    if (next === null) return;
    e.preventDefault();
    setWidth(next);
  };

  return (
    <aside
      id={CONTEXT_PANEL_ID}
      aria-labelledby={`${CONTEXT_PANEL_ID}-title`}
      data-testid="context-panel"
      data-mode={wide ? 'push' : 'overlay'}
      className={cx(
        'flex shrink-0 border-l border-border bg-canvas-secondary',
        wide
          ? 'sticky top-14 h-[calc(100dvh-3.5rem)]'
          : 'fixed bottom-0 right-0 top-14 z-overlay w-[min(24rem,100vw)] shadow-e3 animate-slide-in-right',
      )}
      style={wide ? { width: current } : undefined}
    >
      {wide ? (
        // Фокусируемый role=separator — виджет «разделитель окна» (WAI-ARIA APG Window Splitter):
        // стрелки/Home/End меняют ширину, Enter скрывает панель; jsx-a11y считает separator неинтерактивным.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          role="separator"
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
          aria-orientation="vertical"
          aria-label={t('shell.context.resize')}
          aria-controls={CONTEXT_PANEL_ID}
          aria-valuemin={CONTEXT_WIDTH.min}
          aria-valuemax={CONTEXT_WIDTH.max}
          aria-valuenow={current}
          aria-valuetext={t('shell.context.widthValue', { px: current })}
          data-testid="context-separator"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onKeyDown={onKeyDown}
          className="group relative -ml-3 w-6 shrink-0 cursor-col-resize touch-none outline-none focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-focus"
        >
          <span
            aria-hidden="true"
            className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-border transition-colors duration-instant group-hover:bg-border-active group-focus-visible:bg-focus"
          />
        </div>
      ) : null}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-1 border-b border-border px-3 py-2">
          <h2 id={`${CONTEXT_PANEL_ID}-title`} tabIndex={-1} className="mr-auto min-w-0 truncate text-base font-semibold text-fg">
            {ctx.registration.title}
          </h2>
          {wide ? (
            // Ширина без перетаскивания (WCAG 2.5.7): шаг ±CONTEXT_WIDTH.step×4 и сброс из меню.
            <Menu>
              <MenuTrigger asChild>
                <IconButton icon="panelSizes" label={t('shell.context.widthMenu')} data-testid="context-width-menu" />
              </MenuTrigger>
              <MenuContent>
                <MenuItem disabled={current >= CONTEXT_WIDTH.max} onSelect={() => setWidth(current + CONTEXT_WIDTH.step * 4)}>
                  {t('shell.context.wider')}
                </MenuItem>
                <MenuItem disabled={current <= CONTEXT_WIDTH.min} onSelect={() => setWidth(current - CONTEXT_WIDTH.step * 4)}>
                  {t('shell.context.narrower')}
                </MenuItem>
                <MenuItem disabled={width === CONTEXT_WIDTH.default} onSelect={() => setWidth(null)}>
                  {t('shell.context.resetWidth')}
                </MenuItem>
              </MenuContent>
            </Menu>
          ) : null}
          <IconButton icon="close" label={t('shell.context.close')} onClick={close} />
        </div>
        <div ref={bodyRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-3" data-testid="context-panel-body" />
      </div>
    </aside>
  );
}
