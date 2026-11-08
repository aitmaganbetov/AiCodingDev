import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-10 Evidence & Quality. Заглушка «в разработке»: замените element страницей области. */
export const qualityArea: AreaModule = {
  id: 'quality',
  nav: { labelKey: 'nav.quality', path: 'quality', icon: 'quality', group: 'quality', order: 10, shortcut: 'q' },
  workspaceRoutes: [
    {
      path: 'quality',
      element: <AreaPlaceholderPage titleKey="quality.title" />,
    },
  ],
};
