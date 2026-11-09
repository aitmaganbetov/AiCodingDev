import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

/** Ленивый чанк (бюджет начального JS, план §4.3): плитки, диаграммы и лента грузятся с экраном. */
const DashboardPage = lazyPage(() => import('../features/dashboard/DashboardPage'), 'default', ['dashboard', 'runs']);

/** F3 Bento Dashboard — стартовый экран workspace после входа (home.priority > projects). */
export const dashboardArea: AreaModule = {
  id: 'dashboard',
  nav: { labelKey: 'nav.dashboard', path: 'dashboard', icon: 'dashboard', group: 'overview', order: 10, shortcut: 'd' },
  workspaceRoutes: [{ path: 'dashboard', element: <DashboardPage /> }],
  home: { path: 'dashboard', priority: 10 },
};
