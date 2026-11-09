import { RequirePermission } from '../auth/RequirePermission';
import { RunGraphPage } from '../features/runs/RunGraphPage';
import { WorkspaceRunsPage } from '../features/runs/WorkspaceRunsPage';
import { ProjectRunsPage } from '../pages/ProjectRunsPage';
import { RunPage } from '../pages/RunPage';
import type { AreaModule, RouteHandle } from './types';

/**
 * Workflows (пункт «Процессы», план §6.1 п. 5) = запуски: список по workspace (фильтры статуса/проекта), запуски проекта, Run view (P3-15) и Run Graph (P7-05).
 * Холст графа (@xyflow/react) загружается лениво внутри RunGraphPage.
 */
export const runsArea: AreaModule = {
  id: 'runs',
  nav: { labelKey: 'nav.runs', path: 'runs', icon: 'workflow', group: 'execution', order: 30, shortcut: 'r', permission: 'run:read' },
  workspaceRoutes: [
    {
      path: 'runs',
      element: (
        <RequirePermission permission="run:read">
          <WorkspaceRunsPage />
        </RequirePermission>
      ),
    },
    { path: 'projects/:projectId/runs', element: <ProjectRunsPage /> },
    { path: 'projects/:projectId/runs/:runId', element: <RunPage /> },
    {
      path: 'projects/:projectId/runs/:runId/graph',
      handle: { width: 'full' } satisfies RouteHandle,
      element: (
        <RequirePermission permission="run:read">
          <RunGraphPage />
        </RequirePermission>
      ),
    },
  ],
};
