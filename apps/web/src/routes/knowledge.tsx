import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const KnowledgePage = lazyPage(() => import('../features/knowledge/KnowledgePage'), 'KnowledgePage', ['knowledge', 'projects']);

/** P7-12 Knowledge/Documentation (documentation:read — все роли). */
export const knowledgeArea: AreaModule = {
  id: 'knowledge',
  nav: { labelKey: 'nav.knowledge', path: 'knowledge', icon: 'book', group: 'platform', order: 30, shortcut: 'k' },
  workspaceRoutes: [{ path: 'knowledge', element: <KnowledgePage /> }],
};
