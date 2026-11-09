import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, Outlet, useLocation, useNavigate } from 'react-router';
import { IconButton } from '../components/ui/Button';
import { Dialog, DialogContent } from '../components/ui/Dialog';
import { cx } from '../components/ui/cx';
import { LogoMark } from '../components/ui/Logo';
import { MQ_DESKTOP, MQ_REDUCED_MOTION, MQ_WIDE, useMediaQuery, useSidebarMode } from '../lib/layoutPrefs';
import { useGlobalShortcuts, useShortcutsEnabled } from '../lib/shortcuts';
import { mainWidthFor, type MainWidth } from '../routes';
import { useCurrentWorkspace } from '../workspace';
import { lazyPage } from '../routes/lazyPage';
import { ContextPanelProvider, ContextPanelToggle, ShellContextPanel } from './ContextPanel';
import { GlobalSearch } from './GlobalSearch';
import { NotificationsBell } from './Notifications';
import { ProjectSwitcher } from './ProjectSwitcher';
import { ProviderChips } from './ProviderChips';
import { ShortcutsDialog } from './ShortcutsDialog';
import { SidebarNav, useNavEntries } from './Sidebar';
import { UserMenu } from './UserMenu';
import { WorkspaceSwitcher } from './WorkspaceSwitcher';

// Палитра — отдельный чанк; подписи подразделов Настроек — из namespace settings.
const CommandPalette = lazyPage(() => import('./CommandPalette'), 'default', ['settings']);

/** Ширина рабочей области по типу экрана (handle.width маршрута). */
const MAIN_WIDTH: Record<MainWidth, string> = {
  standard: 'max-w-[100rem]',
  form: 'max-w-4xl',
  full: 'max-w-none',
};

/**
 * Плавное появление нового экрана (только opacity/transform, 180 мс; при prefers-reduced-motion —
 * без анимации). Через Web Animations API: поддерево не перемонтируется, состояние страниц и фокус
 * (PageHeading) не затрагиваются.
 */
function usePageTransition(ref: React.RefObject<HTMLElement | null>, pathname: string, reduced: boolean) {
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const el = ref.current;
    if (!el || reduced || typeof el.animate !== 'function') return;
    el.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], {
      duration: 180,
      easing: 'cubic-bezier(0.2, 0, 0, 1)',
    });
  }, [pathname, reduced, ref]);
}

/**
 * Оболочка «Neural Engineering Command Center» (F2, план §2.1, §5.8, §6.1): skip link → верхняя
 * панель (знак, workspace/проект, поиск, Command Palette, статус CLI, справка, уведомления, профиль)
 * → боковая навигация (≥ 768 px; rail/развёрнута, сохраняется; < 768 px — drawer) → main → правая
 * контекстная панель (если экран её включил). Порядок фокуса — как порядок в DOM (WCAG 2.4.3).
 */
