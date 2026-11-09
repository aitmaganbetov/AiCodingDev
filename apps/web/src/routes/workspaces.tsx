import { WorkspaceCreatePage } from '../pages/WorkspaceCreatePage';
import { WorkspacesPage } from '../pages/WorkspacesPage';
import type { AreaModule, RouteHandle } from './types';

/** Рабочие пространства (вне /w/:workspaceId). Ссылка — в подвале боковой навигации. */
export const workspacesArea: AreaModule = {
  id: 'workspaces',
  routes: [
    { path: 'workspaces', element: <WorkspacesPage /> },
    { path: 'workspaces/new', element: <WorkspaceCreatePage />, handle: { width: 'form' } satisfies RouteHandle },
  ],
};
