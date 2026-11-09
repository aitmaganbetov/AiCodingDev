import { lazyPage } from './lazyPage';
import type { AreaModule, RouteHandle } from './types';

const WorkspacesPage = lazyPage(() => import('../pages/WorkspacesPage'), 'WorkspacesPage', []);
const WorkspaceCreatePage = lazyPage(() => import('../pages/WorkspaceCreatePage'), 'WorkspaceCreatePage', []);

/** Рабочие пространства (вне /w/:workspaceId). Ссылка — в подвале боковой навигации. */
export const workspacesArea: AreaModule = {
  id: 'workspaces',
  routes: [
    { path: 'workspaces', element: <WorkspacesPage /> },
    { path: 'workspaces/new', element: <WorkspaceCreatePage />, handle: { width: 'form' } satisfies RouteHandle },
  ],
};
