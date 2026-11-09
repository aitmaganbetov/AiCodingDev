import { lazy } from 'react';
import { RequirePermission } from '../auth/RequirePermission';
import type { AreaModule, RouteHandle } from './types';

const TaskDetailPage = lazy(() => import('../features/tasks/TaskDetailPage'));

/** P7-08 Task Detail (попытки, события/логи, diff viewer на Monaco — лениво, evidence). Контекстный экран run — без пункта меню. */
export const tasksArea: AreaModule = {
  id: 'tasks',
  workspaceRoutes: [
    {
      path: 'projects/:projectId/runs/:runId/tasks/:taskId',
      handle: { width: 'full' } satisfies RouteHandle,
      element: (
        <RequirePermission permission="run:read">
          <TaskDetailPage />
        </RequirePermission>
      ),
    },
  ],
};
