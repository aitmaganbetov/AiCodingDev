import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-11 Approvals. Заглушка «в разработке»: замените element страницей области. */
export const approvalsArea: AreaModule = {
  id: 'approvals',
  nav: { labelKey: 'nav.approvals', path: 'approvals', icon: 'approval', group: 'work', order: 40, shortcut: 'a' },
  workspaceRoutes: [
    {
      path: 'approvals',
      element: <AreaPlaceholderPage titleKey="approvals.title" />,
    },
  ],
};
