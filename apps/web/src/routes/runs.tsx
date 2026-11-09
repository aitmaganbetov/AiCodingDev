import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import { RunRedirect } from './redirect';
import type { AreaModule } from './types';

const WorkspaceRunsPage = lazyPage(() => import('../features/runs/WorkspaceRunsPage'), 'WorkspaceRunsPage', ['runs', 'projects']);
const ProjectRunsPage = lazyPage(() => import('../pages/ProjectRunsPage'), 'ProjectRunsPage', ['runs', 'projects']);

/**
 * Workflows (пункт «Процессы», план §6.1 п. 5) = запуски: список по workspace (фильтры статуса/проекта) и
 * запуски проекта. С F4 обзор run (P3-15) и граф задач (P7-05) — панели AI Mission Control: прежние
 * пути перенаправляются туда с тем же run и видом (`routes/redirect.tsx`).
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
    { path: 'projects/:projectId/runs/:runId', element: <RunRedirect from="run" /> },
    { path: 'projects/:projectId/runs/:runId/graph', element: <RunRedirect from="graph" /> },
  ],
};