export function AppLayout() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [navOpen, setNavOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteLoaded, setPaletteLoaded] = useState(false);
  const shortcutsOn = useShortcutsEnabled();
  const desktop = useMediaQuery(MQ_DESKTOP, true);
  const wide = useMediaQuery(MQ_WIDE, true);
  // Полные подписи чипов CLI — только на очень широком экране; иначе компактный чип (код + иконка, подсказка).
  const xwide = useMediaQuery('(min-width: 1536px)', true);
  const reducedMotion = useMediaQuery(MQ_REDUCED_MOTION, false);
  const [sidebarMode, setSidebarMode] = useSidebarMode();
  const rail = sidebarMode === 'collapsed' || (sidebarMode === 'auto' && !wide);
  const inWorkspace = Boolean(workspaceId && membership);
  const mainRef = useRef<HTMLDivElement>(null);
  usePageTransition(mainRef, pathname, reducedMotion);

  // Drawer закрывается при переходе на ≥ 768 px (sidebar уже виден) — корректировка состояния при рендере.
  if (desktop && navOpen) setNavOpen(false);

  const entries = useNavEntries();
  const sequences = useMemo(() => {
    const map: Record<string, () => void> = {};
    if (workspaceId) for (const e of entries) if (e.shortcut) map[e.shortcut] = () => navigate(`/w/${workspaceId}/${e.path}`);
    return map;
  }, [entries, navigate, workspaceId]);
  const openHelp = useCallback(() => setHelpOpen(true), []);
  const openPalette = useCallback(() => {
    setPaletteLoaded(true);
    setPaletteOpen(true);
  }, []);
  const toggleSidebar = useCallback(() => setSidebarMode(rail ? 'expanded' : 'collapsed'), [rail, setSidebarMode]);
  useGlobalShortcuts({ onHelp: openHelp, sequences }, shortcutsOn && !helpOpen && !navOpen && !paletteOpen);

  // Ctrl/Cmd+K — Command Palette (сочетание с модификатором: работает и при выключенных односимвольных, WCAG 2.1.4).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (paletteOpen) setPaletteOpen(false);
        else openPalette();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [openPalette, paletteOpen]);

  const width = mainWidthFor(pathname);

  return (
    <ContextPanelProvider>
      <div className="flex min-h-screen flex-col" data-sidebar={desktop ? (rail ? 'rail' : 'expanded') : 'drawer'}>
        <a
          href="#main"
          className="sr-only z-toast rounded-md bg-surface px-4 py-2 font-medium text-link focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
          onClick={(e) => {
            e.preventDefault();
            document.getElementById('main')?.focus();
          }}
        >
          {t('layout.skipToContent')}
        </a>
        <header className="glass sticky top-0 z-overlay border-x-0 border-t-0" data-testid="topbar">
          <div className="flex h-topbar items-center gap-2 px-2 sm:px-3">
            {!desktop ? (
              <IconButton icon="menu" label={t('layout.openNav')} onClick={() => setNavOpen(true)} aria-expanded={navOpen} aria-haspopup="dialog" />
            ) : null}
            <Link to="/" className="inline-flex shrink-0 items-center gap-2 rounded-md px-1 text-base font-semibold text-fg no-underline" data-testid="topbar-logo">
              <LogoMark className="h-7 w-7" />
              <span className={cx(wide ? '' : 'sr-only')}>{t('app.name')}</span>
            </Link>
            {inWorkspace && desktop ? (
              <div className="flex min-w-0 items-center gap-2">
                <WorkspaceSwitcher compact={!wide} />
                {wide ? <ProjectSwitcher /> : null}
              </div>
            ) : null}
            <div className="flex min-w-0 flex-1 justify-center px-1">{inWorkspace && desktop ? <GlobalSearch /> : null}</div>
            {inWorkspace && desktop ? <ProviderChips compact={!xwide} /> : null}
            <IconButton
              icon={desktop ? 'command' : 'search'}
              label={desktop ? t('shell.palette.open') : t('shell.palette.openMobile')}
              onClick={openPalette}
              aria-haspopup="dialog"
              data-testid="palette-trigger"
            />
            <IconButton icon="keyboard" label={t('layout.shortcuts')} onClick={() => setHelpOpen(true)} className="hidden sm:inline-flex" />
            {inWorkspace ? <NotificationsBell /> : null}
            <ContextPanelToggle />
            <UserMenu compact={!desktop} onShowShortcuts={desktop ? undefined : openHelp} />
          </div>
        </header>

        <div className="flex flex-1">
          {desktop ? (
            <aside
              className={cx(
                'sticky top-topbar flex h-[calc(100dvh-theme(spacing.topbar))] shrink-0 flex-col border-r border-border bg-canvas-secondary transition-[width] duration-slow ease-standard',
                rail ? 'w-rail' : 'w-sidebar',
              )}
              data-testid="sidebar"
            >
              <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overflow-x-hidden">
                <SidebarNav rail={rail} />
              </div>
              <div className={cx('flex border-t border-border p-2', rail ? 'justify-center' : 'justify-end')}>
                <IconButton
                  icon={rail ? 'panelLeftOpen' : 'panelLeftClose'}
                  label={rail ? t('shell.sidebar.expand') : t('shell.sidebar.collapse')}
                  onClick={toggleSidebar}
                  data-testid="sidebar-toggle"
                />
              </div>
            </aside>
          ) : null}

          <main id="main" tabIndex={-1} className="min-w-0 flex-1" data-width={width}>
            <div ref={mainRef} className={cx('mx-auto w-full px-4 py-6 md:px-6 xl:px-8', MAIN_WIDTH[width])}>
              <Outlet />
            </div>
          </main>
          <ShellContextPanel />
        </div>

        {!desktop ? (
          <Dialog open={navOpen} onOpenChange={setNavOpen}>
            <DialogContent title={t('layout.navDrawerTitle')} variant="drawer-left" className="p-0">
              <div className="flex flex-col gap-3">
                {inWorkspace ? (
                  <div className="flex flex-col gap-2 border-b border-border pb-3">
                    <WorkspaceSwitcher />
                    <ProjectSwitcher />
                    <ProviderChips onNavigate={() => setNavOpen(false)} />
                  </div>
                ) : null}
                <SidebarNav onNavigate={() => setNavOpen(false)} />
              </div>
            </DialogContent>
          </Dialog>
        ) : null}

        <ShortcutsDialog open={helpOpen} onOpenChange={setHelpOpen} entries={entries} />
        {paletteLoaded ? (
          <Suspense fallback={null}>
            <CommandPalette
              open={paletteOpen}
              onOpenChange={setPaletteOpen}
              onShowShortcuts={openHelp}
              onToggleSidebar={desktop ? toggleSidebar : null}
            />
          </Suspense>
        ) : null}
      </div>
    </ContextPanelProvider>
  );
}
