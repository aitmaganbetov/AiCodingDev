import { lazy } from 'react';
import { RequirePermission } from '../auth/RequirePermission';
import type { AreaModule, RouteHandle } from './types';

const MissionControlPage = lazy(() => import('../features/missionControl/MissionControlPage'));

/**
 * AI Mission Control (план §6.1 п. 3). F2: выбор активного run (список, задачи выбранного run в
 * split view, сводка в контекстной панели) и `?run=<id>` — переход к run по одному id (уведомления,
 * поиск). Объединённый экран Navigator / Live Chat / Inspector — фаза F4.
 */
export const missionControlArea: AreaModule = {
  id: 'mission-control',
  nav: { labelKey: 'nav.missionControl', path: 'mission-control', icon: 'missionControl', group: 'execution', order: 10, shortcut: 'm', permission: 'run:read' },
  workspaceRoutes: [
    {
      path: 'mission-control',
      handle: { width: 'full' } satisfies RouteHandle,
      element: (
        <RequirePermission permission="run:read">
          <MissionControlPage />
        </RequirePermission>
      ),
    },
  ],
};
