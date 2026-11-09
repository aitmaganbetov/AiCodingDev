import { ProjectDetailPage } from '../features/projects/ProjectDetailPage';
import { ProjectCreatePage } from '../pages/ProjectCreatePage';
import { ProjectsPage } from '../pages/ProjectsPage';
import type { AreaModule, RouteHandle } from './types';

/** Проекты (P1-13) + P7-04 Repository Explorer: `projects/:projectId` (репозитории, индекс, стек). */
export const projectsArea: AreaModule = {
  id: 'projects',
  nav: { labelKey: 'nav.projects', path: 'projects', icon: 'folder', group: 'overview', order: 20, shortcut: 'p' },
  workspaceRoutes: [
    { path: 'projects', element: <ProjectsPage /> },
    { path: 'projects/new', element: <ProjectCreatePage />, handle: { width: 'form' } satisfies RouteHandle },
    { path: 'projects/:projectId', element: <ProjectDetailPage /> },
  ],
  home: { path: 'projects', priority: 0 },
};
