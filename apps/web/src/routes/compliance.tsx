import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-10 Standards & Compliance. Заглушка «в разработке»: замените element страницей области. */
export const complianceArea: AreaModule = {
  id: 'compliance',
  nav: { labelKey: 'nav.compliance', path: 'compliance', icon: 'compliance', group: 'quality', order: 20, shortcut: 's' },
  workspaceRoutes: [
    {
      path: 'compliance',
      element: <AreaPlaceholderPage titleKey="compliance.title" />,
    },
  ],
};
