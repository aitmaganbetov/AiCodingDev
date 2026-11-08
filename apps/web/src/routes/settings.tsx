import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-13 Settings. Заглушка «в разработке»: замените element страницей области. */
export const settingsArea: AreaModule = {
  id: 'settings',
  nav: { labelKey: 'nav.settings', path: 'settings', icon: 'settings', group: 'admin', order: 20, shortcut: 'o' },
  workspaceRoutes: [
    {
      path: 'settings',
      element: <AreaPlaceholderPage titleKey="settings.title" />,
    },
  ],
};
