import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import { ProjectRunsPage } from '../pages/ProjectRunsPage';
import { RunPage } from '../pages/RunPage';
import type { AreaModule } from './types';

/**
 * Запуски: список по workspace (заглушка), запуски проекта, Run view (P3-15).
 * P7-05 Run Graph добавляет сюда маршрут(ы) графа (например `…/runs/:runId/graph`).
 */
export const runsArea: AreaModule = {
  id: 'runs',
  nav: { labelKey: 'nav.runs', path: 'runs', icon: 'play', group: 'work', order: 30, shortcut: 'r' },
  workspaceRoutes: [
    { path: 'runs', element: <AreaPlaceholderPage titleKey="runs.allTitle" /> },
    { path: 'projects/:projectId/runs', element: <ProjectRunsPage /> },
    { path: 'projects/:projectId/runs/:runId', element: <RunPage /> },
  ],
};
