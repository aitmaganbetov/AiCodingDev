import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-03 Dashboard. Заглушка: замените element своей страницей (и home.priority > 0, чтобы открывать её после входа). */
export const dashboardArea: AreaModule = {
  id: 'dashboard',
  nav: { labelKey: 'nav.dashboard', path: 'dashboard', icon: 'dashboard', group: 'work', order: 10, shortcut: 'd' },
  workspaceRoutes: [{ path: 'dashboard', element: <AreaPlaceholderPage titleKey="dashboard.title" /> }],
};
