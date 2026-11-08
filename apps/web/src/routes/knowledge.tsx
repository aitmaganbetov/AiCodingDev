import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-12 Knowledge/Documentation. Заглушка «в разработке»: замените element страницей области. */
export const knowledgeArea: AreaModule = {
  id: 'knowledge',
  nav: { labelKey: 'nav.knowledge', path: 'knowledge', icon: 'book', group: 'quality', order: 30, shortcut: 'k' },
  workspaceRoutes: [
    {
      path: 'knowledge',
      element: <AreaPlaceholderPage titleKey="knowledge.title" />,
    },
  ],
};
