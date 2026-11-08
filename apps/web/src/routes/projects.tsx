import { ProjectCreatePage } from '../pages/ProjectCreatePage';
import { ProjectsPage } from '../pages/ProjectsPage';
import type { AreaModule } from './types';

/** Проекты (P1-13; P7-04 добавит Repository Explorer: `projects/:projectId/repository…`). */
export const projectsArea: AreaModule = {
  id: 'projects',
  nav: { labelKey: 'nav.projects', path: 'projects', icon: 'folder', group: 'work', order: 20, shortcut: 'p' },
  workspaceRoutes: [
    { path: 'projects', element: <ProjectsPage /> },
    { path: 'projects/new', element: <ProjectCreatePage /> },
  ],
  home: { path: 'projects', priority: 0 },
};
