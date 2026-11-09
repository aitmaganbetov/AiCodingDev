import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const CodeIntelligencePage = lazyPage(() => import('../features/codeIntel/CodeIntelligencePage'), 'CodeIntelligencePage', ['codeIntel', 'projects', 'knowledge']);

/**
 * F8 Code Intelligence (план §3.6): explorer, символы и граф зависимостей (B3), Context Packs, индексация,
 * знания, история commit, поиск. Холст графа (React Flow) — отдельный ленивый чанк внутри экрана.
 */
export const codeIntelligenceArea: AreaModule = {
  id: 'code',
  nav: { labelKey: 'nav.code', path: 'code', icon: 'code', group: 'code', order: 10, shortcut: 'x', permission: 'project:read' },
  workspaceRoutes: [
    {
      path: 'code',
      element: (
        <RequirePermission permission="project:read">
          <CodeIntelligencePage />
        </RequirePermission>
      ),
    },
  ],
};
