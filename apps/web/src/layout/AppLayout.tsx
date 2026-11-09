import { useCallback, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, Outlet, useNavigate } from 'react-router';
import { IconButton } from '../components/ui/Button';
import { Dialog, DialogContent, DialogTrigger } from '../components/ui/Dialog';
import { Icon } from '../components/ui/Icon';
import { LogoMark } from '../components/ui/Logo';
import { can } from '../lib/permissions';
import { useGlobalShortcuts, useShortcutsEnabled } from '../lib/shortcuts';
import { navEntries } from '../routes';
import { useCurrentWorkspace } from '../workspace';
import { ShortcutsDialog } from './ShortcutsDialog';
import { SidebarNav } from './Sidebar';
import { UserMenu } from './UserMenu';
import { WorkspaceSwitcher } from './WorkspaceSwitcher';

/**
 * Каркас Mission Control: skip link → боковая навигация (≥ md; на узких экранах — выезжающая
 * панель Dialog) → шапка (workspace, справка по клавишам, пользователь) → main.
 */
export function AppLayout() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [navOpen, setNavOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const shortcutsOn = useShortcutsEnabled();

  const entries = useMemo(
    () => navEntries().filter((e) => !e.permission || can(membership?.role, e.permission)),
    [membership?.role],
  );
  const sequences = useMemo(() => {
    const map: Record<string, () => void> = {};
    if (workspaceId) for (const e of entries) if (e.shortcut) map[e.shortcut] = () => navigate(`/w/${workspaceId}/${e.path}`);
    return map;
  }, [entries, navigate, workspaceId]);
  const openHelp = useCallback(() => setHelpOpen(true), []);
  useGlobalShortcuts({ onHelp: openHelp, sequences }, shortcutsOn && !helpOpen && !navOpen);

  return (
    <div className="min-h-screen md:grid md:grid-cols-[theme(spacing.sidebar)_minmax(0,1fr)]">
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
      <aside className="sticky top-0 hidden h-screen flex-col overflow-y-auto border-r border-border bg-canvas-secondary md:flex">
        <Link to="/" className="flex min-h-[3.75rem] items-center gap-2 border-b border-border px-6 text-lg font-semibold text-fg no-underline">
          <LogoMark className="h-7 w-7" />
          {t('app.name')}
        </Link>
        <SidebarNav />
      </aside>
      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-overlay border-b border-border bg-surface">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2">
            <Dialog open={navOpen} onOpenChange={setNavOpen}>
              <DialogTrigger className="btn-ghost min-w-touch px-2 md:hidden" aria-label={t('layout.openNav')}>
                <Icon name="menu" />
              </DialogTrigger>
              <DialogContent title={t('layout.navDrawerTitle')} variant="drawer-left" className="p-0">
                <SidebarNav onNavigate={() => setNavOpen(false)} />
              </DialogContent>
            </Dialog>
            <Link to="/" className="inline-flex items-center gap-2 text-lg font-semibold text-fg no-underline md:hidden">
              <LogoMark className="h-6 w-6" />
              {t('app.name')}
            </Link>
            <div className="mr-auto" />
            <WorkspaceSwitcher />
            <IconButton icon="keyboard" label={t('layout.shortcuts')} onClick={() => setHelpOpen(true)} />
            <UserMenu />
          </div>
        </header>
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
          <Outlet />
        </main>
      </div>
      <ShortcutsDialog open={helpOpen} onOpenChange={setHelpOpen} entries={entries} />
    </div>
  );
}
