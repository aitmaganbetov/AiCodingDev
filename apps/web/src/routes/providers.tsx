import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-09 CLI Providers. Заглушка «в разработке»: замените element страницей области. */
export const providersArea: AreaModule = {
  id: 'providers',
  nav: { labelKey: 'nav.providers', path: 'providers', icon: 'terminal', group: 'admin', order: 10, shortcut: 'c' },
  workspaceRoutes: [
    {
      path: 'providers',
      element: <AreaPlaceholderPage titleKey="providers.title" />,
    },
  ],
};
