import { DashboardPage } from '../features/dashboard/DashboardPage';
import type { AreaModule } from './types';

/** P7-03 Dashboard — стартовый экран workspace после входа (home.priority > projects). */
export const dashboardArea: AreaModule = {
  id: 'dashboard',
  nav: { labelKey: 'nav.dashboard', path: 'dashboard', icon: 'dashboard', group: 'overview', order: 10, shortcut: 'd' },
  workspaceRoutes: [{ path: 'dashboard', element: <DashboardPage /> }],
  home: { path: 'dashboard', priority: 10 },
};
