import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-08 Task Detail (diff viewer, evidence). Контекстный экран run — без пункта меню. */
export const tasksArea: AreaModule = {
  id: 'tasks',
  workspaceRoutes: [
    { path: 'projects/:projectId/runs/:runId/tasks/:taskId', element: <AreaPlaceholderPage titleKey="tasks.title" /> },
  ],
};
