import { RequirePermission } from '../auth/RequirePermission';
import { RunGraphPage } from '../features/runs/RunGraphPage';
import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import { ProjectRunsPage } from '../pages/ProjectRunsPage';
import { RunPage } from '../pages/RunPage';
import type { AreaModule } from './types';

/**
 * Запуски: список по workspace (заглушка), запуски проекта, Run view (P3-15) и Run Graph (P7-05).
 * Холст графа (@xyflow/react) загружается лениво внутри RunGraphPage.
 */
export const runsArea: AreaModule = {
  id: 'runs',
  nav: { labelKey: 'nav.runs', path: 'runs', icon: 'play', group: 'work', order: 30, shortcut: 'r' },
  workspaceRoutes: [
    { path: 'runs', element: <AreaPlaceholderPage titleKey="runs.allTitle" /> },
    { path: 'projects/:projectId/runs', element: <ProjectRunsPage /> },
    { path: 'projects/:projectId/runs/:runId', element: <RunPage /> },
    {
      path: 'projects/:projectId/runs/:runId/graph',
      element: (
        <RequirePermission permission="run:read">
          <RunGraphPage />
        </RequirePermission>
      ),
    },
  ],
};
